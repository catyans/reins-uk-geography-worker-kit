"""Private, bounded data-extraction pilot using pinned documents and existing Gemini adapter."""

from __future__ import annotations

import json
import time
from pathlib import Path

from reins.control.client import ControlDenied, ControlUnavailable
from reins.projects.documents import FIELDS, atomic_json, digest, prompt, supported


def validate_input(body):
    sources = body.get("sources")
    fields = body.get("fields", list(FIELDS))
    if (
        not isinstance(sources, dict)
        or not sources
        or any(not isinstance(k, str) or not isinstance(v, str) for k, v in sources.items())
    ):
        raise ValueError("Supply pinned document names and text in sources")
    if not isinstance(fields, list) or not fields or len(set(fields)) != len(fields):
        raise ValueError("Supply distinct required fields")
    if set(fields) - set(FIELDS):
        raise ValueError("Supported fields: " + ", ".join(FIELDS))
    if len(json.dumps(sources).encode()) > 100000:
        raise ValueError("Pinned documents exceed 100 KB")
    return sources, fields


async def extract(
    body, controlled, provider, output, *, model="lite", max_attempts=6, resume=False
):
    """Persist before dispatch and after every response. Never repeat an uncertain attempt.

    Quote checks establish source support, not semantic correctness. Human/offline
    review determines business acceptance. ``provider`` is the existing Gemini adapter.
    """
    if type(max_attempts) is not int or not 1 <= max_attempts <= 30:
        raise ValueError("Use 1 to 30 attempts")
    sources, fields = validate_input(body)
    path = Path(output)
    task = controlled.context["task_id"]
    identity = digest([body, task, model, max_attempts, "pilot-extraction-v1"])
    result = None
    if path.exists():
        saved = json.loads(path.read_text())
        if saved["identity"] != identity:
            raise ValueError("Output belongs to different input, policy task or configuration")
        if not resume:
            return saved
        if saved["state"] != "held":
            raise ValueError(
                "Only an explicitly held task can resume; reconcile other states first"
            )
        result = saved
    if result is None:
        result = {
            "identity": identity,
            "task_id": task,
            "source_version": digest(sources),
            "state": "running",
            "fields": {},
            "attempts": [],
            "semantic_acceptance": None,
        }
    result["state"] = "running"
    for index in range(len(result["attempts"]), max_attempts):
        missing = [f for f in fields if f not in result["fields"]]
        if not missing:
            result["state"] = "source_supported"
            break
        # This journal entry precedes dispatch, including crashes and lost acknowledgements.
        attempt = {"index": index + 1, "state": "dispatching", "required": missing}
        result["attempts"].append(attempt)
        atomic_json(path, result)
        receipt = {}
        started = time.perf_counter()
        try:
            answer = await provider.call(
                prompt(missing, sources),
                model,
                task + ":" + str(index),
                receipt=receipt,
                operation_scope=task,
            )
        except ControlDenied as exc:
            attempt.update(state="not_dispatched", reason=str(exc))
            result["state"] = "held"
            break
        except Exception as exc:
            # Avoid leaking provider response bodies or secrets into review artifacts.
            attempt.update(state="needs_inspection", error_type=type(exc).__name__)
            result["state"] = "needs_review"
            break
        attempt.update(
            state="returned",
            receipt=receipt,
            latency_ms=(time.perf_counter() - started) * 1000,
            output=answer,
        )
        if isinstance(answer, dict):
            for field in missing:
                value = answer.get(field)
                if supported(value, sources) and value.get("value") is not None:
                    if field != "license" and value["value"] not in sources[value["source"]]:
                        continue
                    result["fields"][field] = value
        # Save paid output before any further service interaction.
        atomic_json(path, result)
        if not receipt.get("settled"):
            result["state"] = "needs_reconciliation"
            break
        checks = [
            {"item": f, "check": "verbatim-source-v1", "source": v["source"], "passed": True}
            for f, v in result["fields"].items()
        ]
        try:
            decision = controlled.evidence(
                event_id=digest([identity, index]),
                request_id=receipt["request_id"],
                stage="extract",
                source_version=digest(sources),
                required=fields,
                checks=checks,
            )
        except (ValueError, ControlUnavailable):
            result["state"] = "needs_review"
            break
        attempt["decision"] = decision["decision"]
        if decision["decision"] in ("pause", "wrapup", "wait"):
            result["state"] = "held" if decision["decision"] != "wrapup" else "partial"
            break
    else:
        result["state"] = "source_supported" if len(result["fields"]) == len(fields) else "partial"
    atomic_json(path, result)
    # Retries of this submission are safe through the immutable review ID.
    try:
        controlled.client.post(
            "pilot/reviews/submit",
            {
                "review_id": digest([identity, len(result["attempts"])]),
                "workflow_id": controlled.context["workflow_id"],
                "input": body,
                "output": result["fields"],
                "source_version": result["source_version"],
            },
        )
    except (ValueError, ControlUnavailable):
        result["review_submission"] = "pending_operator_import"
        atomic_json(path, result)
    return result
