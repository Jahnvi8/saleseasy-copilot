"""
The verified data source. In production this would be the inventory/ERP system.
Here it is the fictional table from the brief, hard-coded on purpose.
"""
from __future__ import annotations

import re

from .schemas import VerifiedRow

VERIFIED_STOCK: list[VerifiedRow] = [
    VerifiedRow(outlet="Indiranagar Mart", sku="Cola 500ml", system_stock_units=0),
    VerifiedRow(outlet="Metro Fresh", sku="Cola 500ml", system_stock_units=48),
    VerifiedRow(outlet="City Corner", sku="Juice 1L", system_stock_units=12),
]

# Loose aliases so "cola 500 ml", "Cola500ml", "juice 1 litre" still resolve.
_SKU_ALIASES: dict[str, list[str]] = {
    "Cola 500ml": [r"cola\s*500\s*ml", r"\bcola\b"],
    "Juice 1L": [r"juice\s*1\s*(l|litre|liter)\b", r"\bjuice\b"],
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def find_outlets(text: str) -> list[str]:
    t = _norm(text)
    return sorted({row.outlet for row in VERIFIED_STOCK if _norm(row.outlet) in t})


def find_skus(text: str) -> list[str]:
    t = _norm(text)
    hits: set[str] = set()
    for sku, patterns in _SKU_ALIASES.items():
        if any(re.search(p, t) for p in patterns):
            hits.add(sku)
    return sorted(hits)


def lookup(text: str) -> list[VerifiedRow]:
    """Return every verified row whose outlet AND sku are mentioned in the update."""
    outlets, skus = find_outlets(text), find_skus(text)
    return [r for r in VERIFIED_STOCK if r.outlet in outlets and r.sku in skus]


def row_as_quote(row: VerifiedRow) -> str:
    return f"{row.outlet} | {row.sku} | system stock {row.system_stock_units} units"


def table_as_text() -> str:
    lines = ["Outlet | SKU | System stock"]
    lines += [row_as_quote(r) for r in VERIFIED_STOCK]
    return "\n".join(lines)
