from __future__ import annotations

from datetime import datetime, timezone

from qaperf.results import TestResult
from qaperf.runners.locust_runner import REPO_ROOT, LocustBackedRunner


class WebLoadRunner(LocustBackedRunner):
    """HTTP load against a web app: declarative YAML tasks, or an existing locustfile."""

    type = "web_load"
    builtin_locustfile = REPO_ROOT / "qaperf" / "locustfiles" / "web_user.py"

    def locust_config(self) -> dict:
        t = self.test
        return {
            "vars": self.vars(),
            "tasks": t.get("tasks") or [],
            "on_start": t.get("on_start") or [],
            "mode": t.get("mode", "weighted"),
            "auth": t.get("auth") or {},
            "wait_time": t.get("wait_time", [1, 3]),
            "users_file": str(self.scenario.resolve(t["users_file"])) if t.get("users_file") else None,
            "stages": self.stages_seconds(),
        }

    def run(self) -> TestResult:
        result = TestResult(self.test_id, self.type, started=datetime.now(timezone.utc).isoformat())
        proc = self.make_process()
        proc.start()
        rc = proc.wait()
        self.collect(proc, result, rc)
        return result
