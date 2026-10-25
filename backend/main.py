"""
SalesEasy - reliable AI sales copilot (FastAPI backend).

Run:  uvicorn main:app --reload
Docs: http://localhost:8000/docs
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.llm import FailingAdapter, build_adapter_from_env
from app.rate_limit import RateLimiter
from app.schemas import AnalyzeRequest, AnalyzeResponse
from app.service import analyze
from app.verified_data import VERIFIED_STOCK

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("saleseasy")

MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS", "2000"))
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "20"))
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",") if o.strip()]

app = FastAPI(title="SalesEasy copilot", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

adapter = build_adapter_from_env()
limiter = RateLimiter(RATE_LIMIT_PER_MINUTE)
log.info("LLM provider: %s (model=%s)", adapter.name, adapter.model)

SCENARIOS_PATH = Path(__file__).parent / "tests" / "scenarios.json"
STATIC_DIR = Path(__file__).parent / "static"


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


@app.get("/api/health")
def health():
    return {"status": "ok", "provider": adapter.name, "model": adapter.model, "max_input_chars": MAX_INPUT_CHARS}


@app.get("/api/verified-data")
def verified_data():
    return [r.model_dump() for r in VERIFIED_STOCK]


@app.get("/api/scenarios")
def scenarios():
    import json

    data = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    return [{"id": s["id"], "title": s["title"], "text": s["text"], "simulate_failure": s.get("simulate_failure", False)} for s in data]


@app.post("/api/analyze", response_model=AnalyzeResponse)
def analyze_endpoint(body: AnalyzeRequest, request: Request):
    allowed, retry_in = limiter.allow(_client_ip(request))
    if not allowed:
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded. Try again in {retry_in}s.")
    if len(body.text) > MAX_INPUT_CHARS:
        raise HTTPException(status_code=413, detail=f"Input too long. Max {MAX_INPUT_CHARS} characters.")
    if not body.text.strip():
        raise HTTPException(status_code=422, detail="Field update is empty.")

    chosen = FailingAdapter() if body.simulate_failure else adapter
    # Only the length of the input is logged; the content may contain personal data.
    log.info("analyze: %d chars, provider=%s", len(body.text), chosen.name)
    return analyze(body.text, chosen, max_chars=MAX_INPUT_CHARS)


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse(status_code=500, content={"detail": "Internal error. Please retry."})


# Serve the built React app when present (single-service deployment).
if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        candidate = STATIC_DIR / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
