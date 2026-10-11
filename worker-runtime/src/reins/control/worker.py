"""Trusted local agent adapter runner. No arbitrary code is accepted from the browser."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import threading
import time
from pathlib import Path

from reins.projects.documents import atomic_json

from .client import Workflow


async def gemini_agent(inputs, config, workflow):
    from reins.projects.pilot import extract
    from reins.projects.runtime import Gemini, State, load_key

    model = config.get("model", "lite")
    if model not in ("lite", "flash"):
        raise ValueError("Use lite or flash")
    root = (
        Path(os.environ.get("REINS_AGENT_STATE", "~/.reins/agents")).expanduser()
        / workflow.context["workflow_id"]
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = State(root / "provider")
    try:
        result = await extract(
            inputs,
            workflow,
            Gemini(state, load_key(), control=workflow),
            root / "result.json",
            model=model,
            max_attempts=config.get("attempts", 6),
        )
        return {k: v["value"] for k, v in result["fields"].items()}
    finally:
        state.close()


def load_adapter(reference):
    module, sep, name = reference.partition(":")
    if not sep:
        raise ValueError("Use an installed module:function")
    adapter = getattr(importlib.import_module(module), name)
    if not callable(adapter):
        raise ValueError("Adapter must be callable")
    return adapter


def run_one(client, adapter_id, worker_id, adapter, state):
    root = Path(state).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    job = client.post("console/experiments/claim", {"adapter": adapter_id, "worker_id": worker_id})[
        "job"
    ]
    if not job:
        return False
    path = root / (job["id"] + ".json")
    if path.exists():
        raise ValueError("Job already journaled; inspect before submitting a saved result")
    atomic_json(path, {"state": "dispatching", "job_id": job["id"]})
    stop = threading.Event()

    def beat():
        while not stop.wait(20):
            try:
                client.post(
                    "console/experiments/heartbeat", {"id": job["id"], "worker_id": worker_id}
                )
            except Exception:
                pass  # Admission remains fail-closed; never replay a paid operation.

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    started = time.monotonic()
    output = None
    error = None
    try:
        output = adapter(job["input"], job["config"], Workflow(client, job["context"]))
        if inspect.isawaitable(output):
            output = asyncio.run(output)
    except Exception as exc:
        output = None
        error = type(exc).__name__
    finally:
        stop.set()
        thread.join(timeout=1)
    result = {
        "id": job["id"],
        "worker_id": worker_id,
        "output": output,
        "error": error,
        "latency_ms": (time.monotonic() - started) * 1000,
    }
    atomic_json(path, {"state": "returned", "submission": result})
    client.post("console/experiments/result", result)
    atomic_json(path, {"state": "submitted", "submission": result})
    return True


def recover(client, path):
    saved = json.loads(Path(path).read_text())
    if saved.get("state") not in ("returned", "submitted"):
        raise ValueError("No saved result; do not re-execute uncertain work")
    return client.post("console/experiments/result", saved["submission"])
