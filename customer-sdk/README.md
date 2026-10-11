# Reins Python SDK

[简体中文](README.zh-CN.md)

A small, backend-only client for the hosted Reins worker API. It lets your existing Agent ask whether a task may continue, reserve a maximum cost before a paid model call, report the actual charge afterward, and inspect the task record. **Your model provider calls and API keys remain in your application.** This repository contains no Reins server, policy engine, database, dashboard, or deployment credentials.

## Install

Python 3.10+; the SDK has no runtime dependencies.

```bash
python -m pip install git+https://github.com/catyans/reins-sdk.git
```

For the isolated demo, run `python -m reins_sdk.pair` to generate a private code, then give it to the Reins operator to pair in Settings using one Hosted task. Pair once; afterward `ReinsClient.start_run()` creates each new run and returns its internal ID automatically. See the [skill installation and pairing guide](docs/SKILL_INSTALL.md). For other hosted workspaces, your Reins contact supplies a worker token and HTTPS API base URL privately. Keep the credential in your backend secret store, not JavaScript, a mobile app, or a source file. The browser pairing token is different and cannot be used here.

Your hosted API base may end in `/api` or `/v1` (for example, `https://reins.example.com/api`). Use the exact URL supplied for your workspace. A task and its approved configuration must be prepared on the Reins side before the worker can run it.

## First paid call

```python
import os
from reins_sdk import Charge, ReinsClient

client = ReinsClient(
    os.environ["REINS_BASE_URL"],
    token=os.environ["REINS_WORKER_TOKEN"],
)

def call_your_provider(config):
    # Use your existing provider client and its actual usage/cost reporting.
    response = your_provider.generate(model=config["model"], prompt="...")
    return Charge(
        result=response.output,
        actual_cost=response.cost_usd,
        usage={"input_tokens": response.input_tokens, "output_tokens": response.output_tokens},
        validated=your_business_check(response.output),
        validation_name="business-result-v1",
    )

outcome = client.run_paid_call(
    workflow_id="task-created-in-reins",
    execute=call_your_provider,
    max_cost="0.10",  # Conservative upper bound in USD, not average cost.
)
print(outcome.result)
```

`run_paid_call` requests the task's pinned configuration, reserves the upper bound, and calls your provider only if Reins admits the operation. It then records the actual charge. It never retries a paid call. If the provider response or charge is uncertain, the reservation stays visible for reconciliation. If settlement cannot be acknowledged, it raises `SettlementUnknown` with the request ID. Do not repeat the provider call to resolve that error.

## Reuse an unchanged, verified result

For a **read-only** operation, pass the exact business inputs and a versioned check. Reins hashes the inputs locally and reuses only a previously settled, validated result **within the same run**. The SDK checks the returned result again. A cache hit skips the provider callback and returns `Charge(reused_from=..., actual_cost="0")`; it does not create a second paid request.

```python
result = client.run_paid_call(
    workflow_id,
    call_your_provider,
    max_cost="0.10",
    reuse_inputs={"document_id": document_id, "source_revision": source_revision},
    read_only=True,
    validator=lambda output: your_business_check(output),
    validation_name="business-result-v2",
)
if result.reused_from:
    print("Reused verified result from", result.reused_from)
```

Include every input that can change the answer, such as source revision, prompt version and tool output. Change `validation_name` when the check changes. Never enable reuse for actions with side effects. A failed check is still charged but cannot enter the result cache. The server may refuse even a reuse request when the task itself is stopped or over budget.

Gemini's **provider context cache is separate** from this result cache. The SDK does not create provider caches. It can price text-only usage that reports cached input tokens using `normalize_google_text_usage()` and `estimate_google_text_cost()` with explicit current prices; pass the normalized counts in `Charge.usage`. The returned amount is a list-price estimate until reconciled with the provider invoice. [Usage and safety details](docs/API.md).

For a multi-step Agent, use `decision()`, `reserve()`, `settle()`, `uncertain()`, `progress()`, and `status()` directly. [Explicit-call example](examples/explicit_calls.py) shows the boundary. To adapt an existing customer repository, install the [integration skill](docs/SKILL_INSTALL.md). The [API guide](docs/API.md) describes request IDs, budget decisions and error handling. The SDK only controls calls you connect to it.

The SDK also supports [sub-agent budgets, repeat-loop detection, one-step quality fallback, and context-token attribution](docs/API.md#runtime-cost-controls). These use server-side policy and the customer's business validator; the SDK reports the facts and obeys admission decisions. [Example integration](examples/runtime_controls.py) shows them together. Provider context-cache hits are separately priced from provider-reported usage as described above.

## What is sent

The worker API receives the task ID, selected model/configuration, estimated and actual USD cost, optional token counts, and the result you choose to record. The SDK does not upload a prompt or document automatically. The `result` argument to `settle()` **does** go to Reins and is saved there; omit or redact sensitive output if your agreement requires it. Your provider keys never go to Reins.

## Test locally

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Tests use a local fake HTTP service and do not call a model provider or spend money. This client is synchronous and uses Python's standard library. It never disables TLS certificate verification and never sends its token through an environment-configured proxy.

Apache-2.0 licensed. The hosted Reins service is separate and is **not** part of this repository.
