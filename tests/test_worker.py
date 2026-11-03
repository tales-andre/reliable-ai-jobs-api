"""Worker against the fake provider (in-process): retries, permanent errors, timeouts,
budget, dry run, crashed workers and stale workers."""
from __future__ import annotations

import random
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from fakeprovider.core import FakeProviderCore
from jobsapi import ledger
from jobsapi.api import create_app
from jobsapi.db import connect
from jobsapi.providers import FakeProvider
from jobsapi.worker import claim, process, run_once

from .conftest import mock_transport, set_cap

PROMPT = "Write a two-sentence reply to a customer asking where order {n} is."


class Harness:
    def __init__(self, conn, settings, core: FakeProviderCore, dedupes=True):
        self.conn, self.settings, self.core = conn, settings, core
        self.provider = FakeProvider(settings, mock_transport(core, settings.provider_timeout_s))
        self.provider.dedupes = dedupes
        self.rng = random.Random(0)
        self.app = create_app(settings)

    def submit(self, key: str, **body) -> str:
        with TestClient(self.app) as c:
            r = c.post("/jobs", json={"prompt": PROMPT.format(n=key), "max_tokens": 200, **body},
                       headers={"Idempotency-Key": key})
            assert r.status_code == 201, r.text
            return r.json()["id"]

    def drain(self, worker="w1", max_loops=200) -> None:
        """Run the worker until nothing is left, skipping backoff waits (fast-forward time)."""
        for _ in range(max_loops):
            if run_once(self.conn, self.provider, self.settings, worker, self.rng):
                continue
            left = self.conn.execute("UPDATE jobs SET run_after = now() WHERE status = 'queued' RETURNING id"
                                     ).fetchall()
            if not left:
                return
        raise AssertionError("jobs did not finish")

    def job(self, jid):
        return self.conn.execute("SELECT * FROM jobs WHERE id = %s", (jid,)).fetchone()

    def outcomes(self, jid):
        rows = self.conn.execute("SELECT outcome FROM attempts WHERE job_id = %s ORDER BY attempt", (jid,))
        return [r["outcome"] for r in rows.fetchall()]

    def assert_money_consistent(self):
        b = ledger.snapshot(self.conn)
        bill = self.core.bill()
        assert b.reserved_micros == 0, "every reservation is closed at the end"
        assert b.spent_micros >= bill["total_micros"], "the ledger never under-counts what was charged"
        assert bill["total_micros"] <= b.cap_micros
        assert all(n == 1 for n in bill["charges_by_ref"].values()), f"paid twice: {bill['charges_by_ref']}"


def test_happy_path_settles_actual_cost(conn, settings):
    h = Harness(conn, settings, FakeProviderCore(seed=1))
    jid = h.submit("order-1")
    h.drain()
    job = h.job(jid)
    assert job["status"] == "succeeded" and job["result"]
    assert h.outcomes(jid) == ["ok"]
    b = ledger.snapshot(conn)
    assert b.spent_micros == h.core.bill()["total_micros"] > 0
    assert b.spent_micros <= job["estimate_micros"]       # the estimate is on the high side
    h.assert_money_consistent()


def test_rate_limit_and_server_errors_are_retried(conn, settings):
    core = FakeProviderCore(seed=1, retry_after_s=0, script={"order-2": ["rate_limited", "server_error", "ok"]})
    h = Harness(conn, settings, core)
    jid = h.submit("order-2")
    h.drain()
    assert h.job(jid)["status"] == "succeeded"
    assert h.outcomes(jid) == ["rate_limited", "server_error", "ok"]
    assert ledger.job_spent(conn, jid) == core.bill()["total_micros"]  # failed tries cost nothing
    h.assert_money_consistent()


