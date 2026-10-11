"""Paired reruns of immutable human-reviewed cases; labels never enter agent inputs."""

from copy import deepcopy
from decimal import Decimal

from reins.control.regression import evaluate_case, validate_contract
from reins.projects.documents import digest


def validate_suite(suite):
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Nonempty reviewed suite required")
    ids = [c["id"] for c in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate case IDs")
    for case in cases:
        if not isinstance(case.get("expected"), dict) or not case["expected"]:
            raise ValueError("Every case needs confirmed expected fields")
        if case.get("provenance", {}).get("review", {}).get("decision") != "approve":
            raise ValueError("Every case must carry human approval provenance")
    return cases


def acceptance(output, expected):
    return isinstance(output, dict) and all(
        k in output and output[k] == v for k, v in expected.items()
    )


async def compare(suite, run, *, contract=None):
    """run(input, case_id, arm) returns output/cost/latency_ms/trajectory.

    All attempts, unsuccessful cases and evaluation charges must be included by
    the adapter. An unknown cost prevents a savings claim. Order alternates to
    reduce simple time-of-day bias; this is a paired audit, not a significance test.
    """
    cases = validate_suite(suite)
    validate_contract({} if contract is None else contract)
    records = []
    totals = {"baseline": Decimal(0), "candidate": Decimal(0)}
    known = True
    accepted = {"baseline": 0, "candidate": 0}
    for index, case in enumerate(cases):
        pair = {}
        for arm in ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline"):
            observed = await run(deepcopy(case["input"]), case["id"], arm)
            ok = acceptance(observed.get("output"), case["expected"])
            evaluated = evaluate_case(
                {**observed, "case_id": case["id"], "accepted": ok}, contract or {}
            )
            pair[arm] = {
                **observed,
                "accepted": evaluated["passed"],
                "failures": evaluated["failures"],
            }
            accepted[arm] += int(evaluated["passed"])
            try:
                cost = Decimal(str(observed["cost"]))
                if not cost.is_finite() or cost < 0:
                    raise ValueError("Invalid cost")
                totals[arm] += cost
            except (KeyError, ValueError, ArithmeticError):
                known = False
        passed = not pair["baseline"]["accepted"] or pair["candidate"]["accepted"]
        records.append(
            {
                "case_id": case["id"],
                "passed": passed,
                "failures": [] if passed else ["Candidate lost an accepted baseline result"],
                **pair,
            }
        )
    quality = all(r["passed"] for r in records) and accepted["candidate"] > 0
    return {
        "suite_id": suite["suite_id"],
        "suite_hash": digest(suite),
        "cases": records,
        "total": len(records),
        "passing": sum(r["passed"] for r in records),
        "passed": quality and known,
        "quality_preserved": quality,
        "accepted": accepted,
        "cost_complete": known,
        "cost": {k: str(v) for k, v in totals.items()} if known else None,
        "savings_fraction": float(1 - totals["candidate"] / totals["baseline"])
        if quality and known and totals["baseline"] > 0
        else None,
        "scope": "Paired test-set rerun; source support alone is not business acceptance.",
    }
