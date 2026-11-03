from __future__ import annotations

import os
from dataclasses import replace

import httpx
import psycopg
import pytest

from fakeprovider.core import FakeProviderCore
from jobsapi.config import Settings
from jobsapi.db import connect, ensure_database, reset_schema

ADMIN_URL = os.environ.get("DATABASE_URL", "postgresql://jobs:jobs@127.0.0.1:54330/jobs")
CAP = 1_000_000


@pytest.fixture(scope="session")
def db_url() -> str:
    try:
        return ensure_database(ADMIN_URL, "jobs_test")
    except psycopg.OperationalError as e:
        pytest.exit(f"Postgres is not reachable at {ADMIN_URL} ({e}). Start it with: docker compose up -d db",
                    returncode=2)


@pytest.fixture
def settings(db_url) -> Settings:
    s = Settings.from_env()
    return replace(s, database_url=db_url, provider="fake", provider_timeout_s=1.0, lease_s=3.0,
                   max_attempts=5, backoff_base_s=0.01, backoff_cap_s=0.05, budget_cap_micros=CAP,
                   price_in_micros=10, price_out_micros=10, dry_run=False)


@pytest.fixture
def conn(db_url):
    c = connect(db_url)
    reset_schema(c, CAP)
    yield c
    c.close()


def set_cap(conn, cap: int) -> None:
    conn.execute("UPDATE budget SET cap_micros = %s WHERE id = 1", (cap,))


def mock_transport(core: FakeProviderCore, timeout_s: float) -> httpx.MockTransport:
    """Serve the fake provider in-process. A reply slower than the client timeout
    becomes a ReadTimeout, after the provider has already charged (as in real life)."""
    def handler(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content)
        action = core.handle(request.headers.get("idempotency-key"), body["prompt"], body["max_tokens"],
                             request.headers.get("x-request-ref", ""))
        if action.delay_s >= timeout_s:
            raise httpx.ReadTimeout("simulated timeout", request=request)
        return httpx.Response(action.status, json=action.body, headers=action.headers)
    return httpx.MockTransport(handler)
