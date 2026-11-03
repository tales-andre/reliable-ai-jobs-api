"""The "before" picture: a worker written the way many first versions are.

Used only by the chaos run, with chaos/naive_api.py, to compare against
jobsapi.api + jobsapi.worker at the same fault rates.
It deliberately has none of the safeguards:
  * no lease: if it dies mid-job, the job stays "running" forever;
  * retries every error at once, up to 5 times, including errors that will never succeed;
  * no Idempotency-Key, so a retry after a timeout is new, paid work;
  * no budget check before calling.
"""
from __future__ import annotations

import argparse
import sys
import time

from jobsapi.config import Settings
from jobsapi.db import connect
from jobsapi.providers import FakeProvider, Generation
from jobsapi.retry import ProviderError


class NaiveProvider(FakeProvider):
    def generate(self, prompt, max_tokens, idempotency_key, request_ref="", job_ref=""):
        # No Idempotency-Key. X-Request-Ref and X-Job-Ref are labels for the chaos
        # report's bill only; the fake provider does not deduplicate on them.
        resp = self._post(f"{self.settings.fake_provider_url}/v1/generate",
                          {"prompt": prompt, "max_tokens": max_tokens},
                          {"X-Request-Ref": request_ref, "X-Job-Ref": job_ref})
        b = resp.json()
        return Generation(b["text"], b["usage"]["input_tokens"], b["usage"]["output_tokens"],
                          b["usage"]["cost_micros"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    args = ap.parse_args(argv)
    s = Settings.from_env()
    provider = NaiveProvider(s)
    conn = connect(s.database_url)
    w = args.id
    print(f"{w} started (naive: no lease, no idempotency key, no budget check)", flush=True)
    while True:
        job = conn.execute(
            "UPDATE jobs SET status = 'running', worker_id = %s, updated_at = now() WHERE id = "
            "(SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) "
            "RETURNING *", (w,)).fetchone()
        if job is None:
            time.sleep(s.poll_interval_s)
            continue
        ref = job["idempotency_key"].split("#")[0]
        last = ""
        for attempt in range(1, 6):
            conn.execute("UPDATE jobs SET attempts = %s WHERE id = %s", (attempt, job["id"]))
            conn.execute("INSERT INTO attempts (job_id, attempt, worker_id) VALUES (%s, %s, %s)",
                         (job["id"], attempt, w))
            try:
                gen = provider.generate(job["prompt"], job["max_tokens"], "", ref, job["idempotency_key"])
            except ProviderError as e:
                last = e.message
                conn.execute("UPDATE attempts SET finished_at = clock_timestamp(), outcome = %s "
                             "WHERE job_id = %s AND attempt = %s", (e.kind, job["id"], attempt))
                print(f"{w} job {str(job['id'])[:8]} attempt {attempt}: {e.kind}, retrying now", flush=True)
                continue
            conn.execute("UPDATE attempts SET finished_at = clock_timestamp(), outcome = 'ok' "
                         "WHERE job_id = %s AND attempt = %s", (job["id"], attempt))
            conn.execute("UPDATE jobs SET status = 'succeeded', result = %s, finished_at = clock_timestamp() WHERE id = %s",
                         (gen.text, job["id"]))
            print(f"{w} job {str(job['id'])[:8]} attempt {attempt}: ok", flush=True)
            break
        else:
            conn.execute("UPDATE jobs SET status = 'failed', error = %s, finished_at = clock_timestamp() WHERE id = %s",
                         (last, job["id"]))
            print(f"{w} job {str(job['id'])[:8]}: failed after 5 tries", flush=True)


if __name__ == "__main__":
    sys.exit(main())
