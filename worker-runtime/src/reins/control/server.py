"""Authenticated loopback control API; never a public agent execution endpoint."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from reins.control.ledger import Ledger
from reins.control.run_chat import (
    ChatLimit,
    ChatUnavailable,
    answer_run_question,
    reserve_public_question,
)
from reins.control.worker_pairs import WorkerPairs


class ControlServer(ThreadingHTTPServer):
    request_queue_size = 64

    def __init__(
        self,
        ledger: Ledger,
        token: str,
        port=8795,
        *,
        worker_token=None,
        local_ui=True,
        public_guest=False,
        public_quota_file=None,
        pair_token_file=None,
        worker_pairs_file=None,
    ):
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("Use a credential of at least 32 characters")
        if worker_token is not None and (len(worker_token) < 32 or worker_token == token):
            raise ValueError("Worker credential must be distinct and at least 32 characters")
        self.pair_token_file = Path(pair_token_file) if pair_token_file else None
        if self.pair_token_file and self.pair_token_file.stat().st_mode & 0o077:
            raise ValueError("Pairing token file must have permissions 0600")
        self.pair_lock = threading.Lock()
        self.pair_failures = []
        self.worker_pairs = WorkerPairs(worker_pairs_file, ledger) if worker_pairs_file else None
        self.ui_token = secrets.token_urlsafe(48) if local_ui and not pair_token_file else None
        self.public_guest = public_guest
        self.public_quota_file = Path(public_quota_file) if public_quota_file else None
        self.public_quota_lock = threading.Lock()
        self.public_chat_quota_file = (
            self.public_quota_file.with_name(self.public_quota_file.stem + "-chat.json")
            if self.public_quota_file
            else None
        )
        self._last_tick = 0
        self.workspace_label = "Private workspace"
        self.worker_token = worker_token
        self.ledger, self.token = ledger, token
        super().__init__(("127.0.0.1", port), Handler)

    def issue_pair_session(self):
        expires = int(time.time()) + 7 * 86400
        payload = f"{expires}.{secrets.token_urlsafe(24)}"
        signature = hmac.new(
            self.token.encode(), ("reins-pair-v1:" + payload).encode(), hashlib.sha256
        ).hexdigest()
        return payload + "." + signature

    def valid_pair_session(self, authorization):
        if not self.pair_token_file or not authorization.startswith("Bearer "):
            return False
        candidate = authorization.removeprefix("Bearer ")
        if len(candidate) > 256:
            return False
        parts = candidate.split(".")
        if len(parts) != 3 or not parts[0].isdigit() or len(parts[2]) != 64:
            return False
        if not int(time.time()) < int(parts[0]) <= int(time.time()) + 7 * 86400:
            return False
        payload = parts[0] + "." + parts[1]
        expected = hmac.new(
            self.token.encode(), ("reins-pair-v1:" + payload).encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(parts[2], expected)

    def service_actions(self):
        if time.monotonic() - self._last_tick >= 5:
            self.ledger.console_tick()
            self.ledger.automation_tick()
            self._last_tick = time.monotonic()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def respond(self, code, payload, content_type="application/json"):
        content = (
            json.dumps(payload, allow_nan=False).encode()
            if content_type == "application/json"
            else payload
        )
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(content)

    def trusted_origin(self):
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return host in allowed and (origin is None or origin == "http://" + host)

    def do_GET(self):
        if not self.trusted_origin():
            return self.respond(403, {"error": "Local origin required"})
        if self.path == "/v1/pair/mode":
            return self.respond(200, {"pairing": bool(self.server.pair_token_file)})
        if self.path == "/v1/local-session":
            if (
                not self.server.ui_token
                or self.headers.get("X-Reins-Local-UI") != "1"
                or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin")
            ):
                return self.respond(403, {"error": "Local workspace session unavailable"})
            return self.respond(200, {"token": self.server.ui_token})
        names = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/control.js": ("control.js", "application/javascript"),
            "/control.css": ("control.css", "text/css"),
            "/operations.js": ("operations.js", "application/javascript"),
            "/pilot.js": ("pilot.js", "application/javascript"),
            "/console.js": ("console.js", "application/javascript"),
            "/workload-cases.json": ("workload-cases.json", "text/plain; charset=utf-8"),
            "/workloads.js": ("workloads.js", "application/javascript"),
            "/company.js": ("company.js", "application/javascript"),
            "/company.css": ("company.css", "text/css"),
            "/advanced.js": ("advanced.js", "application/javascript"),
            "/console.css": ("console.css", "text/css"),
            "/reins-wordmark-v3.svg": ("reins-wordmark-v3.svg", "image/svg+xml"),
        }
        if self.path not in names:
            return self.respond(404, {"error": "Not found"})
        name, mime = names[self.path]
        self.respond(200, (Path(__file__).parent / "static" / name).read_bytes(), mime)

    def do_POST(self):
        if self.path == "/v1/pair/claim":
            if not self.trusted_origin() or not self.server.pair_token_file:
                return self.respond(403, {"error": "Pairing unavailable"})
            if self.headers.get("X-Reins-Pair") != "1":
                return self.respond(403, {"error": "Pairing request header required"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if not 0 < n <= 512:
                    raise ValueError("Invalid pairing request")
                body = json.loads(self.rfile.read(n))
                supplied = body.get("token") if isinstance(body, dict) else None
                if not isinstance(supplied, str) or not 32 <= len(supplied) <= 256:
                    raise ValueError("Invalid pairing token")
            except ValueError:
                return self.respond(400, {"error": "Invalid pairing request"})
            with self.server.pair_lock:
                now = time.monotonic()
                self.server.pair_failures = [t for t in self.server.pair_failures if now - t < 600]
                if len(self.server.pair_failures) >= 10:
                    return self.respond(429, {"error": "Too many pairing attempts"})
                path = self.server.pair_token_file
                expected = path.read_text().strip()
                if not hmac.compare_digest(supplied, expected):
                    self.server.pair_failures.append(now)
                    return self.respond(403, {"error": "Pairing token was not accepted"})
                replacement = path.with_name(path.name + ".new-" + secrets.token_hex(6))
                fd = os.open(replacement, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, "w") as stream:
                        stream.write(secrets.token_urlsafe(48))
                    os.replace(replacement, path)
                finally:
                    replacement.unlink(missing_ok=True)
                self.server.pair_failures.clear()
                return self.respond(
                    200, {"token": self.server.issue_pair_session(), "expires_in": 604800}
                )
        auth = self.headers.get("Authorization", "")
        operator = hmac.compare_digest(
            auth.encode("utf-8"), ("Bearer " + self.server.token).encode("utf-8")
        )
        ui_session = (
            bool(self.server.ui_token)
            and hmac.compare_digest(
                auth.encode("utf-8"), ("Bearer " + (self.server.ui_token or "")).encode("utf-8")
            )
        ) or self.server.valid_pair_session(auth)
        guest = bool(ui_session and self.server.public_guest)
        operator = bool(operator or (ui_session and not self.server.public_guest))
        worker = bool(self.server.worker_token) and hmac.compare_digest(
            auth.encode("utf-8"), ("Bearer " + (self.server.worker_token or "")).encode("utf-8")
        )
        scoped_pair = self.server.worker_pairs.pair_for(auth) if self.server.worker_pairs else None
        scoped_customer = scoped_pair["customer_id"] if scoped_pair else None
        worker = bool(worker or scoped_customer)
        if not self.trusted_origin() or not (operator or worker or guest):
            return self.respond(403, {"error": "Local credential required"})
        guest_paths = {
            "/v1/console/query",
            "/v1/console/detail",
            "/v1/console/pools",
            "/v1/console/budgets/list",
            "/v1/console/alerts/list",
            "/v1/console/approvals/list",
            "/v1/console/experiments/catalog",
            "/v1/console/experiments/report",
            "/v1/console/company/list",
            "/v1/console/company/create",
            "/v1/console/workloads/create",
            "/v1/console/company/ask",
            "/v1/console/automation/list",
            "/v1/ops/status",
            "/v1/pilot/status",
            "/v1/pilot/evidence",
            "/v1/pilot/costs/provenance",
            "/v1/status",
            "/v1/context",
        }
        if guest and self.path not in guest_paths:
            return self.respond(
                403, {"error": "This public workspace cannot change control settings"}
            )
        if self.path in ("/v1/worker-pair/me", "/v1/worker-pair/start") and not scoped_pair:
            return self.respond(403, {"error": "Customer SDK pairing required"})
        # The public browser token can use only the explicit routes above. Worker and
        # administrator credentials retain their original, separate permissions.
        operator = bool(operator or guest)
        privileged = {
            "/v1/pools",
            "/v1/tools",
            "/v1/approvals",
            "/v1/reconcile",
            "/v1/economics",
            "/v1/worker-pair/list",
            "/v1/worker-pair/register",
            "/v1/worker-pair/revoke",
        }
        if not operator and self.path in privileged:
            return self.respond(403, {"error": "Operator credential required"})
        pilot_actions = {
            "/v1/ops/status": self.server.ledger.operations_status,
            "/v1/ops/alerts/acknowledge": self.server.ledger.acknowledge_alert,
            "/v1/ops/handoffs/receive": self.server.ledger.receive_handoff,
            "/v1/ops/tools/save": self.server.ledger.save_tool_policy,
            "/v1/ops/failures/capture": self.server.ledger.capture_failure,
            "/v1/pilot/invoices/preview": self.server.ledger.invoice_preview,
            "/v1/pilot/status": self.server.ledger.pilot_status,
            "/v1/pilot/policies/validate": self.server.ledger.policy_validate,
            "/v1/pilot/policies/save": self.server.ledger.policy_save,
            "/v1/pilot/policies/preview": self.server.ledger.policy_preview,
            "/v1/pilot/evidence": self.server.ledger.progress_evidence,
            "/v1/pilot/issues/action": self.server.ledger.issue_action,
            "/v1/pilot/reviews/submit": self.server.ledger.review_submit,
            "/v1/pilot/reviews/decide": self.server.ledger.review_decide,
            "/v1/pilot/suites/export": self.server.ledger.suite_export,
            "/v1/pilot/costs/provenance": self.server.ledger.cost_provenance,
        }
        if (
            not operator
            and self.path in pilot_actions
            and self.path
            not in (
                "/v1/ops/handoffs/receive",
                "/v1/pilot/evidence",
                "/v1/pilot/reviews/submit",
                "/v1/pilot/costs/provenance",
            )
        ):
            return self.respond(403, {"error": "Operator credential required"})
        console_actions = {
            "query": "console_query",
            "reviews/batch": "bulk_reviews",
            "economics/decision": "economic_decision",
            "runtime/decision": "runtime_decision",
            "outcomes/confirm": "outcome_confirm",
            "automation/save": "automation_save",
            "automation/list": "automation_list",
            "automation/disable": "automation_disable",
            "detail": "console_detail",
            "pools": "console_pools",
            "budgets/list": "console_budgets",
            "budgets/save": "console_budget_save",
            "alerts/list": "console_alerts",
            "alerts/acknowledge": "console_ack",
            "approvals/request": "console_approval_request",
            "approvals/list": "console_approval_list",
            "approvals/decide": "console_approval_decide",
            "adapters/save": "adapter_save",
            "experiments/catalog": "experiment_catalog",
            "experiments/create": "experiment_create",
            "experiments/claim": "experiment_claim",
            "experiments/heartbeat": "experiment_heartbeat",
            "experiments/result": "experiment_result",
            "experiments/report": "experiment_report",
            "experiments/cancel": "experiment_cancel",
            "workloads/create": "workload_create",
            "company/create": "company_create",
            "company/list": "company_list",
            "deployments/adopt": "deployment_adopt",
            "deployments/rollback": "deployment_rollback",
            "deployments/start": "deployment_start",
            "deployments/launch": "deployment_launch",
        }
        console_routes = {
            "/v1/console/" + k: getattr(self.server.ledger, v) for k, v in console_actions.items()
        }
        console_routes["/v1/console/pools"] = lambda b: {
            **self.server.ledger.console_pools(b),
            "workspace_label": self.server.workspace_label,
        }

        def ask_run(body):
            run_id = body.get("run_id")
            if not isinstance(run_id, str) or not 1 <= len(run_id) <= 160:
                raise ValueError("Choose a saved run")
            question = body.get("question")
            history = body.get("history", [])
            if not isinstance(question, str) or not 3 <= len(question.strip()) <= 400:
                raise ValueError("Ask a question between 3 and 400 characters")
            if not isinstance(history, list) or len(history) > 6:
                raise ValueError("Conversation history is too long")
            task = next(
                (t for t in self.server.ledger.company_list({})["tasks"] if t["id"] == run_id),
                None,
            )
            if not task:
                raise ValueError("Run not found")
            from reins.projects.runtime import load_key

            try:
                key = load_key()
            except ValueError as exc:
                raise ChatUnavailable("AI questions are not configured on this server") from exc
            if guest:
                quota = self.server.public_chat_quota_file
                if quota is None:
                    raise ChatUnavailable("Public AI question quota is unavailable")
                reserve_public_question(quota, self.server.public_quota_lock)
            return answer_run_question(task, question, history, key=key)

        console_routes["/v1/console/company/ask"] = ask_run
        worker_console = {
            "economics/decision",
            "runtime/decision",
            "approvals/request",
            "approvals/list",
            "experiments/claim",
            "experiments/heartbeat",
            "experiments/result",
        }
        if (
            self.path in console_routes
            and not operator
            and self.path.removeprefix("/v1/console/") not in worker_console
        ):
            return self.respond(403, {"error": "Operator credential required"})

        def unavailable_pairing(_body):
            raise ValueError("Worker pairing is unavailable")

        actions = {
            **console_routes,
            **pilot_actions,
            "/v1/worker-pair/list": self.server.worker_pairs.list
            if self.server.worker_pairs
            else lambda b: {"pairs": []},
            "/v1/worker-pair/register": self.server.worker_pairs.register
            if self.server.worker_pairs
            else unavailable_pairing,
            "/v1/worker-pair/revoke": self.server.worker_pairs.revoke
            if self.server.worker_pairs
            else unavailable_pairing,
            "/v1/worker-pair/me": lambda b: self.server.worker_pairs.info(scoped_pair),
            "/v1/worker-pair/start": lambda b: self.server.worker_pairs.start(scoped_pair, b),
            "/v1/regression/case": self.server.ledger.regression_case,
            "/v1/pools": self.server.ledger.pool,
            "/v1/tools": self.server.ledger.register_tool,
            "/v1/approvals": self.server.ledger.approval,
            "/v1/state/write": self.server.ledger.state_write,
            "/v1/state/read": self.server.ledger.state_read,
            "/v1/handoff": self.server.ledger.handoff,
            "/v1/economics": self.server.ledger.economic_entry,
            "/v1/context/sample": self.server.ledger.context_sample,
            "/v1/workflows": self.server.ledger.workflow,
            "/v1/tasks": self.server.ledger.branch,
            "/v1/reserve": self.server.ledger.reserve,
            "/v1/settle": self.server.ledger.settle,
            "/v1/uncertain": self.server.ledger.uncertain,
            "/v1/transition": self.server.ledger.transition,
            "/v1/progress": self.server.ledger.progress,
            "/v1/reconcile": self.server.ledger.reconcile,
            "/v1/context": lambda b: self.server.ledger.context(b["task_id"]),
            "/v1/status": lambda b: self.server.ledger.status(b.get("workflow_id")),
        }
        if self.path not in actions:
            return self.respond(404, {"error": "Not found"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if not 0 < n <= 1048576:
                raise ValueError("Body must be 1 byte to 1 MiB")
            body = json.loads(self.rfile.read(n))
            if not isinstance(body, dict):
                raise ValueError("Expected object")
            if scoped_customer and not self.server.worker_pairs.allows(
                scoped_customer, self.path, body
            ):
                return self.respond(403, {"error": "Pairing is limited to its customer"})
            if guest and self.path in (
                "/v1/console/company/create",
                "/v1/console/workloads/create",
            ):
                try:
                    requested = Decimal(str(body.get("budget", "0.1")))
                except InvalidOperation as exc:
                    raise ValueError("Budget must be a number") from exc
                if not requested.is_finite() or not Decimal("0") < requested <= Decimal("1.5"):
                    raise ValueError("Public task budget must be above zero and at most $1.50")
                quota_path = self.server.public_quota_file
                if quota_path is None:
                    return self.respond(503, {"error": "Public task quota is unavailable"})
                with self.server.public_quota_lock:
                    quota = (
                        json.loads(quota_path.read_text())
                        if quota_path.exists()
                        else {"reserved": "0", "tasks": 0}
                    )
                    if quota["tasks"] >= 100 or Decimal(quota["reserved"]) + requested > 20:
                        return self.respond(429, {"error": "Public demo allocation is exhausted"})
                    quota = {
                        "reserved": str(Decimal(quota["reserved"]) + requested),
                        "tasks": quota["tasks"] + 1,
                    }
                    tmp = quota_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(quota))
                    os.replace(tmp, quota_path)
            if not operator:
                if self.path == "/v1/workflows":
                    # Operators provision immutable workflow policy before dispatching workers.
                    wid = body.get("workflow_id")
                    with self.server.ledger.lock:
                        exists = self.server.ledger.db.execute(
                            "SELECT 1 FROM workflows WHERE id=?", (wid,)
                        ).fetchone()
                    if not exists:
                        return self.respond(403, {"error": "Operator must provision workflow"})
                if self.path == "/v1/transition" and body.get("action") not in ("finish", "wrapup"):
                    return self.respond(403, {"error": "Operator required to pause or resume"})
            result = actions[self.path](body)
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            return self.respond(400, {"error": str(exc)})
        except ChatLimit as exc:
            return self.respond(429, {"error": str(exc)})
        except ChatUnavailable as exc:
            return self.respond(503, {"error": str(exc)})
        except Exception:
            return self.respond(500, {"error": "Control operation failed; do not repeat paid work"})
        self.respond(200, result)
