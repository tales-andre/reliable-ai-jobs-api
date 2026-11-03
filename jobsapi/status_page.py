"""A plain HTML page listing jobs and the spending cap. Refreshes itself every 2 seconds."""
from __future__ import annotations

from html import escape

from .costs import fmt_usd
from .ledger import Budget

STYLE = """
body{font:15px/1.45 system-ui,-apple-system,'Segoe UI',sans-serif;margin:0;background:#f6f7f9;color:#18202a}
header{background:#14324a;color:#fff;padding:18px 28px}
h1{font-size:20px;margin:0}
main{padding:20px 28px}
.cards{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:18px}
.card{background:#fff;border:1px solid #d9dee5;border-radius:8px;padding:10px 16px;min-width:130px}
.card b{display:block;font-size:20px}
.bar{height:10px;background:#e3e7ec;border-radius:5px;overflow:hidden;margin-top:6px;width:260px;display:flex}
.bar i{display:block;height:100%}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #d9dee5}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #edf0f3;font-size:14px;vertical-align:top}
th{background:#eef1f5;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#4b5868}
code{font-size:13px}
.s{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600}
.queued{background:#e6ecf5;color:#2c4a73}.running{background:#fff3d6;color:#7a5200}
.succeeded{background:#dff3e4;color:#1d6b33}.failed{background:#fbe1e1;color:#9b1c1c}
.err{color:#9b1c1c;max-width:520px}
"""


def render_status_page(b: Budget, rows: list[dict], counts: dict[str, int]) -> str:
    cap = max(b.cap_micros, 1)
    spent_pct = min(100, 100 * b.spent_micros / cap)
    res_pct = min(100 - spent_pct, 100 * b.reserved_micros / cap)
    cards = "".join(
        f'<div class="card">{escape(k)}<b>{counts.get(k, 0)}</b></div>'
        for k in ("queued", "running", "succeeded", "failed")
    )
    budget = (
        f'<div class="card">Spending cap<b>{fmt_usd(b.cap_micros)}</b>'
        f'spent {fmt_usd(b.spent_micros)} · reserved {fmt_usd(b.reserved_micros)}'
        f'<div class="bar"><i style="width:{spent_pct:.1f}%;background:#2f6fb0"></i>'
        f'<i style="width:{res_pct:.1f}%;background:#9cc0e6"></i></div></div>'
    )
    trs = []
    for r in rows:
        err = escape(r["error"] or "") if r["status"] != "succeeded" else ""
        trs.append(
            f"<tr><td><code>{str(r['id'])[:8]}</code></td><td><code>{escape(r['idempotency_key'][:28])}</code></td>"
            f'<td><span class="s {r["status"]}">{r["status"]}</span></td>'
            f"<td>{r['attempts']}</td><td>{fmt_usd(int(r['spent']))}</td>"
            f'<td class="err">{err}</td></tr>'
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'><meta http-equiv='refresh' content='2'>"
        f"<title>Job status</title><style>{STYLE}</style></head><body>"
        "<header><h1>Job status</h1></header><main>"
        f'<div class="cards">{cards}{budget}</div>'
        "<table><tr><th>Job</th><th>Idempotency key</th><th>Status</th><th>Attempts</th>"
        "<th>Spent</th><th>Last error</th></tr>"
        + "".join(trs)
        + "</table></main></body></html>"
    )