def test_retry_waits_at_least_retry_after(conn, settings):
    core = FakeProviderCore(seed=1, retry_after_s=2, script={"order-3": ["rate_limited"]})
    h = Harness(conn, settings, core)
    jid = h.submit("order-3")
    job = claim(conn, "w1", settings.lease_s)
    process(conn, job, h.provider, settings, "w1", h.rng)
    att = conn.execute("SELECT retry_after_s, next_delay_s FROM attempts WHERE job_id = %s", (jid,)).fetchone()
    assert att["retry_after_s"] == 2 and att["next_delay_s"] >= 2
    wait = conn.execute("SELECT extract(epoch FROM run_after - now()) AS s FROM jobs WHERE id = %s",
                        (jid,)).fetchone()["s"]
    assert wait > 1.5
    assert claim(conn, "w2", settings.lease_s) is None    # nobody picks it up early


def test_permanent_error_fails_once_with_clear_message(conn, settings):
    h = Harness(conn, settings, FakeProviderCore(seed=1))
    jid = h.submit("order-4", prompt="[reject] please")
    h.drain()
    job = h.job(jid)
    assert job["status"] == "failed" and job["attempts"] == 1
    assert job["error_kind"] == "bad_request" and "not retryable" in job["error"]
    assert h.core.calls == 1
    assert ledger.job_spent(conn, jid) == 0
    h.assert_money_consistent()


def test_gives_up_after_max_attempts(conn, settings):
    core = FakeProviderCore(seed=1, script={"order-5": ["server_error"] * 10})
    h = Harness(conn, settings, core)
    jid = h.submit("order-5")
    h.drain()
    job = h.job(jid)
    assert job["status"] == "failed" and job["attempts"] == settings.max_attempts
    assert job["error"].startswith(f"gave up after {settings.max_attempts} attempts")
    assert core.calls == settings.max_attempts


def test_timeout_then_retry_is_paid_once(conn, settings):
    """The provider did the work but replied too late. The retry gets the stored
    reply; the provider charged once; the ledger counted at least that."""
    core = FakeProviderCore(seed=1, script={"order-6": ["timeout"]})
    h = Harness(conn, settings, core)
    jid = h.submit("order-6")
    h.drain()
    assert h.job(jid)["status"] == "succeeded"
    assert h.outcomes(jid) == ["timeout", "replayed"]
    assert core.bill()["charges"] == 1
    # The timeout was first counted at the full estimate, then reconciled to the real charge.
    assert ledger.snapshot(conn).spent_micros == core.bill()["total_micros"]
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM ledger WHERE job_id = %s", (jid,)).fetchall()]
    assert kinds.count("refund") == 1
    h.assert_money_consistent()


def test_retry_after_timeout_needs_no_extra_budget(conn, settings):
    """The timed-out attempt already holds the job's worst case; with a deduplicating
    provider the retry reserves nothing more, so a tight cap does not block it."""
    core = FakeProviderCore(seed=1, script={"order-6c": ["timeout"]})
    h = Harness(conn, settings, core)
    jid = h.submit("order-6c")
    set_cap(conn, h.job(jid)["estimate_micros"])          # room for exactly one worst case
    h.drain()
    assert h.job(jid)["status"] == "succeeded"
    assert h.outcomes(jid) == ["timeout", "replayed"]
    h.assert_money_consistent()


def test_unconfirmed_spend_stays_counted_when_job_never_succeeds(conn, settings):
    """Timeout, then the provider cannot deduplicate, so no retry: nothing confirms the
    real charge and the full reservation stays counted."""
    core = FakeProviderCore(seed=1, script={"order-6b": ["timeout"]})
    h = Harness(conn, settings, core, dedupes=False)
    jid = h.submit("order-6b")
    h.drain()
    assert h.job(jid)["status"] == "failed" and h.outcomes(jid) == ["timeout"]
    # The provider did charge for the timed-out call; the ledger keeps counting it.
    assert ledger.job_spent(conn, jid) == h.job(jid)["estimate_micros"] >= core.bill()["total_micros"] > 0
    h.assert_money_consistent()


