"""Spending ledger: reserve before a call, close (settle or release) exactly once after.

All functions run inside the caller's transaction, so a job state change and its
money movement commit together or not at all.

Invariants (checked by tests/test_ledger.py):
  * a reservation is only granted if spent + reserved + amount <= cap;
  * closing the same call twice changes nothing the second time;
  * reserved never goes negative; spent only goes down through reconcile(), and
    never below what the provider confirmed.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg


class BudgetExceeded(Exception):
    def __init__(self, key: str, remaining_micros: int, cap_micros: int):
        super().__init__(key)
        self.remaining_micros, self.cap_micros = remaining_micros, cap_micros


@dataclass(frozen=True)
class Budget:
    cap_micros: int
    reserved_micros: int
    spent_micros: int

    @property
    def remaining_micros(self) -> int:
        return self.cap_micros - self.reserved_micros - self.spent_micros


def call_key(job_id: object, attempt: int) -> str:
    return f"{job_id}:{attempt}"


def snapshot(conn: psycopg.Connection) -> Budget:
    row = conn.execute("SELECT cap_micros, reserved_micros, spent_micros FROM budget WHERE id = 1").fetchone()
    return Budget(row["cap_micros"], row["reserved_micros"], row["spent_micros"])


def reserve(conn: psycopg.Connection, job_id: object, key: str, amount: int) -> None:
    """Set `amount` aside for call `key`. Idempotent. Raises BudgetExceeded if over the cap."""
    with conn.transaction():  # savepoint: a refused reservation leaves no ledger row
        inserted = conn.execute(
            "INSERT INTO ledger (call_key, job_id, kind, amount_micros) VALUES (%s, %s, 'reserve', %s) "
            "ON CONFLICT (call_key, kind) DO NOTHING RETURNING call_key",
            (key, job_id, amount),
        ).fetchone()
        if not inserted:
            return  # already reserved earlier: nothing to do
        # Lock the budget row, check, then increment: concurrent workers queue up here,
        # so two of them cannot both see room for the last slot of budget.
        b = conn.execute("SELECT cap_micros, reserved_micros, spent_micros FROM budget WHERE id = 1 FOR UPDATE"
                         ).fetchone()
        remaining = b["cap_micros"] - b["reserved_micros"] - b["spent_micros"]
        if amount > remaining:
            raise BudgetExceeded(key, remaining, b["cap_micros"])
        conn.execute("UPDATE budget SET reserved_micros = reserved_micros + %s WHERE id = 1", (amount,))


def has_reservation(conn: psycopg.Connection, key: str) -> bool:
    row = conn.execute("SELECT 1 FROM ledger WHERE call_key = %s AND kind = 'reserve'", (key,)).fetchone()
    return row is not None


def close(conn: psycopg.Connection, key: str, spent: int | None, reason: str) -> bool:
    """Close the reservation of call `key` once.

    spent=None means "we do not know if the provider charged" (timeout, crashed
    worker): the whole reservation is counted as spent. Never under-count.
    Returns True if this call closed it, False if it was already closed or never reserved.
    """
    with conn.transaction():
        res = conn.execute(
            "SELECT job_id, amount_micros FROM ledger WHERE call_key = %s AND kind = 'reserve'", (key,)
        ).fetchone()
        if not res:
            return False
        amount = res["amount_micros"] if spent is None else spent
        inserted = conn.execute(
            "INSERT INTO ledger (call_key, job_id, kind, amount_micros, reason) VALUES (%s, %s, 'close', %s, %s) "
            "ON CONFLICT (call_key, kind) DO NOTHING RETURNING call_key",
            (key, res["job_id"], amount, reason),
        ).fetchone()
        if not inserted:
            return False
        conn.execute(
            "UPDATE budget SET reserved_micros = reserved_micros - %s, spent_micros = spent_micros + %s WHERE id = 1",
            (res["amount_micros"], amount),
        )
        return True


def job_spent(conn: psycopg.Connection, job_id: object) -> int:
    row = conn.execute(
        "SELECT coalesce(sum(CASE kind WHEN 'close' THEN amount_micros WHEN 'refund' THEN -amount_micros END), 0) "
        "AS s FROM ledger WHERE job_id = %s", (job_id,)
    ).fetchone()
    return int(row["s"])


def reconcile(conn: psycopg.Connection, job_id: object, confirmed_micros: int) -> int:
    """Give back what earlier unconfirmed attempts of this job over-counted.

    Only call this when the provider deduplicates by job: then every attempt of the
    job shares one charge, and a successful reply states that charge. Happens at
    most once per job. Returns the refunded amount (0 if nothing was over-counted).
    """
    with conn.transaction():
        over = job_spent(conn, job_id) - confirmed_micros
        if over <= 0:
            return 0
        inserted = conn.execute(
            "INSERT INTO ledger (call_key, job_id, kind, amount_micros, reason) VALUES (%s, %s, 'refund', %s, %s) "
            "ON CONFLICT (call_key, kind) DO NOTHING RETURNING call_key",
            (f"{job_id}:reconcile", job_id, over, "provider confirmed a single charge for this job"),
        ).fetchone()
        if not inserted:
            return 0
        conn.execute("UPDATE budget SET spent_micros = spent_micros - %s WHERE id = 1", (over,))
        return over
