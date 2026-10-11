"""Use explicit calls when a task has several model or paid-tool operations."""

import os
from uuid import uuid4

from reins_sdk import ReinsClient

client = ReinsClient(os.environ["REINS_BASE_URL"], token=os.environ["REINS_WORKER_TOKEN"])
workflow_id = "customer-task-id"  # Created by the Reins operator first.
plan = client.decision(workflow_id)
if plan["action"] in ("continue", "escalate"):
    model = plan["configuration"]["config"]["model"]
    request_id = str(uuid4())
    admission = client.reserve(
        task_id=workflow_id,
        request_id=request_id,
        model=model,
        max_cost="0.10",
    )
    if admission["decision"] == "continue":
        # Execute your own paid provider call here. If its charge is unknown,
        # call client.uncertain(request_id) instead of claiming zero cost.
        pass
