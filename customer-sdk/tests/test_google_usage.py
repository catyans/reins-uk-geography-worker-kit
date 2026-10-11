"""Usage pricing requires reported token counts and explicit rates."""

import unittest
from decimal import Decimal

from reins_sdk import (
    GoogleTextPrices,
    IncompleteGoogleUsage,
    estimate_google_text_cost,
    normalize_google_text_usage,
)


class GoogleUsageTests(unittest.TestCase):
    def setUp(self):
        self.prices = GoogleTextPrices(Decimal("0.10"), Decimal("0.40"), Decimal("0.01"))

    def test_cached_input_is_priced_once_at_cached_rate(self):
        usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 800},
        }
        self.assertEqual(
            normalize_google_text_usage(usage),
            {
                "input_tokens": 1000,
                "output_tokens": 100,
                "cached_input_tokens": 800,
                "reasoning_tokens": 0,
            },
        )
        self.assertEqual(estimate_google_text_cost(usage, self.prices), Decimal("0.000068"))

    def test_native_usage_includes_thoughts_in_output(self):
        usage = {
            "promptTokenCount": 100,
            "candidatesTokenCount": 20,
            "thoughtsTokenCount": 10,
            "cachedContentTokenCount": 50,
        }
        self.assertEqual(normalize_google_text_usage(usage)["output_tokens"], 30)
        self.assertEqual(estimate_google_text_cost(usage, self.prices), Decimal("0.0000175"))

    def test_reject_missing_and_non_text_usage(self):
        cases = [
            {},
            {"prompt_tokens": 10},
            {
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 11},
            },
            {
                "promptTokenCount": 10,
                "candidatesTokenCount": 1,
                "promptTokensDetails": [{"modality": "AUDIO", "tokenCount": 10}],
            },
        ]
        for usage in cases:
            with self.subTest(usage=usage), self.assertRaises(IncompleteGoogleUsage):
                estimate_google_text_cost(usage, self.prices)


if __name__ == "__main__":
    unittest.main()
