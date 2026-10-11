# UK geography · Reins experiment handoff

This folder is the set of files to give the UK geography Agent engineer. It contains an installable snapshot of the Reins experiment-worker CLI (`worker-runtime/`), the customer SDK and integration skill (`customer-sdk/`), a small Agent entry-point wrapper (`my_agent.py`), and a case-format guide (`cases/`). No live credential or actual UK geography case is included.

**Current state:** `uk-geography-20261011` is paired to the customer SDK, but it is pinned to the `synthetic-extraction` demonstration deployment (`demo/lite`). That task is useful for checking the connection; it is **not** the UK geography experiment or a real geography deployment. Adapter `uk-geography-v1`, the proposed 49-case set, and the actual Agent function have not been supplied or registered yet.

## What the engineer does

1. Install Python 3.10 or newer. From this folder, create a virtual environment and install both local packages:

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   python -m pip install ./worker-runtime ./customer-sdk
   ```

2. Implement an installed Python function `run(case_input, configuration, workflow)` in the UK geography Agent project. Set `UK_GEO_ENTRYPOINT` to its `module:function` name. `my_agent.py` forwards the three arguments unchanged. The result must be a JSON-serializable object matching the reviewed rubric. Each paid provider or tool call must use `workflow.call(...)` (or its async equivalent), which reserves a cost bound **before** dispatch and settles actual cost afterward. Do not make untracked paid calls inside the adapter.

3. Supply real reviewed cases and expected decisions using [the case guide](cases/README.md). The worker does not receive expected answers. The Reins operator approves the cases, freezes the test set, registers adapter `uk-geography-v1`, saves a budget/failure policy, and queues the experiment. These console steps are needed before the worker has jobs to claim.

4. Obtain a **separate experiment-worker credential** from the Reins operator through a private channel. The customer SDK pairing code and the browser's one-time pairing token do not authorize experiment jobs. Keep the credential outside this folder, set `REINS_EXPERIMENT_WORKER_TOKEN_FILE` to its path, and provide the Agent's provider key through its existing secret mechanism. Do not commit either secret.

5. Start one claim attempt to check connectivity, then run continuously when the experiment is ready:

   ```bash
   export UK_GEO_ENTRYPOINT=uk_geo_agent:run
   export REINS_EXPERIMENT_WORKER_TOKEN_FILE=/private/path/worker.token
   ./run_worker.sh --once
   ./run_worker.sh
   ```

   `--once` exits quietly when no experiment job has been queued; a quiet exit does not mean the Agent ran.

   The script uses `https://47.245.114.167:8444` as its default **origin**. Do not append `/api` to `REINS_CONTROL_URL`; this CLI adds `/v1/` itself. It writes a local journal in `worker-state/`. Keep the journal so a response lost after a paid call can be recovered without repeating that call. Set `REINS_WORKER_STATE` to a private durable path for real runs.

The included [integration skill](customer-sdk/skills/reins-integrate/SKILL.md) can help an engineer connect existing model calls. The [customer SDK guide](customer-sdk/README.md) covers ordinary runtime calls; the experiment-worker CLI is in `worker-runtime/` and uses its own credential.

## Before sharing

This bundle includes proprietary Reins source under [its license](worker-runtime/LICENSE). Check that the recipient is authorized to receive it. It intentionally omits the live server database, operator/worker tokens, browser pairing token, provider API keys, and the internal 49-case dataset. The `env.example` contains only placeholders. Any decision about giving a worker credential is separate from sharing code.
