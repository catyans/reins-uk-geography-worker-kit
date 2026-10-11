"""Deterministic trajectory contracts complement business-output validators."""

from __future__ import annotations

import html
import json
from decimal import Decimal
from pathlib import Path


def validate_contract(contract):
    """Reject misspelled or malformed gates before dispatching a paid evaluation."""
    lists = {
        "required_tools",
        "allowed_tools",
        "ordered_tools",
        "required_validations",
        "approval_tools",
    }
    allowed = lists | {"max_tool_calls", "max_cost", "max_latency_ms"}
    if not isinstance(contract, dict) or set(contract) - allowed:
        raise ValueError("Unknown regression contract field")
    for key in lists & set(contract):
        if not isinstance(contract[key], list) or any(
            not isinstance(v, str) or not v.strip() for v in contract[key]
        ):
            raise ValueError(f"{key} must be a list of names")
    if "max_tool_calls" in contract and (
        type(contract["max_tool_calls"]) is not int or contract["max_tool_calls"] < 0
    ):
        raise ValueError("max_tool_calls must be a nonnegative integer")
    for key in {"max_cost", "max_latency_ms"} & set(contract):
        try:
            value = Decimal(str(contract[key]))
            if not value.is_finite() or value < 0:
                raise ValueError("Invalid bound")
        except (ValueError, ArithmeticError):
            raise ValueError(f"{key} must be a finite nonnegative number") from None
    return contract


def evaluate_case(case, contract):
    """Missing measurements fail a requested gate, including a missing trajectory."""
    validate_contract(contract)
    failures = []
    events = case.get("trajectory")
    if not isinstance(events, list):
        events = []
        failures.append("Missing execution trajectory")

    def valid_event(event):
        if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
            return False
        if not event["kind"].strip():
            return False
        if event["kind"] == "tool":
            return isinstance(event.get("tool"), str) and bool(event["tool"].strip())
        if event["kind"] == "validation":
            return (
                isinstance(event.get("name"), str)
                and bool(event["name"].strip())
                and type(event.get("passed")) is bool
            )
        return True

    if any(not valid_event(e) for e in events):
        failures.append("Malformed execution trajectory")
        events = [e for e in events if valid_event(e)]
    tools = [e.get("tool") for e in events if e.get("kind") == "tool"]
    for tool in contract.get("required_tools", []):
        if tool not in tools:
            failures.append(f"Required tool missing: {tool}")
    allowed = contract.get("allowed_tools")
    if allowed is not None and any(t not in allowed for t in tools):
        failures.append("Unexpected tool executed")
    cursor = 0
    for tool in contract.get("ordered_tools", []):
        try:
            cursor = tools.index(tool, cursor) + 1
        except ValueError:
            failures.append("Required tool order not followed")
            break
    if "max_tool_calls" in contract and len(tools) > contract["max_tool_calls"]:
        failures.append("Tool count exceeded")
    for check in contract.get("required_validations", []):
        if not any(
            e.get("kind") == "validation" and e.get("name") == check and e.get("passed") is True
            for e in events
        ):
            failures.append(f"Validation missing or failed: {check}")
    for e in events:
        if e.get("kind") == "tool" and e.get("tool") in contract.get("approval_tools", []):
            if not e.get("approval_id") or e.get("approval_valid") is not True:
                failures.append(f"Valid approval missing: {e.get('tool')}")
    for field, limit in (("cost", "max_cost"), ("latency_ms", "max_latency_ms")):
        if limit in contract:
            try:
                value = Decimal(str(case[field]))
                maximum = Decimal(str(contract[limit]))
                if not value.is_finite() or value < 0 or not maximum.is_finite() or maximum < 0:
                    raise ValueError("Invalid measurement")
                if value > maximum:
                    failures.append(f"{field} exceeded")
            except (KeyError, ValueError, ArithmeticError):
                failures.append(f"{field} missing or invalid")
    if case.get("accepted") is not True:
        failures.append("Business acceptance failed or missing")
    return {"case_id": case["case_id"], "passed": not failures, "failures": failures}


def evaluate_suite(cases, contract, *, expected_case_ids):
    expected = list(expected_case_ids)
    ids = [c["case_id"] for c in cases]
    if len(set(ids)) != len(ids) or len(set(expected)) != len(expected):
        raise ValueError("Duplicate case identity")
    if set(ids) != set(expected):
        raise ValueError("Missing or unexpected cases in frozen suite")
    if not expected:
        raise ValueError("Empty regression suite")
    results = [evaluate_case(c, contract) for c in cases]
    return {
        "passed": all(r["passed"] for r in results),
        "cases": results,
        "total": len(results),
        "passing": sum(r["passed"] for r in results),
    }


def write_report(report, path):
    """Always provide both readable HTML and structured JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    rows = "".join(
        "<tr><td>"
        + html.escape(str(r["case_id"]))
        + "</td><td>"
        + ("Passed" if r["passed"] else "Needs attention")
        + "</td><td>"
        + html.escape("; ".join(r["failures"]))
        + "</td></tr>"
        for r in report["cases"]
    )
    comparison = ""
    if "cost_complete" in report:
        comparison = (
            "<h2>Paired outcome and cost comparison</h2><p>"
            + html.escape(
                "Accepted baseline/candidate: "
                + str(report["accepted"])
                + ". Total measured cost (including unsuccessful tasks): "
                + str(report["cost"])
                + ". Savings: "
                + (
                    f"{report['savings_fraction']:.1%}"
                    if report["savings_fraction"] is not None
                    else "Not established by this run"
                )
                + ". "
                + report["scope"]
            )
            + "</p>"
        )
    summary_label = (
        "pairs preserved baseline acceptance"
        if "cost_complete" in report
        else "cases passed the frozen contract"
    )
    path.with_suffix(".html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" '
        'content="width=device-width,initial-scale=1"><title>Reins regression report</title>'
        "<style>body{font:16px system-ui;max-width:1100px;margin:3em auto;padding:1em;"
        "color:#203d2b}table{border-collapse:collapse;width:100%}td,th{padding:1em;"
        "text-align:left;border-bottom:1px solid #ddd}</style><h1>Execution regression report</h1>"
        f"<p>{report['passing']} / {report['total']} {summary_label}.</p>"
        + comparison
        + "<table><tr><th>Case</th><th>Result</th><th>Details</th></tr>"
        + rows
        + "</table></html>",
        encoding="utf-8",
    )
