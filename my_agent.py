"""Trusted handoff from the Reins experiment worker to the UK geography Agent.

Set UK_GEO_ENTRYPOINT to an installed ``module:function`` owned by the Agent
team. The function receives ``(case_input, configuration, workflow)`` and must
return a JSON-serializable dict. It must account for paid calls through workflow.
"""

import importlib
import os


def run(case_input, configuration, workflow):
    reference = os.environ.get("UK_GEO_ENTRYPOINT", "")
    module_name, separator, function_name = reference.partition(":")
    if not separator or not module_name or not function_name:
        raise RuntimeError("Set UK_GEO_ENTRYPOINT to an installed module:function")
    target = getattr(importlib.import_module(module_name), function_name)
    if not callable(target):
        raise TypeError("UK_GEO_ENTRYPOINT is not callable")
    return target(case_input, configuration, workflow)
