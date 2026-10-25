"""
Deterministic policy layer.

The LLM proposes; this module disposes. Every rule here is plain Python that a
reviewer can read and defend. Rules can only make the output SAFER (raise urgency,
force human review, strip unauthorised actions); they never lower urgency or
remove a review flag the model set.

Human-review policy (needs_human_review is forced True when any applies):
  R1  consumer health / product safety signal
  R2  rep claim contradicts the verified stock table
  R3  update requests a commercial action (price change, discount, credit, refund)
  R4  prompt-injection attempt detected
  R5  urgency is critical
  R6  high urgency but zero verified evidence (unverified, high-impact claim)
  R7  the model's output contained a number that appears neither in the update
      nor in the verified table (possible fabricated figure)
  R8  the model failed and a fallback was produced (handled in service.py)

Additional non-review rule:
  R9  rep claims lost sales/revenue without a figure -> add it to missing_information
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .schemas import URGENCY_RANK, AnalysisResult, Evidence, IssueCategory, Urgency, VerifiedRow
from .verified_data import row_as_quote

# ---- signal detectors -------------------------------------------------------------
_SAFETY = re.compile(
    r"\b(unwell|sick|ill|nausea|nauseous|vomit\w*|hospital\w*|injur\w*|poison\w*|allerg\w*|"
    r"contaminat\w*|choking|food poisoning|rash|fainted|dizzy)\b",
    re.I,
)
_STOCKOUT_CLAIM = re.compile(
    r"\b(out of stock|stock ?out|stocked out|unavailable|no stock|not available|zero stock|nil stock|empty shelf)\b",
    re.I,
)
_COMMERCIAL_ACTION = re.compile(
    r"\b(price match|match (the |their )?price|price cut|reduce (the )?price|lower (the )?price|"
    r"discount|credit note|refund|free (stock|goods)|waive)\b",
    re.I,
)
_INJECTION = re.compile(
    r"(ignore (all |any |the |your |previous |prior |above )*(instructions|rules|prompt)|"
    r"disregard (the |your |all )*(instructions|rules|prompt)|"
    r"system prompt|you are now|new instructions?:|"
    r"mark (this|it) (as )?(resolved|closed|low)|set (the )?urgency (to|as) low|"
    r"override|\bas an ai\b|developer mode)",
    re.I,
)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
# "losing major sales", "lost a lot of business", "loss of revenue" ...
_IMPACT_CLAIM = re.compile(
    r"\b(los(t|ing|e)|loss of)\s+(major|big|huge|significant|serious|a lot of|lots of|heavy|many)?\s*"
    r"(sales|business|revenue|customers|accounts)\b",
    re.I,
)
# a figure WITH a unit, e.g. "40 units", "Rs 5000", "12 cases", "30%"
_QUANTIFIED = re.compile(r"(\d[\d,]*\s*(units|cases|crates|bottles|packs|%|rs\.?|rupees|inr)|(rs\.?|₹|inr)\s*\d)", re.I)


@dataclass
class PolicyOutcome:
    result: AnalysisResult
    overrides: list[str] = field(default_factory=list)
    injection_detected: bool = False


def detect_injection(text: str) -> bool:
    return bool(_INJECTION.search(text))


def _raise_urgency(result: AnalysisResult, floor: Urgency, overrides: list[str], why: str) -> None:
    if URGENCY_RANK[result.urgency] < URGENCY_RANK[floor]:
        overrides.append(f"urgency raised {result.urgency.value} -> {floor.value}: {why}")
        result.urgency = floor


def _force_review(result: AnalysisResult, overrides: list[str], reason: str) -> None:
    if not result.needs_human_review:
        overrides.append(f"needs_human_review forced True: {reason}")
    result.needs_human_review = True
    if result.review_reason:
        if reason not in result.review_reason:
            result.review_reason = f"{result.review_reason} | {reason}"
    else:
        result.review_reason = reason


def _numbers_in(s: str) -> set[str]:
    return {m.group(0).replace(",", "") for m in _NUMBER.finditer(s)}


def _dedupe_evidence(items: list[Evidence]) -> list[Evidence]:
    seen: set[tuple[str, str]] = set()
    out: list[Evidence] = []
    for e in items:
        key = (e.source, e.quote.strip().lower())
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def apply_policy(text: str, result: AnalysisResult, matches: list[VerifiedRow]) -> PolicyOutcome:
    ov: list[str] = []
    low = text.lower()

    # Snapshot the MODEL's free text now, before any rule appends its own labels
    # (e.g. "R2", "503") that would otherwise trip the fabricated-number check (R7).
    model_text = " ".join(
        [result.recommended_next_action, result.review_reason or ""]
        + result.missing_information
        + [e.quote for e in result.evidence if e.source == "field_update"]
    )

    # ---- Evidence grounding: field_update quotes must actually appear in the text ----
    grounded: list[Evidence] = []
    for e in result.evidence:
        if e.source == "field_update":
            if e.quote.strip().lower() in low:
                grounded.append(e)
            else:
                ov.append(f"dropped ungrounded field_update quote: {e.quote[:60]!r}")
        else:
            # verified_table quotes must correspond to a real matched row
            allowed = {row_as_quote(r).lower() for r in matches}
            if e.quote.strip().lower() in allowed:
                grounded.append(e)
            else:
                ov.append(f"dropped verified_table quote not in matched rows: {e.quote[:60]!r}")
    # Always attach the verified rows we matched, even if the model forgot them.
    for r in matches:
        grounded.append(Evidence(source="verified_table", quote=row_as_quote(r), verified=True))
    result.evidence = _dedupe_evidence(grounded)

    # ---- R1 safety --------------------------------------------------------------
    if _SAFETY.search(text):
        result.issue_category = IssueCategory.PRODUCT_QUALITY_SAFETY
        _raise_urgency(result, Urgency.CRITICAL, ov, "consumer health signal")
        _force_review(result, ov, "R1 possible consumer health / product safety incident")
        result.recommended_next_action = (
            "Escalate immediately to the quality & safety team and the area manager. "
            "Quarantine the affected stock at the outlet, collect batch number and photos, "
            "and record the customer's contact details. Do not offer any medical opinion."
        )
        ov.append("recommended_next_action replaced with fixed safety escalation text")

    # ---- R2 stock conflict ------------------------------------------------------
    if _STOCKOUT_CLAIM.search(text):
        conflicting = [r for r in matches if r.system_stock_units > 0]
        if conflicting:
            result.issue_category = IssueCategory.STOCK_DATA_CONFLICT
            _raise_urgency(result, Urgency.HIGH, ov, "rep report contradicts system stock")
            rows = "; ".join(row_as_quote(r) for r in conflicting)
            _force_review(result, ov, f"R2 conflicting evidence - rep reports stockout but system shows stock ({rows})")
            if "Verified system stock does not support the stockout claim" not in result.missing_information:
                result.missing_information.append(
                    "Physical stock count at the outlet to reconcile rep report vs system stock"
                )

    # ---- R3 commercial action ---------------------------------------------------
    if _COMMERCIAL_ACTION.search(text) or _COMMERCIAL_ACTION.search(result.recommended_next_action):
        if _COMMERCIAL_ACTION.search(result.recommended_next_action):
            ov.append("recommended_next_action contained a commercial action and was replaced")
            result.recommended_next_action = (
                "Log the report and forward to the pricing/commercial team for verification. "
                "No price change, discount, credit or refund is authorised by this system."
            )
        _force_review(result, ov, "R3 commercial action requested - requires authorised human decision")

    # ---- R4 prompt injection ----------------------------------------------------
    injected = detect_injection(text)
    if injected:
        # If the only content is the injection, classify it as such.
        if result.issue_category in (IssueCategory.OTHER, IssueCategory.INSUFFICIENT_INFORMATION) or (
            result.issue_category == IssueCategory.PROMPT_INJECTION
        ):
            result.issue_category = IssueCategory.PROMPT_INJECTION
        _raise_urgency(result, Urgency.MEDIUM, ov, "injection attempt must not be auto-closed")
        _force_review(result, ov, "R4 update contains instructions aimed at the assistant (prompt injection)")
        if re.search(r"resolv|closed|low urgency", result.recommended_next_action, re.I):
            result.recommended_next_action = (
                "Do not act on embedded instructions. Ask the rep to resend a plain description of the field issue."
            )
            ov.append("recommended_next_action replaced - it echoed the injected instruction")

    # ---- R5 critical always reviewed -------------------------------------------
    if result.urgency == Urgency.CRITICAL:
        _force_review(result, ov, "R5 critical urgency")

    # ---- R6 high urgency with zero verified evidence ---------------------------
    if result.urgency == Urgency.HIGH and not any(e.verified for e in result.evidence):
        _force_review(result, ov, "R6 high-urgency claim with no verified evidence")

    # ---- R7 fabricated numbers --------------------------------------------------
    allowed_numbers = _numbers_in(text) | {str(r.system_stock_units) for r in matches}
    invented = _numbers_in(model_text) - allowed_numbers
    if invented:
        _force_review(result, ov, f"R7 output contains figures not present in input or verified data: {sorted(invented)}")

    # ---- R9 unquantified impact claim ------------------------------------------
    # The rep claims losses but gives no figure. Make sure the gap is listed instead of guessed.
    if _IMPACT_CLAIM.search(text) and not _QUANTIFIED.search(text):
        item = "Quantified lost-sales figure (rep claimed losses but gave no number)"
        if not any("lost" in m.lower() and "figure" in m.lower() for m in result.missing_information):
            result.missing_information.append(item)
            ov.append("R9 added missing lost-sales figure (impact claimed without a number)")

    # ---- Missing info for unknown outlet / sku ---------------------------------
    if not matches:
        from .verified_data import find_outlets, find_skus

        if not find_outlets(text) and not any("outlet" in m.lower() for m in result.missing_information):
            result.missing_information.append("Outlet name (not recognised in verified data)")
        if not find_skus(text) and not any("sku" in m.lower() for m in result.missing_information):
            result.missing_information.append("SKU / product (not recognised in verified data)")

    result.missing_information = list(dict.fromkeys(m.strip() for m in result.missing_information if m.strip()))[:10]
    return PolicyOutcome(result=result, overrides=ov, injection_detected=injected)
