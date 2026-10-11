"""Sketch of one controlled sub-agent call; replace the provider callback.

Run only after your Reins deployment, worker credential, and provider client
have been configured. No provider key is sent to Reins.
"""

import os

from reins_sdk import Charge, ReinsClient

client = ReinsClient(os.environ["REINS_BASE_URL"], token=os.environ["REINS_WORKER_TOKEN"])
workflow_id = client.start_run()["workflow_id"]
researcher_id = client.child_task(workflow_id, budget="0.20")

# Your own tokenizer or prompt builder estimates where the input budget went.
client.context_sample(
    researcher_id,
    revision="research-prompt-v1",
    components={"system": 320, "tool_schema": 180, "history": 400, "user": 60},
)


def call_provider(config):
    response = your_provider.generate(model=config["model"], prompt="...")  # noqa: F821
    return Charge(
        result=response.output,
        actual_cost=response.cost_usd,
        usage={"input_tokens": response.input_tokens, "output_tokens": response.output_tokens},
    )


answer = client.run_with_fallback(
    workflow_id,
    call_provider,
    task_id=researcher_id,
    max_cost_for_model=lambda model: "0.10" if model.endswith("lite") else "0.20",
    validator=lambda result: bool(result and result.get("sources")),
    validation_name="source-backed-answer-v1",
    operation_inputs={"question_id": "example-1", "source_revision": "2026-10-11"},
)
print(answer.result)
