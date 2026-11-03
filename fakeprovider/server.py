"""HTTP wrapper around FakeProviderCore.

    python -m fakeprovider --port 8090 --seed 42 --chaos

POST /v1/generate   {"prompt": "...", "max_tokens": 200}   headers: Idempotency-Key, X-Request-Ref, X-Job-Ref
GET  /_bill         what the provider charged (used by the chaos report)
"""
from __future__ import annotations

import argparse
import asyncio

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from .core import CHAOS_RATES, FakeProviderCore


def create_app(core: FakeProviderCore) -> FastAPI:
    app = FastAPI(title="Fake unreliable provider")

    @app.post("/v1/generate")
    async def generate(request: Request,
                       idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                       request_ref: str = Header(default="", alias="X-Request-Ref"),
                       job_ref: str = Header(default="", alias="X-Job-Ref")):
        body = await request.json()
        action = core.handle(idempotency_key, str(body.get("prompt", "")), int(body.get("max_tokens", 100)),
                             request_ref, job_ref)
        if action.delay_s:
            # The work is done and charged already; the reply is just late.
            await asyncio.sleep(action.delay_s)
        return JSONResponse(action.body, status_code=action.status, headers=action.headers)

    @app.get("/_bill")
    def bill():
        return core.bill()

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--chaos", action="store_true", help="turn on timeouts, 429s, 500s and slow replies")
    ap.add_argument("--timeout-delay", type=float, default=1.6)
    ap.add_argument("--delay", type=float, default=0.0, help="add this delay to every successful reply")
    args = ap.parse_args(argv)
    core = FakeProviderCore(seed=args.seed, rates=CHAOS_RATES if args.chaos else None,
                            timeout_delay_s=args.timeout_delay, fixed_delay_s=args.delay)
    uvicorn.run(create_app(core), host=args.host, port=args.port, log_level="warning")
