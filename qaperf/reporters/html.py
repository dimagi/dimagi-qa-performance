"""Self-contained HTML report (no external assets) - one page per run, with trend sparklines."""
from __future__ import annotations

from html import escape

from qaperf.history import series
from qaperf.reporters.console import fmt_value
from qaperf.results import RunResult

STAT_ORDER = ["requests", "failures", "error_rate_pct", "rps", "avg_ms", "p50_ms", "p90_ms", "p95_ms",
              "p99_ms", "max_ms", "samples", "success_rate_pct", "p50_s", "max_s", "pss_mb", "peak_pss_mb",
              "avg_cpu_pct", "peak_cpu_pct", "janky_frames_pct", "frame_p90_ms"]

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1b;--muted:#6b6b66;--line:#e4e3de;--pass:#1f7a4d;--warn:#a05a00;
--fail:#b3261e;--miss:#6b6b66;--accent:#3b5bdb;--chip:#f0efea}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#161615;--card:#1f1f1d;--fg:#ecebe6;
--muted:#9c9b94;--line:#33322f;--pass:#4cc38a;--warn:#f0a43a;--fail:#f2726a;--miss:#9c9b94;--accent:#8ea4ff;--chip:#2a2a27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
.meta{color:var(--muted);font-size:13px}.pill{display:inline-block;padding:2px 10px;border-radius:999px;
font-weight:600;font-size:12px;letter-spacing:.04em;color:#fff}
.s-pass{background:var(--pass)}.s-warn{background:var(--warn)}.s-fail,.s-error{background:var(--fail)}.s-missing{background:var(--miss)}
.t-pass{color:var(--pass)}.t-warn{color:var(--warn)}.t-fail,.t-error{color:var(--fail)}.t-missing{color:var(--miss)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:18px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi b{display:block;font-size:24px}.kpi span{color:var(--muted);font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:4px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:7px 12px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:12px;color:var(--muted);font-weight:600}tr:last-child td{border-bottom:0}
td.num,th.num{text-align:right}code{font-size:12.5px;background:var(--chip);padding:1px 5px;border-radius:4px}
.note{color:var(--muted);font-size:13px;margin:6px 0}.err{color:var(--fail);white-space:pre-wrap;font-size:13px}
a{color:var(--accent)}svg.spark{vertical-align:middle}
"""


def _spark(values: list[float], current: float | None) -> str:
    vals = values + ([current] if current is not None else [])
    if len(vals) < 2:
        return ""
    w, h = 90, 22
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    pts = [(i * w / (len(vals) - 1), h - 2 - (v - lo) / span * (h - 4)) for i, v in enumerate(vals)]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    x, y = pts[-1]
    return (f'<svg class="spark" width="{w}" height="{h}" aria-label="trend over last {len(vals)} runs">'
            f'<polyline fill="none" stroke="var(--accent)" stroke-width="1.5" points="{path}"/>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="var(--accent)"/></svg>')


def render(run: RunResult, past: list[dict]) -> str:
    e = escape
    budgets = run.budgets
    counts = {s: sum(b.status == s for b in budgets) for s in ("pass", "warn", "fail", "missing")}
    out = [f"<!doctype html><html lang=en><head><meta charset=utf-8>"
           f"<meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>Perf report: {e(run.scenario)}</title><style>{CSS}</style></head><body><main>",
           f"<h1>{e(run.scenario)} <span class='pill s-{run.status}'>{run.status.upper()}</span></h1>",
           f"<div class=meta>{e(run.environment)} · {e(run.host or '')} · run <code>{e(run.run_id)}</code>"
           f" · {e(run.started[:19].replace('T', ' '))} UTC</div>",
           "<div class=kpis>",
           f"<div class=kpi><b>{len(run.tests)}</b><span>tests ({sum(t.status == 'error' for t in run.tests)} errored)</span></div>",
           f"<div class=kpi><b class=t-pass>{counts['pass']}</b><span>budgets passed</span></div>",
           f"<div class=kpi><b class=t-warn>{counts['warn'] + counts['missing']}</b><span>budget warnings</span></div>",
           f"<div class=kpi><b class=t-fail>{counts['fail']}</b><span>budgets failed</span></div>",
           f"<div class=kpi><b>{len(run.regressions)}</b><span>regressions vs baseline</span></div></div>"]

    if budgets:
        order = {"fail": 0, "missing": 1, "warn": 2, "pass": 3}
        out.append("<h2>Budgets</h2><div class=card><table><tr><th>Status</th><th>Metric</th>"
                   "<th class=num>Value</th><th class=num>Warn</th><th class=num>Fail</th><th>Trend</th></tr>")
        for b in sorted(budgets, key=lambda b: order[b.status]):
            out.append(f"<tr><td class='t-{b.status}'><b>{b.status.upper()}</b></td><td><code>{e(b.key)}</code></td>"
                       f"<td class=num>{fmt_value(b.value, b.unit)}</td><td class=num>{fmt_value(b.warn, b.unit)}</td>"
                       f"<td class=num>{fmt_value(b.fail, b.unit)}</td><td>{_spark(series(past, b.key), b.value)}</td></tr>")
        out.append("</table></div>")

    if run.regressions:
        out.append("<h2>Regressions vs baseline</h2><div class=card><table><tr><th>Metric</th>"
                   "<th class=num>Now</th><th class=num>Baseline (median)</th><th class=num>Change</th></tr>")
        for r in run.regressions:
            out.append(f"<tr><td><code>{e(r.key)}</code></td><td class=num>{fmt_value(r.value, r.unit)}</td>"
                       f"<td class=num>{fmt_value(r.baseline, r.unit)}</td>"
                       f"<td class='num t-{r.severity}'>{r.change_pct:+.0f}%</td></tr>")
        out.append("</table></div>")

    for t in run.tests:
        out.append(f"<h2>{e(t.test_id)} <span class=meta>({e(t.type)}, {t.duration_s}s)</span></h2>")
        if t.error:
            out.append(f"<div class=err>{e(t.error)}</div>")
        for n in t.notes:
            out.append(f"<div class=note>{e(n)}</div>")
        if t.artifacts:
            out.append("<div class=note>" + " · ".join(f"<a href='{e(p)}'>{e(k)}</a>" for k, p in t.artifacts.items())
                       + "</div>")
        subjects: dict[str, dict] = {}
        for m in t.metrics:
            subjects.setdefault(m.subject, {})[m.stat] = m
        if subjects:
            stats = [s for s in STAT_ORDER if any(s in v for v in subjects.values())]
            stats += sorted({s for v in subjects.values() for s in v} - set(stats))
            out.append("<div class=card><table><tr><th>Subject</th>"
                       + "".join(f"<th class=num>{e(s)}</th>" for s in stats) + "</tr>")
            rows = sorted(subjects.items(), key=lambda kv: (kv[0] == "Aggregated", kv[0]))
            for subject, ms in rows:
                cells = "".join(f"<td class=num>{fmt_value(ms[s].value, '') if s in ms else ''}</td>" for s in stats)
                out.append(f"<tr><td>{'<b>' if subject == 'Aggregated' else ''}{e(subject)}</td>{cells}</tr>")
            out.append("</table></div>")
        if t.top_failures:
            out.append("<div class=card style='margin-top:10px'><table><tr><th class=num>Count</th><th>Request</th><th>Error</th></tr>")
            for f in t.top_failures:
                out.append(f"<tr><td class=num>{f['count']}</td><td>{e(str(f['name']))}</td>"
                           f"<td style='white-space:normal'>{e(str(f['error'])[:300])}</td></tr>")
            out.append("</table></div>")
    out.append("<p class=note>Units: *_ms milliseconds, *_s seconds, *_mb megabytes (PSS), *_pct percent, "
               "rps requests/second. Budget/regression keys are runner/test/subject/stat.</p></main></body></html>")
    return "\n".join(out)
