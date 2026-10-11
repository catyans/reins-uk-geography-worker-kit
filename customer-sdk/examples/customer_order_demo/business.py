"""An example customer's existing, local order-checking task."""

from __future__ import annotations

import csv
from pathlib import Path

DATA = Path(__file__).with_name("orders.csv")


def review_orders(path: Path = DATA) -> dict:
    """Return a small, source-backed review without calling any model provider."""
    issues = []
    seen = set()
    with path.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    for row_number, row in enumerate(rows, start=2):
        order_id = row["order_id"]
        if order_id in seen:
            issues.append({"row": row_number, "order_id": order_id, "issue": "duplicate order ID"})
        seen.add(order_id)
        if not row["customer"].strip():
            issues.append({"row": row_number, "order_id": order_id, "issue": "missing customer"})
        if float(row["amount_usd"]) < 0:
            issues.append({"row": row_number, "order_id": order_id, "issue": "negative amount"})
    return {"rows_checked": len(rows), "issues": issues}
