"""Integration: real worker processes, real fake-provider server, kill -9 mid-call."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from chaos.run import free_port
from jobsapi import ledger

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.slow


def wait_for(cond, timeout, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not met in time")


def test_kill_worker_mid_call_then_recover(conn, db_url):
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    # every reply takes 0.8 s, so the worker is surely mid-call when we kill it
    prov = subprocess.Popen([sys.executable, "-m", "fakeprovider", "--port", str(port), "--delay", "0.8"],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env = {**os.environ, "DATABASE_URL": db_url, "FAKE_PROVIDER_URL": url, "PROVIDER": "fake",
           "PROVIDER_TIMEOUT": "1.5", "LEASE_SECONDS": "4", "BUDGET_CAP_MICROS": "1000000", "DRY_RUN": "0"}
    procs = []
    try:
        wait_for(lambda: _healthy(url), 15, 0.1)
        job_id = conn.execute(
            "INSERT INTO jobs (idempotency_key, request_hash, prompt, max_tokens, max_attempts, estimate_micros) "
            "VALUES ('crash-1', 'h', 'Reply to a customer about a late parcel.', 100, 5, 5000) RETURNING id"
        ).fetchone()["id"]

        w1 = subprocess.Popen([sys.executable, "-m", "jobsapi.worker", "--id", "w1"], cwd=ROOT, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(w1)
        wait_for(lambda: conn.execute("SELECT 1 FROM attempts WHERE job_id = %s AND finished_at IS NULL",
                                      (job_id,)).fetchone(), 30)
        w1.kill()                                    # abrupt: no cleanup code runs
        w1.wait()
        job = conn.execute("SELECT status FROM jobs WHERE id = %s", (job_id,)).fetchone()
        assert job["status"] == "running"            # left behind by the dead worker

        w2 = subprocess.Popen([sys.executable, "-m", "jobsapi.worker", "--id", "w2"], cwd=ROOT, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(w2)
        wait_for(lambda: conn.execute("SELECT 1 FROM jobs WHERE id = %s AND status IN ('succeeded', 'failed')",
                                      (job_id,)).fetchone(), 40, 0.1)

        job = conn.execute("SELECT status, attempts FROM jobs WHERE id = %s", (job_id,)).fetchone()
        outcomes = [r["outcome"] for r in conn.execute(
            "SELECT outcome FROM attempts WHERE job_id = %s ORDER BY attempt", (job_id,)).fetchall()]
        bill = httpx.get(f"{url}/_bill").json()
        assert job["status"] == "succeeded"
        assert outcomes == ["worker_lost", "replayed"]   # the dead worker's call had been charged
        assert bill["charges"] == 1                      # ...and it was not paid again
        b = ledger.snapshot(conn)
        assert b.reserved_micros == 0
        assert b.spent_micros == bill["total_micros"]
    finally:
        for p in procs + [prov]:
            if p.poll() is None:
                p.kill()
            p.wait()


def _healthy(url):
    try:
        return httpx.get(f"{url}/healthz", timeout=0.5).status_code == 200
    except httpx.HTTPError:
        return False
