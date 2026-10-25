"""
Output contract for the sales copilot.

Every field the brief asks for is defined here with explicit types and allowed
values. The LLM is asked to produce this shape, but nothing is trusted until it
passes Pydantic validation AND the deterministic policy layer in policy.py.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class IssueCategory(str, Enum):
    STOCKOUT = "stockout"                      # outlet has no stock of a SKU
    STOCK_DATA_CONFLICT = "stock_data_conflict"  # rep claim contradicts the verified table
    COMPETITOR_PRICING = "competitor_pricing"  # competitor discount / price-match request
    PRODUCT_QUALITY_SAFETY = "product_quality_safety"  # damaged product, health complaint
    DELIVERY_LOGISTICS = "delivery_logistics"  # late / wrong delivery, van issues
    PROMPT_INJECTION = "prompt_injection"      # update tries to control the assistant
    INSUFFICIENT_INFORMATION = "insufficient_information"  # cannot classify safely
    OTHER = "other"


class Urgency(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


URGENCY_RANK = {Urgency.LOW: 0, Urgency.MEDIUM: 1, Urgency.HIGH: 2, Urgency.CRITICAL: 3}


class Evidence(BaseModel):
    """
    One traceable piece of evidence.

    source = "field_update"   -> quote must be a verbatim substring of the rep's text (UNVERIFIED claim)
    source = "verified_table" -> quote is a row from the system stock table (VERIFIED fact)
    """
    source: Literal["field_update", "verified_table"]
    quote: str = Field(min_length=1, max_length=500)
    verified: bool = Field(
        description="True only when source is verified_table. Field-update quotes are always unverified."
    )


class AnalysisResult(BaseModel):
    issue_category: IssueCategory
    urgency: Urgency
    evidence: list[Evidence] = Field(default_factory=list, max_length=12)
    recommended_next_action: str = Field(min_length=1, max_length=600)
    missing_information: list[str] = Field(default_factory=list, max_length=10)
    needs_human_review: bool
    review_reason: Optional[str] = Field(default=None, max_length=600)

    @field_validator("evidence")
    @classmethod
    def _verified_flag_matches_source(cls, items: list[Evidence]) -> list[Evidence]:
        # The model is not allowed to mark a rep claim as verified.
        for e in items:
            e.verified = e.source == "verified_table"
        return items

    @field_validator("review_reason")
    @classmethod
    def _blank_to_none(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            return None
        return v


class VerifiedRow(BaseModel):
    outlet: str
    sku: str
    system_stock_units: int


class AnalysisMeta(BaseModel):
    """Diagnostics returned alongside the result. Useful for the demo and for tests."""
    provider: str
    model: Optional[str] = None
    degraded: bool = False               # True when the LLM failed and a safe fallback was returned
    llm_error: Optional[str] = None
    attempts: int = 0
    injection_detected: bool = False
    verified_matches: list[VerifiedRow] = Field(default_factory=list)
    policy_overrides: list[str] = Field(default_factory=list)  # human-readable list of rules that fired
    latency_ms: int = 0


class AnalyzeRequest(BaseModel):
    text: str = Field(min_length=1, description="Plain-text field update from a sales rep")
    # Test hook: forces the LLM call to raise, so the failure path can be demonstrated end to end.
    simulate_failure: bool = False


class AnalyzeResponse(BaseModel):
    result: AnalysisResult
    meta: AnalysisMeta


# JSON schema handed to Gemini as response_schema (keeps it simple: no $defs, no enums-as-objects).
LLM_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "issue_category": {"type": "string", "enum": [c.value for c in IssueCategory]},
        "urgency": {"type": "string", "enum": [u.value for u in Urgency]},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": ["field_update", "verified_table"]},
                    "quote": {"type": "string"},
                    "verified": {"type": "boolean"},
                },
                "required": ["source", "quote", "verified"],
            },
        },
        "recommended_next_action": {"type": "string"},
        "missing_information": {"type": "array", "items": {"type": "string"}},
        "needs_human_review": {"type": "boolean"},
        "review_reason": {"type": "string"},
    },
    "required": [
        "issue_category",
        "urgency",
        "evidence",
        "recommended_next_action",
        "missing_information",
        "needs_human_review",
    ],
}
