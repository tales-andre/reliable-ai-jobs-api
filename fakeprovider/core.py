"""A deliberately unreliable text-generation provider, deterministic for a given seed.

What can happen to a call (chosen from the seed, the idempotency key and how many
times that key was seen, so a rerun with the same seed makes the same choices):

  ok            reply at once, charge for it
  slow          reply after a short delay, charge for it
  timeout       do the work and charge for it, but reply later than the client waits
  rate_limited  429 with Retry-After, no charge
  server_error  500, no charge

Stored replies (not a fault, a feature real providers may or may not have): when
a key that already has a finished reply comes back (the client retried after a
timeout), the stored reply is returned again with "replayed": true and no new
charge. Without an Idempotency-Key every call is
new work and is charged; this is what a naive client gets.

A prompt containing "[reject]" always gets 400 (a permanent error to test with).
"""
from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass, field

DEFAULT_RATES = {"timeout": 0.0, "rate_limited": 0.0, "server_error": 0.0, "slow": 0.0}
CHAOS_RATES = {"timeout": 0.12, "rate_limited": 0.12, "server_error": 0.10, "slow": 0.15}

WORDS = ("the order ships within two business days and tracking is sent by email once the "
         "parcel leaves the warehouse returns are accepted for thirty days in original packaging").split()


@dataclass
class Action:
    status: int
    body: dict
    headers: dict = field(default_factory=dict)
    delay_s: float = 0.0
    fault: str = "ok"


@dataclass
class Charge:
    key: str | None
    ref: str
    job_ref: str
    cost_micros: int
    at: float


def count_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


class FakeProviderCore:
    def __init__(self, seed: int = 0, rates: dict | None = None, price_in: int = 10, price_out: int = 10,
                 timeout_delay_s: float = 1.6, slow_range_s: tuple[float, float] = (0.2, 0.6),
                 retry_after_s: int = 1, script: dict[str, list[str]] | None = None,
                 fixed_delay_s: float = 0.0):
        self.seed = seed
        self.rates = {**DEFAULT_RATES, **(rates or {})}
        self.price_in, self.price_out = price_in, price_out
        self.timeout_delay_s = timeout_delay_s
        self.slow_range_s = slow_range_s
        self.retry_after_s = retry_after_s
        self.script = script or {}          # tests: forced fault sequence per request ref
        self.fixed_delay_s = fixed_delay_s  # tests: make every successful reply this slow
        self._lock = threading.Lock()
        self.seen: dict[str, int] = {}
        self.replies: dict[str, dict] = {}
        self.charges: list[Charge] = []
        self.calls = 0

    def _pick(self, key: str, n: int) -> str:
        r = random.Random(f"{self.seed}:{key}:{n}").random()
        acc = 0.0
        for fault in ("timeout", "rate_limited", "server_error", "slow"):
            acc += self.rates.get(fault, 0.0)
            if r < acc:
                return fault
        return "ok"

    def _reply(self, key: str, prompt: str, max_tokens: int) -> dict:
        rng = random.Random(f"{self.seed}:text:{key}")
        n_out = min(max_tokens, rng.randint(20, 120))
        words = [rng.choice(WORDS) for _ in range(max(3, n_out * 3 // 4))]
        text = " ".join(words).capitalize() + "."
        tin = count_tokens(prompt)
        return {"text": text, "usage": {"input_tokens": tin, "output_tokens": n_out,
                                        "cost_micros": tin * self.price_in + n_out * self.price_out},
                "replayed": False}

    def handle(self, key: str | None, prompt: str, max_tokens: int, ref: str = "", job_ref: str = "") -> Action:
        """ref groups calls by client request (faults are chosen per request); job_ref
        is only a label for the bill, so a report can tell which job paid."""
        with self._lock:
            self.calls += 1
            if "[reject]" in prompt:
                return Action(400, {"error": "this prompt is refused by the fake provider"}, fault="bad_request")
            if key and key in self.replies:
                body = {**self.replies[key], "replayed": True}
                return Action(200, body, fault="replayed")
            # Faults depend on the request and on how many times it was seen, not on
            # arrival order, so the same seed gives the same story.
            seen_key = key or f"ref:{ref or self.calls}"
            n = self.seen.get(seen_key, 0)
            self.seen[seen_key] = n + 1
            forced = self.script.get(ref, [])
            fault = forced[n] if n < len(forced) else self._pick(ref or seen_key, n)
            if fault == "rate_limited":
                return Action(429, {"error": "rate limited"}, {"Retry-After": str(self.retry_after_s)}, fault=fault)
            if fault == "server_error":
                return Action(500, {"error": "internal error"}, fault=fault)
            body = self._reply(f"{ref or seen_key}#{n}", prompt, max_tokens)
            self.charges.append(Charge(key, ref, job_ref, body["usage"]["cost_micros"], time.time()))
            if key:
                self.replies[key] = body
            delay = self.fixed_delay_s
            if fault == "timeout":
                delay = self.timeout_delay_s
            elif fault == "slow":
                delay = random.Random(f"{self.seed}:slow:{ref or seen_key}:{n}").uniform(*self.slow_range_s)
            return Action(200, body, delay_s=delay, fault=fault)

    def bill(self) -> dict:
        with self._lock:
            by_ref: dict[str, int] = {}
            by_job: dict[str, int] = {}
            for c in self.charges:
                r = c.ref or c.key or "?"
                j = c.key or c.job_ref or r      # with an Idempotency-Key, the key is the job
                by_ref[r] = by_ref.get(r, 0) + 1
                by_job[j] = by_job.get(j, 0) + 1
            return {
                "calls": self.calls,
                "charges": len(self.charges),
                "total_micros": sum(c.cost_micros for c in self.charges),
                "charges_by_ref": by_ref,
                "charges_by_job": by_job,
            }
