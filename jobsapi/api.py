"""HTTP API: submit generation jobs and read their state.

POST /jobs requires an Idempotency-Key header. Sending the same key again
returns the same job (200) instead of creating a new one; the same key with a
different body is refused (409).
"""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import HTMLResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from . import ledger
from .config import Settings
from .costs import estimate_cost_micros, fmt_usd
from .db import init_schema
from .status_page import render_status_page


class JobRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    max_tokens: int = Field(default=300, ge=1, le=4000)
    dry_run: bool = False


def request_hash(req: JobRequest) -> str:
    canon = json.dumps(req.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


PUBLIC_FIELDS = ("id", "idempotency_key", "status", "attempts", "max_attempts", "dry_run", "max_tokens",
                 "estimate_micros", "result", "error_kind", "error", "created_at", "finished_at")


def public_job(row: dict, spent: int | None = None) -> dict:
    out = {k: row[k] for k in PUBLIC_FIELDS}
    out["id"] = str(out["id"])
    if spent is not None:
        out["spent_micros"] = spent
    return out


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    holder: dict[str, ConnectionPool] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # A new pool per startup, so the app can be started and stopped more than once (tests).
        holder["pool"] = ConnectionPool(settings.database_url, min_size=1, max_size=10, open=True,
                                        kwargs={"autocommit": True, "row_factory": dict_row})
        with holder["pool"].connection() as conn:
            init_schema(conn, settings.budget_cap_micros)
        yield
        holder.pop("pool").close()

    class _Pool:
        def connection(self):
            return holder["pool"].connection()

    pool = _Pool()
    app = FastAPI(title="Reliable AI jobs API", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings

    @app.post("/jobs", status_code=201)
    def submit_job(req: JobRequest, response: Response,
                   idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> dict:
        if not idempotency_key or not (1 <= len(idempotency_key) <= 200):
            raise HTTPException(400, "Idempotency-Key header is required (1-200 characters). "
                                     "Reuse the same key when you retry the same request.")
        h = request_hash(req)
        estimate = estimate_cost_micros(req.prompt, req.max_tokens, settings.price_in_micros,
                                        settings.price_out_micros)
        with pool.connection() as conn:
            existing = conn.execute("SELECT * FROM jobs WHERE idempotency_key = %s",
                                    (idempotency_key,)).fetchone()
            if existing is None and not (req.dry_run or settings.dry_run):
                b = ledger.snapshot(conn)
                if estimate > b.remaining_micros:
                    raise HTTPException(402, f"Spending cap: this job may cost up to {fmt_usd(estimate)} and "
                                             f"only {fmt_usd(b.remaining_micros)} of {fmt_usd(b.cap_micros)} "
                                             f"is left. Job not created.")
            if existing is None:
                existing = conn.execute(
                    "INSERT INTO jobs (idempotency_key, request_hash, prompt, max_tokens, dry_run, "
                    "max_attempts, estimate_micros) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (idempotency_key) DO NOTHING RETURNING *",
                    (idempotency_key, h, req.prompt, req.max_tokens, req.dry_run or settings.dry_run,
                     settings.max_attempts, estimate),
                ).fetchone()
                if existing is not None:
                    response.headers["Location"] = f"/jobs/{existing['id']}"
                    return public_job(existing)
                # Lost a race with a concurrent request carrying the same key.
                existing = conn.execute("SELECT * FROM jobs WHERE idempotency_key = %s",
                                        (idempotency_key,)).fetchone()
        if existing["request_hash"] != h:
            raise HTTPException(409, "This Idempotency-Key was already used with a different request body.")
        response.status_code = 200
        response.headers["Idempotent-Replayed"] = "true"
        response.headers["Location"] = f"/jobs/{existing['id']}"
        return public_job(existing)

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        with pool.connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id::text = %s", (job_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "job not found")
            attempts = conn.execute(
                "SELECT attempt, worker_id, started_at, finished_at, outcome, http_status, retry_after_s, "
                "next_delay_s, detail FROM attempts WHERE job_id = %s ORDER BY attempt", (row["id"],)
            ).fetchall()
            out = public_job(row, ledger.job_spent(conn, row["id"]))
        out["attempt_log"] = attempts
        return out

    @app.get("/jobs")
    def list_jobs(status: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[dict]:
        with pool.connection() as conn:
            if status:
                rows = conn.execute("SELECT * FROM jobs WHERE status = %s ORDER BY created_at DESC LIMIT %s",
                                    (status, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
        return [public_job(r) for r in rows]

    @app.get("/budget")
    def budget() -> dict:
        with pool.connection() as conn:
            b = ledger.snapshot(conn)
        return {"cap_micros": b.cap_micros, "reserved_micros": b.reserved_micros,
                "spent_micros": b.spent_micros, "remaining_micros": b.remaining_micros}

    @app.get("/", response_class=HTMLResponse)
    def status_page() -> str:
        with pool.connection() as conn:
            b = ledger.snapshot(conn)
            rows = conn.execute(
                "SELECT j.*, coalesce((SELECT sum(CASE l.kind WHEN 'close' THEN l.amount_micros "
                "WHEN 'refund' THEN -l.amount_micros END) FROM ledger l WHERE l.job_id = j.id), 0) AS spent "
                "FROM jobs j ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
            counts = conn.execute("SELECT status, count(*) AS n FROM jobs GROUP BY status").fetchall()
        return render_status_page(b, rows, {c["status"]: c["n"] for c in counts})

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    return app


def main() -> None:
    import os

    import uvicorn
    uvicorn.run(create_app(), host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
