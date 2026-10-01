"""Run history (one JSON line per run) and baseline regression detection.

Budgets catch "too slow in absolute terms"; regressions catch "slower than we
usually are" - e.g. login p95 jumping from 600ms to 1100ms is still under a
2000ms budget but almost certainly a real change worth flagging.
"""
from __future__ import annotations

import json
from fnmatch import fnmatchcase
from pathlib import Path

from qaperf.results import Metric, Regression, RunResult, percentile


class History:
    def __init__(self, path: Path):
        self.path = Path(path)

    def entries(self, scenario: str, environment: str) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("scenario") == scenario and e.get("environment") == environment:
                out.append(e)
        return out

    def append(self, run: RunResult) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "run_id": run.run_id,
            "scenario": run.scenario,
            "environment": run.environment,
            "started": run.started,
            "status": run.status,
            "metrics": {m.key: m.value for m in run.metrics},
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def _unit_family(unit: str) -> str:
    return {"ms": "ms", "s": "s", "MB": "mb", "mb": "mb", "%": "pct", "pct": "pct"}.get(unit, unit)


def find_regressions(metrics: list[Metric], past: list[dict], cfg: dict) -> list[Regression]:
    if not cfg.get("enabled", True):
        return []
    usable = [e for e in past if e.get("status") != "error"][-int(cfg.get("baseline_runs", 5)):]
    if not usable:
        return []
    threshold = float(cfg.get("threshold_pct", 25))
    floors = cfg.get("min_abs_delta") or {}
    patterns = cfg.get("metrics") or []
    severity = "fail" if cfg.get("fail") else "warn"
    out = []
    for m in metrics:
        if not any(fnmatchcase(m.key, p) for p in patterns):
            continue
        prev = [e["metrics"][m.key] for e in usable if m.key in e.get("metrics", {})]
        if not prev:
            continue
        baseline = percentile(prev, 50)
        if baseline <= 0:
            continue
        delta = (baseline - m.value) if m.higher_is_better else (m.value - baseline)
        change_pct = 100 * delta / baseline
        if change_pct > threshold and delta >= float(floors.get(_unit_family(m.unit), 0)):
            out.append(Regression(m.key, m.value, baseline, round(change_pct, 1), m.unit, severity))
    return out


def series(past: list[dict], key: str, limit: int = 20) -> list[float]:
    return [e["metrics"][key] for e in past if key in e.get("metrics", {})][-limit:]
