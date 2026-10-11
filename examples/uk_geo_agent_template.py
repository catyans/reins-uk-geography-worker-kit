"""Replace this template with the real Agent's implementation.

The worker passes only the input and the chosen configuration. It does not
pass the human-reviewed expected answer. Never return a canned decision in a
real experiment.
"""


def run(case_input, configuration, workflow):
    model = configuration["model"]

    def paid_operation():
        # Call the UK geography Agent/provider here and read the provider's
        # actual output, token usage and USD charge. Return (output, cost).
        raise NotImplementedError("Connect the real UK geography Agent here")

    return workflow.call(
        paid_operation,
        model=model,
        category="model",
        max_cost="0.10",  # Replace with a conservative bound for this model.
        validator=lambda output: isinstance(output, dict)
        and output.get("decision") in {"automatic_pass", "human_review"},
        validation_name="uk-geography-decision-v1",
        operation_inputs={
            "case_id": case_input.get("case_id"),
            "source_version": case_input.get("source_version"),
        },
    )
