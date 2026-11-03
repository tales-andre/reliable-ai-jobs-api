"""Worker: claims jobs with a lease, reserves budget, calls the provider, records the outcome.

Run several at once: `python -m jobsapi.worker --id w1`. Each one can be killed at
any moment (kill -9) without losing a job or counting money twice.
"""
from __future__ import annotations

import argparse
import os
import random
import socket
import sys
import time

import psycopg

from . import ledger
from .config import Settings
from .costs import estimate_tokens, fmt_usd
from .db import connect, init_schema
from .providers import Generation, HttpProvider, make_provider
from .retry import UNKNOWN, ProviderError, backoff_delay, is_retryable

CLAIM_SQL = """
UPDATE jobs
   SET status = 'running', attempts = attempts + 1, worker_id = %(w)s,
       lease_until = now() + make_interval(secs => %(lease)s), updated_at = now()
 WHERE id = (SELECT id FROM jobs
              WHERE status = 'queued' AND run_after <= now()
              ORDER BY run_after, created_at
              FOR UPDATE SKIP LOCKED
              LIMIT 1)
RETURNING *
"""


def log(worker_id: str, msg: str) -> None:
    print(f"{worker_id} {msg}", flush=True)


def short(job_id: object) -> str:
    return str(job_id)[:8]


def claim(conn: psycopg.Connection, worker_id: str, lease_s: float) -> dict | None:
    with conn.transaction():
        return conn.execute(CLAIM_SQL, {"w": worker_id, "lease": lease_s}).fetchone()


def _fail(conn: psycopg.Connection, job_id: object, kind: str, message: str) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'failed', error_kind = %s, error = %s, lease_until = NULL, "
        "finished_at = clock_timestamp(), updated_at = now() WHERE id = %s",
        (kind, message, job_id),
    )


def _finish_attempt(conn, job_id, attempt, outcome, http_status=None, retry_after=None,
                    next_delay=None, detail=None) -> None:
    conn.execute(
        "UPDATE attempts SET finished_at = clock_timestamp(), outcome = %s, http_status = %s, "
        "retry_after_s = %s, next_delay_s = %s, detail = %s "
        "WHERE job_id = %s AND attempt = %s AND finished_at IS NULL",
        (outcome, http_status, retry_after, next_delay, detail, job_id, attempt),
    )


def reap_expired(conn: psycopg.Connection, provider_dedupes: bool, worker_id: str = "") -> int:
    """Take back jobs whose worker stopped renewing its lease (crashed, killed, frozen).

    The interrupted attempt may or may not have reached the provider, so its
    reservation is counted as spent. The job is retried only if that is safe.
    """
    n = 0
    with conn.transaction():
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = 'running' AND lease_until < now() "
            "ORDER BY lease_until FOR UPDATE SKIP LOCKED LIMIT 10"
        ).fetchall()
        for job in rows:
            key = ledger.call_key(job["id"], job["attempts"])
            maybe_called = ledger.has_reservation(conn, key)
            ledger.close(conn, key, None, "unconfirmed: worker lost")
            _finish_attempt(conn, job["id"], job["attempts"], "worker_lost",
                            detail=f"lease of {job['worker_id']} expired")
            safe = provider_dedupes or not maybe_called
            if safe and job["attempts"] < job["max_attempts"]:
                conn.execute(
                    "UPDATE jobs SET status = 'queued', run_after = now(), lease_until = NULL, "
                    "worker_id = NULL, updated_at = now() WHERE id = %s", (job["id"],))
                verdict = "requeued"
            elif not safe:
                _fail(conn, job["id"], "worker_lost",
                      "worker stopped mid-call and this provider does not deduplicate; "
                      "not retried to avoid paying twice")
                verdict = "failed"
            else:
                _fail(conn, job["id"], "worker_lost",
                      f"worker stopped mid-call; gave up after {job['attempts']} attempts")
                verdict = "failed"
            if worker_id:
                log(worker_id, f"job {short(job['id'])} attempt {job['attempts']}: lease of "
                               f"{job['worker_id']} expired, job {verdict}")
            n += 1
    return n


