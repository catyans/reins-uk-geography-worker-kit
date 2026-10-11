"""Operator views and durable acknowledgements; no background paid execution."""

import json
import os
import sqlite3
import time
from pathlib import Path

from .governance import encoded


class Operations:
    def migrate_operations(self, path):
        if self.db.execute("SELECT max(version) FROM schema_version").fetchone()[0] >= 4:
            return
        backup = Path(str(path) + ".pre-v4.sqlite")
        temporary = Path(str(backup) + ".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with sqlite3.connect(temporary) as target:
            self.db.backup(target)
        os.replace(temporary, backup)
        with self.transaction():
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS alert_receipts("
                "issue TEXT PRIMARY KEY, version INTEGER, reason TEXT, created REAL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS handoff_receipts("
                "id TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS tool_history("
                "name TEXT, digest TEXT, policy TEXT, created REAL, "
                "PRIMARY KEY(name,digest))"
            )
            self.db.execute(
                "INSERT OR IGNORE INTO tool_history SELECT name,digest,policy,? FROM tools",
                (time.time(),),
            )
            self.db.execute("UPDATE schema_version SET version=4")

    def acknowledge_alert(self, body):
        from .pilot import text

        text(body.get("issue_id"), "issue ID")
        text(body.get("reason"), "acknowledgement reason")
        if type(body.get("version")) is not int or body["version"] < 1:
            raise ValueError("Version must be a positive integer")
        with self.transaction():
            issue = self.db.execute(
                "SELECT * FROM pilot_issues WHERE id=?", (body["issue_id"],)
            ).fetchone()
            if not issue or issue["state"] != "open" or issue["version"] != body.get("version"):
                raise ValueError("Alert changed; refresh before acknowledging")
            old = self.db.execute(
                "SELECT version FROM alert_receipts WHERE issue=?", (issue["id"],)
            ).fetchone()
            if old and old[0] == issue["version"]:
                return {"status": "acknowledged", "duplicate": True}
            self.db.execute(
                "INSERT INTO alert_receipts VALUES(?,?,?,?) ON CONFLICT(issue) "
                "DO UPDATE SET version=excluded.version,reason=excluded.reason,"
                "created=excluded.created",
                (issue["id"], issue["version"], body["reason"], time.time()),
            )
            self.event(
                issue["workflow"],
                issue["task"],
                "alert_acknowledged",
                issue_id=issue["id"],
                version=issue["version"],
                reason=body["reason"],
            )
        return {"status": "acknowledged"}

    def receive_handoff(self, body):
        """Receiver confirms a still-current state version; replay never executes tools."""
        from .pilot import text

        text(body.get("handoff_id"), "handoff ID")
        text(body.get("task_id"), "receiver task ID")
        if type(body.get("version")) is not int or body["version"] < 1:
            raise ValueError("Version must be a positive integer")
        with self.transaction():
            old = self.db.execute(
                "SELECT body FROM handoff_receipts WHERE id=?", (body["handoff_id"],)
            ).fetchone()
            if old:
                if old[0] != encoded(body):
                    raise ValueError("Receipt identity cannot change")
                return {"status": "received", "duplicate": True}
            row = self.db.execute(
                "SELECT * FROM handoffs WHERE id=?", (body["handoff_id"],)
            ).fetchone()
            if not row or row["receiver"] != body["task_id"]:
                raise ValueError("Receipt must name the registered receiver")
            _, workflow = self._task(row["receiver"])
            if workflow["finished"] is not None:
                raise ValueError("Completed workflow cannot receive new handoffs")
            state = self.state_read({"task_id": row["receiver"], "name": row["state_name"]})
            if row["version"] != body.get("version") or state["version"] != row["version"]:
                raise ValueError("Handoff state changed; create a new handoff")
            self.db.execute(
                "INSERT INTO handoff_receipts VALUES(?,?,?)",
                (row["id"], encoded(body), time.time()),
            )
            self.event(
                workflow["id"],
                row["receiver"],
                "handoff_received",
                handoff_id=row["id"],
                version=row["version"],
            )
        return {"status": "received"}

    def save_tool_policy(self, body):
        if "expected_digest" not in body:
            raise ValueError("Provide expected_digest; use null for a new tool")
        return self.register_tool(body)

    def capture_failure(self, body):
        """Capture an operator-supplied repro with the observed incident attached.

        Expected answers are intentionally absent. Existing human review is required
        before this example can enter a frozen suite.
        """
        from .pilot import text

        text(body.get("issue_id"), "issue ID")
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM pilot_issues WHERE id=?", (body["issue_id"],)
            ).fetchone()
            if not row:
                raise ValueError("Unknown issue")
            if not isinstance(body.get("input"), dict) or not body["input"]:
                raise ValueError(
                    "Provide original task inputs; they cannot be inferred from a trace"
                )
            clean = {
                "review_id": body["review_id"],
                "workflow_id": row["workflow"],
                "input": body["input"],
                "output": body["output"],
                "source_version": body["source_version"],
                "failure_context": {
                    "issue_id": row["id"],
                    "task_id": row["task"],
                    "kind": row["kind"],
                    "version": row["version"],
                    "evidence": json.loads(row["body"])["evidence"],
                },
            }
            return self.review_submit(clean)

    def operations_status(self, body):
        from .ledger import dollars
        from .pilot import text

        with self.lock:
            wid = body.get("workflow_id")
            if wid is not None:
                text(wid, "workflow ID")
            if wid:
                self._task(wid)
            where, params = (" WHERE workflow=?", (wid,)) if wid else ("", ())
            tasks = self.db.execute(
                "SELECT * FROM tasks" + where + " ORDER BY created,id", params
            ).fetchall()
            controls = {
                r["task"]: r["state"] for r in self.db.execute("SELECT * FROM pilot_controls")
            }
            by_id = {t["id"]: t for t in tasks}
            nodes = []
            for task in tasks:
                workflow = self.db.execute(
                    "SELECT * FROM workflows WHERE id=?", (task["workflow"],)
                ).fetchone()
                calls = self.db.execute(
                    "SELECT * FROM requests WHERE task=?", (task["id"],)
                ).fetchall()
                cursor, control, owner = task, None, None
                while cursor:
                    if cursor["id"] in controls:
                        control, owner = controls[cursor["id"]], cursor["id"]
                        break
                    cursor = by_id.get(cursor["parent"])
                pending = sum(r["actual"] is None for r in calls)
                uncertain = any(r["state"] == "uncertain" for r in calls)
                state = (
                    "completed"
                    if workflow["finished"] is not None
                    else "paused"
                    if workflow["state"] == "paused" or control == "pause"
                    else "wrapping"
                    if workflow["state"] == "wrapping" or control == "wrapup"
                    else "uncertain_charge"
                    if uncertain
                    else "awaiting_settlement"
                    if pending
                    else "ready"
                )
                latest = self.db.execute(
                    "SELECT kind,created FROM events WHERE task=? ORDER BY id DESC LIMIT 1",
                    (task["id"],),
                ).fetchone()
                progress = self.db.execute(
                    "SELECT body FROM pilot_evidence WHERE task=? ORDER BY created DESC LIMIT 1",
                    (task["id"],),
                ).fetchone()
                evidence = json.loads(progress[0]) if progress else None
                nodes.append(
                    {
                        "task_id": task["id"],
                        "parent_task_id": task["parent"],
                        "workflow_id": task["workflow"],
                        "state": state,
                        "control_owner": owner,
                        "pending_calls": pending,
                        "unconfirmed_charge": uncertain,
                        "known_cost": dollars(
                            sum(r["actual"] for r in calls if r["actual"] is not None)
                        ),
                        "reserved_cost": dollars(
                            sum(r["reserved"] for r in calls if r["actual"] is None)
                        ),
                        "last_event": latest["kind"] if latest else None,
                        "last_activity": latest["created"] if latest else task["created"],
                        "stage": evidence["stage"] if evidence else None,
                        "checked": sum(c["passed"] for c in evidence["checks"])
                        if evidence
                        else None,
                        "required": len(evidence["required"]) if evidence else None,
                    }
                )
            handoffs = []
            for row in self.db.execute(
                "SELECT h.*,r.created received FROM handoffs h "
                "LEFT JOIN handoff_receipts r ON r.id=h.id"
            ):
                if row["sender"] not in by_id:
                    continue
                task = by_id[row["sender"]]
                latest = self.db.execute(
                    "SELECT version,expires FROM shared_state WHERE workflow=? AND name=? "
                    "ORDER BY version DESC LIMIT 1",
                    (task["workflow"], row["state_name"]),
                ).fetchone()
                status = (
                    "expired"
                    if not latest or latest["expires"] <= time.time()
                    else "superseded"
                    if latest["version"] != row["version"]
                    else "current"
                )
                payload = json.loads(row["payload"])
                handoffs.append(
                    {
                        "id": row["id"],
                        "sender": row["sender"],
                        "receiver": row["receiver"],
                        "state_name": row["state_name"],
                        "version": row["version"],
                        "state_status": status,
                        "received_at": row["received"],
                        "goal": payload["goal"],
                        "completed_count": len(payload["completed"]),
                    }
                )
            alerts = []
            for row in self.db.execute(
                "SELECT i.*,a.version seen FROM pilot_issues i LEFT JOIN "
                "alert_receipts a ON a.issue=i.id WHERE i.state='open' ORDER BY i.updated DESC"
            ):
                if wid and row["workflow"] != wid:
                    continue
                alerts.append(
                    {
                        "issue_id": row["id"],
                        "workflow_id": row["workflow"],
                        "task_id": row["task"],
                        "kind": row["kind"],
                        "version": row["version"],
                        "acknowledged": row["seen"] == row["version"],
                        "updated": row["updated"],
                        "severity": "critical"
                        if row["kind"] in ("uncertain", "bound_exceeded")
                        else "warning",
                    }
                )
            tools = [
                {"name": r["name"], "digest": r["digest"], "policy": json.loads(r["policy"])}
                for r in self.db.execute("SELECT * FROM tools ORDER BY name")
            ]
            history = [
                {
                    "name": r["name"],
                    "digest": r["digest"],
                    "created": r["created"],
                    "policy": json.loads(r["policy"]),
                }
                for r in self.db.execute(
                    "SELECT * FROM tool_history ORDER BY created DESC LIMIT 200"
                )
            ]
            return {
                "tasks": nodes,
                "handoffs": handoffs,
                "alerts": alerts,
                "tools": tools,
                "tool_history": history,
            }
