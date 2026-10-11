"""Private console queries, period budgets and durable exact-call approval requests."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .advancement import Advancement
from .company import CompanyTasks
from .experiments import Experiments
from .governance import fingerprint, validate_fields
from .runtime import Runtime
from .workloads import Workloads


def ident(value, label="ID"):
    from .ledger import identity

    return identity(value, label)


def money(value):
    from .ledger import dollars

    return dollars(value)


def nano(value):
    from .ledger import amount

    return amount(value)


def stamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError("Time requires an explicit timezone")
        return value.timestamp()
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid timestamp")
    return value


def encoded(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def version(value):
    if type(value) is not int or value < 1:
        raise ValueError("Positive integer version required")
    return value


class Console(Workloads, CompanyTasks, Advancement, Experiments, Runtime):
    def migrate_console(self, path):
        if self.db.execute("SELECT max(version) FROM schema_version").fetchone()[0] >= 5:
            return
        backup = Path(str(path) + ".pre-v5.sqlite")
        temporary = Path(str(backup) + ".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_RDWR, 0o600)
        os.close(fd)
        with sqlite3.connect(temporary) as target:
            self.db.backup(target)
        os.replace(temporary, backup)
        tables = [
            "CREATE TABLE IF NOT EXISTS console_budgets(id TEXT PRIMARY KEY,body TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS console_alerts(id TEXT PRIMARY KEY,body TEXT NOT NULL,"
            "version INTEGER NOT NULL,ack INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS console_approvals(id TEXT PRIMARY KEY,body TEXT NOT NULL,"
            "state TEXT NOT NULL,version INTEGER NOT NULL,created REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS console_audit(id INTEGER PRIMARY KEY,kind TEXT,"
            "subject TEXT,body TEXT,created REAL)",
            "CREATE TABLE IF NOT EXISTS console_adapters(id TEXT PRIMARY KEY,body TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS console_experiments(id TEXT PRIMARY KEY,body TEXT NOT NULL,"
            "state TEXT NOT NULL,created REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS console_jobs(id TEXT PRIMARY KEY,experiment TEXT NOT NULL,"
            "case_id TEXT NOT NULL,arm TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL,"
            "owner TEXT,claimed REAL,result TEXT)",
            "CREATE TABLE IF NOT EXISTS console_deployments(id TEXT PRIMARY KEY,body TEXT NOT NULL,"
            "version INTEGER NOT NULL)",
            "CREATE INDEX IF NOT EXISTS requests_created_idx ON requests(created)",
            "CREATE INDEX IF NOT EXISTS events_workflow_idx ON events(workflow,id)",
            "CREATE INDEX IF NOT EXISTS requests_workflow_idx ON requests(workflow)",
        ]
        with self.transaction():
            for statement in tables:
                self.db.execute(statement)
            self.db.execute("UPDATE schema_version SET version=5")

    def console_audit(self, kind, subject, body):
        self.db.execute(
            "INSERT INTO console_audit(kind,subject,body,created) VALUES(?,?,?,?)",
            (kind, subject, encoded(body), time.time()),
        )

    def console_pools(self, body):
        return self.governance_status({"workflows": []})

    def console_query(self, body):
        """A task-start cohort: lifetime charges of those tasks, including failed attempts."""
        now = time.time()
        end, start = stamp(body.get("end", now + 1)), stamp(body.get("start", now - 30 * 86400))
        if start >= end:
            raise ValueError("Start must precede end")
        page, size = body.get("page", 1), body.get("page_size", 25)
        if type(page) is not int or page < 1 or type(size) is not int or not 1 <= size <= 100:
            raise ValueError("Invalid page")
        dimension = body.get("group_by", "customer")
        if dimension not in ("customer", "team", "task_type", "task", "model", "tool"):
            raise ValueError("Unknown grouping")
        filters = {}
        for key in ("customer", "team", "task_type", "state", "search"):
            if body.get(key):
                filters[key] = ident(body[key], key)
        with self.lock:
            all_ws = [
                dict(r)
                for r in self.db.execute(
                    "SELECT w.*,m.team,m.pool FROM workflows w LEFT JOIN memberships m ON "
                    "m.workflow=w.id"
                )
            ]
            controls = {
                r["task"]: r["state"] for r in self.db.execute("SELECT * FROM pilot_controls")
            }
            for w in all_ws:
                w["workflow_state"] = w["state"]
                if w["finished"] is None and controls.get(w["id"]) in ("pause", "wrapup"):
                    w["state"] = "paused" if controls[w["id"]] == "pause" else "wrapping"

            def select(w, lo, hi):
                return lo <= w["created"] < hi and all(
                    (v.lower() in (w["id"] + " " + w["task_type"]).lower())
                    if k == "search"
                    else w.get(k) == v
                    for k, v in filters.items()
                )

            selected = {w["id"]: w for w in all_ws if select(w, start, end)}
            previous = {w["id"]: w for w in all_ws if select(w, start - (end - start), start)}
            calls = [
                dict(r)
                for r in self.db.execute(
                    "SELECT r.id,r.workflow,r.task,r.model,r.category,r.actual,r.reserved,r.state,"
                    "r.created,r.settled,m.tool,m.success FROM requests r "
                    "LEFT JOIN operation_meta m ON m.request=r.id"
                )
            ]
            by_workflow = defaultdict(list)
            for r in calls:
                by_workflow[r["workflow"]].append(r)
            entries = defaultdict(list)
            for r in self.db.execute("SELECT * FROM economics"):
                entries[r["workflow"]].append(dict(r))

            def total(wid):
                return sum(r["actual"] or 0 for r in by_workflow[wid]) + sum(
                    e["amount"]
                    for e in entries[wid]
                    if e["kind"] in ("human_review", "infrastructure")
                )

            workflows = []
            buckets, groups = {}, {}
            categories = defaultdict(int)
            for wid, w in selected.items():
                rs = by_workflow[wid]
                cost = total(wid)
                reserved = sum(r["reserved"] for r in rs if r["actual"] is None)
                pending = sum(r["actual"] is None for r in rs)
                revenue_entries = [e for e in entries[wid] if e["kind"] == "revenue"]
                complete = not pending and any(
                    e["kind"] == "coverage_complete" for e in entries[wid]
                )
                revenue = sum(e["amount"] for e in revenue_entries)
                row = dict(
                    id=wid,
                    customer=w["customer"],
                    team=w["team"],
                    task_type=w["task_type"],
                    state=w["state"],
                    workflow_state=w["workflow_state"],
                    mode=w["mode"],
                    created=w["created"],
                    finished=w["finished"],
                    cost=money(cost),
                    admission_cost=money(sum(r["actual"] or 0 for r in rs)),
                    reserved=money(reserved),
                    budget=money(w["budget"]),
                    accepted=None if w["accepted"] is None else bool(w["accepted"]),
                    duration=(w["finished"] - w["created"]) if w["finished"] else None,
                    calls=len(rs),
                    failed_calls=sum(r["success"] == 0 for r in rs),
                    pending=pending,
                    revenue=money(revenue) if revenue_entries else None,
                    margin=money(revenue - cost) if complete and revenue_entries else None,
                    coverage_complete=complete,
                    policy=w["policy"],
                    pool=w["pool"],
                )
                workflows.append(row)
                day = datetime.fromtimestamp(w["created"], timezone.utc).strftime("%Y-%m-%d")
                bucket = buckets.setdefault(day, {"day": day, "cost": 0, "tasks": 0})
                bucket["cost"] += cost
                bucket["tasks"] += 1
                for r in rs:
                    categories[r["category"]] += r["actual"] or 0
                for e in entries[wid]:
                    if e["kind"] in ("human_review", "infrastructure"):
                        categories[e["kind"]] += e["amount"]
                if dimension not in ("model", "tool"):
                    key = wid if dimension == "task" else w.get(dimension) or "Unattributed"
                    g = groups.setdefault(
                        key,
                        {
                            "name": key,
                            "cost": 0,
                            "tasks": 0,
                            "accepted": 0,
                            "evaluated": 0,
                            "pending": 0,
                            "revenue": 0,
                            "complete": True,
                            "has_revenue": True,
                        },
                    )
                    g["cost"] += cost
                    g["tasks"] += 1
                    g["accepted"] += w["accepted"] == 1
                    g["evaluated"] += w["accepted"] is not None
                    g["pending"] += pending
                    g["revenue"] += revenue
                    g["complete"] &= complete
                    g["has_revenue"] &= bool(revenue_entries)
                else:
                    for r in rs:
                        key = (r["model"] if dimension == "model" else r["tool"]) or "Unattributed"
                        g = groups.setdefault(
                            key, {"name": key, "cost": 0, "ids": set(), "pending": 0}
                        )
                        g["cost"] += r["actual"] or 0
                        g["ids"].add(wid)
                        g["pending"] += r["actual"] is None
                    extras = cost - sum(r["actual"] or 0 for r in rs)
                    if extras:
                        g = groups.setdefault(
                            "Unallocated business costs",
                            {
                                "name": "Unallocated business costs",
                                "cost": 0,
                                "ids": set(),
                                "pending": 0,
                            },
                        )
                        g["cost"] += extras
                        g["ids"].add(wid)
            total_cost = sum(total(w) for w in selected)
            for g in groups.values():
                g["tasks"] = g.get("tasks", len(g.get("ids", [])))
                g.pop("ids", None)
                g["share"] = g["cost"] / total_cost if total_cost else 0
                g["cost_per_accepted"] = (
                    money(Decimal(g["cost"]) / g["accepted"])
                    if g.get("accepted") and not g["pending"]
                    else None
                )
                g["margin"] = (
                    money(g["revenue"] - g["cost"])
                    if g.get("complete") and g.get("has_revenue")
                    else None
                )
                g["revenue"] = money(g["revenue"]) if g.get("has_revenue") else None
                g["cost"] = money(g["cost"])
            accepted = sum(w["accepted"] == 1 for w in selected.values())
            evaluated = sum(w["accepted"] is not None for w in selected.values())
            pending = sum(w["pending"] for w in workflows)
            prev_cost = sum(total(w) for w in previous)
            # Exact two-factor decomposition; new cohorts have no historical unit cost.
            previous_avg = Decimal(prev_cost) / len(previous) if previous else None
            volume = previous_avg * (len(selected) - len(previous)) if previous else None
            delta = total_cost - prev_cost
            key = body.get("sort", "cost")
            if key not in ("cost", "created", "duration"):
                raise ValueError("Invalid sort")
            workflows.sort(key=lambda w: Decimal(w[key] or 0), reverse=True)
            result = {
                "basis": "Tasks started in this period; all recorded costs of those tasks",
                "start": start,
                "end": end,
                "total": len(workflows),
                "page": page,
                "workflows": workflows[(page - 1) * size : page * size],
                "summary": {
                    "cost": money(total_cost),
                    "reserved": money(sum(nano(w["reserved"]) for w in workflows)),
                    "accepted": accepted,
                    "evaluated": evaluated,
                    "pending": pending,
                    "cost_per_accepted": money(Decimal(total_cost) / accepted)
                    if accepted and not pending
                    else None,
                    "acceptance_rate": accepted / evaluated if evaluated else None,
                },
                "trend": [
                    {**b, "cost": money(b["cost"])}
                    for b in sorted(buckets.values(), key=lambda b: b["day"])
                ],
                "distribution": [
                    {"id": w["id"], "cost": w["cost"]}
                    for w in sorted(workflows, key=lambda w: Decimal(w["cost"]), reverse=True)[:20]
                ],
                "groups": sorted(groups.values(), key=lambda g: Decimal(g["cost"]), reverse=True),
                "composition": [{"name": k, "cost": money(v)} for k, v in categories.items()],
                "change": {
                    "previous_cost": money(prev_cost),
                    "delta": money(delta),
                    "volume": money(volume) if volume is not None else None,
                    "unit_cost": money(Decimal(delta) - volume) if volume is not None else None,
                },
                "options": {
                    k: sorted({w.get(k) or "Unattributed" for w in all_ws})
                    for k in ("customer", "team", "task_type", "state")
                },
            }
            return result

    def console_detail(self, body):
        wid = ident(body.get("workflow_id"))
        with self.lock:
            self._task(wid)
            data = self.status(wid)["workflows"][0]
            data["requests"] = []
            for r in self.db.execute(
                "SELECT r.*,m.tool,m.success FROM requests r LEFT JOIN operation_meta m "
                "ON m.request=r.id WHERE r.workflow=? ORDER BY r.created,r.rowid",
                (wid,),
            ):
                data["requests"].append(
                    {
                        "request_id": r["id"],
                        "task_id": r["task"],
                        "model": r["model"],
                        "tool": r["tool"],
                        "category": r["category"],
                        "state": r["state"],
                        "created": r["created"],
                        "settled": r["settled"],
                        "cost": money(r["actual"]) if r["actual"] is not None else None,
                        "reserved": money(r["reserved"]),
                        "usage": json.loads(r["usage"] or "{}"),
                        "success": r["success"],
                        "output": json.loads(r["result"]) if r["result"] else None,
                    }
                )
            data["events"] = [
                dict(r) | {"payload": json.loads(r["payload"])}
                for r in self.db.execute(
                    "SELECT * FROM events WHERE workflow=? ORDER BY id", (wid,)
                )
            ]
            data["activity"] = self.operations_status({"workflow_id": wid})
            data["period_budgets"] = [
                b for b in self.console_budgets({})["budgets"] if self._scope_matches(b, wid, None)
            ]
            data["reviews"] = [
                dict(r) | {"body": json.loads(r["body"])}
                for r in self.db.execute("SELECT * FROM pilot_reviews WHERE workflow=?", (wid,))
            ]
            data["runtime_assignment"] = self.runtime_assignment(wid)
            return data

    def _scope_matches(self, b, wid, model):
        w = self.db.execute(
            "SELECT w.*,m.team FROM workflows w LEFT JOIN memberships m ON "
            "m.workflow=w.id WHERE w.id=?",
            (wid,),
        ).fetchone()
        if not w:
            return False
        scope = b["scope"]
        return (
            scope == "workspace"
            or (scope == "model" and model == b["value"])
            or (scope in ("customer", "team", "task_type") and w[scope] == b["value"])
        )

    def console_budget_save(self, body):
        bid = ident(body.get("id"))
        scope = body.get("scope", "workspace")
        if scope not in ("workspace", "customer", "team", "task_type", "model"):
            raise ValueError("Invalid budget scope")
        thresholds = body.get("thresholds", [80, 95, 100])
        if (
            not isinstance(thresholds, list)
            or not thresholds
            or len(thresholds) > 10
            or any(type(n) is not int or not 1 <= n <= 100 for n in thresholds)
        ):
            raise ValueError("Use 1 to 10 integer thresholds from 1 to 100")
        b = dict(
            id=bid,
            scope=scope,
            value=ident(body.get("value")) if scope != "workspace" else "*",
            start=stamp(body["start"]),
            end=stamp(body["end"]),
            amount=money(nano(body["amount"])),
            thresholds=sorted(set(thresholds)),
        )
        if b["start"] >= b["end"] or nano(b["amount"]) == 0:
            raise ValueError("Use a positive budget and valid period")
        with self.transaction():
            old = self.db.execute("SELECT body FROM console_budgets WHERE id=?", (bid,)).fetchone()
            if old and old[0] != encoded(b):
                raise ValueError("Budget periods are immutable; use a new ID")
            self.db.execute("INSERT OR IGNORE INTO console_budgets VALUES(?,?)", (bid, encoded(b)))
        return b

    def _budget_usage(self, b):
        where, params = "r.created>=? AND r.created<?", [b["start"], b["end"]]
        field = {
            "customer": "w.customer",
            "team": "m.team",
            "task_type": "w.task_type",
            "model": "r.model",
        }.get(b["scope"])
        if field:
            where += " AND " + field + "=?"
            params.append(b["value"])
        return self.db.execute(
            "SELECT r.actual,r.reserved,r.created FROM requests r JOIN workflows w "
            "ON w.id=r.workflow LEFT JOIN memberships m ON m.workflow=w.id WHERE " + where,
            params,
        ).fetchall()

    def console_budgets(self, body):
        now = time.time()
        with self.lock:
            result = []
            first = self.db.execute("SELECT min(created) FROM workflows").fetchone()[0]
            midnight = math.floor(now / 86400) * 86400
            for row in self.db.execute("SELECT body FROM console_budgets"):
                b = json.loads(row[0])
                rs = self._budget_usage(b)
                spent = sum(r["actual"] or 0 for r in rs)
                reserved = sum(r["reserved"] for r in rs if r["actual"] is None)
                prediction = None
                if (
                    first is not None
                    and first <= midnight - 7 * 86400
                    and b["start"] <= midnight - 7 * 86400
                    and b["start"] <= now < b["end"]
                ):
                    recent = sum(
                        r["actual"] or 0
                        for r in rs
                        if midnight - 7 * 86400 <= r["created"] < midnight
                    )
                    prediction = spent + Decimal(recent) / 7 * Decimal(
                        str((b["end"] - now) / 86400)
                    )
                result.append(
                    {
                        **b,
                        "spent": money(spent),
                        "reserved": money(reserved),
                        "forecast": money(prediction) if prediction is not None else None,
                        "active": b["start"] <= now < b["end"],
                    }
                )
            return {"budgets": result}

    def console_admission(self, body, workflow, reserve):
        now = time.time()
        for row in self.db.execute("SELECT body FROM console_budgets"):
            b = json.loads(row[0])
            if b["start"] <= now < b["end"] and self._scope_matches(b, workflow, body["model"]):
                used = sum(
                    r["actual"] if r["actual"] is not None else r["reserved"]
                    for r in self._budget_usage(b)
                )
                if used + reserve > nano(b["amount"]):
                    return "period_budget:" + b["id"]
        approval = body.get("approval_id")
        if approval:
            row = self.db.execute(
                "SELECT body FROM console_approvals WHERE id=?", (approval,)
            ).fetchone()
            if row and reserve > nano(json.loads(row[0])["max_cost"]):
                return "approval_cost_limit"
        return None

    def console_tick(self):
        """Called by the service, independent of browser activity. Never dispatches work."""
        budgets = self.console_budgets({})["budgets"]
        with self.transaction():
            for b in budgets:
                if not b["active"]:
                    continue
                committed = nano(b["spent"]) + nano(b["reserved"])
                hit = [n for n in b["thresholds"] if committed * 100 >= nano(b["amount"]) * n]
                level = max(hit) if hit else 0
                forecast = b["forecast"] is not None and Decimal(b["forecast"]) > Decimal(
                    b["amount"]
                )
                aid = "budget:" + b["id"]
                old = self.db.execute("SELECT * FROM console_alerts WHERE id=?", (aid,)).fetchone()
                if not level and not forecast:
                    if old and old["state"] == "open":
                        self.db.execute(
                            "UPDATE console_alerts SET state='resolved' WHERE id=?", (aid,)
                        )
                    continue
                signature = [level, forecast]
                payload = dict(
                    budget_id=b["id"],
                    title=b["id"]
                    + " · "
                    + (str(level) + "% threshold" if level else "forecast over budget"),
                    level=level,
                    forecast_over=forecast,
                    spent=b["spent"],
                    reserved=b["reserved"],
                    amount=b["amount"],
                    updated=time.time(),
                    signature=signature,
                )
                changed = (
                    not old
                    or json.loads(old["body"])["signature"] != signature
                    or old["state"] != "open"
                )
                v = old["version"] + int(changed) if old else 1
                self.db.execute(
                    "INSERT INTO console_alerts(id,body,version,state) VALUES(?,?,?,'open') "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "body=excluded.body,version=excluded.version,state='open'",
                    (aid, encoded(payload), v),
                )
                if changed:
                    self.console_audit("budget_alert", aid, payload)
            # An interrupted experiment is never automatically dispatched again.
            stale = self.db.execute(
                "SELECT DISTINCT experiment FROM console_jobs WHERE state IN "
                "('running','provisioning') AND claimed<?",
                (time.time() - 300,),
            ).fetchall()
            for row in stale:
                self.db.execute(
                    "UPDATE console_experiments SET state='needs_inspection' WHERE id=? AND "
                    "state='running'",
                    (row[0],),
                )

    def console_alerts(self, body):
        with self.lock:
            return {
                "alerts": [
                    dict(r) | {"body": json.loads(r["body"])}
                    for r in self.db.execute("SELECT * FROM console_alerts ORDER BY rowid DESC")
                ],
                "audit": [
                    dict(r) | {"body": json.loads(r["body"])}
                    for r in self.db.execute(
                        "SELECT * FROM console_audit ORDER BY id DESC LIMIT 200"
                    )
                ],
            }

    def console_ack(self, body):
        aid = ident(body.get("id"))
        v = version(body.get("version"))
        reason = ident(body.get("reason"), "reason")
        with self.transaction():
            row = self.db.execute("SELECT * FROM console_alerts WHERE id=?", (aid,)).fetchone()
            if not row or row["version"] != v:
                raise ValueError("Alert changed; refresh")
            if row["ack"] != v:
                self.db.execute("UPDATE console_alerts SET ack=? WHERE id=?", (v, aid))
                self.console_audit("alert_acknowledged", aid, {"reason": reason, "version": v})
        return {"status": "acknowledged"}

    def console_approval_request(self, body):
        aid = ident(body.get("id"))
        task = ident(body.get("task_id"))
        tool = ident(body.get("tool_name"))
        with self.transaction():
            _, w = self._task(task)
            cfg = json.loads(w["config"])
            row = self.db.execute("SELECT * FROM tools WHERE name=?", (tool,)).fetchone()
            if not row or tool not in cfg.get("allowed_tools", []):
                raise ValueError("Tool not allowed")
            if cfg.get("tool_policy_digests", {}).get(tool, row["digest"]) != row["digest"]:
                raise ValueError("Workflow tool permission has changed")
            policy = json.loads(row["policy"])
            args = body.get("arguments", {})
            validate_fields(args, policy["fields"])
            for field, values in policy["allowed_values"].items():
                if args[field] not in values:
                    raise ValueError("Tool argument outside allowed scope")
            saved = dict(
                id=aid,
                task_id=task,
                workflow_id=w["id"],
                tool_name=tool,
                arguments=args,
                policy=row["digest"],
                max_cost=money(nano(body["max_cost"])),
                purpose=ident(body.get("purpose"), "purpose"),
            )
            old = self.db.execute("SELECT * FROM console_approvals WHERE id=?", (aid,)).fetchone()
            if old and old["body"] != encoded(saved):
                raise ValueError("Approval request ID cannot be reused")
            self.db.execute(
                "INSERT OR IGNORE INTO console_approvals VALUES(?,?,'pending',1,?)",
                (aid, encoded(saved), time.time()),
            )
        return {"id": aid, "state": old["state"] if old else "pending"}

    def console_approval_decide(self, body):
        aid = ident(body.get("id"))
        v = version(body.get("version"))
        reason = ident(body.get("reason"), "reason")
        decision = body.get("decision")
        if decision not in ("approve", "reject"):
            raise ValueError("Choose approve or reject")
        with self.transaction():
            row = self.db.execute("SELECT * FROM console_approvals WHERE id=?", (aid,)).fetchone()
            if not row or row["state"] != "pending" or row["version"] != v:
                raise ValueError("Approval request changed")
            req = json.loads(row["body"])
            tool = self.db.execute(
                "SELECT digest FROM tools WHERE name=?", (req["tool_name"],)
            ).fetchone()
            if not tool or tool[0] != req["policy"]:
                raise ValueError("Tool policy changed; submit a new request")
            if decision == "approve":
                if self.db.execute("SELECT 1 FROM approvals WHERE id=?", (aid,)).fetchone():
                    raise ValueError("Approval ID already exists")
                self.db.execute(
                    "INSERT INTO approvals VALUES(?,?,?,?,?,?,'approved',NULL)",
                    (
                        aid,
                        req["task_id"],
                        req["tool_name"],
                        fingerprint(req["arguments"]),
                        req["policy"],
                        time.time() + 300,
                    ),
                )
            self.db.execute(
                "UPDATE console_approvals SET state=?,version=version+1 WHERE id=?", (decision, aid)
            )
            self.console_audit("approval_" + decision, aid, {"reason": reason})
            self.event(req["workflow_id"], req["task_id"], "approval_" + decision, approval_id=aid)
        return {"id": aid, "state": decision}

    def console_approval_list(self, body):
        requested = ident(body["id"]) if body.get("id") is not None else None
        with self.lock:
            rows = []
            for r in self.db.execute(
                "SELECT c.*,a.expires,a.state execution_state,a.request FROM "
                "console_approvals c LEFT JOIN approvals a ON a.id=c.id ORDER BY "
                "c.created DESC"
            ):
                if requested is not None and r["id"] != requested:
                    continue
                row = dict(r)
                row["body"] = json.loads(row["body"])
                if row["execution_state"] == "approved" and row["expires"] <= time.time():
                    row["execution_state"] = "expired"
                rows.append(row)
            return {"approvals": rows}