def process(conn: psycopg.Connection, job: dict, provider: HttpProvider, settings: Settings,
            worker_id: str, rng: random.Random) -> str:
    """Run one attempt of a claimed job. Returns the attempt outcome."""
    jid, attempt = job["id"], job["attempts"]
    key = ledger.call_key(jid, attempt)

    # 1. Record the attempt and reserve money in one transaction, before any call.
    with conn.transaction():
        conn.execute("INSERT INTO attempts (job_id, attempt, worker_id) VALUES (%s, %s, %s)",
                     (jid, attempt, worker_id))
        if job["dry_run"] or settings.dry_run:
            text = (f"[dry run] would send about {estimate_tokens(job['prompt'])} input tokens to "
                    f"'{provider.name}' with max_tokens={job['max_tokens']}; "
                    f"estimated maximum cost {fmt_usd(job['estimate_micros'])}. Provider not called.")
            _finish_attempt(conn, jid, attempt, "dry_run")
            conn.execute("UPDATE jobs SET status = 'succeeded', result = %s, lease_until = NULL, "
                         "finished_at = clock_timestamp(), updated_at = now() WHERE id = %s", (text, jid))
            log(worker_id, f"job {short(jid)} attempt {attempt}: dry run, provider not called")
            return "dry_run"
        amount = job["estimate_micros"]
        if provider.dedupes:
            # All attempts of this job share one charge at the provider, so the job never
            # needs more than its estimate in total: earlier unconfirmed attempts
            # already hold part of it.
            amount = max(0, amount - ledger.job_spent(conn, jid))
        try:
            ledger.reserve(conn, jid, key, amount)
        except ledger.BudgetExceeded as e:
            msg = (f"spending cap reached: this call may cost up to {fmt_usd(job['estimate_micros'])} and "
                   f"{fmt_usd(max(0, e.remaining_micros))} of {fmt_usd(e.cap_micros)} was left after what is "
                   f"spent and reserved for calls in flight. Provider not called.")
            if e.remaining_micros < 0:
                msg += (f" Spending is already {fmt_usd(-e.remaining_micros)} over the cap because a call "
                        f"cost more than its estimate.")
            _finish_attempt(conn, jid, attempt, "budget_exceeded", detail=msg)
            _fail(conn, jid, "budget_exceeded", msg)
            log(worker_id, f"job {short(jid)} attempt {attempt}: refused, spending cap reached")
            return "budget_exceeded"

    # 2. Call the provider outside any transaction (no row locks held while waiting).
    gen: Generation | None = None
    err: ProviderError | None = None
    try:
        gen = provider.generate(job["prompt"], job["max_tokens"], idempotency_key=str(jid),
                                request_ref=job["idempotency_key"])
    except ProviderError as e:
        err = e

    # 3. Record the outcome. Money first, then the job, fenced on the lease.
    with conn.transaction():
        cur = conn.execute("SELECT status, attempts, worker_id FROM jobs WHERE id = %s FOR UPDATE",
                           (jid,)).fetchone()
        if gen is not None:
            if gen.replayed:
                ledger.close(conn, key, 0, "replayed: charged on an earlier attempt")
            else:
                ledger.close(conn, key, gen.cost_micros, "settled")
        elif err.billing == UNKNOWN:
            ledger.close(conn, key, None, f"unconfirmed: {err.kind}")
        else:
            ledger.close(conn, key, 0, f"released: {err.kind}")

        still_mine = (cur["status"] == "running" and cur["attempts"] == attempt
                      and cur["worker_id"] == worker_id)
        if not still_mine:
            # Our lease expired and another worker took the job back: drop this result.
            _finish_attempt(conn, jid, attempt, "lease_lost", detail="result discarded: lease expired")
            log(worker_id, f"job {short(jid)} attempt {attempt}: lease lost, result discarded")
            return "lease_lost"

        if gen is not None:
            outcome = "replayed" if gen.replayed else "ok"
            if provider.dedupes:
                # The reply states the single charge for this job: give back what earlier
                # timeouts or crashed attempts counted on top of it.
                ledger.reconcile(conn, jid, gen.cost_micros)
            _finish_attempt(conn, jid, attempt, outcome)
            conn.execute("UPDATE jobs SET status = 'succeeded', result = %s, lease_until = NULL, "
                         "error_kind = NULL, error = NULL, finished_at = clock_timestamp(), updated_at = now() "
                         "WHERE id = %s", (gen.text, jid))
            extra = " (stored reply, not paid again)" if gen.replayed else f", cost {fmt_usd(gen.cost_micros)}"
            log(worker_id, f"job {short(jid)} attempt {attempt}: {outcome}{extra}")
            return outcome

        retryable = is_retryable(err, provider.dedupes)
        if retryable and attempt < job["max_attempts"]:
            delay = backoff_delay(attempt, settings.backoff_base_s, settings.backoff_cap_s, rng,
                                  err.retry_after_s)
            _finish_attempt(conn, jid, attempt, err.kind, err.status, err.retry_after_s, delay, err.message)
            conn.execute("UPDATE jobs SET status = 'queued', run_after = now() + make_interval(secs => %s), "
                         "lease_until = NULL, worker_id = NULL, error_kind = %s, error = %s, "
                         "updated_at = now() WHERE id = %s", (delay, err.kind, err.message, jid))
            log(worker_id, f"job {short(jid)} attempt {attempt}: {err.kind}, retry in {delay:.1f}s")
            return err.kind

        if retryable:
            msg = f"gave up after {attempt} attempts; last error: {err.message}"
        elif err.billing == UNKNOWN:
            msg = (f"{err.message}; the provider may have done the work and it does not deduplicate, "
                   f"so it was not retried to avoid paying twice")
        else:
            msg = f"{err.message} (not retryable)"
        _finish_attempt(conn, jid, attempt, err.kind, err.status, err.retry_after_s, None, err.message)
        _fail(conn, jid, err.kind, msg)
        log(worker_id, f"job {short(jid)} attempt {attempt}: {err.kind}, job failed")
        return err.kind


def run_once(conn, provider, settings, worker_id, rng) -> bool:
    reap_expired(conn, provider.dedupes, worker_id)
    job = claim(conn, worker_id, settings.lease_s)
    if job is None:
        return False
    process(conn, job, provider, settings, worker_id, rng)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Job worker")
    # The id fences results (see process), so it must be unique: host name + pid.
    ap.add_argument("--id", default=f"{socket.gethostname()[:16]}-{os.getpid()}")
    ap.add_argument("--exit-when-idle", type=float, default=0,
                    help="exit after this many idle seconds (0 = run forever)")
    args = ap.parse_args(argv)
    settings = Settings.from_env()
    provider = make_provider(settings)
    rng = random.Random()
    conn = connect(settings.database_url)
    init_schema(conn, settings.budget_cap_micros)
    log(args.id, f"started (provider={provider.name}, dry_run={settings.dry_run}, lease={settings.lease_s}s)")
    idle_since = time.monotonic()
    try:
        while True:
            if run_once(conn, provider, settings, args.id, rng):
                idle_since = time.monotonic()
            else:
                if args.exit_when_idle and time.monotonic() - idle_since > args.exit_when_idle:
                    return 0
                time.sleep(settings.poll_interval_s)
    except KeyboardInterrupt:
        return 0
    finally:
        provider.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
