"""Cost estimate made before a call. It is deliberately on the high side.

It is an estimate, not a proof: a real tokenizer can still count more tokens than
this for unusual text. When that happens the ledger records the real cost anyway
(see ledger.close), the overshoot is visible in the budget, and no new call is
reserved until there is room under the cap again.
"""
from __future__ import annotations

import math

# Real tokenizers average around 4 characters per English token. The estimate
# assumes 3 for ASCII text, so the reservation is usually larger than the real cost.
ESTIMATE_CHARS_PER_TOKEN = 3
# Text outside ASCII (accents, other scripts) often takes a token per character or
# more; emoji and other characters outside the Basic Multilingual Plane take several.
NON_ASCII_TOKENS_PER_CHAR = 2
ASTRAL_TOKENS_PER_CHAR = 4
# Chat formats add tokens around each message (role markers, separators).
REQUEST_OVERHEAD_TOKENS = 20


def estimate_tokens(text: str) -> int:
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    astral = sum(1 for ch in text if ord(ch) > 0xFFFF)
    other = len(text) - ascii_chars - astral
    return (math.ceil(ascii_chars / ESTIMATE_CHARS_PER_TOKEN) + other * NON_ASCII_TOKENS_PER_CHAR
            + astral * ASTRAL_TOKENS_PER_CHAR + REQUEST_OVERHEAD_TOKENS)


def estimate_cost_micros(prompt: str, max_tokens: int, price_in: int, price_out: int) -> int:
    """High estimate for one call: the whole prompt plus every allowed output token."""
    return estimate_tokens(prompt) * price_in + max_tokens * price_out


def fmt_usd(micros: int) -> str:
    return f"${micros / 1_000_000:.4f}"
