# Hosted worker integration reference

The public package is `reins-sdk` (`from reins_sdk import ReinsClient, Charge`). Python 3.10+ and no runtime third-party dependencies. Install from `https://github.com/catyans/reins-sdk`; `REINS_BASE_URL` is the supplied HTTPS worker API root ending in `/api` or `/v1`. `REINS_WORKER_TOKEN` is tenant-specific and backend-only. A browser pairing token is not a worker token. Reins's operator creates the task and approves its deployment before the worker calls it; the SDK does not create operator-owned tasks or expose service code.

## One synchronous paid model call

```python
client = ReinsClient(os.environ["REINS_BASE_URL"], token=os.environ["REINS_WORKER_TOKEN"])


def execute(config):
    response = existing_provider_call(model=config["model"], prompt=prompt)
    return Charge(
        result=response.output, actual_cost=response.cost_usd,
        usage={"input_tokens": response.input_tokens,
               "output_tokens": response.output_tokens},
        validated=existing_business_check(response.output),
        validation_name="business-check-v1",
    )

charge = client.run_paid_call(workflow_id, execute, max_cost="0.10")
return charge.result
```

Replace the provider and validation functions with real application code. `max_cost` must cover the worst allowed charge of this operation, including provider-internal retries. The callback receives the pinned configuration; do not quietly substitute another model. A named failed validation is recorded and can affect future decisions. `success` means the provider operation succeeded; `validated` means the business result passed the customer's check.

`run_paid_call` submits `Charge.result` to the Reins service. If the result contains customer-sensitive content that must not leave their environment, use explicit calls and settle with a redacted result or `None` while preserving the local output.

## Explicit boundary (including async Agent code)

1. `plan = client.decision(workflow_id)`; proceed only if `plan["action"]` is `continue` or `escalate`. Use `plan["configuration"]["config"]` for the provider configuration.
2. Generate one stable `request_id` per actual paid operation and call `client.reserve(task_id=..., request_id=..., model=..., max_cost=...)`. Dispatch only when its `decision` is `continue`.
3. Call the customer's provider once. On a dispatched exception or unknown provider charge, call `client.uncertain(request_id)` and keep the original error. An unknown charge is not free.
4. Call `client.settle(request_id=..., actual_cost=..., success=..., validated=..., validation_name=..., usage=..., result=...)` with actual measured values. If acknowledgement is lost, retain the request ID and reconcile; do not dispatch another call.
5. `client.status(workflow_id)` returns the saved task record. `client.progress(task_id, stage, completed, total)` records measured progress.

The SDK is synchronous. In async code, run network-bound SDK calls in a thread (for example `await asyncio.to_thread(client.decision, workflow_id)`) or use an existing approved async HTTP adapter with the same API contract. Never block the event loop with a slow network request. Do not put a worker token into browser JavaScript.

## Verification and limits

Use a fake provider, not a real API key, in the first integration test. Exercise accepted, stopped, held, provider exception, and settlement-unknown paths. Compare the business result and side effects against the pre-integration path. The SDK does not automatically intercept framework calls or create a hosted task. A denied reservation must prevent the paid call; a dispatched call can still be billed even if its output fails validation. Reins only controls operations connected to this SDK.
