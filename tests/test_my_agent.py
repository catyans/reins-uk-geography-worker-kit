import os
import asyncio
import sys
import types
import unittest
from unittest.mock import patch

import my_agent


class AdapterHandoffTests(unittest.TestCase):
    def test_entrypoint_is_required(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "UK_GEO_ENTRYPOINT"):
                my_agent.run({}, {}, object())

    def test_input_config_and_workflow_pass_through(self):
        module = types.ModuleType("uk_geo_test_target")
        seen = []

        def run(case_input, configuration, workflow):
            seen.append((case_input, configuration, workflow))
            return {"decision": "human_review"}

        module.run = run
        workflow = object()
        with patch.dict(sys.modules, {"uk_geo_test_target": module}):
            with patch.dict(os.environ, {"UK_GEO_ENTRYPOINT": "uk_geo_test_target:run"}):
                result = my_agent.run({"case_id": "one"}, {"model": "lite"}, workflow)
        self.assertEqual(result, {"decision": "human_review"})
        self.assertEqual(seen, [({"case_id": "one"}, {"model": "lite"}, workflow)])

    def test_async_agent_is_forwarded_to_worker(self):
        module = types.ModuleType("uk_geo_async_target")

        async def run(case_input, configuration, workflow):
            return {"decision": case_input["decision"]}

        module.run = run
        with patch.dict(sys.modules, {"uk_geo_async_target": module}):
            with patch.dict(os.environ, {"UK_GEO_ENTRYPOINT": "uk_geo_async_target:run"}):
                pending = my_agent.run({"decision": "automatic_pass"}, {}, object())
                result = asyncio.run(pending)
        self.assertEqual(result, {"decision": "automatic_pass"})


if __name__ == "__main__":
    unittest.main()
