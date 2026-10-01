"""Runs Locust headless as a subprocess (optionally master + N local workers) and turns
its CSV output into Metrics. Shared by web_load and mobile_load."""
from __future__ import annotations

import csv
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

from qaperf.config import parse_duration
from qaperf.results import Metric, TestResult
from qaperf.runners.base import Runner, RunnerError

REPO_ROOT = Path(__file__).resolve().parents[2]

PCT_COLUMNS = {"p50_ms": "50%", "p90_ms": "90%", "p95_ms": "95%", "p99_ms": "99%"}


def _num(value) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return math.nan
    return f


def parse_stats_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def metrics_from_stats(rows: list[dict], runner: str, test_id: str, prefix: str = "") -> list[Metric]:
    out = []
    for row in rows:
        subject = prefix + (row.get("Name") or "?")
        requests = _num(row.get("Request Count"))
        failures = _num(row.get("Failure Count"))
        if not requests:
            continue
        vals = {
            "requests": (requests, ""),
            "failures": (failures, ""),
            "error_rate_pct": (round(100 * failures / requests, 3), "%"),
            "rps": (round(_num(row.get("Requests/s")), 3), "req/s"),
            "avg_ms": (round(_num(row.get("Average Response Time")), 1), "ms"),
            "max_ms": (_num(row.get("Max Response Time")), "ms"),
        }
        for stat, col in PCT_COLUMNS.items():
            vals[stat] = (_num(row.get(col)), "ms")
        for stat, (value, unit) in vals.items():
            if not math.isnan(value):
                out.append(Metric(runner, test_id, subject, stat, value, unit))
    return out


def top_failures(path: Path, limit: int = 10) -> list[dict]:
    if not path.exists():
        return []
    rows = parse_stats_csv(path)
    rows.sort(key=lambda r: -_num(r.get("Occurrences")))
    return [{"name": r.get("Name"), "error": r.get("Error"), "count": int(_num(r.get("Occurrences")) or 0)}
            for r in rows[:limit]]


