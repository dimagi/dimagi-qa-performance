from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qaperf.config import Scenario
from qaperf.results import TestResult


class RunnerError(Exception):
    """A test couldn't run at all (bad environment, missing device, crashed tool)."""


@dataclass
class RunOptions:
    results_dir: Path
    allow_production: bool = False
    allow_high_load: bool = False
    dry_run: bool = False
    slack: bool | None = None  # None -> scenario's reporting.slack
    strict: bool = False
    record_history: bool = True
    adb: str | None = None
    maestro: str | None = None


class Runner:
    type = ""

    def __init__(self, scenario: Scenario, test: dict, run_dir: Path, options: RunOptions):
        self.scenario = scenario
        self.test = test
        self.test_id = test["id"]
        self.run_dir = run_dir
        self.test_dir = run_dir / self.test_id
        self.options = options

    def run(self) -> TestResult:
        raise NotImplementedError

    def plan(self) -> list[str]:
        """Human-readable description of what run() would do (for --dry-run)."""
        return []

    def rel(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.run_dir.resolve()).as_posix()
