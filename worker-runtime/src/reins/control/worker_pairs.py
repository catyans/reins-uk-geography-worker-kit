"""Customer-scoped worker credentials for the isolated hosted demonstration."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
from pathlib import Path


class WorkerPairs:
    def __init__(self, path, ledger):
        self.path = Path(path)
        self.ledger = ledger
        self.lock = threading.RLock()
        if self.path.exists() and self.path.stat().st_mode & 0o077:
            raise ValueError("Worker pairing file must have permissions 0600")
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump({"pairs": []}, stream)

    def _read(self):
        return json.loads(self.path.read_text())

    def _write(self, data):
        temp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.path)

    def list(self, _body):
        with self.lock:
            return {
                "pairs": [
                    {
                        **{k: p[k] for k in ("id", "workflow_id", "created")},
                        "customer_id": self._pair_customer(p),
                    }
                    for p in self._read()["pairs"]
                ]
            }

    def _workflow_customer(self, workflow_id):
        if not isinstance(workflow_id, str) or not workflow_id:
            return None
        with self.ledger.lock:
            row = self.ledger.db.execute(
                "SELECT customer FROM workflows WHERE id=?", (workflow_id,)
            ).fetchone()
        return row[0] if row else None

    def _pair_customer(self, pair):
        # Pairings written before customer-scoped access contained only the
        # original task. Resolve its customer so the existing code keeps working.
        return pair.get("customer_id") or self._workflow_customer(pair["workflow_id"])

    def register(self, body):
        code, workflow_id = body.get("code"), body.get("workflow_id")
        if (
            not isinstance(code, str)
            or not 43 <= len(code) <= 128
            or not all(c.isalnum() or c in "-_" for c in code)
        ):
            raise ValueError("Enter the complete generated pairing code")
        if not isinstance(workflow_id, str) or not workflow_id:
            raise ValueError("Choose an existing task ID")
        customer_id = self._workflow_customer(workflow_id)
        if not customer_id:
            raise ValueError("Task does not exist")
        digest = hashlib.sha256(code.encode()).hexdigest()
        pair_id = digest[:16]
        with self.lock:
            data = self._read()
            if any(p["digest"] == digest for p in data["pairs"]):
                raise ValueError("This code is already paired")
            data["pairs"].append(
                {
                    "id": pair_id,
                    "digest": digest,
                    "workflow_id": workflow_id,
                    "customer_id": customer_id,
                    "created": time.time(),
                }
            )
            self._write(data)
        return {"id": pair_id, "workflow_id": workflow_id, "customer_id": customer_id}

    def revoke(self, body):
        pair_id = body.get("id")
        with self.lock:
            data = self._read()
            old = len(data["pairs"])
            data["pairs"] = [p for p in data["pairs"] if p["id"] != pair_id]
            if len(data["pairs"]) == old:
                raise ValueError("Unknown pairing")
            self._write(data)
        return {"revoked": pair_id}

    def pair_for(self, authorization):
        if not authorization.startswith("Bearer "):
            return None
        candidate = authorization.removeprefix("Bearer ")
        if not 43 <= len(candidate) <= 128:
            return None
        digest = hashlib.sha256(candidate.encode()).hexdigest()
        with self.lock:
            for pair in self._read()["pairs"]:
                if hmac.compare_digest(pair["digest"], digest):
                    customer_id = self._pair_customer(pair)
                    if customer_id:
                        return {
                            "id": pair["id"],
                            "customer_id": customer_id,
                            "workflow_id": pair["workflow_id"],
                        }
        return None

    def info(self, pair):
        with self.ledger.lock:
            row = self.ledger.db.execute(
                "SELECT deployment FROM runtime_assignments WHERE workflow=?",
                (pair["workflow_id"],),
            ).fetchone()
        return {
            "pairing_id": pair["id"],
            "customer_id": pair["customer_id"],
            "deployment_id": row[0] if row else None,
        }

    def start(self, pair, body):
        run_key = body.get("run_key")
        if (
            set(body) != {"run_key"}
            or not isinstance(run_key, str)
            or not re.fullmatch(r"[0-9a-f]{32}", run_key)
        ):
            raise ValueError("Supply one 32-character run key")
        deployment_id = self.info(pair)["deployment_id"]
        if not deployment_id:
            raise ValueError("Pairing needs a Hosted task with an adopted deployment")
        workflow_id = f"sdk-{pair['id']}-{run_key}"
        with self.ledger.lock:
            existing = self.ledger.db.execute(
                "SELECT customer,task_type FROM workflows WHERE id=?", (workflow_id,)
            ).fetchone()
            if existing:
                if (existing["customer"], existing["task_type"]) != (
                    pair["customer_id"],
                    deployment_id,
                ):
                    raise ValueError("Run key belongs to another task")
            else:
                self.ledger.deployment_start(
                    {
                        "id": deployment_id,
                        "workflow_id": workflow_id,
                        "customer_id": pair["customer_id"],
                    }
                )
        return {"workflow_id": workflow_id, "customer_id": pair["customer_id"]}

    def allows(self, customer_id, path, body):
        if path in ("/v1/worker-pair/me", "/v1/worker-pair/start"):
            return True
        if path in ("/v1/status", "/v1/console/runtime/decision"):
            return self._workflow_customer(body.get("workflow_id")) == customer_id
        if path == "/v1/transition":
            return (
                body.get("action") == "finish"
                and self._workflow_customer(body.get("workflow_id")) == customer_id
            )
        if path in ("/v1/reserve", "/v1/progress"):
            with self.ledger.lock:
                row = self.ledger.db.execute(
                    "SELECT workflows.customer FROM tasks JOIN workflows "
                    "ON tasks.workflow=workflows.id WHERE tasks.id=?",
                    (body.get("task_id"),),
                ).fetchone()
            return bool(row and row[0] == customer_id)
        if path in ("/v1/settle", "/v1/uncertain"):
            with self.ledger.lock:
                row = self.ledger.db.execute(
                    "SELECT workflows.customer FROM requests JOIN workflows "
                    "ON requests.workflow=workflows.id WHERE requests.id=?",
                    (body.get("request_id"),),
                ).fetchone()
            return bool(row and row[0] == customer_id)
        return False
