"""Unit tests: which errors are retried, and the backoff delay."""
import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jobsapi.retry import NOT_CHARGED, UNKNOWN, ProviderError, backoff_delay, is_retryable, parse_retry_after


@pytest.mark.parametrize("kind,billing,dedupes,expected", [
    ("rate_limited", NOT_CHARGED, False, True),
    ("server_error", NOT_CHARGED, False, True),
    ("connect_failed", NOT_CHARGED, False, True),
    ("timeout", UNKNOWN, True, True),            # provider deduplicates: a retry cannot pay twice
    ("timeout", UNKNOWN, False, False),          # it might pay twice: do not retry
    ("connection_lost", UNKNOWN, True, True),
    ("connection_lost", UNKNOWN, False, False),
    ("bad_request", NOT_CHARGED, True, False),   # will fail the same way again
    ("auth", NOT_CHARGED, True, False),
])
def test_retry_classification(kind, billing, dedupes, expected):
    assert is_retryable(ProviderError(kind, "x", billing), dedupes) is expected


@given(attempt=st.integers(1, 30), base=st.floats(0.01, 5), cap=st.floats(0.01, 60), seed=st.integers())
def test_backoff_stays_within_exponential_ceiling(attempt, base, cap, seed):
    d = backoff_delay(attempt, base, cap, random.Random(seed))
    assert 0 <= d <= min(cap, base * 2 ** (attempt - 1)) + 1e-9


@given(attempt=st.integers(1, 30), retry_after=st.floats(0, 120), seed=st.integers())
def test_backoff_never_earlier_than_retry_after(attempt, retry_after, seed):
    assert backoff_delay(attempt, 0.2, 3.0, random.Random(seed), retry_after) >= retry_after


def test_jitter_spreads_retries():
    rng = random.Random(1)
    delays = {round(backoff_delay(4, 0.2, 3.0, rng), 3) for _ in range(50)}
    assert len(delays) > 40   # workers retrying at the same moment would hit the provider together


def test_parse_retry_after():
    assert parse_retry_after("2") == 2.0
    assert parse_retry_after("0.5") == 0.5
    assert parse_retry_after(None) is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") is None
