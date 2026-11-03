"""The chaos run, small: safeguarded workers pass every check, the naive ones do not."""
import json

import pytest

from chaos.run import main

pytestmark = pytest.mark.slow


def test_chaos_run_small(tmp_path):
    code = main(["--seed", "7", "--jobs", "16", "--workers", "3", "--kills", "2", "--cap", "1000000",
                 "--out", str(tmp_path), "--quiet"])
    assert code == 0
    data = json.loads((tmp_path / "chaos.json").read_text(encoding="utf-8"))
    safe = {c["id"]: c for c in data["results"]["safe"]["checks"]}
    naive = {c["id"]: c for c in data["results"]["naive"]["checks"]}
    assert all(c["ok"] for c in safe.values()), safe
    assert data["results"]["safe"]["stats"]["workers_killed"] >= 1
    # Deterministic for this seed: repeated POSTs and retries without a key.
    assert not naive["duplicate_jobs"]["ok"]
    assert not naive["duplicate_charges"]["ok"]
    assert (tmp_path / "chaos.html").read_text(encoding="utf-8").count("<svg") == 2
