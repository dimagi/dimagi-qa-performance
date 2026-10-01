"""On-device performance for an Android app, on one or many devices at once.

Per device:
  cold_start / warm_start   am start -W TotalTime, repeated 'iterations' times
  memory                    idle PSS after launch
  flows                     each Maestro flow timed end-to-end while the app's PSS and CPU are
                            sampled in the background; frame stats (jank %, p90 frame time)
                            come from dumpsys gfxinfo, reset before each run

With several devices and a users_file, each device logs in as a different user
(round-robin), and devices run in parallel - a small "real phones, real users" load.
"""
from __future__ import annotations

import contextlib
import csv
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from qaperf.mobile.adb import Adb, AdbError, list_devices
from qaperf.results import Metric, TestResult, summarize
from qaperf.runners.base import Runner, RunnerError


class _Sampler(threading.Thread):
    """Samples the app's PSS (MB) and CPU (% of one core) every interval seconds."""

    def __init__(self, adb: Adb, package: str, interval: float, clk_tck: int):
        super().__init__(daemon=True)
        self.adb, self.package, self.interval, self.clk_tck = adb, package, interval, clk_tck
        self.stop_event = threading.Event()
        self.pss_mb: list[float] = []
        self.cpu_pct: list[float] = []

    def run(self):
        pid, last = None, None
        while not self.stop_event.is_set():
            try:
                current = self.adb.pid(self.package)
                if current != pid:
                    pid, last = current, None  # app restarted (e.g. Maestro launchApp)
                if pid:
                    ticks = self.adb.cpu_ticks(pid)
                    now = time.monotonic()
                    if ticks is not None and last:
                        dt = now - last[1]
                        if dt > 0:
                            self.cpu_pct.append(round(100 * (ticks - last[0]) / self.clk_tck / dt, 1))
                    last = (ticks, now) if ticks is not None else None
                    pss = self.adb.pss_kb(self.package)
                    if pss:
                        self.pss_mb.append(round(pss / 1024, 1))
            except AdbError:
                pass
            self.stop_event.wait(self.interval)

    def stop(self):
        self.stop_event.set()
        self.join(timeout=30)


