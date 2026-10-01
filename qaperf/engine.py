"""Runs a scenario end to end: tests -> budgets -> regressions -> status -> history -> reports."""
from __future__ import annotations

import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from qaperf.budgets import evaluate_budgets
from qaperf.config import Scenario
from qaperf.history import History, find_regressions
from qaperf.results import RunResult, TestResult
from qaperf.runners import RUNNERS
from qaperf.runners.base import RunOptions, RunnerError


def make_run_id(scenario: Scenario) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", scenario.name).strip("-").lower()
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{slug}"


def overall_status(run: RunResult) -> str:
    if any(t.status == "error" for t in run.tests):
        return "error"
    if any(b.status == "fail" for b in run.budgets) or any(r.severity == "fail" for r in run.regressions):
        return "fail"
    if any(b.status in ("warn", "missing") for b in run.budgets) or run.regressions:
        return "warn"
    return "pass"


def run_scenario(scenario: Scenario, options: RunOptions, log=print) -> tuple[RunResult, Path]:
    run_id = make_run_id(scenario)
    run_dir = options.results_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run = RunResult(run_id=run_id, scenario=scenario.name, scenario_path=str(scenario.path),
                    environment=scenario.environment, host=scenario.host,
                    started=datetime.now(timezone.utc).isoformat())

    for test in scenario.tests:
        runner = RUNNERS[test["type"]](scenario, test, run_dir, options)
        for line in runner.plan():
            log(f"> {line}")
        t0 = time.monotonic()
        try:
            result = runner.run()
        except RunnerError as e:
            result = TestResult(test["id"], test["type"], status="error", error=str(e))
        except Exception as e:  # a runner bug shouldn't lose the other tests' results
            result = TestResult(test["id"], test["type"], status="error",
                                error=f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}")
        result.duration_s = round(time.monotonic() - t0, 1)
        run.tests.append(result)
        log(f"  {test['id']}: {result.status} in {result.duration_s}s, {len(result.metrics)} metrics"
            + (f" - {result.error.splitlines()[0]}" if result.error else ""))

    history = History(options.results_dir / "history.jsonl")
    past = history.entries(scenario.name, scenario.environment)
    run.budgets = evaluate_budgets(run.metrics, scenario.budgets)
    run.regressions = find_regressions(run.metrics, past, scenario.regression)
    run.status = overall_status(run)
    run.finished = datetime.now(timezone.utc).isoformat()
    run.write(run_dir / "results.json")
    if options.record_history and run.status != "error":
        history.append(run)
    return run, run_dir
