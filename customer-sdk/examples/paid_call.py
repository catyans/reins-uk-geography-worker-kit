"""One existing provider call with a Reins admission boundary.

Set REINS_BASE_URL and REINS_WORKER_TOKEN in your backend environment.
The provider function is intentionally a placeholder: keep your real provider
SDK and API key in your own application.
"""

import os

from reins_sdk import Charge, ReinsClient


def existing_provider_call(prompt: str, model: str) -> Charge:
    raise NotImplementedError(
        "Call your existing provider here and return Charge(output, actual_cost, usage)."
    )


def handle_task(workflow_id: str, prompt: str):
    client = ReinsClient(
        os.environ["REINS_BASE_URL"],
        token=os.environ["REINS_WORKER_TOKEN"],
    )
    charge = client.run_paid_call(
        workflow_id,
        lambda config: existing_provider_call(prompt, config["model"]),
        max_cost="0.10",  # Replace with a conservative upper bound in USD.
    )
    return charge.result
