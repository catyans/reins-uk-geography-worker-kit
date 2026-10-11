# UK geography evaluation cases

The 49 reviewed cases described in the pilot plan are **not in this kit**. Do not treat the sample schema as an approved case or a measured result.

For each real case, prepare:

- a stable case ID and source-data version;
- the exact input sent to the Agent, with sensitive content approved for the chosen worker environment;
- a human-reviewed expected decision (for example, automatic pass or human review);
- a rubric explaining which geometry errors require human review;
- a reviewer and approval date.

Keep expected answers outside the worker's input. The worker receives only the case input and a configuration; Reins compares its submitted output against the frozen expected answer after execution. Approve cases in **Approvals**, export a frozen test set, register adapter `uk-geography-v1`, and only then queue the comparison in **Experiments**.

Suggested input shape: `{"case_id":"...","source_version":"...","geometry_reference":"...","task":"assess_boundary"}`. Suggested output shape: `{"decision":"automatic_pass|human_review","reason":"...","evidence":[...]}`. These are proposed fields, not the current UK Agent's confirmed contract; align them with its real output before freezing the test set.