def test_timeout_is_not_retried_when_provider_cannot_deduplicate(conn, settings):
    core = FakeProviderCore(seed=1, script={"order-7": ["timeout"]})
    h = Harness(conn, settings, core, dedupes=False)
    jid = h.submit("order-7")
    h.drain()
    job = h.job(jid)
    assert job["status"] == "failed" and job["attempts"] == 1
    assert "avoid paying twice" in job["error"]
    assert core.calls == 1


def test_dry_run_never_calls_the_provider(conn, settings):
    h = Harness(conn, settings, FakeProviderCore(seed=1))
    jid = h.submit("order-8", dry_run=True)
    h2 = Harness(conn, replace(settings, dry_run=True), h.core)
    jid2 = h2.submit("order-9")
    h2.drain()
    for j in (jid, jid2):
        job = h.job(j)
        assert job["status"] == "succeeded" and job["result"].startswith("[dry run]")
    assert h.core.calls == 0
    assert ledger.snapshot(conn).spent_micros == 0


def test_spending_cap_refuses_calls_before_they_happen(conn, settings):
    h = Harness(conn, settings, FakeProviderCore(seed=1))
    ids = [h.submit(f"order-c{i}") for i in range(8)]
    est = h.job(ids[0])["estimate_micros"]
    set_cap(conn, int(est * 3.5))
    h.drain()
    jobs = [h.job(j) for j in ids]
    refused = [j for j in jobs if j["error_kind"] == "budget_exceeded"]
    assert refused, "some jobs must hit the cap"
    assert all("Provider not called" in j["error"] for j in refused)
    charged_refs = set(h.core.bill()["charges_by_ref"])
    assert not charged_refs & {j["idempotency_key"] for j in refused}
    h.assert_money_consistent()


class DiesMidCall(BaseException):
    """Stands in for kill -9: nothing after the provider call runs."""


def test_crashed_worker_job_is_recovered_and_not_paid_twice(conn, settings, db_url):
    core = FakeProviderCore(seed=1)
    h = Harness(conn, settings, core)
    jid = h.submit("order-10")
    real_transport_provider = h.provider

    class Dying(FakeProvider):
        def generate(self, *a, **kw):
            real_transport_provider.generate(*a, **kw)   # the provider does the work and charges...
            raise DiesMidCall()                          # ...but the worker dies before recording it

    dying = Dying(settings, mock_transport(core, settings.provider_timeout_s))
    job = claim(conn, "w-dead", settings.lease_s)
    with pytest.raises(DiesMidCall):
        process(conn, job, dying, settings, "w-dead", h.rng)
    assert h.job(jid)["status"] == "running"
    conn.execute("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (jid,))
    h.drain(worker="w2")
    assert h.job(jid)["status"] == "succeeded"
    assert h.outcomes(jid) == ["worker_lost", "replayed"]
    assert core.bill()["charges"] == 1
    assert ledger.snapshot(conn).spent_micros == core.bill()["total_micros"]
    h.assert_money_consistent()


def test_stale_worker_result_is_discarded(conn, settings, db_url):
    """Worker A freezes past its lease; B takes the job and finishes. When A wakes
    up, its result is dropped and its money is still recorded correctly."""
    core = FakeProviderCore(seed=1)
    h = Harness(conn, settings, core)
    jid = h.submit("order-11")
    other = connect(db_url)
    b_provider = FakeProvider(settings, mock_transport(core, settings.provider_timeout_s))

    class Freezes(FakeProvider):
        def generate(self, *a, **kw):
            other.execute("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (jid,))
            assert run_once(other, b_provider, settings, "w-B", random.Random(1))
            return b_provider.generate(*a, **kw)

    job = claim(conn, "w-A", settings.lease_s)
    outcome = process(conn, job, Freezes(settings, mock_transport(core, 1.0)), settings, "w-A", h.rng)
    other.close()
    assert outcome == "lease_lost"
    assert h.job(jid)["status"] == "succeeded"
    assert h.outcomes(jid) == ["worker_lost", "ok"]
    assert core.bill()["charges"] == 1
    h.assert_money_consistent()
