"""Local operator commands. Credentials are read from private files."""

import csv
import json
import os
import secrets
from pathlib import Path

import click

from .client import Client
from .ledger import Ledger
from .server import ControlServer


@click.group()
@click.option("--url", default="http://127.0.0.1:8795")
@click.option("--token-file", default="~/.reins/control.token")
@click.pass_context
def control(ctx, url, token_file):
    """Shared runtime budget control (one host)."""
    ctx.obj = {"url": url, "token_file": str(Path(token_file).expanduser())}


@control.command()
@click.option("--database", default="~/.reins/control.sqlite")
@click.option("--port", type=click.IntRange(1, 65535), default=8795)
@click.option("--worker-token-file", default="~/.reins/worker.token")
@click.option("--workspace-label", default="Private workspace")
@click.option("--pair-token-file", default=None)
@click.option("--worker-pairs-file", default=None)
@click.pass_context
def serve(
    ctx, database, port, worker_token_file, workspace_label, pair_token_file, worker_pairs_file
):
    """Start the authoritative ledger and local operator dashboard."""
    path = Path(ctx.obj["token_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.stat().st_mode & 0o077:
            raise click.ClickException("Credential file must have permissions 0600")
    else:
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(48))
    ledger = Ledger(Path(database).expanduser())
    worker = Path(worker_token_file).expanduser()
    worker.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(worker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if worker.stat().st_mode & 0o077:
            ledger.close()
            raise click.ClickException("Worker credential file must have permissions 0600")
    else:
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(48))
    pair_path = Path(pair_token_file).expanduser() if pair_token_file else None
    if pair_path:
        pair_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(pair_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if pair_path.stat().st_mode & 0o077:
                ledger.close()
                raise click.ClickException("Pairing token file must have permissions 0600")
        else:
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(48))
    server = ControlServer(
        ledger,
        path.read_text().strip(),
        port,
        worker_token=worker.read_text().strip(),
        pair_token_file=pair_path,
        worker_pairs_file=Path(worker_pairs_file).expanduser() if worker_pairs_file else None,
    )
    server.workspace_label = workspace_label
    click.echo(f"Control dashboard: http://127.0.0.1:{port}\nCredential file: {path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ledger.close()


@control.command()
@click.argument("workflow_id", required=False)
@click.pass_context
def status(ctx, workflow_id):
    """Show recorded costs, reservations, tasks and forecasts."""
    click.echo(json.dumps(Client(**ctx.obj).post("status", {"workflow_id": workflow_id}), indent=2))


def transition(ctx, workflow_id, reason, action):
    click.echo(
        json.dumps(
            Client(**ctx.obj).post(
                "transition", {"workflow_id": workflow_id, "reason": reason, "action": action}
            )
        )
    )


@control.command()
@click.argument("workflow_id")
@click.option("--reason", required=True)
@click.pass_context
def pause(ctx, workflow_id, reason):
    """Block future admission; does not cancel in-flight provider calls."""
    transition(ctx, workflow_id, reason, "pause")


@control.command()
@click.argument("workflow_id")
@click.option("--reason", required=True)
@click.pass_context
def resume(ctx, workflow_id, reason):
    """Resume admission; existing reservations remain charged."""
    transition(ctx, workflow_id, reason, "resume")


@control.command()
@click.option("--csv", "csv_path", type=click.Path(exists=True), required=True)
@click.pass_context
def reconcile(ctx, csv_path):
    """Import explicit USD invoice lines; immutable, sequential revisions."""
    client = Client(**ctx.obj)
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            row["revision"] = int(row.get("revision") or 1)
            click.echo(json.dumps({"line_id": row.get("line_id"), **client.post("reconcile", row)}))


@control.command("apply")
@click.argument(
    "operation",
    type=click.Choice(
        [
            "pools",
            "tools",
            "approvals",
            "economics",
            "state/write",
            "state/read",
            "handoff",
            "workflows",
            "context/sample",
            "pilot/status",
            "ops/status",
            "ops/alerts/acknowledge",
            "ops/handoffs/receive",
            "ops/tools/save",
            "ops/failures/capture",
            "pilot/policies/validate",
            "pilot/policies/save",
            "pilot/policies/preview",
            "pilot/evidence",
            "pilot/issues/action",
            "pilot/reviews/submit",
            "pilot/reviews/decide",
            "pilot/suites/export",
            "pilot/costs/provenance",
            "pilot/invoices/preview",
        ]
    ),
)
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def apply_operation(ctx, operation, input_file):
    """Submit an explicit JSON document to the local control API."""
    click.echo(
        json.dumps(
            Client(**ctx.obj).post(operation, json.loads(Path(input_file).read_text())), indent=2
        )
    )


@control.command("regression")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--output", required=True, type=click.Path())
def regression(input_file, output):
    """Evaluate a frozen trajectory suite; write HTML/JSON and fail on regression."""
    from .regression import evaluate_suite, write_report

    body = json.loads(Path(input_file).read_text())
    report = evaluate_suite(
        body["cases"], body["contract"], expected_case_ids=body["expected_case_ids"]
    )
    write_report(report, output)
    click.echo(
        f"{report['passing']}/{report['total']} cases passed; "
        f"report: {Path(output).with_suffix('.html')}"
    )
    if not report["passed"]:
        raise click.exceptions.Exit(1)


@control.command("pilot-run")
@click.argument("task_id")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--output", required=True, type=click.Path())
@click.option("--state", required=True, type=click.Path())
@click.option("--key-file", type=click.Path(exists=True, dir_okay=False))
@click.option("--model", type=click.Choice(["lite", "flash"]), default="lite")
@click.option("--attempts", type=click.IntRange(1, 30), default=6)
@click.option(
    "--resume", is_flag=True, help="Explicitly continue a held task after operator resume"
)
@click.pass_context
def pilot_run(ctx, task_id, input_file, output, state, key_file, model, attempts, resume):
    """Run a provisioned private data task; persist every attempt for human review."""
    import asyncio

    from reins.control.client import Workflow
    from reins.projects.pilot import extract
    from reins.projects.runtime import Gemini, State, load_key

    client = Client(**ctx.obj)
    context = client.post("context", {"task_id": task_id})
    controlled = Workflow(client, context)
    store = State(state)
    try:
        provider = Gemini(store, load_key(key_file), control=controlled)
        result = asyncio.run(
            extract(
                json.loads(Path(input_file).read_text()),
                controlled,
                provider,
                output,
                model=model,
                max_attempts=attempts,
                resume=resume,
            )
        )
        click.echo(f"{result['state']}; saved to {output}")
    finally:
        store.close()


@control.command("pilot-evaluate")
@click.argument("suite_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--baseline", required=True, help="Saved baseline policy version")
@click.option("--candidate", required=True, help="Saved candidate policy version")
@click.option("--customer", required=True)
@click.option("--output", required=True, type=click.Path())
@click.option("--key-file", type=click.Path(exists=True, dir_okay=False))
@click.option("--model", type=click.Choice(["lite", "flash"]), default="lite")
@click.option(
    "--contract",
    type=click.Path(exists=True, dir_okay=False),
    help="Frozen trajectory/cost/latency contract JSON",
)
@click.pass_context
def pilot_evaluate(
    ctx, suite_file, baseline, candidate, customer, output, key_file, model, contract
):
    """Explicitly run paid paired evaluations, saving all attempts and HTML/JSON reports."""
    import asyncio
    from uuid import uuid4

    from reins.control.client import Workflow
    from reins.projects.documents import atomic_json, digest
    from reins.projects.pilot import extract, validate_input
    from reins.projects.pilot_evaluation import acceptance, compare, validate_suite
    from reins.projects.runtime import Gemini, State, load_key

    from .regression import validate_contract, write_report

    suite = json.loads(Path(suite_file).read_text())
    cases = validate_suite(suite)
    contract_body = json.loads(Path(contract).read_text()) if contract else {}
    validate_contract(contract_body)
    for case in cases:
        validate_input(case["input"])
    client = Client(**ctx.obj)
    policies = {p["id"]: p["body"] for p in client.post("pilot/status", {})["policies"]}
    if baseline not in policies or candidate not in policies:
        raise click.ClickException("Provision both immutable policies first")
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "experiment.json"
    spec = {
        "suite_hash": digest(suite),
        "contract": contract_body,
        "baseline": baseline,
        "candidate": candidate,
        "customer": customer,
        "model": model,
    }
    if manifest.exists():
        saved = json.loads(manifest.read_text())
        if saved["spec"] != spec:
            raise click.ClickException("Output directory belongs to a different experiment")
        run_id = saved["run_id"]
    else:
        run_id = str(uuid4())
        atomic_json(manifest, {"run_id": run_id, "spec": spec})
    key = load_key(key_file)
    store = State(root / "provider")
    by_id = {c["id"]: c for c in cases}

    async def run(body, case_id, arm):
        task_id = digest([run_id, case_id, arm])
        context = client.post(
            "workflows",
            {
                "workflow_id": task_id,
                "customer_id": customer,
                "task_type": "pilot-evaluation",
                "policy_template": spec[arm],
            },
        )
        controlled = Workflow(client, context)
        provider = Gemini(store, key, control=controlled)
        result = await extract(body, controlled, provider, root / (task_id + ".json"), model=model)
        values = {k: v["value"] for k, v in result["fields"].items()}
        status = client.post("status", {"workflow_id": task_id})["workflows"][0]
        # Final acceptance uses frozen labels only after execution, never as progress signals.
        if status["state"] not in ("completed", "failed"):
            unresolved = any(r["actual_cost"] is None for r in status["requests"])
            if not unresolved:
                controlled.finish(accepted=acceptance(values, by_id[case_id]["expected"]))
        record = client.post("regression/case", {"workflow_id": task_id, "case_id": case_id})
        return {**record, "output": values, "run_state": result["state"]}

    try:
        report = asyncio.run(compare(suite, run, contract=contract_body))
        report.update(model=model, baseline_policy=baseline, candidate_policy=candidate)
        write_report(report, root / "comparison")
        click.echo(f"Saved paired report: {root / 'comparison.html'}")
        if not report["passed"]:
            raise click.exceptions.Exit(1)
    finally:
        store.close()


@control.command("experiment-worker")
@click.option("--adapter", required=True)
@click.option("--callable", "reference", required=True, help="Trusted installed module:function")
@click.option("--worker-id", required=True)
@click.option("--state", default="~/.reins/experiment-worker")
@click.option("--once", is_flag=True)
@click.pass_context
def experiment_worker(ctx, adapter, reference, worker_id, state, once):
    """Run registered agent experiments; provider secrets stay on this worker."""
    import time

    from .worker import load_adapter, run_one

    function = load_adapter(reference)
    client = Client(**ctx.obj)
    try:
        while True:
            worked = run_one(client, adapter, worker_id, function, state)
            if once:
                break
            if not worked:
                time.sleep(2)
    except KeyboardInterrupt:
        pass


@control.command("experiment-recover")
@click.argument("saved_result", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def experiment_recover(ctx, saved_result):
    """Resubmit a saved worker result without executing the Agent again."""
    from .worker import recover

    click.echo(json.dumps(recover(Client(**ctx.obj), saved_result)))


@control.command("run-deployed")
@click.argument("deployment")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--callable", "reference", required=True)
@click.option("--customer", required=True)
@click.option("--output", required=True, type=click.Path())
@click.pass_context
def run_deployed(ctx, deployment, input_file, reference, customer, output):
    """Start a new task with the manually adopted immutable configuration."""
    import asyncio
    import inspect
    from uuid import uuid4

    from reins.projects.documents import atomic_json

    from .client import Workflow
    from .worker import load_adapter

    path = Path(output)
    if path.exists():
        raise click.ClickException("Output already exists; inspect saved work before rerunning")
    function = load_adapter(reference)
    client = Client(**ctx.obj)
    run = client.post(
        "console/deployments/start",
        {"id": deployment, "workflow_id": str(uuid4()), "customer_id": customer},
    )
    atomic_json(path, {"state": "dispatching", **run})
    value = function(
        json.loads(Path(input_file).read_text()),
        run["configuration"]["config"],
        Workflow(client, run["context"]),
    )
    if inspect.isawaitable(value):
        value = asyncio.run(value)
    atomic_json(path, {"state": "returned", **run, "output": value})
    click.echo(str(path))


@control.command("gateway-serve")
@click.option("--manifest", required=True, type=click.Path(exists=True))
@click.option("--cert", required=True, type=click.Path(exists=True))
@click.option("--key", required=True, type=click.Path(exists=True))
@click.option("--host", default="127.0.0.1")
@click.option("--port", type=click.IntRange(1, 65535), default=8843)
def gateway_serve(manifest, cert, key, host, port):
    """Serve isolated tenant workspaces over TLS, with remote worker access."""
    from .gateway import Gateway

    server = Gateway(manifest, cert, key, host=host, port=port)
    click.echo("Tenant gateway: " + server.origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


@control.command("gateway-init")
@click.option("--directory", required=True, type=click.Path())
@click.option("--origin", required=True)
@click.option("--tenant", multiple=True, required=True)
def gateway_init(directory, origin, tenant):
    """Generate private, distinct credentials for each tenant and role."""
    import re
    from urllib.parse import urlparse

    parsed = urlparse(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise click.ClickException("Use an HTTPS origin without a path")
    if len(set(tenant)) != len(tenant) or any(
        not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", t) for t in tenant
    ):
        raise click.ClickException(
            "Use distinct alphanumeric tenant names (hyphen/underscore allowed)"
        )
    root = Path(directory).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = root / "tenants.json"
    config = {"origin": origin.rstrip("/"), "tenants": []}
    for t in tenant:
        config["tenants"].append(
            dict(
                id=t,
                database=t + ".sqlite",
                operator_token=secrets.token_urlsafe(48),
                worker_token=secrets.token_urlsafe(48),
            )
        )
    # Never overwrite existing credentials or ledgers.
    fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)
    click.echo("Private tenant manifest: " + str(manifest))
    click.echo("Use its role-specific credentials with your TLS gateway. Values were not printed.")
