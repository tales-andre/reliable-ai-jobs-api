"""Cost estimate made before a call. It is deliberately on the high side."""
from __future__ import annotations

import math

# Real tokenizers average around 4 characters per English token. The estimate
# assumes 3, so the reservation is usually larger than the real cost.
ESTIMATE_CHARS_PER_TOKEN = 3


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / ESTIMATE_CHARS_PER_TOKEN))


def estimate_cost_micros(prompt: str, max_tokens: int, price_in: int, price_out: int) -> int:
    """Worst case for one call: the whole prompt plus every allowed output token."""
    return estimate_tokens(prompt) * price_in + max_tokens * price_out


def fmt_usd(micros: int) -> str:
    return f"${micros / 1_000_000:.4f}"
