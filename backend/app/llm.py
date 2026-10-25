"""
LLM adapters.

- GeminiAdapter : real model, JSON mode, temperature 0.
- MockAdapter   : deterministic keyword rules, no network. CLEARLY LABELLED as a mock.
                  Used by the automated test-suite so results are reproducible, and as a
                  fallback when no API key is configured.
- FailingAdapter: always raises. Used to simulate a model/API outage.

All adapters expose the same `generate(system_prompt, user_prompt) -> str` and return a
JSON string. Parsing/validation happens in service.py, not here.
"""
from __future__ import annotations

import json
import os
import re
from typing import Protocol


class LLMError(Exception):
    """Raised for any transport / provider failure."""


class LLMAdapter(Protocol):
    name: str
    model: str | None

    def generate(self, system_prompt: str, user_prompt: str) -> str: ...


# --------------------------------------------------------------------------- Gemini
DEFAULT_MODEL_CHAIN = ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-3.1-flash-lite"]

# Errors that mean "this model is busy right now", worth trying the next model for.
_TRANSIENT = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "ReadTimeout", "timed out")


class GeminiAdapter:
    """
    Real model. `models` is a fallback chain: the first entry is tried; on a transient
    error (overload / quota / timeout) the same request is retried on the next entry.
    `last_model` records which one actually answered, for the response metadata.
    """
    name = "gemini"

    def __init__(self, api_key: str, models: list[str] | None = None, timeout_s: float = 30.0):
        try:
            from google import genai  # imported lazily so the mock path has no SDK dependency
            from google.genai import types
        except ImportError as exc:  # pragma: no cover
            raise LLMError("google-genai SDK not installed") from exc

        self._types = types
        self.models = models or list(DEFAULT_MODEL_CHAIN)
        self.model = self.models[0]
        self.last_model: str | None = None
        self._client = genai.Client(api_key=api_key, http_options={"timeout": int(timeout_s * 1000)})

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        from .schemas import LLM_RESPONSE_SCHEMA

        last_err: Exception | None = None
        for model in self.models:
            try:
                resp = self._client.models.generate_content(
                    model=model,
                    contents=user_prompt,
                    config=self._types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0,
                        response_mime_type="application/json",
                        response_schema=LLM_RESPONSE_SCHEMA,
                    ),
                )
            except Exception as exc:  # network, auth, quota, safety block, ...
                last_err = exc
                msg = f"{type(exc).__name__}: {exc}"
                if any(t in msg for t in _TRANSIENT):
                    continue  # busy -> try the next model in the chain
                raise LLMError(msg) from exc

            text = getattr(resp, "text", None)
            if not text:
                last_err = LLMError(f"empty response from {model}")
                continue
            self.last_model = model
            return text

        raise LLMError(f"all models busy ({len(self.models)} tried): {type(last_err).__name__}: {last_err}")


