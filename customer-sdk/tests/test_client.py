import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from reins_sdk import (
    AdmissionDenied,
    Charge,
    ReinsAuthenticationError,
    ReinsClient,
    SettlementUnknown,
)

TOKEN = "w" * 48


class Handler(BaseHTTPRequestHandler):
    requests = []
    action = "continue"
    admission = "continue"
    settle_error = False
    cache = {}
    reservations = {}
    model = "google/test"
    escalate_after_failure = False

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append((self.path, body, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self.send_json(403, {"error": "Worker credential required"})
        if self.path == "/api/console/runtime/decision":
            return self.send_json(
                200,
                {
                    "action": self.action,
                    "reason": "test-stop" if self.action == "stop" else "pinned",
                    "configuration": {"config": {"model": self.model}},
                },
            )
        if self.path == "/api/reserve":
            key = body.get("cache_key")
            if key in self.cache:
                return self.send_json(
                    200,
                    {
                        "decision": "reuse",
                        "result": self.cache[key]["result"],
                        "original_request": self.cache[key]["request_id"],
                    },
                )
            self.reservations[body["request_id"]] = key
            return self.send_json(200, {"decision": self.admission, "reason": "test-hold"})
        if self.path == "/api/settle" and self.settle_error:
            return self.send_json(503, {"error": "temporarily unavailable"})
        if self.path == "/api/settle":
            key = self.reservations.get(body["request_id"])
            if key and body.get("validated"):
                self.cache[key] = {
                    "result": body["result"],
                    "request_id": body["request_id"],
                }
            if self.escalate_after_failure and not body.get("validated"):
                Handler.model = "google/strong"
        if self.path == "/api/status":
            return self.send_json(200, {"workflows": [{"id": body["workflow_id"]}]})
        if self.path == "/api/worker-pair/me":
            return self.send_json(200, {"customer_id": "demo-customer", "pairing_id": "demo-pair"})
        if self.path == "/api/worker-pair/start":
            return self.send_json(200, {"workflow_id": "run-" + body["run_key"]})
        if self.path == "/api/tasks":
            return self.send_json(200, {"decision": "continue"})
        return self.send_json(200, {"status": "recorded"})

    def send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/api"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        Handler.requests = []
        Handler.action = "continue"
        Handler.admission = "continue"
        Handler.settle_error = False
        Handler.cache = {}
        Handler.reservations = {}
        Handler.model = "google/test"
        Handler.escalate_after_failure = False
        self.client = ReinsClient(self.url, token=TOKEN)

    def test_paid_call_orders_admission_provider_and_settlement(self):
        called = []

        def execute(config):
            called.append(config)
            return Charge({"answer": 42}, "0.03", {"input_tokens": 5, "output_tokens": 1})

        outcome = self.client.run_paid_call("job-1", execute, max_cost="0.10")
        self.assertEqual(outcome.result, {"answer": 42})
        self.assertEqual(called, [{"model": "google/test"}])
        self.assertEqual(
            [path for path, _, _ in Handler.requests],
            ["/api/console/runtime/decision", "/api/reserve", "/api/settle"],
        )
        _, reserve, _ = Handler.requests[1]
        _, settle, _ = Handler.requests[2]
        self.assertEqual(reserve["model"], "google/test")
        self.assertEqual(reserve["max_cost"], "0.10")
        self.assertEqual(settle["request_id"], reserve["request_id"])
        self.assertEqual(settle["actual_cost"], "0.03")
        self.assertEqual(settle["usage"], {"input_tokens": 5, "output_tokens": 1})
        self.assertTrue(all(auth == "Bearer " + TOKEN for _, _, auth in Handler.requests))

    def test_named_business_validation_reaches_settlement(self):
        self.client.run_paid_call(
            "job-1",
            lambda _: Charge(
                {"answer": "wrong"},
                "0.02",
                success=True,
                validated=False,
                validation_name="invoice-check-v1",
            ),
            max_cost="0.10",
        )
        settlement = Handler.requests[-1][1]
        self.assertEqual(settlement["validation_name"], "invoice-check-v1")
        self.assertFalse(settlement["validated"])
        self.assertTrue(settlement["success"])

    def test_stop_and_hold_never_dispatch_provider(self):
        def forbidden(_config):
            self.fail("Provider should not run")

        Handler.action = "stop"
        with self.assertRaises(AdmissionDenied):
            self.client.run_paid_call("job-1", forbidden, max_cost="0.10")
        self.assertEqual(len(Handler.requests), 1)
        Handler.requests = []
        Handler.action = "continue"
        Handler.admission = "hold"
        with self.assertRaises(AdmissionDenied):
            self.client.run_paid_call("job-1", forbidden, max_cost="0.10")
        self.assertEqual(len(Handler.requests), 2)

    def test_provider_exception_marks_charge_uncertain(self):
        def broken(_config):
            raise RuntimeError("provider timed out")

        with self.assertRaisesRegex(RuntimeError, "provider timed out"):
            self.client.run_paid_call("job-1", broken, max_cost="0.10")
        self.assertEqual(Handler.requests[-1][0], "/api/uncertain")
        self.assertEqual(
            Handler.requests[-1][1]["request_id"], Handler.requests[1][1]["request_id"]
        )

    def test_settlement_failure_exposes_original_request_id_without_retry(self):
        Handler.settle_error = True
        with self.assertRaises(SettlementUnknown) as caught:
            self.client.run_paid_call("job-1", lambda _: Charge("done", "0.02"), max_cost="0.10")
        self.assertEqual(caught.exception.request_id, Handler.requests[1][1]["request_id"])
        self.assertEqual([p for p, _, _ in Handler.requests].count("/api/settle"), 1)

    def test_auth_and_rejected_request(self):
        other = ReinsClient(self.url, token="z" * 48)
        with self.assertRaises(ReinsAuthenticationError):
            other.status("job-1")
        Handler.action = "stop"
        with self.assertRaises(AdmissionDenied):
            self.client.run_paid_call("job-1", lambda _: Charge(None, "0"), max_cost="0.10")
        with self.assertRaises(ValueError):
            self.client.post("../server", {})

    def test_status(self):
        self.assertEqual(self.client.status("job-1")["workflows"][0]["id"], "job-1")

    def test_pairing_and_start_run(self):
        self.assertEqual(self.client.pairing()["customer_id"], "demo-customer")
        first = self.client.start_run(run_key="a" * 32)["workflow_id"]
        self.assertEqual(first, "run-" + "a" * 32)
        self.assertEqual(Handler.requests[-1][1], {"run_key": "a" * 32})

    def test_tls_and_credentials_required(self):
        with self.assertRaises(ValueError):
            ReinsClient("http://example.com/api", token=TOKEN)
        with self.assertRaises(ValueError):
            ReinsClient("https://example.com/api?token=foo", token=TOKEN)
        with self.assertRaises(ValueError):
            ReinsClient(self.url, token="short")
        with self.assertRaises(ValueError):
            ReinsClient(self.url, token=TOKEN, token_file="somewhere")

    def test_validated_read_only_result_is_reused_without_provider_call(self):
        calls = []

        def execute(_config):
            calls.append(1)
            return Charge({"answer": 42}, "0.03", {"input_tokens": 5})

        args = dict(
            max_cost="0.10",
            reuse_inputs={"source": "invoice-1", "revision": 1},
            read_only=True,
            validator=lambda result: result == {"answer": 42},
            validation_name="invoice-check-v1",
        )
        first = self.client.run_paid_call("job-1", execute, **args)
        second = self.client.run_paid_call("job-1", execute, **args)
        self.assertEqual(calls, [1])
        self.assertTrue(first.validated)
        self.assertEqual(second.result, first.result)
        self.assertEqual(second.actual_cost, "0")
        self.assertEqual(second.reused_from, Handler.requests[1][1]["request_id"])
        self.assertEqual([p for p, _, _ in Handler.requests].count("/api/settle"), 1)
        reserve = [b for p, b, _ in Handler.requests if p == "/api/reserve"]
        self.assertTrue(all(b["read_only"] is True for b in reserve))
        self.assertEqual(reserve[0]["cache_key"], reserve[1]["cache_key"])

        self.client.run_paid_call(
            "job-1", execute, **{**args, "reuse_inputs": {"source": "invoice-1", "revision": 2}}
        )
        self.assertEqual(calls, [1, 1])
        self.assertNotEqual(reserve[0]["cache_key"], Handler.requests[-2][1]["cache_key"])

    def test_reuse_requires_explicit_safety_contract(self):
        def execute(_config):
            return Charge("done", "0.01")

        for options in (
            {"reuse_inputs": {"id": 1}},
            {"reuse_inputs": {"id": 1}, "read_only": True},
            {"reuse_inputs": {"id": 1}, "read_only": True, "validator": lambda _: True},
        ):
            with self.assertRaises(ValueError):
                self.client.run_paid_call("job-1", execute, max_cost="0.10", **options)
        self.assertEqual(Handler.requests, [])

    def test_failed_validation_is_charged_but_not_cached(self):
        calls = []

        def execute(_config):
            calls.append(1)
            return Charge({"answer": "wrong"}, "0.02")

        args = dict(
            max_cost="0.10",
            reuse_inputs={"source": "invoice-2"},
            read_only=True,
            validator=lambda result: result == {"answer": "right"},
            validation_name="invoice-check-v1",
        )
        result = self.client.run_paid_call("job-1", execute, **args)
        self.assertFalse(result.validated)
        self.assertEqual(Handler.requests[-1][1]["actual_cost"], "0.02")
        self.assertFalse(Handler.requests[-1][1]["validated"])
        self.client.run_paid_call("job-1", execute, **args)
        self.assertEqual(len(calls), 2)

    def test_invalid_reused_result_never_dispatches_provider(self):
        Handler.cache["bad"] = {"result": {"answer": "wrong"}, "request_id": "old"}
        self.client.reserve = lambda **_: {"decision": "reuse", "result": {"answer": "wrong"}}
        with self.assertRaisesRegex(Exception, "failed validation"):
            self.client.run_paid_call(
                "job-1",
                lambda _: self.fail("Provider should not run"),
                max_cost="0.10",
                reuse_inputs={"id": 1},
                read_only=True,
                validator=lambda result: result == {"answer": "right"},
                validation_name="check-v1",
            )

    def test_operation_identity_and_retry_are_sent_before_paid_call(self):
        self.client.run_paid_call(
            "job-1",
            lambda _: Charge("done", "0.02"),
            max_cost="0.10",
            operation_inputs={"tool": "search", "query": "same"},
            retry=True,
        )
        reserve = Handler.requests[1][1]
        self.assertEqual(len(reserve["operation_key"]), 64)
        self.assertTrue(reserve["retry"])
        self.assertNotIn("operation_inputs", reserve)

    def test_child_budget_and_context_sample(self):
        child = self.client.child_task("job-1", budget="0.05", task_id="research-agent")
        self.assertEqual(child, "research-agent")
        self.assertEqual(Handler.requests[-1][1]["budget"], "0.05")
        result = self.client.context_sample(
            child,
            revision="prompt-v2",
            components={"system": 60, "tool_schema": 40, "user": 12},
            provider_input_tokens=115,
            sample_id="sample-1",
        )
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(Handler.requests[-1][1]["components"]["tool_schema"], 40)
        with self.assertRaises(ValueError):
            self.client.context_sample(child, revision="v1", components={"system": -1})

    def test_fallback_charges_both_attempts_and_uses_server_model(self):
        Handler.escalate_after_failure = True
        seen = []

        def execute(config):
            seen.append(config["model"])
            return Charge(config["model"], "0.01" if len(seen) == 1 else "0.03")

        result = self.client.run_with_fallback(
            "job-1",
            execute,
            max_cost_for_model=lambda model: "0.10" if model == "google/test" else "0.20",
            validator=lambda output: output == "google/strong",
            validation_name="business-v1",
        )
        self.assertTrue(result.validated)
        self.assertEqual(seen, ["google/test", "google/strong"])
        reserves = [body for path, body, _ in Handler.requests if path == "/api/reserve"]
        settlements = [body for path, body, _ in Handler.requests if path == "/api/settle"]
        self.assertEqual([item["max_cost"] for item in reserves], ["0.10", "0.20"])
        self.assertEqual([item["actual_cost"] for item in settlements], ["0.01", "0.03"])
        self.assertEqual([item["validated"] for item in settlements], [False, True])

    def test_fallback_without_escalation_never_repeats_same_model(self):
        seen = []

        def execute(config):
            seen.append(config["model"])
            return Charge("bad", "0.01")

        with self.assertRaises(AdmissionDenied):
            self.client.run_with_fallback(
                "job-1",
                execute,
                max_cost_for_model=lambda _: "0.10",
                validator=lambda _: False,
                validation_name="business-v1",
            )
        self.assertEqual(seen, ["google/test"])

    def test_fallback_does_not_retry_after_unknown_settlement(self):
        Handler.settle_error = True
        calls = []

        def execute(config):
            calls.append(config["model"])
            return Charge("bad", "0.01")

        with self.assertRaises(SettlementUnknown):
            self.client.run_with_fallback(
                "job-1",
                execute,
                max_cost_for_model=lambda _: "0.10",
                validator=lambda _: False,
                validation_name="business-v1",
            )
        self.assertEqual(calls, ["google/test"])
        self.assertEqual([path for path, _, _ in Handler.requests].count("/api/reserve"), 1)

    def test_fallback_never_dispatches_when_budget_denies_admission(self):
        Handler.admission = "pause"
        with self.assertRaises(AdmissionDenied):
            self.client.run_with_fallback(
                "job-1",
                lambda _: self.fail("Provider must not be called"),
                max_cost_for_model=lambda _: "0.10",
                validator=lambda _: False,
                validation_name="business-v1",
            )
        self.assertEqual([path for path, _, _ in Handler.requests].count("/api/settle"), 0)


if __name__ == "__main__":
    unittest.main()
