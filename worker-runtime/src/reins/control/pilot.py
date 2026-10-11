"""Single-customer pilot workflows: evidence, intervention, review and cost provenance.

Evidence is supplied by a trusted application validator, never inferred from silence
or a model's self-reported percentage. The admission ledger remains authoritative.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from .governance import encoded
from .operations import Operations

POLICY_FIELDS = {
    "budget",
    "mode",
    "max_iterations",
    "max_retries",
    "max_branches",
    "repeat_limit",
    "repeat_window_seconds",
    "failure_limit",
    "max_depth",
    "max_tools",
    "wrapup_reserve",
    "approved_models",
    "allowed_tools",
    "tool_policy_digests",
}


def text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(f"Invalid {label}")
    return value


class Pilot(Operations):
    def migrate_pilot(self, path):
        version = self.db.execute("SELECT max(version) FROM schema_version").fetchone()[0]
        if version >= 3:
            return
        backup = Path(str(path) + ".pre-v3.sqlite")
        temporary = Path(str(backup) + ".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with sqlite3.connect(temporary) as destination:
            self.db.backup(destination)
        os.replace(temporary, backup)
        statements = [
            "CREATE TABLE IF NOT EXISTS pilot_policies(id TEXT PRIMARY KEY, body TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS pilot_evidence(id TEXT PRIMARY KEY, task TEXT NOT NULL, "
            "request TEXT UNIQUE NOT NULL, body TEXT NOT NULL, created REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS pilot_controls(task TEXT PRIMARY KEY, state TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS pilot_issues(id TEXT PRIMARY KEY, workflow TEXT NOT NULL, "
            "task TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, "
            "version INTEGER NOT NULL, "
            "body TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS pilot_actions(id TEXT PRIMARY KEY, body TEXT NOT NULL, "
            "result TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS pilot_reviews(id TEXT PRIMARY KEY, workflow TEXT NOT NULL, "
            "body TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS pilot_suites(id TEXT PRIMARY KEY, body TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS pilot_prices(request TEXT PRIMARY KEY, body TEXT NOT NULL)",
        ]
        with self.transaction():
            for statement in statements:
                self.db.execute(statement)
            self.db.execute("UPDATE schema_version SET version=3")

    def policy_validate(self, body):
        from .ledger import amount, dollars

        wid = text(body["policy_id"], "policy ID")
        config = dict(body.get("workflow", {}))
        allowed = POLICY_FIELDS
        if set(config) - allowed:
            raise ValueError("Unknown policy field")
        config["budget"] = dollars(amount(config.get("budget", "1")))
        config.setdefault("mode", "enforce")
        if config["mode"] not in ("observe", "enforce"):
            raise ValueError("Invalid mode")
        for key in allowed - {
            "budget",
            "mode",
            "wrapup_reserve",
            "approved_models",
            "allowed_tools",
            "tool_policy_digests",
        }:
            if key in config and (type(config[key]) is not int or not 0 <= config[key] <= 100000):
                raise ValueError("Policy limits must be nonnegative integers up to 100000")
        if amount(config.get("wrapup_reserve", "0")) > amount(config["budget"]):
            raise ValueError("Wrap-up reserve exceeds budget")
        models = config.get("approved_models", [])
        if not isinstance(models, list) or any(
            not isinstance(m, str) or "/" not in m for m in models
        ):
            raise ValueError("Use provider/model identifiers")
        if "allowed_tools" in config and "tool_policy_digests" not in config:
            if not isinstance(config["allowed_tools"], list):
                raise ValueError("Allowed tools must be a list")
            pins = {}
            with self.lock:
                for name in config["allowed_tools"]:
                    text(name, "tool name")
                    row = self.db.execute(
                        "SELECT digest FROM tools WHERE name=?", (name,)
                    ).fetchone()
                    if not row:
                        raise ValueError("Register the tool before adding it to a policy")
                    pins[name] = row[0]
            config["tool_policy_digests"] = pins
        self.governance_config(config)
        progress = dict(body.get("progress", {}))
        if set(progress) - {"window", "action"}:
            raise ValueError("Unknown progress policy field")
        progress.setdefault("window", 3)
        progress.setdefault("action", "advise")
        if type(progress["window"]) is not int or not 2 <= progress["window"] <= 100:
            raise ValueError("Progress window must be between 2 and 100")
        if progress["action"] not in ("advise", "pause", "wrapup"):
            raise ValueError("Invalid progress action")
        return {"policy_id": wid, "workflow": config, "progress": progress}

    def policy_save(self, body):
        clean = self.policy_validate(body)
        with self.transaction():
            old = self.db.execute(
                "SELECT body FROM pilot_policies WHERE id=?", (clean["policy_id"],)
            ).fetchone()
            if old and old[0] != encoded(clean):
                raise ValueError("Policy versions are immutable; save a new ID")
            self.db.execute(
                "INSERT OR IGNORE INTO pilot_policies VALUES(?,?)",
                (clean["policy_id"], encoded(clean)),
            )
        return clean

    def bind_policy(self, body):
        if not body.get("policy_template"):
            return body
        with self.lock:
            row = self.db.execute(
                "SELECT body FROM pilot_policies WHERE id=?", (body["policy_template"],)
            ).fetchone()
        if not row:
            raise ValueError("Unknown policy template")
        policy = json.loads(row[0])
        if any(k in body and body[k] != policy["workflow"].get(k) for k in POLICY_FIELDS):
            raise ValueError("Cannot override a frozen policy")
        if "progress_policy" in body and body["progress_policy"] != policy["progress"]:
            raise ValueError("Cannot override a frozen progress policy")
        return {
            **body,
            **policy["workflow"],
            "policy_version": policy["policy_id"],
            "progress_policy": policy["progress"],
        }

    def issue(self, workflow, task, kind, evidence):
        # Called inside the same transaction as the event causing the issue.
        old = self.db.execute(
            "SELECT * FROM pilot_issues WHERE task=? AND kind=? AND state='open'", (task, kind)
        ).fetchone()
        now = time.time()
        if old:
            previous = json.loads(old["body"])
            if previous.get("evidence") == evidence:
                return old["id"]
            body = {"count": previous["count"] + 1, "evidence": evidence}
            self.db.execute(
                "UPDATE pilot_issues SET body=?,version=version+1,updated=? WHERE id=?",
                (encoded(body), now, old["id"]),
            )
            return old["id"]
        identity = hashlib.sha256(
            encoded([workflow, task, kind, time.time_ns()]).encode()
        ).hexdigest()
        self.db.execute(
            "INSERT INTO pilot_issues VALUES(?,?,?,?, 'open',1,?,?,?)",
            (identity, workflow, task, kind, encoded({"count": 1, "evidence": evidence}), now, now),
        )
        return identity

    def progress_evidence(self, body):
        clean = dict(body)
        for name in ("event_id", "task_id", "request_id", "stage", "source_version"):
            text(clean.get(name), name)
        required, checks = clean.get("required"), clean.get("checks")
        if not isinstance(required, list) or not required or len(set(required)) != len(required):
            raise ValueError("Unique required result IDs are required")
        for item in required:
            text(item, "required result")
        if not isinstance(checks, list):
            raise ValueError("Checks must be a list")
        seen = set()
        for check in checks:
            if not isinstance(check, dict) or set(check) != {"item", "check", "source", "passed"}:
                raise ValueError("Each check needs item, check, source and passed")
            if check["item"] not in required or check["item"] in seen:
                raise ValueError("Checks must name distinct required results")
            seen.add(check["item"])
            text(check["check"], "check name")
            text(check["source"], "source reference")
            if type(check["passed"]) is not bool:
                raise ValueError("Check outcome must be boolean")
        with self.transaction():
            t, w = self._task(clean["task_id"])
            old = self.db.execute(
                "SELECT body FROM pilot_evidence WHERE id=? OR request=?",
                (clean["event_id"], clean["request_id"]),
            ).fetchone()
            if old:
                if old[0] != encoded(clean):
                    raise ValueError("Conflicting or duplicate evidence")
                return {"status": "recorded", "duplicate": True}
            request = self.db.execute(
                "SELECT * FROM requests WHERE id=? AND task=?", (clean["request_id"], t["id"])
            ).fetchone()
            if not request or request["actual"] is None or w["finished"] is not None:
                raise ValueError("Evidence requires a settled request in an active task")
            self.db.execute(
                "INSERT INTO pilot_evidence VALUES(?,?,?,?,?)",
                (clean["event_id"], t["id"], request["id"], encoded(clean), time.time()),
            )
            self.event(w["id"], t["id"], "progress_evidence", event_id=clean["event_id"])
            decision = self.progress_decision(t, w, json.loads(w["config"]))
        return {"status": "recorded", **decision}

    def progress_signal(self, task, window):
        # A settled call without a validator result is unknown, never evidence of a stall.
        uncovered = self.db.execute(
            "SELECT 1 FROM (SELECT id FROM requests WHERE task=? AND actual IS NOT NULL "
            "ORDER BY settled DESC,rowid DESC LIMIT ?) r LEFT JOIN pilot_evidence e "
            "ON e.request=r.id WHERE e.id IS NULL",
            (task, window),
        ).fetchone()
        if uncovered:
            return None
        rows = self.db.execute(
            "SELECT e.body FROM pilot_evidence e JOIN requests r ON r.id=e.request "
            "WHERE e.task=? AND e.created>coalesce((SELECT max(created) FROM events "
            "WHERE task=? AND kind='pilot_reset'),0) "
            "ORDER BY r.settled DESC,r.rowid DESC LIMIT ?",
            (task, task, window + 1),
        ).fetchall()
        evidence = [json.loads(r[0]) for r in reversed(rows)]
        if len(evidence) < window:
            return None
        recent = evidence[-window:]

        def signature(e):
            return e["stage"], e["source_version"], sorted(e["required"])

        if any(signature(e) != signature(recent[-1]) for e in recent):
            return None

        def checked(e):
            return {c["item"] for c in e["checks"] if c["passed"]}

        # Every failed attempt may have zero useful results; otherwise a baseline is needed.
        previous = (
            checked(evidence[-window - 1])
            if len(evidence) > window and signature(evidence[-window - 1]) == signature(recent[-1])
            else set()
        )
        for e in recent:
            current = checked(e)
            if current - previous or current == set(e["required"]):
                return None
            previous = current
        return {
            "event_ids": [e["event_id"] for e in recent],
            "source_version": recent[-1]["source_version"],
            "checked_results": sorted(previous),
            "missing_results": sorted(set(recent[-1]["required"]) - previous),
            "attempts_without_progress": window,
        }

    def progress_decision(self, t, w, cfg):
        policy = cfg.get("progress_policy")
        if not policy:
            return {"decision": "continue"}
        pending = self.db.execute(
            "SELECT 1 FROM requests r JOIN allocations a ON a.request=r.id "
            "WHERE a.task=? AND r.actual IS NULL",
            (t["id"],),
        ).fetchone()
        if pending:
            return {"decision": "wait", "reason": "unconfirmed_work"}
        signal = self.progress_signal(t["id"], policy["window"])
        if not signal:
            return {"decision": "continue"}
        issue = self.issue(w["id"], t["id"], "no_progress", signal)
        action = policy["action"] if w["mode"] == "enforce" else "advise"
        existing = self.db.execute(
            "SELECT state FROM pilot_controls WHERE task=?", (t["id"],)
        ).fetchone()
        if action != "advise" and not existing:
            self.db.execute("INSERT INTO pilot_controls VALUES(?,?)", (t["id"], action))
            self.event(w["id"], t["id"], "pilot_intervention", action=action, issue_id=issue)
        return {"decision": action, "issue_id": issue, "evidence": signal}

    def pilot_admission(self, t, w, cfg):
        self.progress_decision(t, w, cfg)
        row = self.db.execute(
            "SELECT c.state FROM pilot_controls c JOIN tasks t ON t.id=c.task WHERE c.task=?",
            (t["id"],),
        ).fetchone()
        cursor = t
        while not row and cursor["parent"]:
            cursor = self.db.execute(
                "SELECT * FROM tasks WHERE id=?", (cursor["parent"],)
            ).fetchone()
            row = self.db.execute(
                "SELECT state FROM pilot_controls WHERE task=?", (cursor["id"],)
            ).fetchone()
        return "task_" + row[0] if row else None

    def policy_preview(self, body):
        policy = self.policy_validate(body)
        with self.lock:
            self._task(body["workflow_id"])
            tasks = self.db.execute(
                "SELECT id FROM tasks WHERE workflow=?", (body["workflow_id"],)
            ).fetchall()
            signals = [
                {"task_id": t[0], "evidence": s}
                for t in tasks
                if (s := self.progress_signal(t[0], policy["progress"]["window"]))
            ]
        return {
            "historical_matches": signals,
            "paid_calls": 0,
            "note": "Progress-rule preview only. No counterfactual outcome or savings estimate.",
        }

    def issue_action(self, body):
        for name in ("action_id", "issue_id", "reason"):
            text(body.get(name), name)
        action = body["action"]
        if action not in ("pause", "resume", "wrapup", "resolve"):
            raise ValueError("Invalid issue action")
        with self.transaction():
            old = self.db.execute(
                "SELECT * FROM pilot_actions WHERE id=?", (body["action_id"],)
            ).fetchone()
            if old:
                if old["body"] != encoded(body):
                    raise ValueError("Action ID reused")
                return json.loads(old["result"])
            issue = self.db.execute(
                "SELECT * FROM pilot_issues WHERE id=?", (body["issue_id"],)
            ).fetchone()
            if not issue or issue["version"] != body.get("version") or issue["state"] != "open":
                raise ValueError("Issue changed; refresh before acting")
            t, w = self._task(issue["task"])
            if action != "resolve" and w["finished"] is not None:
                raise ValueError("Completed work cannot be resumed")
            control = self.db.execute(
                "SELECT state FROM pilot_controls WHERE task=?", (t["id"],)
            ).fetchone()
            if action == "resolve" and control and control[0] == "pause":
                raise ValueError("Resume a paused task before closing its issue")
            if action == "resume":
                if control and control[0] == "wrapup":
                    raise ValueError("Wrap-up is terminal; start a new task")
                if self.db.execute(
                    "SELECT 1 FROM requests r JOIN allocations a ON a.request=r.id "
                    "WHERE a.task=? AND r.actual IS NULL",
                    (t["id"],),
                ).fetchone():
                    raise ValueError("Reconcile uncertain work before resuming")
                self.db.execute("DELETE FROM pilot_controls WHERE task=?", (t["id"],))
                # Operator reset starts a new evidence window; history remains intact.
                self.event(w["id"], t["id"], "pilot_reset", reason=body["reason"])
            elif action in ("pause", "wrapup"):
                if control and control[0] == "wrapup" and action == "pause":
                    raise ValueError("Wrap-up is terminal")
                self.db.execute(
                    "INSERT INTO pilot_controls VALUES(?,?) ON CONFLICT(task) "
                    "DO UPDATE SET state=excluded.state",
                    (t["id"], action),
                )
            state = "resolved" if action in ("resolve", "resume") else "open"
            self.db.execute(
                "UPDATE pilot_issues SET state=?,version=version+1,updated=? WHERE id=?",
                (state, time.time(), issue["id"]),
            )
            result = {"status": state, "action": action}
            self.event(w["id"], t["id"], "issue_action", **body)
            self.db.execute(
                "INSERT INTO pilot_actions VALUES(?,?,?)",
                (body["action_id"], encoded(body), encoded(result)),
            )
            return result

    def review_submit(self, body):
        text(body.get("review_id"), "review ID")
        text(body.get("workflow_id"), "workflow ID")
        for name in ("input", "output", "source_version"):
            if name not in body:
                raise ValueError("Review requires input, output and source version")
        text(body["source_version"], "source version")
        if len(encoded(body).encode()) > 131072:
            raise ValueError("Review exceeds 128 KiB")
        with self.transaction():
            self._task(body["workflow_id"])
            old = self.db.execute(
                "SELECT body,state FROM pilot_reviews WHERE id=?", (body["review_id"],)
            ).fetchone()
            if old:
                if json.loads(old[0])["submitted"] != body:
                    raise ValueError("Conflicting review")
            else:
                saved = {"submitted": body, "recorded_case": self.regression_case(body)}
                self.db.execute(
                    "INSERT INTO pilot_reviews VALUES(?,?,?,'pending',1)",
                    (body["review_id"], body["workflow_id"], encoded(saved)),
                )
        return {"status": old["state"] if old else "pending"}

    def review_decide(self, body):
        for name in ("reason", "reviewer", "rubric_version"):
            text(body.get(name), name)
        if body.get("decision") not in ("approve", "reject"):
            raise ValueError("Invalid review decision")
        if body["decision"] == "approve" and (
            not isinstance(body.get("expected"), dict) or not body["expected"]
        ):
            raise ValueError("A human-confirmed expected result is required")
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM pilot_reviews WHERE id=?", (body["review_id"],)
            ).fetchone()
            if not row or row["state"] != "pending" or row["version"] != body.get("version"):
                raise ValueError("Review changed; refresh before deciding")
            saved = json.loads(row["body"])
            saved["review"] = body
            self.db.execute(
                "UPDATE pilot_reviews SET state=?,body=?,version=version+1 WHERE id=?",
                (body["decision"], encoded(saved), row["id"]),
            )
            self.event(
                row["workflow"],
                row["workflow"],
                "human_review",
                review_id=row["id"],
                reviewer=body["reviewer"],
                decision=body["decision"],
            )
        return {"status": body["decision"]}

    def suite_export(self, body):
        text(body.get("suite_id"), "suite ID")
        ids = body.get("review_ids")
        if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
            raise ValueError("Specify distinct reviewed cases")
        with self.transaction():
            cases = []
            for identity in ids:
                row = self.db.execute(
                    "SELECT * FROM pilot_reviews WHERE id=? AND state='approve'", (identity,)
                ).fetchone()
                if not row:
                    raise ValueError("Only human-approved cases may enter a suite")
                data = json.loads(row["body"])
                cases.append(
                    {
                        "id": identity,
                        "input": data["submitted"]["input"],
                        "expected": data["review"]["expected"],
                        "provenance": data,
                    }
                )
            result = {"suite_id": body["suite_id"], "cases": cases}
            old = self.db.execute(
                "SELECT body FROM pilot_suites WHERE id=?", (body["suite_id"],)
            ).fetchone()
            if old and old[0] != encoded(result):
                raise ValueError("Frozen suite cannot change; use a new ID")
            self.db.execute(
                "INSERT OR IGNORE INTO pilot_suites VALUES(?,?)",
                (body["suite_id"], encoded(result)),
            )
            return result

    def cost_provenance(self, body):
        text(body.get("request_id"), "request ID")
        text(body.get("price_version"), "price version")
        if body.get("source") not in ("usage_priced", "provider_reported"):
            raise ValueError("Invalid cost source")
        text(body.get("reference"), "cost reference")
        with self.transaction():
            if not self.db.execute(
                "SELECT 1 FROM requests WHERE id=? AND actual IS NOT NULL", (body["request_id"],)
            ).fetchone():
                raise ValueError("Cost provenance requires a settled request")
            old = self.db.execute(
                "SELECT body FROM pilot_prices WHERE request=?", (body["request_id"],)
            ).fetchone()
            if old and old[0] != encoded(body):
                raise ValueError(
                    "Cost provenance is immutable; use invoice revisions for adjustments"
                )
            self.db.execute(
                "INSERT OR IGNORE INTO pilot_prices VALUES(?,?)",
                (body["request_id"], encoded(body)),
            )
        return {"status": "recorded"}

    def pilot_status(self, body):
        from .ledger import dollars

        with self.lock:
            result = {}
            for key, table in (
                ("policies", "pilot_policies"),
                ("issues", "pilot_issues"),
                ("reviews", "pilot_reviews"),
            ):
                result[key] = [
                    {**dict(row), "body": json.loads(row["body"])}
                    for row in self.db.execute(f"SELECT * FROM {table}")
                ]
            result["controls"] = [dict(r) for r in self.db.execute("SELECT * FROM pilot_controls")]
            result["evidence"] = [
                json.loads(r[0])
                for r in self.db.execute(
                    "SELECT body FROM pilot_evidence ORDER BY created DESC LIMIT 100"
                )
            ]
            result["costs"] = []
            for r in self.db.execute(
                "SELECT r.*,p.body FROM requests r LEFT JOIN pilot_prices p ON p.request=r.id "
                "ORDER BY r.created DESC LIMIT 1000"
            ):
                provenance = json.loads(r["body"]) if r["body"] else {}
                result["costs"].append(
                    {
                        "request_id": r["id"],
                        "workflow_id": r["workflow"],
                        "reserved": dollars(r["reserved"]),
                        "actual": dollars(r["actual"]) if r["actual"] is not None else None,
                        "source": "invoice_reconciled"
                        if r["state"] == "reconciled"
                        else provenance.get(
                            "source",
                            "unconfirmed" if r["actual"] is None else "source_not_recorded",
                        ),
                        "price_version": provenance.get("price_version"),
                        "reference": provenance.get("reference"),
                    }
                )
            result["bills"] = [
                dict(r)
                for r in self.db.execute("SELECT * FROM bills ORDER BY revision DESC LIMIT 1000")
            ]
            for bill in result["bills"]:
                bill["amount"] = dollars(bill["amount"])
            return result

    def invoice_preview(self, body):
        """Validate all rows on an isolated SQLite snapshot; never mutate the live ledger."""
        import tempfile

        from .ledger import Ledger, dollars

        rows = body.get("rows")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
            raise ValueError("Supply 1 to 1000 invoice rows")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "preview.sqlite"
            with self.lock, sqlite3.connect(path) as target:
                self.db.backup(target)
            preview = Ledger(path)
            results = []
            try:
                for index, row in enumerate(rows):
                    try:
                        old = preview.db.execute(
                            "SELECT actual FROM requests WHERE id=?", (row.get("request_id"),)
                        ).fetchone()
                        result = preview.reconcile(row)
                        value = preview.db.execute(
                            "SELECT actual FROM requests WHERE id=?", (row.get("request_id"),)
                        ).fetchone()
                        delta = (
                            dollars(value[0] - old[0])
                            if old and old[0] is not None and result["matched"]
                            else None
                        )
                        results.append({"row": index + 1, **result, "difference": delta})
                    except (ValueError, KeyError, TypeError, AttributeError, sqlite3.Error) as exc:
                        results.append({"row": index + 1, "error": str(exc)})
            finally:
                preview.close()
        return {"rows": results, "valid": all("error" not in r for r in results), "paid_calls": 0}
