"""Operator-authorized automation and outcome-conditioned economic advice."""

import json
import math

from .experiments import key, token, wire


class Advancement:
    def migrate_advancement(self):
        # Additive tables: older v5 readers remain compatible with the existing ledger.
        with self.transaction():
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS automation_rules("
                "id TEXT PRIMARY KEY,body TEXT NOT NULL,state TEXT NOT NULL)"
            )

    def bulk_reviews(self, body):
        batch = key(body.get("batch_id"))
        items = body.get("items")
        if (
            not isinstance(items, list)
            or not 1 <= len(items) <= 50
            or any(not isinstance(item, dict) for item in items)
        ):
            raise ValueError("Select 1 to 50 tasks")
        pending, seen = [], set()
        with self.lock:
            for item in items:
                wid = key(item.get("workflow_id"))
                if wid in seen:
                    raise ValueError("Duplicate task in batch")
                seen.add(wid)
                self._task(wid)
                if not isinstance(item.get("input"), dict) or not item["input"]:
                    raise ValueError("Supply the original Agent input for every selected task")
                source = key(item.get("source_version"))
                rid = "batch-" + token(batch, wid)
                data = dict(
                    review_id=rid,
                    workflow_id=wid,
                    source_version=source,
                    input=item["input"],
                    output=item.get("output"),
                )
                if len(wire(data).encode()) > 131072:
                    raise ValueError("Review exceeds 128 KiB")
                pending.append((rid, wid, data, self.regression_case({"workflow_id": wid})))
            with self.transaction():
                for rid, wid, data, record in pending:
                    old = self.db.execute(
                        "SELECT body FROM pilot_reviews WHERE id=?", (rid,)
                    ).fetchone()
                    if old and json.loads(old[0])["submitted"] != data:
                        raise ValueError("Batch ID conflicts with an existing review")
                    if not old:
                        self.db.execute(
                            "INSERT INTO pilot_reviews VALUES(?,?,?,'pending',1)",
                            (rid, wid, wire(dict(submitted=data, recorded_case=record))),
                        )
                self.console_audit(
                    "batch_reviews_created", batch, {"reviews": [x[0] for x in pending]}
                )
        return {"review_ids": [x[0] for x in pending]}

    def economic_decision(self, body):
        """Observed comparable outcomes, not a causal estimate of treatment effects."""
        from .ledger import amount, dollars

        wid = key(body.get("workflow_id"))
        value = amount(body.get("success_value"))
        with self.lock:
            _, w = self._task(wid)
            p = self.db.execute(
                "SELECT * FROM progress WHERE task=? ORDER BY id DESC LIMIT 1", (wid,)
            ).fetchone()
            result = dict(
                workflow_id=wid,
                action="review",
                reason="insufficient_history",
                samples=0,
                success_probability=None,
                probability_low=None,
                probability_high=None,
                remaining_p90=None,
                historical_workflows=[],
                advisory_only=True,
            )
            if not p:
                result["reason"] = "record_progress_first"
                return result

            def execution_signature(workflow):
                saved = self.db.execute(
                    "SELECT body FROM console_experiments WHERE "
                    "json_extract(body,'$.kind')='deployment' "
                    "AND json_extract(body,'$.run_id')=?",
                    (workflow,),
                ).fetchone()
                if not saved:
                    return None
                config = json.loads(saved[0])["configuration"]
                return wire({k: config[k] for k in ("adapter", "policy", "config")})

            signature = execution_signature(wid)
            rows = self.db.execute(
                "SELECT p.*,w.id wid,w.accepted FROM progress p JOIN workflows w "
                "ON w.id=p.workflow WHERE w.id!=? AND w.customer=? "
                "AND w.task_type=? AND w.policy=? "
                "AND w.state='completed' AND w.accepted IS NOT NULL AND p.task=w.id "
                "AND p.spent IS NOT NULL AND p.stage=? AND p.total=? AND p.done=? "
                "ORDER BY p.id DESC",
                (
                    wid,
                    w["customer"],
                    w["task_type"],
                    w["policy"],
                    p["stage"],
                    p["total"],
                    p["done"],
                ),
            ).fetchall()
            costs, outcomes, seen = [], [], set()
            for r in rows:
                if r["wid"] in seen:
                    continue
                seen.add(r["wid"])
                if execution_signature(r["wid"]) != signature:
                    continue
                if self.db.execute(
                    "SELECT 1 FROM requests WHERE workflow=? AND actual IS NULL", (r["wid"],)
                ).fetchone():
                    continue
                costs.append(max(0, self._charge(r["wid"]) - r["spent"]))
                outcomes.append(r["accepted"])
                result["historical_workflows"].append(r["wid"])
                if len(costs) == 200:
                    break
            n = len(costs)
            result["samples"] = n
            if n < 30:
                return result
            rate = sum(outcomes) / n
            z = 1.96
            center = (rate + z * z / (2 * n)) / (1 + z * z / n)
            radius = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / (1 + z * z / n)
            lo, hi = max(0, center - radius), min(1, center + radius)
            cost = sorted(costs)[math.ceil(n * 0.9) - 1]
            # Compare conservative continuation value with observed residual cost.
            action = "continue" if lo * value > cost else "stop" if hi * value < cost else "review"
            result.update(
                action=action,
                reason="comparable_historical_outcomes",
                success_probability=rate,
                probability_low=lo,
                probability_high=hi,
                remaining_p90=dollars(cost),
                success_value=dollars(value),
                scope="Recorded API cost; excludes unrecorded future external costs",
                method="Exact customer/policy/task/stage/progress cohort; "
                "Wilson 95% outcome interval",
            )
            return result

    def outcome_confirm(self, body):
        wid = key(body.get("workflow_id"))
        reason = key(body.get("reason"))
        if type(body.get("accepted")) is not bool:
            raise ValueError("Explicit boolean business outcome required")
        with self.transaction():
            _, w = self._task(wid)
            if w["finished"] is None:
                raise ValueError("Only finished tasks can receive a business outcome")
            if w["accepted"] is not None and bool(w["accepted"]) != body["accepted"]:
                raise ValueError("Existing outcome differs; do not silently overwrite it")
            self.db.execute(
                "UPDATE workflows SET accepted=? WHERE id=?", (int(body["accepted"]), wid)
            )
            self.console_audit(
                "outcome_confirmed", wid, dict(accepted=body["accepted"], reason=reason)
            )
        return {"status": "recorded"}

    def automation_save(self, body):
        rid, eid, did = (
            key(body.get("id")),
            key(body.get("experiment")),
            key(body.get("deployment")),
        )
        expected = body.get("expected_version")
        minimum = body.get("min_samples", 30)
        savings = float(body.get("min_savings", 0.1))
        rate = float(body.get("rollback_below", 0.95))
        if type(expected) is not int or expected < 1 or type(minimum) is not int or minimum < 30:
            raise ValueError(
                "Existing deployment version and at least 30 monitoring outcomes required"
            )
        if (
            not math.isfinite(savings)
            or not 0.01 <= savings <= 1
            or not math.isfinite(rate)
            or not 0 <= rate <= 1
        ):
            raise ValueError("Invalid savings or acceptance threshold")
        saved = dict(
            id=rid,
            experiment=eid,
            deployment=did,
            expected_version=expected,
            min_samples=minimum,
            min_savings=savings,
            rollback_below=rate,
            reason=key(body.get("reason")),
        )
        with self.transaction():
            if self.db.execute("SELECT 1 FROM automation_rules WHERE id=?", (rid,)).fetchone():
                raise ValueError("Use a new automation rule ID")
            d = self.db.execute("SELECT * FROM console_deployments WHERE id=?", (did,)).fetchone()
            if not d or d["version"] != expected:
                raise ValueError("Deployment changed; refresh")
            e = self.db.execute(
                "SELECT body FROM console_experiments WHERE id=?", (eid,)
            ).fetchone()
            if not e:
                raise ValueError("Unknown experiment")
            spec = json.loads(e[0])
            current = json.loads(d["body"])
            baseline = next(
                (v for v in spec.get("variants", []) if v["id"] == spec["baseline"]), None
            )
            if (
                not baseline
                or current["adapter"] != spec["adapter"]
                or current["config"] != baseline.get("config", {})
                or current["policy"] != baseline["policy"]
            ):
                raise ValueError(
                    "Experiment baseline must match the current deployed configuration"
                )
            if spec["constraints"]["min_cases"] < 30:
                raise ValueError("Automatic adoption requires an experiment with at least 30 cases")
            if self.db.execute(
                "SELECT 1 FROM automation_rules WHERE json_extract(body,'$.deployment')=? "
                "AND state IN ('waiting','monitoring')",
                (did,),
            ).fetchone():
                raise ValueError("Disable the existing rule for this deployment first")
            self.db.execute(
                "INSERT INTO automation_rules VALUES(?,?,'waiting')", (rid, wire(saved))
            )
            self.console_audit("automation_enabled", rid, saved)
        return saved

    def automation_list(self, body):
        with self.lock:
            return {
                "rules": [
                    dict(r) | {"body": json.loads(r["body"])}
                    for r in self.db.execute("SELECT * FROM automation_rules")
                ]
            }

    def automation_disable(self, body):
        rid = key(body.get("id"))
        reason = key(body.get("reason"))
        with self.transaction():
            self.db.execute("UPDATE automation_rules SET state='disabled' WHERE id=?", (rid,))
            self.console_audit("automation_disabled", rid, {"reason": reason})
        return {"status": "disabled"}

    def automation_tick(self):
        # Adoption/rollback and rule state commit together, including crash recovery.
        with self.transaction():
            for row in self.db.execute(
                "SELECT * FROM automation_rules WHERE state IN ('waiting','monitoring')"
            ).fetchall():
                rule = json.loads(row["body"])
                did = rule["deployment"]
                d = self.db.execute(
                    "SELECT * FROM console_deployments WHERE id=?", (did,)
                ).fetchone()
                expected = rule.get("adopted_version", rule["expected_version"])
                state = row["state"]
                if not d or d["version"] != expected:
                    state = "superseded"
                elif state == "waiting":
                    report = self.experiment_report({"id": rule["experiment"]})
                    winner = next(
                        (v for v in report["variants"] if v["id"] == report["recommended"]), None
                    )
                    if (
                        winner
                        and winner["eligible"]
                        and (winner["savings_fraction"] or 0) >= rule["min_savings"]
                    ):
                        self.deployment_adopt(
                            dict(
                                id=did,
                                experiment=rule["experiment"],
                                variant=winner["id"],
                                expected_version=expected,
                                reason="Authorized automation: " + rule["id"],
                            )
                        )
                        rule["adopted_version"] = expected + 1
                        state = "monitoring"
                    elif report["state"] == "cancelled":
                        state = "cancelled"
                else:
                    outcomes = []
                    for run in self.db.execute(
                        "SELECT body FROM console_experiments WHERE "
                        "json_extract(body,'$.kind')='deployment'"
                    ):
                        spec = json.loads(run[0])
                        if spec["deployment"] != did or spec["deployment_version"] != expected:
                            continue
                        w = self.db.execute(
                            "SELECT accepted FROM workflows WHERE id=? AND finished IS NOT NULL",
                            (spec["run_id"],),
                        ).fetchone()
                        if w and w[0] is not None:
                            outcomes.append(w[0])
                    rule["observed_outcomes"] = len(outcomes)
                    if (
                        len(outcomes) >= rule["min_samples"]
                        and sum(outcomes) / len(outcomes) < rule["rollback_below"]
                    ):
                        self.deployment_rollback(
                            dict(
                                id=did,
                                target_version=rule["expected_version"],
                                expected_version=expected,
                                reason="Quality rollback: " + rule["id"],
                            )
                        )
                        state = "rolled_back"
                with self.transaction():
                    self.db.execute(
                        "UPDATE automation_rules SET body=?,state=? WHERE id=?",
                        (wire(rule), state, row["id"]),
                    )
                    if state != row["state"]:
                        self.console_audit(
                            "automation_transition", row["id"], {"from": row["state"], "to": state}
                        )
