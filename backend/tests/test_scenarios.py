"""
Scenario tests.

Default: runs through the FastAPI app with the deterministic MockAdapter, so the
suite is reproducible offline and exercises validation + policy + API wiring.

Live:    RUN_LIVE=1 pytest -s   -> uses the real Gemini adapter (needs GEMINI_API_KEY).
         Results are also written to tests/results_<provider>.json for EVALUATION.md.

Each scenario in scenarios.json carries an `expect` block; `check()` turns it into
pass/fail criteria. One scenario (S7) forces a provider outage.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if not os.getenv("RUN_LIVE"):
    os.environ["LLM_PROVIDER"] = "mock"

import main  # noqa: E402  (after env setup)

SCENARIOS = json.loads((Path(__file__).parent / "scenarios.json").read_text(encoding="utf-8"))
client = TestClient(main.app)
_RESULTS: list[dict] = []


def check(exp: dict, body: dict, status: int) -> list[str]:
    """Return a list of failed criteria (empty = pass)."""
    r, m = body["result"], body["meta"]
    fails: list[str] = []

    if "http_status" in exp and status != exp["http_status"]:
        fails.append(f"http_status {status} != {exp['http_status']}")
    if "issue_category" in exp and r["issue_category"] not in exp["issue_category"]:
        fails.append(f"issue_category {r['issue_category']} not in {exp['issue_category']}")
    if "urgency" in exp and r["urgency"] not in exp["urgency"]:
        fails.append(f"urgency {r['urgency']} not in {exp['urgency']}")
    if "urgency_not" in exp and r["urgency"] in exp["urgency_not"]:
        fails.append(f"urgency {r['urgency']} is forbidden")
    if "needs_human_review" in exp and r["needs_human_review"] != exp["needs_human_review"]:
        fails.append(f"needs_human_review {r['needs_human_review']} != {exp['needs_human_review']}")
    if "has_verified_evidence" in exp:
        has = any(e["verified"] for e in r["evidence"])
        if has != exp["has_verified_evidence"]:
            fails.append(f"has_verified_evidence {has} != {exp['has_verified_evidence']}")
    if "review_reason_contains" in exp:
        if exp["review_reason_contains"].lower() not in (r["review_reason"] or "").lower():
            fails.append(f"review_reason missing '{exp['review_reason_contains']}'")
    if "action_must_not_match" in exp and re.search(exp["action_must_not_match"], r["recommended_next_action"], re.I):
        fails.append("recommended_next_action matched forbidden pattern")
    if "action_must_match" in exp and not re.search(exp["action_must_match"], r["recommended_next_action"], re.I):
        fails.append("recommended_next_action missing required pattern")
    if "missing_info_nonempty" in exp and not r["missing_information"]:
        fails.append("missing_information is empty")
    if "missing_info_mentions" in exp:
        joined = " ".join(r["missing_information"]).lower()
        if exp["missing_info_mentions"].lower() not in joined:
            fails.append(f"missing_information does not mention '{exp['missing_info_mentions']}'")
    if "injection_detected" in exp and m["injection_detected"] != exp["injection_detected"]:
        fails.append("injection_detected mismatch")
    if "degraded" in exp and m["degraded"] != exp["degraded"]:
        fails.append("degraded mismatch")
    # Universal: R7 (fabricated number) must not fire unless a scenario explicitly expects it.
    # Covers the iteration-2 regression (R7 tripping on its own labels) for every scenario.
    if not exp.get("expect_invented_numbers") and any("R7" in o for o in m["policy_overrides"]):
        fails.append("policy flagged invented numbers (R7)")
    return fails


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_scenario(sc: dict):
    payload = {"text": sc["text"], "simulate_failure": sc.get("simulate_failure", False)}
    resp = client.post("/api/analyze", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Grounding invariant for EVERY scenario: a field_update quote must be verbatim from the input.
    low = sc["text"].lower()
    for e in body["result"]["evidence"]:
        if e["source"] == "field_update":
            assert e["quote"].lower() in low, f"ungrounded quote: {e['quote']}"
            assert e["verified"] is False
        else:
            assert e["verified"] is True

    fails = check(sc["expect"], body, resp.status_code)
    _RESULTS.append({"id": sc["id"], "title": sc["title"], "passed": not fails, "failures": fails, "output": body})
    assert not fails, f"{sc['id']} failed: {fails}"


def test_input_too_long():
    resp = client.post("/api/analyze", json={"text": "x" * 5000})
    assert resp.status_code == 413


def test_empty_input_rejected():
    resp = client.post("/api/analyze", json={"text": "   "})
    assert resp.status_code == 422


def test_broken_json_from_model_falls_back():
    from app.llm import BrokenJSONAdapter
    from app.service import analyze

    out = analyze("Indiranagar Mart cola unavailable", BrokenJSONAdapter())
    assert out.meta.degraded is True
    assert out.result.needs_human_review is True
    assert out.meta.attempts == 2


def test_rate_limit():
    from app.rate_limit import RateLimiter

    rl = RateLimiter(per_minute=3)
    assert all(rl.allow("ip")[0] for _ in range(3))
    allowed, retry = rl.allow("ip")
    assert allowed is False and retry > 0


@pytest.fixture(scope="session", autouse=True)
def _write_results():
    yield
    provider = main.adapter.name
    out = Path(__file__).parent / f"results_{provider}.json"
    out.write_text(json.dumps(_RESULTS, indent=2), encoding="utf-8")
    print(f"\nwrote {out} ({sum(r['passed'] for r in _RESULTS)}/{len(_RESULTS)} passed)")
