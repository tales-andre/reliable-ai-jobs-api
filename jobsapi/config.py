"""Settings read from environment variables. Defaults run the offline demo."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


# (PROVIDER_TIMEOUT, LEASE_SECONDS) defaults; the lease must be >= 2 * timeout + 1.
FAKE_TIMEOUTS = ("1.0", "4")
REAL_TIMEOUTS = ("60", "121")


@dataclass(frozen=True)
class Settings:
    database_url: str
    provider: str                 # fake | openai | anthropic
    fake_provider_url: str
    llm_model: str
    provider_timeout_s: float     # client timeout for one provider call
    lease_s: float                # how long a worker owns a job before others may take it back
    max_attempts: int
    backoff_base_s: float
    backoff_cap_s: float
    budget_cap_micros: int        # spending cap, in millionths of a US dollar
    price_in_micros: int          # per input token (illustrative default)
    price_out_micros: int         # per output token (illustrative default)
    dry_run: bool                 # never call the provider, for any job
    poll_interval_s: float

    @classmethod
    def from_env(cls) -> "Settings":
        provider = _env("PROVIDER", "fake").strip().lower()
        # The local fake provider answers in well under a second, so short values keep
        # the demo fast. A real model can take tens of seconds for a long reply.
        timeout_default, lease_default = REAL_TIMEOUTS if provider != "fake" else FAKE_TIMEOUTS
        s = cls(
            database_url=_env("DATABASE_URL", "postgresql://jobs:jobs@127.0.0.1:54330/jobs"),
            provider=provider,
            fake_provider_url=_env("FAKE_PROVIDER_URL", "http://127.0.0.1:8090"),
            llm_model=_env("LLM_MODEL", ""),
            provider_timeout_s=float(_env("PROVIDER_TIMEOUT", "") or timeout_default),
            lease_s=float(_env("LEASE_SECONDS", "") or lease_default),
            max_attempts=int(_env("MAX_ATTEMPTS", "5")),
            backoff_base_s=float(_env("BACKOFF_BASE", "0.2")),
            backoff_cap_s=float(_env("BACKOFF_CAP", "3.0")),
            budget_cap_micros=int(_env("BUDGET_CAP_MICROS", "1000000")),
            price_in_micros=int(_env("PRICE_IN_MICROS", "10")),
            price_out_micros=int(_env("PRICE_OUT_MICROS", "10")),
            dry_run=_bool("DRY_RUN", False),
            poll_interval_s=float(_env("POLL_INTERVAL", "0.1")),
        )
        s.validate()
        return s

    def validate(self) -> None:
        if self.provider not in ("fake", "openai", "anthropic"):
            raise ValueError(f"PROVIDER must be fake, openai or anthropic, got {self.provider!r}")
        # One provider call always ends within 2 * PROVIDER_TIMEOUT (see providers.py),
        # and it must end inside the lease, otherwise another worker could take the
        # job back while it is still running.
        if self.lease_s < 2 * self.provider_timeout_s + 1:
            raise ValueError("LEASE_SECONDS must be at least 2 * PROVIDER_TIMEOUT + 1")
        if self.max_attempts < 1:
            raise ValueError("MAX_ATTEMPTS must be >= 1")
