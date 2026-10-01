"""Budgets: glob pattern over metric keys -> warn / fail thresholds.

    budgets:
      "web_load/*/Aggregated/p95_ms":         {warn: 2000, fail: 4000}
      "web_load/*/Aggregated/error_rate_pct": {fail: 1}
      "mobile_perf/*/cold_start/p50_ms":      {warn: 2500, fail: 4000}
      "*/*/Aggregated/rps":                   {fail: 5}   # rps is higher-is-better, so this is a floor

A pattern that matches no metric is reported as 'missing' (a warning) so a renamed
request or a test that silently didn't run can't make a budget quietly pass.
"""
from __future__ import annotations

from fnmatch import fnmatchcase

from qaperf.results import BudgetResult, Metric


def _breaches(value: float, limit: float | None, direction: str) -> bool:
    if limit is None:
        return False
    return value > limit if direction == "max" else value < limit


def evaluate_budgets(metrics: list[Metric], budgets: dict) -> list[BudgetResult]:
    results: list[BudgetResult] = []
    for pattern, spec in budgets.items():
        warn, fail = spec.get("warn"), spec.get("fail")
        matched = [m for m in metrics if fnmatchcase(m.key, pattern)]
        if not matched:
            results.append(BudgetResult(pattern, pattern, None, "", warn, fail,
                                        spec.get("direction", "max"), "missing"))
            continue
        for m in matched:
            direction = spec.get("direction") or ("min" if m.higher_is_better else "max")
            if _breaches(m.value, fail, direction):
                status = "fail"
            elif _breaches(m.value, warn, direction):
                status = "warn"
            else:
                status = "pass"
            results.append(BudgetResult(pattern, m.key, m.value, m.unit, warn, fail, direction, status))
    return results