# --------------------------------------------------------------------------- Mock
class MockAdapter:
    """
    MOCK ADAPTER - not an AI model.

    Produces plausible structured output from keyword rules so the pipeline, policy
    layer and tests can run without network access. Anything this adapter gets
    'right' is only evidence that the *deterministic* layers work; model quality
    itself is validated separately with the live adapter (see EVALUATION.md).
    """
    name = "mock"
    model = None

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        text = _extract_field_update(user_prompt)
        low = text.lower()
        rows = _extract_verified_rows(user_prompt)

        out: dict = {
            "issue_category": "other",
            "urgency": "medium",
            "evidence": [],
            "recommended_next_action": "Log the update and follow up with the rep for details.",
            "missing_information": [],
            "needs_human_review": False,
            "review_reason": "",
        }

        def quote(fragment_regex: str) -> None:
            m = re.search(fragment_regex, text, flags=re.I)
            if m:
                out["evidence"].append({"source": "field_update", "quote": m.group(0), "verified": False})

        for r in rows:
            out["evidence"].append({"source": "verified_table", "quote": r, "verified": True})

        if re.search(r"\b(unwell|sick|ill|hospital|vomit\w*|nausea|injur\w*)\b", low):
            out.update(
                issue_category="product_quality_safety",
                urgency="critical",
                recommended_next_action="Escalate immediately to the quality and safety team; quarantine the batch.",
                needs_human_review=True,
                review_reason="Possible consumer health incident.",
            )
            quote(r"[^.]*(damaged|unwell|sick)[^.]*")
        elif re.search(r"ignore (all |previous |the )?instructions|mark (this|it) (as )?resolved", low):
            out.update(
                issue_category="prompt_injection",
                urgency="medium",
                recommended_next_action="Disregard embedded instructions; ask the rep for the actual field issue.",
                needs_human_review=True,
                review_reason="Update attempted to control the assistant.",
            )
            quote(r"ignore[^.]*")
        elif re.search(r"price match|discount|competitor", low):
            out.update(
                issue_category="competitor_pricing",
                urgency="medium",
                recommended_next_action="Forward the competitor pricing report to the pricing team for verification.",
                missing_information=["Competitor name", "Specific outlets", "Discount amount", "Proof such as a photo"],
                needs_human_review=True,
                review_reason="Commercial pricing decision requested.",
            )
            quote(r"[^.]*(discount|price match)[^.]*")
        elif re.search(r"out of stock|stockout|stock out|unavailable|no stock|not available", low):
            stock_units = _units_from_rows(rows)
            if stock_units is not None and stock_units > 0:
                out.update(
                    issue_category="stock_data_conflict",
                    urgency="high",
                    recommended_next_action="Request a physical stock count to reconcile the rep's report with system stock.",
                    needs_human_review=True,
                    review_reason="Rep reports stockout but system shows stock.",
                )
            else:
                out.update(
                    issue_category="stockout",
                    urgency="high",
                    recommended_next_action="Raise a replenishment request for the outlet and SKU.",
                    missing_information=["Quantified lost-sales figure (rep said major but gave no number)"]
                    if re.search(r"\blos(t|ing|e)\b", low)
                    else [],
                )
            quote(r"[^.]*(out of stock|stockout|unavailable|no stock|not available)[^.]*")
        else:
            out.update(
                issue_category="insufficient_information",
                urgency="low",
                recommended_next_action="Ask the rep for outlet name, SKU and a description of the issue.",
                missing_information=["Outlet name", "SKU", "Description of the issue"],
            )
            out["evidence"].append({"source": "field_update", "quote": text[:120], "verified": False})

        return json.dumps(out)


class FailingAdapter:
    """Simulates a provider outage: every call raises."""
    name = "failing"
    model = None

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        raise LLMError("Simulated provider outage (503 Service Unavailable)")


class BrokenJSONAdapter:
    """Simulates a model that returns prose instead of JSON."""
    name = "broken-json"
    model = None

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        return "Sure! Here is the analysis you asked for: the outlet is out of stock..."


# --------------------------------------------------------------------------- helpers
def _extract_field_update(user_prompt: str) -> str:
    m = re.search(r"<field_update>\n?(.*?)\n?</field_update>", user_prompt, flags=re.S)
    return m.group(1) if m else user_prompt


def _extract_verified_rows(user_prompt: str) -> list[str]:
    m = re.search(r"<verified_matches>\n?(.*?)\n?</verified_matches>", user_prompt, flags=re.S)
    if not m:
        return []
    return [ln.strip() for ln in m.group(1).splitlines() if "|" in ln and "system stock" in ln]


def _units_from_rows(rows: list[str]) -> int | None:
    for r in rows:
        m = re.search(r"system stock (\d+) units", r)
        if m:
            return int(m.group(1))
    return None


def build_adapter_from_env() -> LLMAdapter:
    provider = os.getenv("LLM_PROVIDER", "gemini").lower()
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if provider == "mock" or not key:
        return MockAdapter()
    # GEMINI_MODEL may be one model or a comma-separated chain. The defaults are appended
    # as fallbacks so a single configured model still has somewhere to go when it is busy.
    configured = [m.strip() for m in os.getenv("GEMINI_MODEL", "").split(",") if m.strip()]
    chain = list(dict.fromkeys(configured + DEFAULT_MODEL_CHAIN))
    return GeminiAdapter(api_key=key, models=chain)
