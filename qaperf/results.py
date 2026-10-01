"""The one result format every runner produces.

Every number is a Metric whose key is  <runner>/<test_id>/<subject>/<stat>,
e.g.  web_load/hq_browse/Aggregated/p95_ms  or  mobile_perf/device/cold_start/p50_ms.
Budgets, regression checks, history and all reporters work only on these keys,
so they behave identically for web, mobile device and mobile load tests.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Stats where a bigger number is good; everything else is lower-is-better.
HIGHER_IS_BETTER_STATS = {"rps", "requests", "success_rate_pct", "samples"}


@dataclass
class Metric:
    runner: str
    test_id: str
    subject: str
    stat: str
    value: float
    unit: str = ""
    higher_is_better: bool | None = None

    def __post_init__(self):
        if self.higher_is_better is None:
            self.higher_is_better = self.stat in HIGHER_IS_BETTER_STATS

    @property
    def key(self) -> str:
        return f"{self.runner}/{self.test_id}/{self.subject}/{self.stat}"


@dataclass
class TestResult:
    __test__ = False  # not a pytest test class
    test_id: str
    type: str
    status: str = "ok"  # ok | error
    started: str = ""
    duration_s: float = 0.0
    metrics: list[Metric] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)  # label -> path relative to run dir
    notes: list[str] = field(default_factory=list)
    error: str = ""
    top_failures: list[dict] = field(default_factory=list)


@dataclass
class BudgetResult:
    pattern: str
    key: str
    value: float | None
    unit: str
    warn: float | None
    fail: float | None
    direction: str  # max: value must stay below; min: value must stay above
    status: str  # pass | warn | fail | missing


@dataclass
class Regression:
    key: str
    value: float
    baseline: float
    change_pct: float
    unit: str
    severity: str  # warn | fail


@dataclass
class RunResult:
    run_id: str
    scenario: str
    scenario_path: str
    environment: str
    host: str
    started: str
    finished: str = ""
    status: str = "pass"  # pass | warn | fail | error
    tests: list[TestResult] = field(default_factory=list)
    budgets: list[BudgetResult] = field(default_factory=list)
    regressions: list[Regression] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def metrics(self) -> list[Metric]:
        return [m for t in self.tests for m in t.metrics]

    def to_dict(self) -> dict:
        return asdict(self)

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> "RunResult":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        tests = [TestResult(**{**t, "metrics": [Metric(**m) for m in t["metrics"]]}) for t in d.pop("tests")]
        budgets = [BudgetResult(**b) for b in d.pop("budgets")]
        regressions = [Regression(**r) for r in d.pop("regressions")]
        return cls(**d, tests=tests, budgets=budgets, regressions=regressions)


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (same method as numpy's default)."""
    if not values:
        return math.nan
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    rank = (len(s) - 1) * pct / 100
    lo, hi = math.floor(rank), math.ceil(rank)
    return float(s[lo] + (s[hi] - s[lo]) * (rank - lo))


def summarize(values: list[float]) -> dict:
    vals = [v for v in values if v is not None and not math.isnan(v)]
    if not vals:
        return {}
    return {
        "p50": percentile(vals, 50),
        "p90": percentile(vals, 90),
        "p95": percentile(vals, 95),
        "max": max(vals),
        "min": min(vals),
        "avg": sum(vals) / len(vals),
        "count": len(vals),
    }
