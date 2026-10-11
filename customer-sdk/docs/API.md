# Worker API contract

The SDK is a thin client. Its request bodies match the hosted worker API; it does not decide whether a result is acceptable or calculate the provider's true bill.

| Method | Purpose | Important response |
| --- | --- | --- |
| `decision(workflow_id)` | Get pinned configuration and continue/escalate/stop | `action`, `configuration`, `budget_work_remaining` |
| `reserve(task_id, request_id, model, max_cost, ...)` | Ask permission before a paid operation; optional read-only result reuse | `decision` (`continue`, `pause`, `hold`, or `reuse`) |
| `settle(request_id, actual_cost, ...)` | Record the actual charge after dispatch | `status` |
| `uncertain(request_id)` | Keep an unknown provider charge visible | `status` |
| `status(workflow_id)` | Read the saved task record | `workflows` |
| `progress(task_id, stage, completed, total)` | Report measured task progress | `status` |
| `pairing()` | Check the customer account connected to a demo pairing | `customer_id`, `deployment_id` |
| `start_run(run_key=None)` | Start a new run from the paired customer's approved deployment | `workflow_id` |
| `child_task(parent_task_id, budget, task_id=None)` | Give a sub-agent its own cap under the parent's budget | Child task ID or `AdmissionDenied` |
| `context_sample(task_id, revision, components, ...)` | Record estimated input-token sources without prompt text | `components_are_estimates` |
| `run_with_fallback(...)` | Retry once only when a failed business check leads to a different server-approved model | Final `Charge` or a control error |

Use a unique, stable `request_id` for each actual provider call. If an HTTP response is lost, **do not create a new request ID and dispatch the provider call again**. Reins may already hold a reservation. Check the task record and reconcile the original request.

`run_paid_call()` covers one model operation with a pinned model in the plan. It requires your callback to return `Charge(result, actual_cost, usage)`. It does not support asynchronous callbacks; use the explicit methods in an async application. A `stop` or `hold` means the callback is not invoked. Reins can pause a task for a quality failure and request a new decision; it cannot undo a charge already incurred.

If you provide `Charge(validated=..., validation_name=...)`, Reins records the named business check. A failed named check can cause the hosted runtime to choose its qualified fallback or stop later calls. `success` describes the provider operation; it is not a substitute for the business check.

### Runtime cost controls

`reserve(..., operation_inputs=..., retry=True)` and `run_paid_call(..., operation_inputs=..., retry=True)` send a local SHA-256 operation identity, rather than raw inputs, so a server policy can detect repeat loops. The server enforces `repeat_limit`/`repeat_window_seconds` and failure limits **only when configured**. The SDK never silently retries a paid call; `retry=True` describes an intentional new attempt. An existing `request_id` that has already reserved is held rather than dispatched again.

`child_task(parent_task_id, budget=...)` creates a child task for another Agent. Use the returned ID for that Agent's `run_paid_call(task_id=...)`. The server checks the child cap, its ancestors and the shared workflow cap on every reservation. The pairing token is scoped to its own customer's tasks; older hosted servers may need the corresponding server update to allow child creation.

`run_with_fallback(workflow_id, execute, max_cost_for_model=..., validator=..., validation_name=...)` settles an unsuccessful business result with its **actual** provider charge, asks the hosted runtime for a new plan, and permits one more call only if it selected a different model. The server must have a qualified baseline for escalation. If the first call was not acknowledged or its settlement is unknown, no fallback runs. The bound callback receives the selected model and must return a conservative upper bound for that model. This helper does not promise a cheaper outcome; measure accepted cost across attempts.

`context_sample()` accepts estimated counts for `system`, `history`, `retrieval`, `tool_schema`, `user`, and `unattributed`. Pass the provider's observed input-token total separately. Because local token counting may differ from the provider's tokenizer, the components remain **estimates**; the provider total remains authoritative for billing. Use a stable `revision` for each prompt/tool-schema version and avoid sending raw prompts as metadata.

### Exact result reuse

`run_paid_call(..., reuse_inputs=..., read_only=True, validator=..., validation_name=...)` enables reuse for a read-only call. All four arguments are required. The SDK hashes the pinned model, validation version and JSON-serializable inputs; only the hash goes to the server. The server scopes reuse to the workflow and returns a previously settled result only when it was validated. The SDK runs the validator again on a hit. A hit returns a `Charge` with `reused_from` set and zero **new** cost; it does not erase the original charge. Failed validation is settled as a charged attempt and cannot be reused. For explicit calls, `reserve(..., cache_key=..., read_only=True)` exposes the same server protocol; the caller owns key construction and validation.

### Provider cache usage

`GoogleTextPrices(Decimal(input_rate), Decimal(output_rate), Decimal(cached_input_rate))` takes USD prices per million tokens. `normalize_google_text_usage(provider_usage)` returns `input_tokens`, `output_tokens`, `cached_input_tokens` and `reasoning_tokens` for settlement. `estimate_google_text_cost(provider_usage, prices)` returns a `Decimal` USD estimate. It rejects missing counts and recognized non-text usage rather than reporting a misleading zero. The helper does not enable Gemini context caching or reconcile the provider bill. Supply rates for the exact model and tier; do not treat a list-price estimate as a confirmed invoice debit.

For the isolated paired demo, a Reins operator approves the deployment and performs the first pairing. The worker then calls `start_run()` for each business run; no task ID needs to be entered by a person. Each run still receives a separate internal ID so its budget, outcome and replay remain independent. If the start response is lost, pass the same `run_key` on retry to avoid a duplicate task. The worker token cannot change operator settings. The browser pairing token is for the dashboard only. A single shared pilot workspace is **not** a production multi-customer tenant setup.
