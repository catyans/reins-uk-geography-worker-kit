"""Price text-only Gemini calls from provider-reported usage.

This calculates a paid-tier list-price estimate, not an invoice charge. The
application must supply the current rates for its exact model and tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class IncompleteGoogleUsage(ValueError):
    """Usage is missing or includes billing categories this helper cannot price."""


@dataclass(frozen=True)
class GoogleTextPrices:
    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Decimal

    def __post_init__(self):
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value < 0
            for value in (
                self.input_per_million,
                self.output_per_million,
                self.cached_input_per_million,
            )
        ):
            raise ValueError("Supply nonnegative finite Decimal prices")


def normalize_google_text_usage(usage: dict) -> dict[str, int]:
    """Return ledger token counts from native or OpenAI-compatible Gemini usage."""
    if not isinstance(usage, dict):
        raise IncompleteGoogleUsage("Missing provider usage")
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        incoming = usage.get("prompt_tokens")
        outgoing = usage.get("completion_tokens")
        details = usage.get("prompt_tokens_details") or {}
        output_details = usage.get("completion_tokens_details") or {}
        if not isinstance(details, dict) or not isinstance(output_details, dict):
            raise IncompleteGoogleUsage("Malformed token details")
        cached = details.get("cached_tokens", 0) or 0
        reasoning = output_details.get("reasoning_tokens", 0) or 0
        if any(details.get(k) for k in ("audio_tokens", "image_tokens", "cache_write_tokens")):
            raise IncompleteGoogleUsage("Non-text or cache-write pricing is unsupported")
        if output_details.get("audio_tokens"):
            raise IncompleteGoogleUsage("Audio output pricing is unsupported")
    else:
        for field in ("promptTokensDetails", "candidatesTokensDetails"):
            modalities = usage.get(field) or []
            if not isinstance(modalities, list) or any(
                not isinstance(item, dict) or item.get("modality") != "TEXT" for item in modalities
            ):
                raise IncompleteGoogleUsage("Non-text token pricing is unsupported")
        incoming = usage.get("promptTokenCount")
        candidates = usage.get("candidatesTokenCount")
        reasoning = usage.get("thoughtsTokenCount", 0)
        cached = usage.get("cachedContentTokenCount", 0)
        if any(type(n) is not int or n < 0 for n in (candidates, reasoning)):
            raise IncompleteGoogleUsage("Missing native output token counts")
        outgoing = candidates + reasoning
    if any(type(n) is not int or n < 0 for n in (incoming, outgoing, cached, reasoning)):
        raise IncompleteGoogleUsage("Missing or invalid token counts")
    if cached > incoming or reasoning > outgoing:
        raise IncompleteGoogleUsage("Cached or reasoning tokens exceed totals")
    return {
        "input_tokens": incoming,
        "output_tokens": outgoing,
        "cached_input_tokens": cached,
        "reasoning_tokens": reasoning,
    }


def estimate_google_text_cost(usage: dict, prices: GoogleTextPrices) -> Decimal:
    """Estimate USD using reported tokens and explicitly supplied model prices."""
    counts = normalize_google_text_usage(usage)
    incoming = counts["input_tokens"]
    cached = counts["cached_input_tokens"]
    return (
        (incoming - cached) * prices.input_per_million
        + cached * prices.cached_input_per_million
        + counts["output_tokens"] * prices.output_per_million
    ) / Decimal(1_000_000)
