"""Static HTML report for a chaos run (no JavaScript, no external files)."""
from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path

from jobsapi.costs import fmt_usd

COLORS = {
    "ok": ("#2e7d32", "reply, paid"),
    "replayed": ("#14b8a6", "stored reply, not paid again"),
    "slow": ("#2e7d32", "slow reply"),
    "rate_limited": ("#f59e0b", "429 rate limited"),
    "server_error": ("#f97316", "500 server error"),
    "timeout": ("#dc2626", "timeout (provider may have charged)"),
    "worker_lost": ("#111827", "worker killed mid-call"),
    "lease_wait": ("#a8b3c2", "job held until the lease expired, then taken back"),
    "budget_exceeded": ("#7c3aed", "refused: spending cap"),
    "bad_request": ("#9f1239", "400, not retryable"),
    "lease_lost": ("#6b7280", "late result discarded"),
    "dry_run": ("#94a3b8", "dry run"),
    None: ("#cbd5e1", "never finished"),
}

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f5f7fa;color:#17202b;font:15px/1.5 'Segoe UI',system-ui,-apple-system,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 24px 60px}
h1{font-size:28px;margin:0 0 4px}h2{font-size:20px;margin:34px 0 10px}
.sub{color:#4a5767;margin:0 0 18px}
code{background:#e8edf3;padding:1px 6px;border-radius:4px;font-size:13px}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #d6dde6;border-radius:8px;overflow:hidden}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid #edf1f5;vertical-align:top}
th{background:#eef2f6;font-size:12px;letter-spacing:.05em;text-transform:uppercase;color:#4a5767}
.b{display:inline-block;min-width:52px;text-align:center;padding:2px 8px;border-radius:6px;font-weight:700;
   font-size:12px;color:#fff;margin-right:8px}
.pass{background:#2e7d32}.fail{background:#c62828}.na{background:#64748b}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin:8px 0 12px;font-size:13px;color:#334155}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend i{width:16px;height:10px;border-radius:2px;display:inline-block}
.panel{background:#fff;border:1px solid #d6dde6;border-radius:8px;padding:12px;overflow-x:auto}
.note{color:#4a5767;font-size:14px}
.summary{display:flex;gap:14px;flex-wrap:wrap;margin:16px 0 4px}
.card{flex:1 1 240px;background:#fff;border:1px solid #d6dde6;border-radius:8px;padding:14px 16px}
.card h3{margin:0 0 6px;font-size:15px}
.big{font-size:22px;font-weight:700}
"""


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def timeline_svg(result: dict, max_lanes: int = 60) -> str:
    tl = result["timeline"]
    if not tl["attempts"]:
        return "<p>No attempts recorded.</p>"
    jobs = {j["id"]: j for j in tl["jobs"]}
    by_job: dict[str, list] = {}
    for a in tl["attempts"]:
        by_job.setdefault(a["job_id"], []).append(a)
    order = sorted(by_job, key=lambda j: min(a["start"] for a in by_job[j]))[:max_lanes]
    t0 = _t(tl["t0"])
    kills = [{**k, "rel": (_t(k["at"]) - t0).total_seconds()} for k in result["kills"]]
    kill_at = {(k["job_id"], k["attempt"]): k["rel"] for k in kills}
    t_end = max([a["end"] or a["start"] for a in tl["attempts"]] + [k["rel"] for k in kills]) + 0.5
    label_w, lane_h, top, width = 170, 15, 26, 1120
    plot_w = width - label_w - 20
    height = top + lane_h * len(order) + 30
    x = lambda t: label_w + plot_w * max(0.0, t) / t_end  # noqa: E731
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'font-family="Segoe UI, system-ui, sans-serif" font-size="11">']
    step = 1 if t_end <= 12 else 2 if t_end <= 30 else 5
    for s in range(0, int(t_end) + 1, step):
        parts.append(f'<line x1="{x(s):.1f}" y1="{top - 6}" x2="{x(s):.1f}" y2="{height - 24}" stroke="#eef2f6"/>'
                     f'<text x="{x(s):.1f}" y="{height - 10}" fill="#64748b" text-anchor="middle">{s}s</text>')
    for i, jid in enumerate(order):
        y = top + i * lane_h
        job = jobs.get(jid, {})
        mark = {"succeeded": "#2e7d32", "failed": "#c62828"}.get(job.get("status"), "#94a3b8")
        parts.append(f'<circle cx="8" cy="{y + 6}" r="4" fill="{mark}"/>'
                     f'<text x="18" y="{y + 10}" fill="#334155">{escape(job.get("key", jid[:8])[:24])}</text>')
        atts = sorted(by_job[jid], key=lambda a: a["attempt"])
        for prev, nxt in zip(atts, atts[1:]):
            if prev["end"] is not None:
                parts.append(f'<line x1="{x(prev["end"]):.1f}" y1="{y + 6}" x2="{x(nxt["start"]):.1f}" '
                             f'y2="{y + 6}" stroke="#cbd5e1" stroke-dasharray="2 2"/>')
        for a in atts:
            color, _ = COLORS.get(a["outcome"], COLORS[None])
            end = a["end"] if a["end"] is not None else t_end
            kill = kill_at.get((jid, a["attempt"]))
            if a["outcome"] == "worker_lost" and kill is not None and kill < end:
                # killed at `kill`; the job waits for the lease to expire before it is taken back
                parts.append(f'<rect x="{x(kill):.1f}" y="{y + 3}" width="{max(2.0, x(end) - x(kill)):.1f}" '
                             f'height="{lane_h - 8}" rx="2" fill="{COLORS["lease_wait"][0]}"/>')
                end = kill
            w = max(4.0, x(end) - x(a["start"]))
            parts.append(f'<rect x="{x(a["start"]):.1f}" y="{y + 1}" width="{w:.1f}" height="{lane_h - 4}" '
                         f'rx="2" fill="{color}"><title>{escape(job.get("key", ""))} attempt {a["attempt"]} '
                         f'({a["worker"]}): {escape(str(a["outcome"]))}</title></rect>')
    for k in kills:
        kx = x(k["rel"])
        parts.append(f'<line x1="{kx:.1f}" y1="{top - 8}" x2="{kx:.1f}" y2="{height - 24}" stroke="#111827" '
                     f'stroke-width="1.2" stroke-dasharray="4 3"/><text x="{kx + 3:.1f}" y="{top - 10}" '
                     f'fill="#111827" font-weight="700">{escape(k["worker"])} killed</text>')
    parts.append("</svg>")
    return "".join(parts)


def legend() -> str:
    seen = set()
    items = []
    for key, (color, label) in COLORS.items():
        if key in ("slow",) or label in seen:
            continue
        seen.add(label)
        items.append(f'<span><i style="background:{color}"></i>{escape(label)}</span>')
    return f'<div class="legend">{"".join(items)}</div>'


def badge(ok: bool | None) -> str:
    if ok is None:
        return '<span class="b na">N/A</span>'
    return '<span class="b pass">PASS</span>' if ok else '<span class="b fail">FAIL</span>'


def write_report(path: Path, params: dict, results: dict) -> None:
    modes = [m for m in ("naive", "safe") if m in results]
    titles = {"naive": "Without safeguards", "safe": "With safeguards"}
    head = "".join(f"<th>{titles[m]}</th>" for m in modes)
    rows = []
    for i, c in enumerate(results[modes[-1]]["checks"]):
        cells = ""
        for m in modes:
            cm = results[m]["checks"][i]
            ok = None if cm["value"] == "not tracked" else cm["ok"]
            cells += f"<td>{badge(ok)}{escape(str(cm['value']))}</td>"
        rows.append(f"<tr><td>{escape(c['label'])}</td>{cells}</tr>")

    def stat_rows():
        keys = [("requests", "Client requests"), ("posts", "POSTs sent (client retries included)"),
                ("jobs", "Jobs created"), ("succeeded", "Jobs succeeded"), ("failed", "Jobs failed, by reason"),
                ("stuck", "Jobs stuck in 'running'"), ("attempts", "Provider attempts"),
                ("workers_killed", "Workers killed with kill -9"), ("provider_charges", "Charges on the provider bill"),
                ("provider_billed_micros", "Provider bill"), ("cap_micros", "Spending cap"),
                ("ledger_spent_micros", "Spent according to our ledger")]
        out = []
        for k, label in keys:
            cells = ""
            for m in modes:
                v = results[m]["stats"][k]
                if k.endswith("micros"):
                    v = "not tracked" if v is None else fmt_usd(v)
                elif isinstance(v, dict):
                    v = ", ".join(f"{a}: {b}" for a, b in sorted(v.items())) or "none"
                cells += f"<td>{escape(str(v))}</td>"
            out.append(f"<tr><td>{label}</td>{cells}</tr>")
        return "".join(out)

    cards = ""
    for m in modes:
        r = results[m]
        passed = sum(c["ok"] for c in r["checks"])
        cards += (f'<div class="card"><h3>{titles[m]}</h3><div class="big">{passed} of {len(r["checks"])} '
                  f'checks pass</div><div class="note">'
                  f'{"jobsapi.worker: idempotency key, lease, safe retries, reserved budget" if m == "safe" else "chaos/naive_worker.py: retries everything at once, no lease, no key, no budget"}'
                  f'</div></div>')

    sections = ""
    for m in reversed(modes):
        sections += (f"<h2>{titles[m]}: timeline</h2>"
                     f'<p class="note">One row per job, in order of first attempt. Each bar is one provider attempt; '
                     f'dashed gaps are backoff waits. Dashed vertical lines are workers killed with kill -9.</p>'
                     f'{legend()}<div class="panel">{timeline_svg(results[m])}</div>')

    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Chaos run report</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>Chaos run report</h1>
<p class="sub">Open-source demo with a fictional support-reply workload and a deliberately unreliable fake provider
(timeouts, 429 with Retry-After, 500, slow and duplicate replies). Seed <code>{params['seed']}</code>,
{params['jobs']} requests, {params['workers']} workers, up to {params['kills']} killed mid-call, spending cap
{fmt_usd(params['cap'])} (illustrative prices). Rerun with
<code>python -m chaos.run --seed {params['seed']}</code>.</p>
<div class="summary">{cards}</div>
<h2>Checks</h2>
<table><tr><th>Check</th>{head}</tr>{''.join(rows)}</table>
<p class="note">The same seed gives the same requests, repeated POSTs and provider faults. Which worker gets killed,
and when, depends on timing, so counts can move a little between runs. The checks in the
"With safeguards" column must pass on every run (the script exits non-zero otherwise).</p>
{sections}
<h2>Numbers behind the checks</h2>
<table><tr><th></th>{head}</tr>{stat_rows()}</table>
</div></body></html>"""
    path.write_text(html, encoding="utf-8")


if __name__ == "__main__":  # rebuild the HTML from a saved chaos.json
    import json
    import sys

    src = Path(sys.argv[1] if len(sys.argv) > 1 else "report/chaos.json")
    data = json.loads(src.read_text(encoding="utf-8"))
    write_report(src.with_suffix(".html"), data["params"], data["results"])
    print("wrote", src.with_suffix(".html"))
