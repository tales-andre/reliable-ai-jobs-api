"""Which errors are safe to retry, and how long to wait."""
from __future__ import annotations

import random
from dataclasses import dataclass

# outcome of a failed call from the point of view of the bill
NOT_CHARGED = "not_charged"   # the provider did not do the work (429, 5xx, request never sent)
UNKNOWN = "unknown"           # the work may have been done and charged (timeout, connection lost)


@dataclass
class ProviderError(Exception):
    kind: str                 # rate_limited | server_error | connect_failed | timeout | connection_lost | bad_request | auth
    message: str
    billing: str = NOT_CHARGED
    status: int | None = None
    retry_after_s: float | None = None

    def __str__(self) -> str:
        return f"{self.kind}: {self.message}"


TRANSIENT_NOT_CHARGED = {"rate_limited", "server_error", "connect_failed"}
TRANSIENT_UNKNOWN = {"timeout", "connection_lost"}


def is_retryable(err: ProviderError, provider_dedupes: bool) -> bool:
    """Retry only when a second try cannot make things worse.

    * 429, 5xx and "could not connect" are transient and nothing was charged: retry.
    * A timeout or a dropped connection may mean the provider did the work. Retrying
      is only safe if the provider deduplicates by idempotency key; otherwise a retry
      could pay for the same work twice, so the job fails with a clear message.
    * Anything else (400, 401, 403, 404, 422...) will fail the same way again: no retry.
    """
    if err.kind in TRANSIENT_NOT_CHARGED:
        return True
    if err.kind in TRANSIENT_UNKNOWN:
        return provider_dedupes
    return False


def backoff_delay(attempt: int, base_s: float, cap_s: float, rng: random.Random,
                  retry_after_s: float | None = None) -> float:
    """Exponential backoff with full jitter; never earlier than the provider's Retry-After.

    attempt is the number of the attempt that just failed (1 for the first).
    """
    ceiling = min(cap_s, base_s * (2 ** (attempt - 1)))
    delay = rng.uniform(0, ceiling)
    if retry_after_s is not None:
        delay = max(delay, retry_after_s)
    return delay


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After in seconds (the HTTP-date form is not used by the providers here)."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
