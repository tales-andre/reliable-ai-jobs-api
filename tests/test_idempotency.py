"""POST /jobs: a repeated request never creates a second job."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings as hsettings
from hypothesis import strategies as st

from jobsapi.api import create_app

BODY = {"prompt": "Summarise the return policy for order 1042.", "max_tokens": 200}


@pytest.fixture
def client(conn, settings):
    with TestClient(create_app(settings)) as c:
        yield c


def count_jobs(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]


def test_key_is_required(client, conn):
    r = client.post("/jobs", json=BODY)
    assert r.status_code == 400
    assert "Idempotency-Key" in r.json()["detail"]
    assert count_jobs(conn) == 0


@hsettings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(n=st.integers(1, 20), key=st.text(st.characters(codec="ascii", categories=["L", "N"]), min_size=1, max_size=40))
def test_n_repeats_make_one_job(client, conn, n, key):
    """Property: N identical POSTs with the same key -> exactly 1 job, same id every time."""
    conn.execute("TRUNCATE jobs CASCADE")
    responses = [client.post("/jobs", json=BODY, headers={"Idempotency-Key": key}) for _ in range(n)]
    assert [r.status_code for r in responses] == [201] + [200] * (n - 1)
    assert len({r.json()["id"] for r in responses}) == 1
    assert count_jobs(conn) == 1


def test_concurrent_repeats_make_one_job(client, conn):
    """16 copies of the same request sent at the same time (a client retrying fast)."""
    def post(_):
        return client.post("/jobs", json=BODY, headers={"Idempotency-Key": "order-1042"})
    with ThreadPoolExecutor(16) as ex:
        responses = list(ex.map(post, range(16)))
    assert sorted(r.status_code for r in responses) == [200] * 15 + [201]
    assert len({r.json()["id"] for r in responses}) == 1
    assert count_jobs(conn) == 1


def test_same_key_different_body_is_refused(client, conn):
    assert client.post("/jobs", json=BODY, headers={"Idempotency-Key": "k1"}).status_code == 201
    r = client.post("/jobs", json={**BODY, "max_tokens": 999}, headers={"Idempotency-Key": "k1"})
    assert r.status_code == 409
    assert count_jobs(conn) == 1


def test_job_over_remaining_budget_is_not_created(conn, settings):
    with TestClient(create_app(replace(settings, budget_cap_micros=100))) as c:
        conn.execute("UPDATE budget SET cap_micros = 100")
        r = c.post("/jobs", json=BODY, headers={"Idempotency-Key": "big"})
    assert r.status_code == 402
    assert "Spending cap" in r.json()["detail"]
    assert count_jobs(conn) == 0


def test_dry_run_job_is_accepted_even_without_budget(conn, settings):
    with TestClient(create_app(settings)) as c:
        conn.execute("UPDATE budget SET cap_micros = 0")
        r = c.post("/jobs", json={**BODY, "dry_run": True}, headers={"Idempotency-Key": "dry"})
    assert r.status_code == 201
    assert r.json()["dry_run"] is True


def test_get_job_shows_attempts_and_spend(client):
    job = client.post("/jobs", json=BODY, headers={"Idempotency-Key": "g1"}).json()
    r = client.get(f"/jobs/{job['id']}")
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert r.json()["spent_micros"] == 0
    assert r.json()["attempt_log"] == []
    assert client.get("/jobs/00000000-0000-0000-0000-000000000000").status_code == 404
