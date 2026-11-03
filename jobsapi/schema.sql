-- One row per client request. The unique idempotency key is what makes a
-- repeated POST return the same job instead of creating a second one.
CREATE TABLE IF NOT EXISTS jobs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key text        NOT NULL UNIQUE,
    request_hash    text        NOT NULL,
    prompt          text        NOT NULL,
    max_tokens      integer     NOT NULL CHECK (max_tokens > 0),
    dry_run         boolean     NOT NULL DEFAULT false,
    status          text        NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    attempts        integer     NOT NULL DEFAULT 0,
    max_attempts    integer     NOT NULL,
    run_after       timestamptz NOT NULL DEFAULT now(),
    lease_until     timestamptz,
    worker_id       text,
    estimate_micros bigint      NOT NULL,
    result          text,
    error_kind      text,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);
CREATE INDEX IF NOT EXISTS jobs_ready_idx ON jobs (run_after) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS jobs_lease_idx ON jobs (lease_until) WHERE status = 'running';

-- One row per try. finished_at IS NULL means the call may be in flight.
CREATE TABLE IF NOT EXISTS attempts (
    job_id        uuid        NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    attempt       integer     NOT NULL,
    worker_id     text        NOT NULL,
    started_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at   timestamptz,
    outcome       text,
    http_status   integer,
    retry_after_s real,
    next_delay_s  real,
    detail        text,
    PRIMARY KEY (job_id, attempt)
);

-- Single-row spending account. reserved = money set aside for calls in flight.
CREATE TABLE IF NOT EXISTS budget (
    id              integer PRIMARY KEY CHECK (id = 1),
    cap_micros      bigint  NOT NULL CHECK (cap_micros >= 0),
    reserved_micros bigint  NOT NULL DEFAULT 0 CHECK (reserved_micros >= 0),
    spent_micros    bigint  NOT NULL DEFAULT 0 CHECK (spent_micros >= 0)
);

-- Append-only ledger. Each provider call has one 'reserve' row and at most one
-- 'close' row; the primary key is what makes settling idempotent. A job can get
-- one 'refund' row when a deduplicating provider confirms what it really charged.
CREATE TABLE IF NOT EXISTS ledger (
    call_key      text        NOT NULL,
    job_id        uuid        NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    kind          text        NOT NULL CHECK (kind IN ('reserve', 'close', 'refund')),
    amount_micros bigint      NOT NULL CHECK (amount_micros >= 0),
    reason        text,
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (call_key, kind)
);