class LocustProcess:
    """Locust master/standalone + optional local workers, started together, waited together."""

    def __init__(self, locustfile: Path, host: str, out_dir: Path, test: dict, config: dict,
                 cwd: Path | None = None, pythonpath: list[Path] | None = None, extra_args=None):
        self.locustfile = locustfile
        self.host = host
        self.out_dir = out_dir
        self.test = test
        self.cwd = cwd
        self.extra_args = [str(a) for a in (extra_args or [])]
        self.workers = int(test.get("workers") or 0)
        self.prefix = out_dir / "locust"
        self.html = out_dir / "locust_report.html"
        self.log = out_dir / "locust.log"
        out_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = out_dir / "test_config.json"
        cfg_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        self.env = {**os.environ, "QAPERF_TEST_CONFIG": str(cfg_path),
                    "QAPERF_WORKER_COUNT": str(max(1, self.workers)), "QAPERF_OUT_DIR": str(out_dir),
                    "PYTHONUNBUFFERED": "1"}
        paths = [str(REPO_ROOT)] + [str(p) for p in (pythonpath or [])]
        if os.environ.get("PYTHONPATH"):
            paths.append(os.environ["PYTHONPATH"])
        self.env["PYTHONPATH"] = os.pathsep.join(paths)
        self.procs: list[subprocess.Popen] = []
        self._log_files = []

    @property
    def expected_duration_s(self) -> int:
        stages = self.test.get("stages")
        if stages:
            return sum(parse_duration(s["duration"]) for s in stages)
        return parse_duration(self.test["run_time"])

    def command(self) -> list[str]:
        cmd = [sys.executable, "-m", "locust", "-f", str(self.locustfile), "--headless",
               "--csv", str(self.prefix), "--html", str(self.html), "--only-summary",
               "--exit-code-on-error", "0", "--stop-timeout", str(self.test.get("stop_timeout_s", 10)),
               "--logfile", str(self.log), "--loglevel", self.test.get("log_level", "INFO")]
        if self.host:
            cmd += ["--host", self.host]
        if not self.test.get("stages"):
            cmd += ["-u", str(self.test["users"]), "-r", str(self.test.get("spawn_rate", 1)),
                    "--run-time", f"{parse_duration(self.test['run_time'])}s"]
        if self.workers:
            cmd += ["--master", "--expect-workers", str(self.workers)]
        return cmd + self.extra_args

    def start(self) -> None:
        console = open(self.out_dir / "locust_console.log", "w", encoding="utf-8")
        self._log_files.append(console)
        role = "master" if self.workers else "standalone"
        self.procs.append(subprocess.Popen(self.command(), cwd=self.cwd, env={**self.env, "QAPERF_ROLE": role},
                                           stdout=console, stderr=subprocess.STDOUT))
        for i in range(self.workers):
            wlog = open(self.out_dir / f"locust_worker{i}.log", "w", encoding="utf-8")
            self._log_files.append(wlog)
            cmd = [sys.executable, "-m", "locust", "-f", str(self.locustfile), "--worker",
                   "--master-host", "127.0.0.1"] + self.extra_args
            self.procs.append(subprocess.Popen(cmd, cwd=self.cwd, env={**self.env, "QAPERF_ROLE": "worker",
                                                                           "QAPERF_WORKER_INDEX": str(i)},
                                               stdout=wlog, stderr=subprocess.STDOUT))

    def wait(self) -> int:
        # Generous timeout: ramp, stop timeout, worker connect, report writing.
        deadline = time.monotonic() + self.expected_duration_s + 180
        master = self.procs[0]
        try:
            rc = master.wait(timeout=max(1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            self.kill()
            raise RunnerError(f"Locust didn't finish within {self.expected_duration_s + 180}s; killed")
        for p in self.procs[1:]:
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
        for f in self._log_files:
            f.close()
        return rc

    def kill(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.kill()
        for f in self._log_files:
            f.close()


class LocustBackedRunner(Runner):
    """Base for runners whose load comes from Locust."""

    builtin_locustfile: Path

    def locust_config(self) -> dict:
        raise NotImplementedError

    def host(self) -> str:
        return (self.test.get("host") or self.scenario.host).rstrip("/")

    def make_process(self) -> LocustProcess:
        t = self.test
        if t.get("locustfile"):  # an existing script, e.g. dimagi-qa/LocustScripts/...
            locustfile = self.scenario.resolve(t["locustfile"])
            cwd = self.scenario.resolve(t.get("cwd")) or locustfile.parent
            pythonpath = [self.scenario.resolve(p) for p in t.get("pythonpath") or []]
            return LocustProcess(locustfile, self.host(), self.test_dir, t, {**t, "vars": self.vars()},
                                 cwd=cwd, pythonpath=pythonpath, extra_args=t.get("locust_args"))
        return LocustProcess(self.builtin_locustfile, self.host(), self.test_dir, t, self.locust_config())

    def vars(self) -> dict:
        return {**self.scenario.vars, **(self.test.get("vars") or {})}

    def stages_seconds(self) -> list[dict] | None:
        stages = self.test.get("stages")
        if not stages:
            return None
        return [{**s, "duration_s": parse_duration(s["duration"])} for s in stages]

    def collect(self, proc: LocustProcess, result: TestResult, rc: int, prefix: str = "") -> None:
        stats = Path(f"{proc.prefix}_stats.csv")
        if not stats.exists():
            tail = ""
            console = proc.out_dir / "locust_console.log"
            if console.exists():
                tail = "\n".join(console.read_text(encoding="utf-8", errors="replace").splitlines()[-25:])
            raise RunnerError(f"Locust exited with code {rc} and wrote no stats.\n{tail}")
        result.metrics += metrics_from_stats(parse_stats_csv(stats), self.type, self.test_id, prefix)
        result.top_failures = top_failures(Path(f"{proc.prefix}_failures.csv"))
        for label, path in (("Locust report", proc.html), ("Locust log", proc.log),
                            ("Stats CSV", stats), ("Stats history CSV", Path(f"{proc.prefix}_stats_history.csv"))):
            if Path(path).exists():
                result.artifacts[label] = self.rel(path)
        if rc not in (0, None):
            result.notes.append(f"Locust exit code {rc}")

    def plan(self) -> list[str]:
        t = self.test
        load = (" -> ".join(f"{s['users']}u for {s['duration']}" for s in t["stages"]) if t.get("stages")
                else f"{t['users']} users, spawn {t.get('spawn_rate', 1)}/s, for {t['run_time']}")
        src = t.get("locustfile") or self.builtin_locustfile.name
        workers = f", {t['workers']} workers" if t.get("workers") else ""
        return [f"{self.type} {self.test_id}: {load}{workers} against {self.host() or '(locustfile default)'} via {src}"]
