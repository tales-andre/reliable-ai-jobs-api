"""The "before" API: what a first version of POST /jobs often looks like.

Used only by the chaos run, as the API half of the naive side (the worker half is
chaos/naive_worker.py). It deliberately has none of the safeguards of jobsapi/api.py:
  * no Idempotency-Key: every POST creates a new job, so a client retry is a second job;
  * no spending check before the job is accepted.

The X-Request-Ref header is only a label so the chaos report can group the jobs of
one client request; the API does not use it to deduplicate anything.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from jobsapi.api import JobRequest
from jobsapi.config import Settings
from jobsapi.costs import estimate_cost_micros


def create_naive_app(settings: Settings) -> FastAPI:
    holder: dict[str, ConnectionPool] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        holder["pool"] = ConnectionPool(settings.database_url, min_size=1, max_size=10, open=True,
                                        kwargs={"autocommit": True, "row_factory": dict_row})
        yield
        holder.pop("pool").close()

    app = FastAPI(title="Naive jobs API (chaos comparison only)", lifespan=lifespan)

    @app.post("/jobs", status_code=201)
    def submit_job(req: JobRequest,
                   request_ref: Annotated[str, Header(alias="X-Request-Ref")] = "") -> dict:
        est = estimate_cost_micros(req.prompt, req.max_tokens, settings.price_in_micros,
                                   settings.price_out_micros)
        with holder["pool"].connection() as conn:
            row = conn.execute(
                "INSERT INTO jobs (idempotency_key, request_hash, prompt, max_tokens, max_attempts, "
                "estimate_micros) VALUES (%s, '', %s, %s, 5, %s) RETURNING id",
                (f"{request_ref or 'req'}#{uuid.uuid4().hex[:8]}", req.prompt, req.max_tokens, est),
            ).fetchone()
        return {"id": str(row["id"])}

    return app
