"""Chaos run: many jobs, an unreliable provider, workers killed mid-call.

    python -m chaos.run                  # both modes, writes report/chaos.html and report/chaos.json
    python -m chaos.run --mode safe      # only the safeguarded worker

The same seed gives the same requests, the same repeated POSTs and the same
provider faults. Worker kills and timings depend on the machine, so the timeline
differs a little between runs; the checks must pass every time.
It uses its own database (jobs_chaos) and recreates it on every run.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import random
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import httpx

from jobsapi.config import Settings
from jobsapi.costs import estimate_cost_micros, fmt_usd
from jobsapi.db import connect, ensure_database, reset_schema

from .report import write_report

ROOT = Path(__file__).resolve().parent.parent
PERMANENT = {"bad_request", "auth", "budget_exceeded"}

TOPICS = ["where is my order", "how do I return a desk", "can I change the delivery address",
          "is the chair in stock", "why was I charged shipping", "how long is the warranty",
          "can I cancel my order", "do you ship on weekends"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_requests(seed: int, n: int) -> list[dict]:
    """The client side of the story: n requests, some sent more than once."""
    rng = random.Random(seed)
    reqs = []
    for i in range(n):
        topic = rng.choice(TOPICS)
        prompt = f"Brightline Desk Co. support: reply to a customer asking {topic}? (ticket {1000 + i})"
        if rng.random() < 0.05:
            prompt = "[reject] " + prompt
        reqs.append({"key": f"ticket-{seed}-{i:03d}", "prompt": prompt,
                     "max_tokens": rng.choice([150, 200, 250]),
                     "sends": rng.choice([1, 1, 1, 2, 2, 3])})   # client retries its own POST
    return reqs


class Clock:
    def __init__(self):
        self.t0 = time.monotonic()

    def now(self) -> float:
        return time.monotonic() - self.t0


class Printer:
    """Collects worker output and chaos events into one timestamped stream."""
    def __init__(self, clock: Clock, quiet: bool):
        self.clock, self.quiet = clock, quiet
        self.lines: list[tuple[float, str]] = []
        self.q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()

    def say(self, msg: str) -> None:
        t = self.clock.now()
        with self._lock:
            self.lines.append((t, msg))
            if not self.quiet:
                print(f"[{t:6.1f}s] {msg}", flush=True)

    def follow(self, proc: subprocess.Popen) -> None:
        def pump():
            for raw in proc.stdout:
                line = raw.rstrip()
                if line:
                    self.say(line)
        threading.Thread(target=pump, daemon=True).start()


def run_mode(mode: str, args, base_settings: Settings, db_url: str) -> dict:
    clock = Clock()
    out = Printer(clock, args.quiet)
    conn = connect(db_url)
    reset_schema(conn, args.cap)
    settings = replace(base_settings, database_url=db_url, budget_cap_micros=args.cap)

    port = free_port()
    provider_url = f"http://127.0.0.1:{port}"
    prov = subprocess.Popen([sys.executable, "-m", "fakeprovider", "--port", str(port), "--seed", str(args.seed),
                             "--chaos"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            if httpx.get(f"{provider_url}/healthz", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    out.say(f"== {mode.upper()} mode: fake provider on :{port} (seed {args.seed}), "
            f"cap {fmt_usd(args.cap)}, {args.jobs} requests")

    reqs = build_requests(args.seed, args.jobs)
    posts = sum(r["sends"] for r in reqs)
    statuses: Counter = Counter()
    if mode == "safe":
        from fastapi.testclient import TestClient

        from jobsapi.api import create_app
        with TestClient(create_app(settings)) as client:
            def send(r):
                return client.post("/jobs", json={"prompt": r["prompt"], "max_tokens": r["max_tokens"]},
                                   headers={"Idempotency-Key": r["key"]}).status_code
            with ThreadPoolExecutor(8) as ex:
                for code in ex.map(send, [r for r in reqs for _ in range(r["sends"])]):
                    statuses[code] += 1
    else:
        # What an API without idempotency keys does: one job per POST.
        for r in reqs:
            for s in range(r["sends"]):
                est = estimate_cost_micros(r["prompt"], r["max_tokens"], settings.price_in_micros,
                                           settings.price_out_micros)
                conn.execute("INSERT INTO jobs (idempotency_key, request_hash, prompt, max_tokens, max_attempts, "
                             "estimate_micros) VALUES (%s, '', %s, %s, 5, %s)",
                             (f"{r['key']}#{s}", r["prompt"], r["max_tokens"], est))
                statuses[201] += 1
    out.say(f"client sent {posts} POSTs for {len(reqs)} requests -> " +
            ", ".join(f"{n}x HTTP {c}" for c, n in sorted(statuses.items())))

    env = {**os.environ, "DATABASE_URL": db_url, "FAKE_PROVIDER_URL": provider_url, "PROVIDER": "fake",
           "PROVIDER_TIMEOUT": "1.0", "LEASE_SECONDS": "3", "MAX_ATTEMPTS": "5", "BACKOFF_BASE": "0.2",
           "BACKOFF_CAP": "2.0", "BUDGET_CAP_MICROS": str(args.cap), "DRY_RUN": "0", "PYTHONUNBUFFERED": "1"}
    module = "jobsapi.worker" if mode == "safe" else "chaos.naive_worker"
    workers: dict[str, subprocess.Popen] = {}
    counter = [0]

    def spawn():
        counter[0] += 1
        wid = f"w{counter[0]}"
        p = subprocess.Popen([sys.executable, "-u", "-m", module, "--id", wid], cwd=ROOT, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        out.follow(p)
        workers[wid] = p
        return wid

    for _ in range(args.workers):
        spawn()

    rng = random.Random(args.seed * 7 + 1)
    kills: list[dict] = []
    killed: set[str] = set()
    crashed: list[str] = []
    next_kill = clock.now() + rng.uniform(1.0, 2.0)
    deadline = clock.now() + args.timeout
    last_change, last_sig = clock.now(), None
    while clock.now() < deadline:
        time.sleep(0.05)
        for wid, p in list(workers.items()):
            if p.poll() is not None and wid not in killed and wid not in crashed:
                crashed.append(wid)          # a worker died on its own: a bug, reported as a failed check
                out.say(f"{wid} exited on its own with code {p.returncode}")
        if len(kills) < args.kills and clock.now() >= next_kill:
            inflight = conn.execute(
                "SELECT a.job_id, a.attempt, a.worker_id FROM attempts a WHERE a.finished_at IS NULL "
                "AND a.worker_id = ANY(%s) ORDER BY a.started_at", ([w for w, p in workers.items()
                                                                       if p.poll() is None],)).fetchall()
            if inflight:
                victim = rng.choice(inflight)
                killed.add(victim["worker_id"])
                workers[victim["worker_id"]].kill()          # abrupt, like kill -9
                at = conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
                kills.append({"worker": victim["worker_id"], "job_id": str(victim["job_id"]),
                              "attempt": victim["attempt"], "at": at.isoformat(), "t": clock.now()})
                out.say(f"KILL {victim['worker_id']} (kill -9) while job {str(victim['job_id'])[:8]} "
                        f"attempt {victim['attempt']} is in flight")
                new = spawn()
                out.say(f"spawn {new}")
                next_kill = clock.now() + rng.uniform(0.8, 1.8)
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, count(*) AS n FROM jobs GROUP BY status").fetchall()}
        sig = tuple(sorted(counts.items()))
        if sig != last_sig:
            last_sig, last_change = sig, clock.now()
        pending = counts.get("queued", 0) + counts.get("running", 0)
        if pending == 0 and len(kills) >= min(args.kills, 1):
            break
        if mode == "naive" and counts.get("queued", 0) == 0 and clock.now() - last_change > 8:
            break   # what is left is stuck in "running" with a dead worker: it will never finish
        if pending == 0 and clock.now() - last_change > 2:
            break
    for p in workers.values():
        if p.poll() is None:
            p.kill()
    for p in workers.values():
        p.wait()
    time.sleep(0.2)

    bill = httpx.get(f"{provider_url}/_bill", timeout=5).json()
    prov.kill()
    prov.wait()

    jobs = conn.execute("SELECT id, idempotency_key, status, attempts, error_kind, error, created_at, finished_at "
                        "FROM jobs ORDER BY created_at, idempotency_key").fetchall()
    attempts = conn.execute("SELECT job_id, attempt, worker_id, started_at, finished_at, outcome "
                            "FROM attempts ORDER BY started_at").fetchall()
    budget = conn.execute("SELECT * FROM budget").fetchone()
    conn.close()

    result = summarize(mode, args, reqs, posts, statuses, jobs, attempts, budget, bill, kills, crashed)
    result["log"] = [[round(t, 2), line] for t, line in out.lines]
    for line in result["check_lines"]:
        out.say(line)
    return result


def summarize(mode, args, reqs, posts, statuses, jobs, attempts, budget, bill, kills, crashed) -> dict:
    by_request = Counter(j["idempotency_key"].split("#")[0] for j in jobs)
    duplicate_jobs = sum(n - 1 for n in by_request.values())
    lost = [j for j in jobs if j["status"] not in ("succeeded", "failed")]
    dup_charges = sum(max(0, n - 1) for n in bill["charges_by_ref"].values())
    over_cap = max(0, bill["total_micros"] - args.cap)
    attempts_by_job: dict = {}
    for a in attempts:
        attempts_by_job.setdefault(a["job_id"], []).append(a)
    retried_permanent = 0
    for rows in attempts_by_job.values():
        rows.sort(key=lambda r: r["attempt"])
        for prev, _nxt in zip(rows, rows[1:]):
            if prev["outcome"] in PERMANENT:
                retried_permanent += 1
    unexplained = [j for j in jobs if j["status"] == "failed" and not (j["error"] or "").strip()]
    ledger_spent = budget["spent_micros"] if mode == "safe" else None
    under_count = (bill["total_micros"] > ledger_spent) if ledger_spent is not None else None

    checks = [
        {"id": "duplicate_jobs", "label": "Repeated POSTs that created a second job", "value": duplicate_jobs,
         "ok": duplicate_jobs == 0},
        {"id": "lost_jobs", "label": "Jobs lost (never finished) after workers were killed", "value": len(lost),
         "ok": not lost},
        {"id": "duplicate_charges", "label": "Requests the provider charged more than once", "value": dup_charges,
         "ok": dup_charges == 0},
        {"id": "over_cap", "label": "Provider charges above the spending cap", "value": fmt_usd(over_cap),
         "ok": over_cap == 0},
        {"id": "retried_permanent", "label": "Retries after an error that cannot succeed", "value": retried_permanent,
         "ok": retried_permanent == 0},
        {"id": "unexplained_failures", "label": "Failed jobs without a reason", "value": len(unexplained),
         "ok": not unexplained},
        {"id": "ledger_covers_bill", "label": "Our ledger counted at least what the provider charged",
         "value": "not tracked" if under_count is None else ("no" if under_count else "yes"),
         "ok": under_count is False},
        {"id": "worker_crashes", "label": "Workers that crashed on their own", "value": len(crashed),
         "ok": not crashed},
    ]
    outcomes = Counter(a["outcome"] or "in flight" for a in attempts)
    stats = {
        "requests": len(reqs), "posts": posts, "http_status": {str(k): v for k, v in statuses.items()},
        "jobs": len(jobs), "succeeded": sum(j["status"] == "succeeded" for j in jobs),
        "failed": Counter(j["error_kind"] or "error" for j in jobs if j["status"] == "failed"),
        "stuck": len(lost), "attempts": len(attempts), "attempt_outcomes": dict(outcomes),
        "workers_killed": len(kills), "provider_calls": bill["calls"], "provider_charges": bill["charges"],
        "provider_billed_micros": bill["total_micros"], "cap_micros": args.cap, "ledger_spent_micros": ledger_spent,
    }
    passed = sum(c["ok"] for c in checks)
    lines = [f"-- {mode} mode checks: {passed}/{len(checks)} passed"] + [
        f"   {'PASS' if c['ok'] else 'FAIL'}  {c['label']}: {c['value']}" for c in checks]
    t0 = min([a["started_at"] for a in attempts], default=None)
    return {
        "mode": mode, "checks": checks, "stats": stats, "check_lines": lines,
        "kills": kills,
        "timeline": {
            "t0": t0.isoformat() if t0 else None,
            "jobs": [{"id": str(j["id"]), "key": j["idempotency_key"], "status": j["status"],
                      "error_kind": j["error_kind"]} for j in jobs],
            "attempts": [{"job_id": str(a["job_id"]), "attempt": a["attempt"], "worker": a["worker_id"],
                          "start": (a["started_at"] - t0).total_seconds(),
                          "end": (a["finished_at"] - t0).total_seconds() if a["finished_at"] else None,
                          "outcome": a["outcome"]} for a in attempts] if t0 else [],
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["safe", "naive", "both"], default="both")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--jobs", type=int, default=40)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--kills", type=int, default=5)
    ap.add_argument("--cap", type=int, default=42_000, help="spending cap in micro-dollars (illustrative)")
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--out", default=str(ROOT / "report"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    base = Settings.from_env()
    db_url = ensure_database(base.database_url, "jobs_chaos")
    modes = ["naive", "safe"] if args.mode == "both" else [args.mode]
    results = {m: run_mode(m, args, base, db_url) for m in modes}

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    params = {k: getattr(args, k) for k in ("seed", "jobs", "workers", "kills", "cap")}
    (outdir / "chaos.json").write_text(json.dumps({"params": params, "results": results}, indent=1, default=str),
                                       encoding="utf-8")
    write_report(outdir / "chaos.html", params, results)
    print(f"\nreport: {outdir / 'chaos.html'}")
    safe = results.get("safe")
    if safe and not all(c["ok"] for c in safe["checks"]):
        print("SAFE MODE CHECK FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
