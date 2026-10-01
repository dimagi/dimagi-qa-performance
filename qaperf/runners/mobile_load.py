from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

from qaperf.results import Metric, TestResult, percentile, summarize
from qaperf.runners.base import RunnerError
from qaperf.runners.locust_runner import REPO_ROOT, LocustBackedRunner


class MobileLoadRunner(LocustBackedRunner):
    """Many simulated CommCare mobile workers (one per virtual user) hitting the server
    with the app's own protocol - restore, form submission, sync, heartbeat.

    Optional 'device_probe': while that load is running, measure a real device/emulator
    (same settings as a mobile_perf test) so you see what a phone user experiences under load.
    """

    type = "mobile_load"
    builtin_locustfile = REPO_ROOT / "qaperf" / "locustfiles" / "commcare_mobile_user.py"

    def locust_config(self) -> dict:
        t = self.test
        actions = []
        for a in t.get("actions") or []:
            a = dict(a)
            if a.get("template"):
                a["template"] = str(self.scenario.resolve(a["template"]))
            actions.append(a)
        keys = ("app_id", "app_version", "cc_version", "user_domain_suffix", "restore_path", "submit_path",
                "initial_restore", "restore_max_wait_s")
        return {
            **{k: t[k] for k in keys if k in t},
            "domain": t.get("domain") or self.scenario.vars.get("domain"),
            "vars": self.vars(),
            "actions": actions,
            "auth": t.get("auth") or {"type": "basic"},
            "wait_time": t.get("wait_time", [5, 15]),
            "users_file": str(self.scenario.resolve(t["users_file"])) if t.get("users_file") else None,
            "stages": self.stages_seconds(),
        }

    def run(self) -> TestResult:
        result = TestResult(self.test_id, self.type, started=datetime.now(timezone.utc).isoformat())
        proc = self.make_process()
        proc.start()

        probe_thread = None
        probe_out: dict = {}
        if self.test.get("device_probe"):
            probe_thread = threading.Thread(target=self._probe, args=(probe_out,), daemon=True)
            probe_thread.start()

        rc = proc.wait()
        if probe_thread:
            probe_thread.join(timeout=60)
            if probe_thread.is_alive():
                result.notes.append("device probe was still running when load ended; its numbers may be partial")
        self.collect(proc, result, rc)
        result.metrics += self.restore_totals()

        if "result" in probe_out:
            probe = probe_out["result"]
            for m in probe.metrics:
                m.runner, m.test_id, m.subject = self.type, self.test_id, f"device:{m.subject}"
            result.metrics += probe.metrics
            result.notes += [f"device probe: {n}" for n in probe.notes]
            result.artifacts.update({f"Device probe - {k}": v for k, v in probe.artifacts.items()})
        elif "error" in probe_out:
            result.notes.append(f"device probe failed: {probe_out['error']}")
        return result

    def restore_totals(self) -> list[Metric]:
        """End-to-end restore times (incl. async waits) that the locustfile kept out of Locust's stats."""
        durations: dict[str, list[float]] = {}
        failures: dict[str, int] = {}
        for f in self.test_dir.glob("restore_totals_*.json"):
            d = json.loads(f.read_text(encoding="utf-8"))
            for name, vals in d.get("durations_ms", {}).items():
                durations.setdefault(name, []).extend(vals)
            for name, n in d.get("failures", {}).items():
                failures[name] = failures.get(name, 0) + n
        out = []
        for name in sorted(set(durations) | set(failures)):
            vals, fails = durations.get(name, []), failures.get(name, 0)
            total = len(vals) + fails
            stats = {"requests": (total, ""), "failures": (fails, ""),
                     "error_rate_pct": (round(100 * fails / total, 3), "%")}
            s = summarize(vals)
            if s:
                stats.update({"avg_ms": (round(s["avg"], 1), "ms"), "p50_ms": (s["p50"], "ms"),
                              "p90_ms": (s["p90"], "ms"), "p95_ms": (s["p95"], "ms"),
                              "p99_ms": (percentile(vals, 99), "ms"), "max_ms": (s["max"], "ms")})
            out += [Metric(self.type, self.test_id, name, stat, round(v, 1), unit) for stat, (v, unit) in stats.items()]
        return out

    def _probe(self, out: dict) -> None:
        from qaperf.runners.mobile_perf import MobilePerfRunner

        cfg = {**self.test["device_probe"], "id": "device_probe", "type": "mobile_perf"}
        time.sleep(float(cfg.get("start_after_s", 60)))  # let the ramp-up finish first
        try:
            runner = MobilePerfRunner(self.scenario, cfg, self.run_dir, self.options)
            runner.test_dir = self.test_dir / "device_probe"
            out["result"] = runner.run()
        except (RunnerError, OSError) as e:
            out["error"] = str(e)

    def plan(self) -> list[str]:
        lines = super().plan()
        kinds = ", ".join(f"{a['kind']}x{a.get('weight', 1)}" for a in self.test.get("actions") or [])
        lines.append(f"  each virtual user = 1 mobile worker on its own device_id; actions: {kinds}")
        if self.test.get("device_probe"):
            lines.append(f"  + real-device probe after {self.test['device_probe'].get('start_after_s', 60)}s")
        return lines
