"""The example business result survives adding the hosted SDK boundary."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples/customer_order_demo"
TOKEN = "t" * 48


class DemoHandler(BaseHTTPRequestHandler):
    events = []
    states = {}
    run_keys = {}

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.events.append((self.path, body))
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self.reply(403, {"error": "Not paired"})
        if self.path == "/api/worker-pair/me":
            return self.reply(200, {"customer_id": "demo-customer"})
        if self.path == "/api/worker-pair/start":
            run_key = body["run_key"]
            task_id = self.run_keys.setdefault(run_key, "auto-" + str(len(self.run_keys) + 1))
            self.states.setdefault(task_id, "running")
            return self.reply(200, {"workflow_id": task_id})
        if self.path == "/api/status":
            return self.reply(
                200,
                {
                    "workflows": [
                        {
                            "workflow_id": body["workflow_id"],
                            "state": self.states.get(body["workflow_id"], "running"),
                        }
                    ]
                },
            )
        if self.path == "/api/console/runtime/decision":
            return self.reply(
                200,
                {"action": "continue", "configuration": {"config": {"model": "demo/lite"}}},
            )
        if self.path == "/api/reserve":
            return self.reply(200, {"decision": "continue"})
        if self.path == "/api/transition":
            self.states[body["workflow_id"]] = "completed"
        return self.reply(200, {"status": "recorded"})

    def reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class CustomerOrderDemoTests(unittest.TestCase):
    def test_before_after_business_output_and_server_record(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        DemoHandler.events = []
        DemoHandler.states = {}
        DemoHandler.run_keys = {}
        try:
            with tempfile.TemporaryDirectory() as folder:
                token_file = Path(folder) / "worker.token"
                token_file.write_text(TOKEN)
                env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
                baseline = subprocess.run(
                    [sys.executable, str(DEMO / "before_reins.py")],
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                command = [
                    sys.executable,
                    str(DEMO / "with_reins.py"),
                    "demo-task",
                    "--base-url",
                    f"http://127.0.0.1:{server.server_port}/api",
                    "--token-file",
                    str(token_file),
                ]
                connection = subprocess.run(
                    command, env=env, check=True, capture_output=True, text=True
                )
                self.assertIn("Connection verified", connection.stdout)
                self.assertEqual([path for path, _ in DemoHandler.events], ["/api/status"])
                completed = subprocess.run(
                    command + ["--run"], env=env, check=True, capture_output=True, text=True
                )
                self.assertIn(json.dumps(json.loads(baseline.stdout), indent=2), completed.stdout)
                self.assertIn("Final task state: completed", completed.stdout)
                self.assertEqual(
                    [path for path, _ in DemoHandler.events][1:],
                    [
                        "/api/status",
                        "/api/console/runtime/decision",
                        "/api/progress",
                        "/api/console/runtime/decision",
                        "/api/reserve",
                        "/api/settle",
                        "/api/progress",
                        "/api/transition",
                        "/api/status",
                    ],
                )
                settlement = next(
                    body for path, body in DemoHandler.events if path == "/api/settle"
                )
                self.assertEqual(settlement["actual_cost"], "0")
                self.assertTrue(settlement["validated"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_repeated_runs_need_no_task_id(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        DemoHandler.events = []
        DemoHandler.states = {}
        DemoHandler.run_keys = {}
        try:
            with tempfile.TemporaryDirectory() as folder:
                token_file = Path(folder) / "worker.token"
                token_file.write_text(TOKEN)
                env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
                command = [
                    sys.executable,
                    str(DEMO / "with_reins.py"),
                    "--base-url",
                    f"http://127.0.0.1:{server.server_port}/api",
                    "--token-file",
                    str(token_file),
                ]
                paired = subprocess.run(
                    command, env=env, check=True, capture_output=True, text=True
                )
                self.assertIn("Connected to Reins customer: demo-customer", paired.stdout)
                for task_id in ("auto-1", "auto-2"):
                    completed = subprocess.run(
                        command + ["--run"], env=env, check=True, capture_output=True, text=True
                    )
                    self.assertIn("New run created: " + task_id, completed.stdout)
                    self.assertIn("Final task state: completed", completed.stdout)
                self.assertEqual(
                    [path for path, _ in DemoHandler.events].count("/api/worker-pair/start"),
                    2,
                )
                self.assertEqual(set(DemoHandler.states.values()), {"completed"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
