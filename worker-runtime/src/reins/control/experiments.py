"""Versioned agent experiments. Workers receive inputs, never expected answers."""

from __future__ import annotations

import hashlib
import json
import math
import time
from decimal import Decimal

from .regression import evaluate_case, validate_contract


def wire(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def key(value):
    from .ledger import identity

    return identity(value, "ID")


def token(*parts):
    return hashlib.sha256(wire(parts).encode()).hexdigest()


class Experiments:
    def adapter_save(self, body):
        aid = key(body.get("id"))
        saved = {
            "id": aid,
            "description": key(body.get("description")),
            "protocol": "reins-agent-v1",
        }
        with self.transaction():
            old = self.db.execute("SELECT body FROM console_adapters WHERE id=?", (aid,)).fetchone()
            if old and old[0] != wire(saved):
                raise ValueError("Adapter versions are immutable")
            self.db.execute(
                "INSERT OR IGNORE INTO console_adapters VALUES(?,?)", (aid, wire(saved))
            )
        return saved

    def experiment_create(self, body):
        from .ledger import amount, dollars

        eid, adapter, suite_id = (
            key(body.get("id")),
            key(body.get("adapter")),
            key(body.get("suite_id")),
        )
        budget = dollars(amount(body["budget"]))
        if Decimal(budget) <= 0:
            raise ValueError("Use a positive experiment budget")
        variants = body.get("variants")
        if (
            not isinstance(variants, list)
            or not 2 <= len(variants) <= 6
            or any(not isinstance(v, dict) for v in variants)
        ):
            raise ValueError("Choose 2 to 6 versioned variants")
        names = [key(v.get("id")) for v in variants]
        if len(set(names)) != len(names) or body.get("baseline") not in names:
            raise ValueError("Distinct variants and a baseline are required")
        constraints = {
            "min_cases": 30,
            "min_success_rate": 0.95,
            "max_quality_drop": 0.0,
            "max_p95_ms": None,
        }
        supplied = body.get("constraints", {})
        if not isinstance(supplied, dict) or set(supplied) - set(constraints):
            raise ValueError("Unknown experiment constraints")
        constraints.update(supplied)
        from reins.optimization import Constraints

        Constraints(**constraints)
        contract = body.get("contract", {})
        validate_contract(contract)
        with self.lock:
            if not self.db.execute(
                "SELECT 1 FROM console_adapters WHERE id=?", (adapter,)
            ).fetchone():
                raise ValueError("Register the adapter first")
            suite = self.db.execute(
                "SELECT body FROM pilot_suites WHERE id=?", (suite_id,)
            ).fetchone()
            if not suite:
                raise ValueError("Select a human-reviewed test set")
            suite = json.loads(suite[0])
            from reins.projects.pilot_evaluation import validate_suite

            cases = validate_suite(suite)
            for v in variants:
                if not isinstance(v.get("config", {}), dict):
                    raise ValueError("Variant config must be an object")
                policy = key(v.get("policy"))
                row = self.db.execute(
                    "SELECT body FROM pilot_policies WHERE id=?", (policy,)
                ).fetchone()
                if not row or json.loads(row[0])["workflow"]["mode"] != "enforce":
                    raise ValueError("Experiment policies must use enforced budgets")
        spec = dict(
            id=eid,
            adapter=adapter,
            suite_id=suite_id,
            suite_hash=token(suite),
            variants=variants,
            baseline=body["baseline"],
            budget=budget,
            constraints=constraints,
            contract=contract,
        )
        with self.lock:
            old = self.db.execute(
                "SELECT body FROM console_experiments WHERE id=?", (eid,)
            ).fetchone()
            if old:
                if old[0] != wire(spec):
                    raise ValueError("Experiment ID cannot be reused")
                return {"id": eid, "duplicate": True}
        self.pool(
            dict(
                pool_id="experiment:" + eid,
                customer_id="experiment",
                team_id="experiments",
                budget=budget,
            )
        )
        with self.transaction():
            old = self.db.execute(
                "SELECT body FROM console_experiments WHERE id=?", (eid,)
            ).fetchone()
            if old:
                if old[0] != wire(spec):
                    raise ValueError("Experiment ID cannot be reused")
                return {"id": eid, "duplicate": True}
            self.db.execute(
                "INSERT INTO console_experiments VALUES(?,?,'queued',?)",
                (eid, wire(spec), time.time()),
            )
            for index, case in enumerate(cases):
                # Alternate candidate order; all arms share the same frozen inputs.
                order = variants[index % len(variants) :] + variants[: index % len(variants)]
                for v in order:
                    jid = token(eid, case["id"], v["id"])
                    job = dict(
                        adapter=adapter,
                        input=case["input"],
                        config=v.get("config", {}),
                        policy=v["policy"],
                        workflow_id="experiment-" + jid,
                    )
                    self.db.execute(
                        "INSERT INTO console_jobs VALUES(?,?,?,?,?,'queued',NULL,NULL,NULL)",
                        (jid, eid, case["id"], v["id"], wire(job)),
                    )
            self.console_audit("experiment_created", eid, {"cases": len(cases), "variants": names})
        return {"id": eid, "jobs": len(cases) * len(variants)}

    def experiment_claim(self, body):
        adapter, owner = key(body.get("adapter")), key(body.get("worker_id"))
        with self.transaction():
            row = self.db.execute(
                "SELECT j.* FROM console_jobs j JOIN console_experiments e ON "
                "e.id=j.experiment WHERE j.state='queued' AND e.state IN "
                "('queued','running') AND json_extract(j.body,'$.adapter')=? ORDER BY "
                "j.rowid LIMIT 1",
                (adapter,),
            ).fetchone()
            if not row:
                return {"job": None}
            self.db.execute(
                "UPDATE console_jobs SET state='provisioning',owner=?,claimed=? WHERE id=?",
                (owner, time.time(), row["id"]),
            )
            self.db.execute(
                "UPDATE console_experiments SET state='running' WHERE id=?", (row["experiment"],)
            )
        job = json.loads(row["body"])
        try:
            context = self.workflow(
                dict(
                    workflow_id=job["workflow_id"],
                    customer_id=job.get("customer", "experiment"),
                    team_id=job.get("team", "experiments"),
                    task_type=job.get("deployment", "console-experiment"),
                    policy_template=job["policy"],
                    **(
                        {"pool_id": "experiment:" + row["experiment"]}
                        if job.get("kind") != "deployment"
                        else {}
                    ),
                )
            )
        except Exception:
            with self.transaction():
                self.db.execute(
                    "UPDATE console_experiments SET state='needs_inspection' WHERE id=? AND "
                    "state!='cancelled'",
                    (row["experiment"],),
                )
            raise
        with self.transaction():
            current = self.db.execute(
                "SELECT state FROM console_experiments WHERE id=?", (row["experiment"],)
            ).fetchone()[0]
            if current == "cancelled":
                self.db.execute(
                    "UPDATE workflows SET state='paused' WHERE id=?", (job["workflow_id"],)
                )
                self.db.execute(
                    "UPDATE console_jobs SET state='cancelled' WHERE id=?", (row["id"],)
                )
                return {"job": None}
            self.db.execute("UPDATE console_jobs SET state='running' WHERE id=?", (row["id"],))
        return {
            "job": {
                "id": row["id"],
                "experiment": row["experiment"],
                "case_id": row["case_id"],
                "variant": row["arm"],
                **job,
                "context": context,
            }
        }

    def experiment_heartbeat(self, body):
        jid, owner = key(body.get("id")), key(body.get("worker_id"))
        with self.transaction():
            row = self.db.execute(
                "SELECT j.*,e.state experiment_state FROM console_jobs j JOIN "
                "console_experiments e ON e.id=j.experiment WHERE j.id=?",
                (jid,),
            ).fetchone()
            if not row or row["owner"] != owner:
                raise ValueError("Worker does not own this job")
            if row["state"] == "running":
                self.db.execute("UPDATE console_jobs SET claimed=? WHERE id=?", (time.time(), jid))
            return {"state": row["state"], "experiment_state": row["experiment_state"]}

    def experiment_result(self, body):
        jid, owner = key(body.get("id")), key(body.get("worker_id"))
        latency = body.get("latency_ms")
        if type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0:
            raise ValueError("Finite nonnegative latency required")
        payload = {"output": body.get("output"), "latency_ms": latency, "error": body.get("error")}
        if len(wire(payload).encode()) > 131072:
            raise ValueError("Result exceeds 128 KiB")
        with self.lock:
            row = self.db.execute("SELECT * FROM console_jobs WHERE id=?", (jid,)).fetchone()
            if not row or row["owner"] != owner:
                raise ValueError("Worker does not own this job")
            if row["result"]:
                if json.loads(row["result"])["submitted"] != payload:
                    raise ValueError("Conflicting result")
                return {"status": "recorded", "duplicate": True}
            if row["state"] != "running":
                raise ValueError("Job is not running")
            spec = json.loads(
                self.db.execute(
                    "SELECT body FROM console_experiments WHERE id=?", (row["experiment"],)
                ).fetchone()[0]
            )
            if spec.get("kind") == "deployment":
                return self.deployment_result(row, payload)
            suite = json.loads(
                self.db.execute(
                    "SELECT body FROM pilot_suites WHERE id=?", (spec["suite_id"],)
                ).fetchone()[0]
            )
            expected = next(c["expected"] for c in suite["cases"] if c["id"] == row["case_id"])
            output = payload["output"]
            accepted = (
                isinstance(output, dict)
                and all(k in output and output[k] == v for k, v in expected.items())
                and not payload["error"]
            )
            wid = json.loads(row["body"])["workflow_id"]
            record = self.regression_case({"workflow_id": wid})
            record.update(accepted=bool(accepted), latency_ms=latency)
            evaluation = evaluate_case(record, spec["contract"])
            accepted = evaluation["passed"]
            result = {
                "business_accepted": record["accepted"],
                "submitted": payload,
                "accepted": accepted,
                "failures": evaluation["failures"],
                "workflow_id": wid,
            }
            if record["cost"] is not None:
                self.transition({"workflow_id": wid, "action": "finish", "accepted": accepted})
            with self.transaction():
                self.db.execute(
                    "UPDATE console_jobs SET state='completed',result=? WHERE id=?",
                    (wire(result), jid),
                )
                remains = self.db.execute(
                    "SELECT count(*) FROM console_jobs WHERE experiment=? AND state!='completed'",
                    (row["experiment"],),
                ).fetchone()[0]
                if not remains:
                    self.db.execute(
                        "UPDATE console_experiments SET state='completed' WHERE id=? AND state "
                        "NOT IN ('cancelled')",
                        (row["experiment"],),
                    )
        return {"status": "recorded"}

    def experiment_cancel(self, body):
        eid, reason = key(body.get("id")), key(body.get("reason"))
        with self.transaction():
            row = self.db.execute(
                "SELECT state FROM console_experiments WHERE id=?", (eid,)
            ).fetchone()
            if not row or row[0] == "completed":
                raise ValueError("Experiment cannot be cancelled")
            self.db.execute("UPDATE console_experiments SET state='cancelled' WHERE id=?", (eid,))
            self.db.execute(
                "UPDATE console_jobs SET state='cancelled' WHERE experiment=? AND state='queued'",
                (eid,),
            )
            ids = [
                json.loads(r[0])["workflow_id"]
                for r in self.db.execute("SELECT body FROM console_jobs WHERE experiment=?", (eid,))
            ]
            for wid in ids:
                self.db.execute(
                    "UPDATE workflows SET state='paused' WHERE id=? AND finished IS NULL", (wid,)
                )
            self.console_audit("experiment_cancelled", eid, {"reason": reason})
        return {"state": "cancelled"}

    def experiment_report(self, body):
        from .ledger import dollars

        eid = key(body.get("id"))
        with self.lock:
            experiment = self.db.execute(
                "SELECT * FROM console_experiments WHERE id=?", (eid,)
            ).fetchone()
            if not experiment:
                raise ValueError("Unknown experiment")
            spec = json.loads(experiment["body"])
            jobs = [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM console_jobs WHERE experiment=? ORDER BY rowid", (eid,)
                )
            ]
            summaries = []
            for variant in spec["variants"]:
                rows = [j for j in jobs if j["arm"] == variant["id"]]
                cost = 0
                eval_cost = 0
                pending = 0
                accepted = 0
                times = []
                for j in rows:
                    wid = json.loads(j["body"])["workflow_id"]
                    for r in self.db.execute(
                        "SELECT actual,category FROM requests WHERE workflow=?", (wid,)
                    ):
                        pending += r["actual"] is None
                        cost += r["actual"] or 0
                        if r["category"] == "evaluation":
                            eval_cost += r["actual"] or 0
                    if j["result"]:
                        result = json.loads(j["result"])
                        current_record = self.regression_case({"workflow_id": wid})
                        current_record.update(
                            accepted=result.get("business_accepted", result["accepted"]),
                            latency_ms=result["submitted"]["latency_ms"],
                        )
                        accepted += evaluate_case(current_record, spec["contract"])["passed"]
                        times.append(result["submitted"]["latency_ms"])
                done = sum(j["state"] == "completed" for j in rows)
                summaries.append(
                    dict(
                        id=variant["id"],
                        total=len(rows),
                        completed=done,
                        accepted=accepted,
                        rate=accepted / len(rows),
                        cost=dollars(cost),
                        evaluation_cost=dollars(eval_cost),
                        pending=pending,
                        cost_per_accepted=dollars(Decimal(cost) / accepted)
                        if accepted and not pending and done == len(rows)
                        else None,
                        p95_ms=sorted(times)[math.ceil(0.95 * len(times)) - 1] if times else None,
                    )
                )
            baseline = next(s for s in summaries if s["id"] == spec["baseline"])
            c = spec["constraints"]
            for s in summaries:
                eligible = (
                    experiment["state"] == "completed"
                    and all(v["completed"] == v["total"] and not v["pending"] for v in summaries)
                    and s["completed"] == s["total"]
                    and s["total"] >= c["min_cases"]
                    and not s["pending"]
                    and s["accepted"] > 0
                    and s["rate"]
                    >= max(c["min_success_rate"], baseline["rate"] - c["max_quality_drop"])
                    and (c["max_p95_ms"] is None or s["p95_ms"] <= c["max_p95_ms"])
                )
                s["eligible"] = bool(eligible)
                s["savings_fraction"] = (
                    (
                        1
                        - float(
                            Decimal(s["cost_per_accepted"]) / Decimal(baseline["cost_per_accepted"])
                        )
                    )
                    if eligible
                    and baseline["cost_per_accepted"]
                    and Decimal(baseline["cost_per_accepted"]) > 0
                    else None
                )
            ready = [s for s in summaries if s["eligible"]]
            winner = min(
                ready,
                key=lambda s: (Decimal(s["cost_per_accepted"]), s["id"] != spec["baseline"]),
                default=None,
            )
            return {
                "id": eid,
                "state": experiment["state"],
                "spec": spec,
                "variants": summaries,
                "recommended": winner["id"] if winner else None,
                "jobs": [
                    {k: j[k] for k in ("id", "case_id", "arm", "state", "claimed")} for j in jobs
                ],
            }

    def experiment_catalog(self, body):
        with self.lock:
            return {
                "adapters": [
                    json.loads(r[0]) for r in self.db.execute("SELECT body FROM console_adapters")
                ],
                "suites": [
                    {"id": r[0], "cases": len(json.loads(r[1])["cases"])}
                    for r in self.db.execute("SELECT * FROM pilot_suites")
                ],
                "experiments": [
                    {"id": r["id"], "state": r["state"], "created": r["created"]}
                    for r in self.db.execute(
                        "SELECT * FROM console_experiments WHERE "
                        "coalesce(json_extract(body,'$.kind'),'experiment')!='deployment' ORDER "
                        "BY created DESC"
                    )
                ],
                "runs": [
                    {
                        "id": r["id"],
                        "state": r["state"],
                        "created": r["created"],
                        "spec": json.loads(r["body"]),
                    }
                    for r in self.db.execute(
                        "SELECT * FROM console_experiments WHERE "
                        "json_extract(body,'$.kind')='deployment' ORDER BY created DESC"
                    )
                ],
                "deployments": [
                    dict(r) | {"body": json.loads(r["body"])}
                    for r in self.db.execute("SELECT * FROM console_deployments")
                ],
            }

    def deployment_adopt(self, body):
        did, eid, variant = (
            key(body.get("id")),
            key(body.get("experiment")),
            key(body.get("variant")),
        )
        expected = body.get("expected_version", 0)
        if type(expected) is not int or expected < 0:
            raise ValueError("Invalid deployment version")
        reason = key(body.get("reason"))
        with self.lock:
            report = self.experiment_report({"id": eid})
            chosen = next((v for v in report["variants"] if v["id"] == variant), None)
            if not chosen or not chosen["eligible"]:
                raise ValueError("Only complete quality-qualified variants can be adopted")
            config = next(v for v in report["spec"]["variants"] if v["id"] == variant)
            saved = dict(
                adapter=report["spec"]["adapter"],
                experiment=eid,
                variant=variant,
                config=config.get("config", {}),
                policy=config["policy"],
            )
            with self.transaction():
                row = self.db.execute(
                    "SELECT * FROM console_deployments WHERE id=?", (did,)
                ).fetchone()
                if (row["version"] if row else 0) != expected:
                    raise ValueError("Deployment changed; refresh")
                self.db.execute(
                    "INSERT INTO console_deployments VALUES(?,?,?) ON CONFLICT(id) DO UPDATE "
                    "SET body=excluded.body,version=excluded.version",
                    (did, wire(saved), expected + 1),
                )
                self.console_audit(
                    "deployment_adopted",
                    did,
                    {"version": expected + 1, "configuration": saved, "reason": reason},
                )
        return {"id": did, "version": expected + 1}

    def deployment_rollback(self, body):
        did = key(body.get("id"))
        target = body.get("target_version")
        expected = body.get("expected_version")
        if type(target) is not int or target < 1 or type(expected) is not int or expected < 1:
            raise ValueError("Positive versions required")
        reason = key(body.get("reason"))
        with self.transaction():
            row = self.db.execute("SELECT * FROM console_deployments WHERE id=?", (did,)).fetchone()
            historical = [
                json.loads(r[0])
                for r in self.db.execute(
                    "SELECT body FROM console_audit WHERE subject=? AND kind IN "
                    "('deployment_adopted','deployment_rollback')",
                    (did,),
                )
            ]
            record = next((h for h in historical if h["version"] == target), None)
            if not row or row["version"] != expected or not record:
                raise ValueError("Unknown or stale deployment version")
            self.db.execute(
                "UPDATE console_deployments SET body=?,version=? WHERE id=?",
                (wire(record["configuration"]), expected + 1, did),
            )
            self.console_audit(
                "deployment_rollback",
                did,
                {
                    "version": expected + 1,
                    "configuration": record["configuration"],
                    "reason": reason,
                },
            )
        return {"id": did, "version": expected + 1}

    def deployment_start(self, body):
        did = key(body.get("id"))
        with self.transaction():
            row = self.db.execute("SELECT * FROM console_deployments WHERE id=?", (did,)).fetchone()
            if not row:
                raise ValueError("Unknown deployment")
            config = json.loads(row["body"])
            ctx = self.workflow(
                dict(
                    workflow_id=key(body.get("workflow_id")),
                    customer_id=key(body.get("customer_id")),
                    task_type=did,
                    policy_template=config["policy"],
                )
            )
            previous = self.db.execute(
                "SELECT * FROM runtime_assignments WHERE workflow=?", (ctx["workflow_id"],)
            ).fetchone()
            if previous and (
                previous["deployment"] != did or previous["version"] != row["version"]
            ):
                raise ValueError("Task already has a different hosted configuration")
            self.db.execute(
                "INSERT OR IGNORE INTO runtime_assignments"
                "(workflow,deployment,version,configuration,created) VALUES(?,?,?,?,?)",
                (ctx["workflow_id"], did, row["version"], wire(config), time.time()),
            )
            return {
                "context": ctx,
                "configuration": json.loads(previous["configuration"]) if previous else config,
                "deployment_version": row["version"],
            }

    def deployment_launch(self, body):
        did, rid = key(body.get("id")), key(body.get("run_id"))
        customer = key(body.get("customer_id"))
        inputs = body.get("input")
        if not isinstance(inputs, dict) or len(wire(inputs).encode()) > 100000:
            raise ValueError("Supply an Agent input object up to 100 KB")
        with self.transaction():
            deployment = self.db.execute(
                "SELECT * FROM console_deployments WHERE id=?", (did,)
            ).fetchone()
            if not deployment:
                raise ValueError("Unknown deployment")
            config = json.loads(deployment["body"])
            eid = "run-" + token(rid)
            spec = dict(
                kind="deployment",
                run_id=rid,
                deployment=did,
                deployment_version=deployment["version"],
                customer=customer,
                input=inputs,
                configuration=config,
            )
            old = self.db.execute(
                "SELECT body FROM console_experiments WHERE id=?", (eid,)
            ).fetchone()
            if old:
                if old[0] != wire(spec):
                    raise ValueError("Run ID belongs to a different input or configuration")
                return {"id": eid, "workflow_id": rid, "duplicate": True}
            if self.db.execute("SELECT 1 FROM workflows WHERE id=?", (rid,)).fetchone():
                raise ValueError("Workflow ID already exists")
            self.db.execute(
                "INSERT INTO console_experiments VALUES(?,?,'queued',?)",
                (eid, wire(spec), time.time()),
            )
            job = dict(
                kind="deployment",
                deployment=did,
                customer=customer,
                team="default",
                adapter=config["adapter"],
                input=inputs,
                config=config["config"],
                policy=config["policy"],
                workflow_id=rid,
            )
            self.db.execute(
                "INSERT INTO console_jobs VALUES(?,?,?,?,?,'queued',NULL,NULL,NULL)",
                (eid, eid, rid, config["variant"], wire(job)),
            )
            self.console_audit(
                "deployment_run_queued", eid, {"deployment": did, "version": deployment["version"]}
            )
        return {"id": eid, "workflow_id": rid}

    def deployment_result(self, row, payload):
        job = json.loads(row["body"])
        wid = job["workflow_id"]
        self.review_submit(
            dict(
                review_id="result-" + row["id"],
                workflow_id=wid,
                source_version=token(job["input"]),
                input=job["input"],
                output=payload["output"],
            )
        )
        record = self.regression_case({"workflow_id": wid})
        with self.transaction():
            self.db.execute(
                "UPDATE console_jobs SET state='completed',result=? WHERE id=?",
                (wire({"submitted": payload}), row["id"]),
            )
            state = "needs_inspection" if record["cost"] is None else "completed"
            self.db.execute(
                "UPDATE console_experiments SET state=? WHERE id=? AND state!='cancelled'",
                (state, row["experiment"]),
            )
            if record["cost"] is not None:
                # Source-supported outputs still await a separate human business judgement.
                self.db.execute(
                    "UPDATE workflows SET state='completed',finished=? WHERE id=?",
                    (time.time(), wid),
                )
                self.event(wid, wid, "result_ready", review_id="result-" + row["id"])
        return {"status": "recorded", "review_id": "result-" + row["id"]}
