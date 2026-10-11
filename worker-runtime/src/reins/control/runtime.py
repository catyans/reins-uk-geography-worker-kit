"""Pinned hosted execution plans for customer-owned agents."""

from __future__ import annotations

import json
import time
from decimal import Decimal


class Runtime:
    def migrate_runtime(self):
        with self.transaction():
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS runtime_assignments("
                "workflow TEXT PRIMARY KEY REFERENCES workflows(id),"
                "deployment TEXT NOT NULL,version INTEGER NOT NULL,"
                "configuration TEXT NOT NULL,stopped INTEGER NOT NULL DEFAULT 0,"
                "escalated INTEGER NOT NULL DEFAULT 0,"
                "created REAL NOT NULL,decided REAL)"
            )

    def runtime_decision(self, body):
        """Return the pinned configuration and current control decision.

        The customer must call this from its backend before paid work. A stop is
        durable: later reservations for the task are refused.
        """
        from .ledger import amount, identity

        wid = identity(body.get("workflow_id"), "workflow_id")
        value = body.get("success_value")
        if value is not None:
            amount(value)
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM runtime_assignments WHERE workflow=?", (wid,)
            ).fetchone()
            if not row:
                raise ValueError("Task has no hosted deployment assignment")
            _, workflow = self._task(wid)
            state = self.status(wid)["workflows"][0]
            remaining = Decimal(state["budget_state"]["work_remaining"])
            advice = (
                self.economic_decision({"workflow_id": wid, "success_value": value})
                if value is not None
                and not row["stopped"]
                and remaining > 0
                and workflow["state"] == "running"
                else None
            )
            stop = bool(
                row["stopped"]
                or workflow["state"] != "running"
                or remaining <= 0
                or (advice and advice["action"] == "stop")
            )
            reason = (
                "previous_runtime_stop"
                if row["stopped"]
                else "workflow_not_running"
                if workflow["state"] != "running"
                else "budget_exhausted"
                if remaining <= 0
                else "negative_continuation_value"
                if advice and advice["action"] == "stop"
                else "pinned_qualified_configuration"
            )
            configuration = json.loads(row["configuration"])
            escalated = False
            if not stop:
                failed = self.db.execute(
                    "SELECT 1 FROM events WHERE workflow=? AND kind='validation' "
                    "AND json_extract(payload,'$.passed')=0 AND created>? LIMIT 1",
                    (wid, row["decided"] if row["escalated"] else 0),
                ).fetchone()
                if failed:
                    if not row["escalated"]:
                        report = self.experiment_report({"id": configuration["experiment"]})
                        baseline_id = report["spec"]["baseline"]
                        baseline_result = next(
                            v for v in report["variants"] if v["id"] == baseline_id
                        )
                        baseline_config = next(
                            v for v in report["spec"]["variants"] if v["id"] == baseline_id
                        )
                    if not row["escalated"] and (
                        configuration["variant"] != baseline_id
                        and baseline_result["eligible"]
                        and baseline_config["policy"] == configuration["policy"]
                    ):
                        configuration = {
                            **configuration,
                            "variant": baseline_id,
                            "config": baseline_config.get("config", {}),
                        }
                        self.db.execute(
                            "UPDATE runtime_assignments SET configuration=?,escalated=1,"
                            "decided=? WHERE workflow=?",
                            (
                                json.dumps(configuration, sort_keys=True, separators=(",", ":")),
                                time.time(),
                                wid,
                            ),
                        )
                        self.event(
                            wid,
                            wid,
                            "runtime_escalated",
                            deployment=row["deployment"],
                            from_variant=json.loads(row["configuration"])["variant"],
                            to_variant=baseline_id,
                        )
                        escalated = True
                        reason = "validation_failed_use_qualified_baseline"
                    else:
                        stop = True
                        reason = "validation_failed_no_qualified_fallback"
            if stop and not row["stopped"]:
                self.db.execute(
                    "UPDATE runtime_assignments SET stopped=1,decided=? WHERE workflow=?",
                    (time.time(), wid),
                )
                self.event(wid, wid, "runtime_stopped", reason=reason)
            elif not row["decided"] and not escalated:
                self.db.execute(
                    "UPDATE runtime_assignments SET decided=? WHERE workflow=?",
                    (time.time(), wid),
                )
                self.event(
                    wid,
                    wid,
                    "runtime_planned",
                    deployment=row["deployment"],
                    version=row["version"],
                )
            return {
                "workflow_id": wid,
                "action": "stop" if stop else "escalate" if escalated else "continue",
                "reason": reason,
                "deployment": row["deployment"],
                "deployment_version": row["version"],
                "configuration": configuration,
                "budget_work_remaining": str(remaining),
                "economic_advice": advice,
                "quality_evidence": self.runtime_evidence(configuration),
            }

    def runtime_evidence(self, configuration):
        report = self.experiment_report({"id": configuration["experiment"]})
        baseline = next(v for v in report["variants"] if v["id"] == report["spec"]["baseline"])
        selected = next(v for v in report["variants"] if v["id"] == configuration["variant"])
        return {
            "experiment": configuration["experiment"],
            "cases": selected["total"],
            "accepted": selected["accepted"],
            "baseline_cost_per_accepted": baseline["cost_per_accepted"],
            "selected_cost_per_accepted": selected["cost_per_accepted"],
            "savings_fraction": selected["savings_fraction"],
            "quality_qualified": selected["eligible"],
        }

    def runtime_assignment(self, workflow_id):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM runtime_assignments WHERE workflow=?", (workflow_id,)
            ).fetchone()
            if not row:
                return None
            configuration = json.loads(row["configuration"])
            return {
                "deployment": row["deployment"],
                "version": row["version"],
                "configuration": configuration,
                "quality_evidence": self.runtime_evidence(configuration),
                "stopped": bool(row["stopped"]),
                "escalated": bool(row["escalated"]),
                "created": row["created"],
                "decided": row["decided"],
            }
