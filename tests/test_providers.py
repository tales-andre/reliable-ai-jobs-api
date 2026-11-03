"""OpenAI and Anthropic adapters against canned HTTP replies (no key, no network),
the overall call deadline, and the timeout defaults for real providers."""
from __future__ import annotations

import random
import time
from dataclasses import replace

import httpx
import pytest

from jobsapi import ledger
from jobsapi.config import Settings
from jobsapi.costs import estimate_cost_micros
from jobsapi.providers import AnthropicProvider, OpenAIProvider
from jobsapi.retry import NOT_CHARGED, UNKNOWN, ProviderError
from jobsapi.worker import claim, process

from .conftest import set_cap


def openai_body(tin: int, tout: int, text: str = "Your order ships today.") -> dict:
    return {"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": tin, "completion_tokens": tout}}


def make(cls, settings, handler):
    name = "openai" if cls is OpenAIProvider else "anthropic"
    return cls(replace(settings, provider=name, llm_model="test-model"), httpx.MockTransport(handler))


def add_job(conn, settings, key: str, prompt: str, max_tokens: int):
    est = estimate_cost_micros(prompt, max_tokens, settings.price_in_micros, settings.price_out_micros)
    return conn.execute(
        "INSERT INTO jobs (idempotency_key, request_hash, prompt, max_tokens, max_attempts, estimate_micros) "
        "VALUES (%s, 'h', %s, %s, 5, %s) RETURNING id, estimate_micros", (key, prompt, max_tokens, est)).fetchone()


def test_openai_adapter_reads_usage_and_maps_errors(settings):
    replies = iter([
        httpx.Response(200, json=openai_body(12, 30)),
        httpx.Response(429, headers={"retry-after": "2"}, json={}),
        httpx.Response(503, json={}),
        httpx.Response(401, json={}),
        httpx.Response(400, json={"error": "bad model"}),
    ])

    def handler(request):
        assert request.headers["authorization"].startswith("Bearer ")
        return next(replies)

    p = make(OpenAIProvider, settings, handler)
    gen = p.generate("Hello", 50, "job-1")
    assert (gen.input_tokens, gen.output_tokens, gen.cost_micros) == (12, 30, 12 * 10 + 30 * 10)
    expected = [("rate_limited", NOT_CHARGED, 2.0), ("server_error", NOT_CHARGED, None),
                ("auth", NOT_CHARGED, None), ("bad_request", NOT_CHARGED, None)]
    for kind, billing, retry_after in expected:
        with pytest.raises(ProviderError) as e:
            p.generate("Hello", 50, "job-1")
        assert (e.value.kind, e.value.billing, e.value.retry_after_s) == (kind, billing, retry_after)


def test_anthropic_adapter_reads_text_blocks_and_usage(settings):
    def handler(request):
        assert request.headers["anthropic-version"]
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Hi "},
                                                     {"type": "text", "text": "there"}],
                                         "usage": {"input_tokens": 9, "output_tokens": 4}})
    gen = make(AnthropicProvider, settings, handler).generate("Hello", 50, "job-1")
    assert (gen.text, gen.cost_micros) == ("Hi there", 9 * 10 + 4 * 10)


def test_read_timeout_means_maybe_charged(settings):
    def handler(request):
        raise httpx.ReadTimeout("no reply", request=request)
    with pytest.raises(ProviderError) as e:
        make(OpenAIProvider, settings, handler).generate("Hello", 50, "job-1")
    assert (e.value.kind, e.value.billing) == ("timeout", UNKNOWN)


class Trickle(httpx.SyncByteStream):
    """A reply that arrives a few bytes at a time: no single read is slow, the whole is."""
    def __iter__(self):
        for _ in range(20):
            time.sleep(0.05)
            yield b" "


def test_slow_trickling_reply_stops_at_the_overall_deadline(settings):
    s = replace(settings, provider_timeout_s=0.3)
    p = make(OpenAIProvider, s, lambda request: httpx.Response(200, stream=Trickle()))
    t0 = time.monotonic()
    with pytest.raises(ProviderError) as e:
        p.generate("Hello", 50, "job-1")
    assert (e.value.kind, e.value.billing) == ("timeout", UNKNOWN)
    assert time.monotonic() - t0 < 2 * s.provider_timeout_s      # well inside the lease


def test_short_prompt_estimate_covers_chat_overhead(conn, settings):
    """'Hi' is one token of text, but a chat request adds formatting tokens (the API
    reports 8 input tokens here). The estimate leaves room for that."""
    job = add_job(conn, settings, "hi-1", "Hi", 10)
    set_cap(conn, job["estimate_micros"])
    p = make(OpenAIProvider, settings, lambda request: httpx.Response(200, json=openai_body(8, 10)))
    assert process(conn, claim(conn, "w1", settings.lease_s), p, settings, "w1", random.Random(0)) == "ok"
    b = ledger.snapshot(conn)
    assert b.spent_micros == 8 * 10 + 10 * 10 <= b.cap_micros


def test_usage_above_the_estimate_is_recorded_and_blocks_the_next_job(conn, settings):
    """If a real provider reports more usage than estimated, the real cost is recorded
    (spent passes the cap by that difference) and no further call is made."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=openai_body(500, 10))   # far more input tokens than estimated
    p = make(OpenAIProvider, settings, handler)
    first = add_job(conn, settings, "big-1", "Summarise the attached ticket thread.", 10)
    set_cap(conn, first["estimate_micros"])
    assert process(conn, claim(conn, "w1", settings.lease_s), p, settings, "w1", random.Random(0)) == "ok"
    b = ledger.snapshot(conn)
    assert b.spent_micros == 500 * 10 + 10 * 10 > b.cap_micros
    reason = conn.execute("SELECT reason FROM ledger WHERE job_id = %s AND kind = 'close'",
                          (first["id"],)).fetchone()["reason"]
    assert "above the reservation" in reason

    second = add_job(conn, settings, "big-2", "Hi", 10)
    assert process(conn, claim(conn, "w1", settings.lease_s), p, settings, "w1",
                   random.Random(0)) == "budget_exceeded"
    err = conn.execute("SELECT error FROM jobs WHERE id = %s", (second["id"],)).fetchone()["error"]
    assert "Provider not called" in err and "over the cap" in err
    assert len(calls) == 1


def test_timeout_and_lease_defaults_depend_on_the_provider(monkeypatch):
    monkeypatch.delenv("PROVIDER_TIMEOUT", raising=False)
    monkeypatch.delenv("LEASE_SECONDS", raising=False)
    monkeypatch.setenv("PROVIDER", "fake")
    s = Settings.from_env()
    assert (s.provider_timeout_s, s.lease_s) == (1.0, 4.0)
    monkeypatch.setenv("PROVIDER", "anthropic")
    monkeypatch.setenv("PROVIDER_TIMEOUT", "")                 # docker compose passes unset values as ""
    s = Settings.from_env()
    assert (s.provider_timeout_s, s.lease_s) == (60.0, 121.0)
    monkeypatch.setenv("PROVIDER_TIMEOUT", "90")
    with pytest.raises(ValueError, match="LEASE_SECONDS"):
        Settings.from_env()                                    # 121 < 2 * 90 + 1
