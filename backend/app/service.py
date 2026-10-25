"""
The analysis pipeline:

    sanitise -> look up verified rows -> prompt LLM (retry once) -> validate JSON
    -> deterministic policy -> response

If the LLM fails twice or never produces valid JSON, a conservative fallback is
returned with needs_human_review=True and meta.degraded=True. The API never 500s
because of the model.
"""
from __future__ import annotations

import json
import logging
import re
import time

from pydantic import ValidationError

from .llm import LLMAdapter, LLMError
from .policy import apply_policy, detect_injection
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .schemas import (
    AnalysisMeta,
    AnalysisResult,
    AnalyzeResponse,
    Evidence,
    IssueCategory,
    Urgency,
)
from .verified_data import lookup, row_as_quote

log = logging.getLogger("saleseasy")

MAX_ATTEMPTS = 2


def _sanitise(text: str, max_chars: int) -> str:
    text = text.replace("\x00", "").strip()
    text = re.sub(r"[ \t]+", " ", text)
    return text[:max_chars]


def _parse(raw: str) -> AnalysisResult:
    # Tolerate ```json fences and leading prose if the model misbehaves slightly.
    m = re.search(r"\{.*\}", raw, flags=re.S)
    if not m:
        raise ValueError("no JSON object in model output")
    data = json.loads(m.group(0))
    return AnalysisResult.model_validate(data)


def _fallback(text: str, error: str) -> AnalysisResult:
    return AnalysisResult(
        issue_category=IssueCategory.INSUFFICIENT_INFORMATION,
        urgency=Urgency.MEDIUM,
        evidence=[Evidence(source="field_update", quote=text[:200], verified=False)],
        recommended_next_action="Automated analysis unavailable. Route this update to a human for manual triage.",
        missing_information=["Automated classification (model unavailable)"],
        needs_human_review=True,
        review_reason=f"R8 model/API failure - safe fallback returned ({error[:120]})",
    )


def analyze(text: str, adapter: LLMAdapter, max_chars: int = 2000) -> AnalyzeResponse:
    started = time.perf_counter()
    text = _sanitise(text, max_chars)
    matches = lookup(text)
    verified_quotes = [row_as_quote(r) for r in matches]
    user_prompt = build_user_prompt(text, verified_quotes)

    result: AnalysisResult | None = None
    error: str | None = None
    attempts = 0

    for attempts in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = adapter.generate(SYSTEM_PROMPT, user_prompt)
            result = _parse(raw)
            error = None
            break
        except LLMError as exc:
            error = f"provider error: {exc}"
            log.warning("LLM attempt %d failed: %s", attempts, error)
        except (ValueError, json.JSONDecodeError, ValidationError) as exc:
            error = f"invalid model output: {type(exc).__name__}"
            log.warning("LLM attempt %d returned invalid output: %s", attempts, exc)

    degraded = result is None
    if degraded:
        result = _fallback(text, error or "unknown")

    outcome = apply_policy(text, result, matches)

    meta = AnalysisMeta(
        provider=adapter.name,
        model=getattr(adapter, "last_model", None) or adapter.model,
        degraded=degraded,
        llm_error=error if degraded else None,
        attempts=attempts,
        injection_detected=outcome.injection_detected or detect_injection(text),
        verified_matches=matches,
        policy_overrides=outcome.overrides,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return AnalyzeResponse(result=outcome.result, meta=meta)
