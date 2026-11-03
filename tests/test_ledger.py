"""Ledger: reserve before the call, close exactly once, never exceed the cap."""
import threading
import uuid

import pytest
from hypothesis import HealthCheck, given, settings as hsettings
from hypothesis import strategies as st

from jobsapi import ledger
from jobsapi.db import connect

from .conftest import set_cap


def new_job(conn) -> uuid.UUID:
    return conn.execute(
        "INSERT INTO jobs (idempotency_key, request_hash, prompt, max_tokens, max_attempts, estimate_micros) "
        "VALUES (%s, 'h', 'p', 10, 5, 100) RETURNING id", (str(uuid.uuid4()),)
    ).fetchone()["id"]


def test_reserve_within_cap(conn):
    set_cap(conn, 1000)
    j = new_job(conn)
    ledger.reserve(conn, j, "c1", 600)
    b = ledger.snapshot(conn)
    assert (b.reserved_micros, b.spent_micros, b.remaining_micros) == (600, 0, 400)


def test_reserve_over_cap_is_refused_and_leaves_no_trace(conn):
    set_cap(conn, 1000)
    j = new_job(conn)
    ledger.reserve(conn, j, "c1", 600)
    with pytest.raises(ledger.BudgetExceeded):
        with conn.transaction():
            ledger.reserve(conn, j, "c2", 401)
    assert not ledger.has_reservation(conn, "c2")
    assert ledger.snapshot(conn).reserved_micros == 600


def test_reserve_is_idempotent(conn):
    j = new_job(conn)
    ledger.reserve(conn, j, "c1", 300)
    ledger.reserve(conn, j, "c1", 300)
    assert ledger.snapshot(conn).reserved_micros == 300


def test_settle_twice_counts_once(conn):
    j = new_job(conn)
    ledger.reserve(conn, j, "c1", 300)
    assert ledger.close(conn, "c1", 120, "settled") is True
    assert ledger.close(conn, "c1", 120, "settled") is False
    assert ledger.close(conn, "c1", None, "unconfirmed") is False   # a late reaper cannot double count
    b = ledger.snapshot(conn)
    assert (b.reserved_micros, b.spent_micros) == (0, 120)


def test_unknown_outcome_counts_the_whole_reservation(conn):
    j = new_job(conn)
    ledger.reserve(conn, j, "c1", 300)
    ledger.close(conn, "c1", None, "unconfirmed: timeout")
    assert ledger.snapshot(conn).spent_micros == 300


def test_reconcile_refunds_over_count_once(conn):
    j = new_job(conn)
    ledger.reserve(conn, j, "c1", 300)
    ledger.close(conn, "c1", None, "unconfirmed: timeout")       # counted 300
    ledger.reserve(conn, j, "c2", 300)
    ledger.close(conn, "c2", 0, "replayed")                      # provider: charged once, 120
    assert ledger.reconcile(conn, j, 120) == 180
    assert ledger.reconcile(conn, j, 120) == 0
    assert ledger.job_spent(conn, j) == 120 == ledger.snapshot(conn).spent_micros


def test_release_returns_the_money(conn):
    j = new_job(conn)
    ledger.reserve(conn, j, "c1", 300)
    ledger.close(conn, "c1", 0, "released: rate_limited")
    b = ledger.snapshot(conn)
    assert (b.reserved_micros, b.spent_micros, b.remaining_micros) == (0, 0, b.cap_micros)


def test_concurrent_reservations_never_exceed_cap(conn, db_url):
    """20 workers race for a budget that fits 7 reservations: exactly 7 win."""
    set_cap(conn, 700)
    job = new_job(conn)
    wins, barrier = [], threading.Barrier(20)

    def worker(i):
        c = connect(db_url)
        barrier.wait()
        try:
            with c.transaction():
                ledger.reserve(c, job, f"k{i}", 100)
            wins.append(i)
        except ledger.BudgetExceeded:
            pass
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    b = ledger.snapshot(conn)
    assert len(wins) == 7
    assert b.reserved_micros == 700 <= b.cap_micros


ops = st.lists(st.tuples(st.sampled_from(["reserve", "settle", "unknown", "release", "reconcile"]),
                         st.integers(0, 5), st.integers(1, 400)), max_size=25)


@hsettings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(seq=ops)
def test_ledger_invariants_hold_for_any_sequence(conn, seq):
    """Property: whatever order of reserve/settle/release, spent + reserved <= cap
    and the budget row always equals what the ledger rows say."""
    conn.execute("TRUNCATE ledger, attempts, jobs CASCADE")
    conn.execute("UPDATE budget SET cap_micros = 1000, reserved_micros = 0, spent_micros = 0")
    j = new_job(conn)
    for op, k, amount in seq:
        key = f"c{k}"
        if op == "reserve":
            try:
                with conn.transaction():
                    ledger.reserve(conn, j, key, amount)
            except ledger.BudgetExceeded:
                pass
        elif op == "reconcile":
            before = ledger.job_spent(conn, j)
            refunded = ledger.reconcile(conn, j, amount)
            assert ledger.job_spent(conn, j) == before - refunded
            assert ledger.job_spent(conn, j) >= min(before, amount)   # never below what was confirmed
        else:
            row = conn.execute("SELECT amount_micros FROM ledger WHERE call_key=%s AND kind='reserve'",
                               (key,)).fetchone()
            spent = {"settle": min(amount, row["amount_micros"]) if row else amount,
                     "unknown": None, "release": 0}[op]
            ledger.close(conn, key, spent, op)
        b = ledger.snapshot(conn)
        open_res = conn.execute(
            "SELECT coalesce(sum(r.amount_micros),0) AS s FROM ledger r WHERE r.kind='reserve' AND NOT EXISTS "
            "(SELECT 1 FROM ledger c WHERE c.call_key=r.call_key AND c.kind='close')").fetchone()["s"]
        closed = conn.execute("SELECT coalesce(sum(CASE kind WHEN 'close' THEN amount_micros "
                              "WHEN 'refund' THEN -amount_micros END),0) AS s FROM ledger").fetchone()["s"]
        assert b.spent_micros + b.reserved_micros <= b.cap_micros
        assert b.reserved_micros == open_res
        assert b.spent_micros == closed
