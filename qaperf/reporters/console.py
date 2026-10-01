from __future__ import annotations

from qaperf.results import RunResult

ICON = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "missing": "MISS", "error": "ERROR"}


def fmt_value(value, unit: str) -> str:
    if value is None:
        return "-"
    text = f"{value:,.0f}" if abs(value) >= 100 else f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{text} {unit}".strip()


def render(run: RunResult) -> str:
    lines = [f"\n=== {run.scenario} [{run.environment}] -> {ICON[run.status]}   ({run.run_id})"]
    for t in run.tests:
        lines.append(f"  test {t.test_id} ({t.type}): {t.status}, {t.duration_s}s")
        if t.error:
            lines.append("    " + t.error.splitlines()[0])
        for f in t.top_failures[:3]:
            lines.append(f"    failure x{f['count']}: {f['name']} - {(f['error'] or '')[:120]}")
    if run.budgets:
        lines.append("  budgets:")
        width = max(len(b.key) for b in run.budgets)
        order = {"fail": 0, "missing": 1, "warn": 2, "pass": 3}
        for b in sorted(run.budgets, key=lambda b: order[b.status]):
            limit = f"warn {fmt_value(b.warn, b.unit)} / fail {fmt_value(b.fail, b.unit)}"
            cmp = "<=" if b.direction == "max" else ">="
            lines.append(f"    {ICON[b.status]:5} {b.key:<{width}}  {fmt_value(b.value, b.unit):>12}  ({cmp} {limit})")
    if run.regressions:
        lines.append("  regressions vs baseline:")
        for r in run.regressions:
            lines.append(f"    {r.severity.upper():5} {r.key}: {fmt_value(r.value, r.unit)} vs "
                         f"{fmt_value(r.baseline, r.unit)} baseline ({r.change_pct:+.0f}%)")
    return "\n".join(lines)