class MobilePerfRunner(Runner):
    type = "mobile_perf"

    def _serials(self) -> list[str]:
        wanted = self.test.get("devices", "auto")
        attached = list_devices(self.options.adb or self.test.get("adb"))
        if wanted in (None, "auto"):
            serials = attached
        else:
            wanted = [wanted] if isinstance(wanted, str) else list(wanted)
            missing = [s for s in wanted if s not in attached]
            if missing:
                raise RunnerError(f"devices not attached: {missing} (attached: {attached or 'none'})")
            serials = wanted
        if not serials:
            raise RunnerError("no Android device/emulator attached (check `adb devices`)")
        limit = self.test.get("max_devices")
        return serials[:int(limit)] if limit else serials

    def _users(self) -> list[dict]:
        if not self.test.get("users_file"):
            return []
        with open(self.scenario.resolve(self.test["users_file"]), newline="", encoding="utf-8-sig") as f:
            return [r for r in csv.DictReader(f) if r.get("username")]

    def _maestro(self) -> str:
        path = self.options.maestro or self.test.get("maestro") or shutil.which("maestro")
        if not path:
            raise RunnerError("maestro not found - install it or pass --maestro (needed for 'flows')")
        return path

    def run(self) -> TestResult:
        result = TestResult(self.test_id, self.type, started=datetime.now(timezone.utc).isoformat())
        serials = self._serials()
        users = self._users()
        maestro = self._maestro() if self.test.get("flows") else None
        parallel = self.test.get("parallel", True)
        # Maestro on one host can trip over itself across devices; serialize flows unless told otherwise.
        flow_lock = contextlib.nullcontext() if self.test.get("maestro_parallel", False) else threading.Lock()
        self.test_dir.mkdir(parents=True, exist_ok=True)

        def job(i_serial):
            i, serial = i_serial
            user = users[i % len(users)] if users else {}
            return serial, self._run_device(serial, user, maestro, flow_lock)

        with ThreadPoolExecutor(max_workers=len(serials) if parallel else 1) as pool:
            per_device = list(pool.map(job, enumerate(serials)))

        errors = [f"{s}: {d['error']}" for s, d in per_device if d.get("error")]
        result.notes += [f"{s}: {d['info'].get('model', '?')} Android {d['info'].get('android', '?')}"
                         for s, d in per_device if d.get("info")]
        result.notes += errors
        if len(errors) == len(per_device):
            raise RunnerError("all devices failed: " + "; ".join(errors))
        result.metrics += self._metrics([d for _, d in per_device if not d.get("error")], "")
        if len(per_device) > 1:
            for serial, d in per_device:
                if not d.get("error"):
                    result.metrics += self._metrics([d], f"@{serial}")
        for p in sorted(self.test_dir.rglob("*.log")):
            result.artifacts[f"log {p.relative_to(self.test_dir).as_posix()}"] = self.rel(p)
        return result

    # --- one device ------------------------------------------------------------------

    def _run_device(self, serial: str, user: dict, maestro: str | None, flow_lock) -> dict:
        t = self.test
        pkg = t["package"]
        data = {"cold_start": [], "warm_start": [], "pss_mb": [], "flows": {}}
        try:
            adb = Adb(serial, self.options.adb or t.get("adb"))
            data["info"] = adb.info()
            if t.get("apk") and (t.get("install") or not adb.is_installed(pkg)):
                adb.install(str(self.scenario.resolve(t["apk"])))
            if not adb.is_installed(pkg):
                raise AdbError(f"{pkg} is not installed (set 'apk' to install it)")
            component = t.get("activity") or adb.launcher_activity(pkg)
            iterations = int(t.get("iterations", 5))
            settle = float(t.get("settle_s", 2))
            measures = t.get("measures") or ["cold_start", "warm_start", "memory"]

            if "cold_start" in measures:
                for _ in range(iterations):
                    r = adb.am_start(component, cold=True)
                    if "total_ms" in r:
                        data["cold_start"].append(r["total_ms"])
                    time.sleep(settle)
            if "warm_start" in measures:
                adb.am_start(component, cold=False)
                time.sleep(settle)
                for _ in range(iterations):
                    adb.home()
                    time.sleep(1)
                    r = adb.am_start(component, cold=False)
                    if "total_ms" in r:
                        data["warm_start"].append(r["total_ms"])
                    time.sleep(settle)
            if "memory" in measures:
                adb.am_start(component, cold=False)
                time.sleep(max(settle, 3))
                pss = adb.pss_kb(pkg)
                if pss:
                    data["pss_mb"].append(round(pss / 1024, 1))

            clk = adb.clk_tck()
            for flow in t.get("flows") or []:
                with flow_lock:
                    data["flows"][flow["name"]] = self._run_flow(adb, flow, user, maestro, clk)
        except (AdbError, OSError) as e:
            data["error"] = str(e)
        return data

    def _run_flow(self, adb: Adb, flow: dict, user: dict, maestro: str, clk: int) -> list[dict]:
        pkg = self.test["package"]
        ctx = {**self.scenario.vars, **user, "serial": adb.serial}
        env = {k: str(v).format_map(_Default(ctx)) for k, v in (flow.get("env") or {}).items()}
        if user:  # a per-device user fills USERNAME/PASSWORD unless the flow sets them
            env.setdefault("USERNAME", user.get("username", ""))
            env.setdefault("PASSWORD", user.get("password", ""))
        base = list(maestro) if isinstance(maestro, (list, tuple)) else [maestro]
        cmd = base + ["--device", adb.serial, "test", str(self.scenario.resolve(flow["path"]))]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        runs = []
        log_dir = self.test_dir / adb.serial.replace(":", "_")
        log_dir.mkdir(parents=True, exist_ok=True)
        for i in range(int(flow.get("iterations", 1))):
            adb.gfx_reset(pkg)
            sampler = _Sampler(adb, pkg, float(self.test.get("sample_interval_s", 2)), clk)
            sampler.start()
            log = log_dir / f"{flow['name']}_{i + 1}.log"
            start = time.monotonic()
            try:
                with open(log, "w", encoding="utf-8") as fh:
                    rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                        timeout=int(flow.get("timeout_s", 600))).returncode
            except subprocess.TimeoutExpired:
                rc = -1
            duration = time.monotonic() - start
            sampler.stop()
            runs.append({"ok": rc == 0, "duration_s": round(duration, 2), "pss_mb": sampler.pss_mb,
                         "cpu_pct": sampler.cpu_pct, "gfx": adb.gfxinfo(pkg)})
        return runs

    # --- aggregation -----------------------------------------------------------------

    def _metrics(self, devices: list[dict], suffix: str) -> list[Metric]:
        out: list[Metric] = []

        def add(subject, stat, value, unit, hib=None):
            if value is not None:
                out.append(Metric(self.type, self.test_id, subject + suffix, stat, round(value, 2), unit, hib))

        for measure in ("cold_start", "warm_start"):
            s = summarize([v for d in devices for v in d[measure]])
            if s:
                add(measure, "p50_ms", s["p50"], "ms")
                add(measure, "p90_ms", s["p90"], "ms")
                add(measure, "max_ms", s["max"], "ms")
                add(measure, "samples", s["count"], "")
        s = summarize([v for d in devices for v in d["pss_mb"]])
        if s:
            add("idle_memory", "pss_mb", s["p50"], "MB")

        names = {n for d in devices for n in d["flows"]}
        for name in sorted(names):
            runs = [r for d in devices for r in d["flows"].get(name, [])]
            if not runs:
                continue
            subject = f"flow:{name}"
            add(subject, "success_rate_pct", 100 * sum(r["ok"] for r in runs) / len(runs), "%", True)
            ok = [r for r in runs if r["ok"]] or runs
            dur = summarize([r["duration_s"] for r in ok])
            add(subject, "p50_s", dur.get("p50"), "s")
            add(subject, "max_s", dur.get("max"), "s")
            pss = [v for r in ok for v in r["pss_mb"]]
            cpu = [v for r in ok for v in r["cpu_pct"]]
            if pss:
                add(subject, "peak_pss_mb", max(pss), "MB")
            if cpu:
                add(subject, "avg_cpu_pct", sum(cpu) / len(cpu), "%")
                add(subject, "peak_cpu_pct", max(cpu), "%")
            jank = summarize([r["gfx"]["janky_pct"] for r in ok if "janky_pct" in r["gfx"]])
            frame = summarize([r["gfx"]["p90_ms"] for r in ok if "p90_ms" in r["gfx"]])
            add(subject, "janky_frames_pct", jank.get("p50"), "%")
            add(subject, "frame_p90_ms", frame.get("p50"), "ms")
        return out

    def plan(self) -> list[str]:
        t = self.test
        flows = ", ".join(f["name"] for f in t.get("flows") or []) or "none"
        return [f"mobile_perf {self.test_id}: {t['package']} on devices={t.get('devices', 'auto')}, "
                f"measures={t.get('measures') or ['cold_start', 'warm_start', 'memory']} x{t.get('iterations', 5)}, "
                f"flows: {flows}"]


class _Default(dict):
    def __missing__(self, key):
        return "{" + key + "}"
