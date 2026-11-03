"""Database helpers (psycopg 3)."""
from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


def connect(url: str, autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=autocommit, row_factory=dict_row)


def init_schema(conn: psycopg.Connection, cap_micros: int) -> None:
    """Create tables if missing and make sure the budget row exists.

    When the schema is already there, no DDL runs: a worker starting next to busy
    workers must not take table locks (CREATE INDEX IF NOT EXISTS still locks the
    table and can deadlock with running transactions).
    """
    ready = conn.execute("SELECT to_regclass('public.ledger') IS NOT NULL AS ok").fetchone()
    if ready["ok"] and conn.execute("SELECT 1 FROM budget WHERE id = 1").fetchone():
        return
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(4242)")  # several processes may start at once
        conn.execute(SCHEMA)
        conn.execute(
            "INSERT INTO budget (id, cap_micros) VALUES (1, %s) ON CONFLICT (id) DO NOTHING",
            (cap_micros,),
        )


def reset_schema(conn: psycopg.Connection, cap_micros: int) -> None:
    """Drop everything and start from an empty database (tests and chaos run only)."""
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS ledger, attempts, budget, jobs CASCADE")
    init_schema(conn, cap_micros)


def ensure_database(admin_url: str, name: str) -> str:
    """Create database `name` on the server of `admin_url` if needed; return its URL."""
    with psycopg.connect(admin_url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{name}"')
    base, _, _ = admin_url.rpartition("/")
    return f"{base}/{name}"
