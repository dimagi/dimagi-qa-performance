"""Thin adb wrapper plus parsers for the Android outputs qa-perf measures.

Works with any attached device or emulator (USB, Wi-Fi adb, or a local emulator).
Cloud device farms that don't expose adb (e.g. BrowserStack App Automate) can't be
probed this way.
"""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess

_TOTAL_TIME = re.compile(r"^TotalTime:\s*(\d+)", re.M)
_WAIT_TIME = re.compile(r"^WaitTime:\s*(\d+)", re.M)
_LAUNCH_STATE = re.compile(r"^LaunchState:\s*(\w+)", re.M)
_PSS_NEW = re.compile(r"TOTAL PSS:\s*(\d+)")
_PSS_OLD = re.compile(r"^\s*TOTAL\s+(\d+)", re.M)
_GFX = {
    "frames": re.compile(r"Total frames rendered:\s*(\d+)"),
    "janky_frames": re.compile(r"Janky frames:\s*(\d+)"),
    "janky_pct": re.compile(r"Janky frames:\s*\d+\s*\(([\d.]+)%\)"),
    "p50_ms": re.compile(r"50th percentile:\s*(\d+)ms"),
    "p90_ms": re.compile(r"90th percentile:\s*(\d+)ms"),
    "p95_ms": re.compile(r"95th percentile:\s*(\d+)ms"),
    "p99_ms": re.compile(r"99th percentile:\s*(\d+)ms"),
}


class AdbError(Exception):
    pass


def adb_command(adb: str | list[str] | None) -> list[str]:
    if isinstance(adb, list):
        return adb
    path = adb or shutil.which("adb")
    if not path:
        raise AdbError("adb not found - install Android platform-tools or pass --adb / set 'adb' in the test")
    return [path]


def list_devices(adb: str | list[str] | None = None) -> list[str]:
    out = subprocess.run(adb_command(adb) + ["devices"], capture_output=True, text=True, timeout=30).stdout
    return [line.split()[0] for line in out.splitlines()[1:] if line.strip().endswith("device")]


def parse_am_start(text: str) -> dict:
    out = {}
    for key, rx in (("total_ms", _TOTAL_TIME), ("wait_ms", _WAIT_TIME)):
        m = rx.search(text)
        if m:
            out[key] = int(m.group(1))
    m = _LAUNCH_STATE.search(text)
    if m:
        out["launch_state"] = m.group(1)
    if "Error" in text and "total_ms" not in out:
        out["error"] = text.strip().splitlines()[-1]
    return out


def parse_meminfo_pss_kb(text: str) -> int | None:
    m = _PSS_NEW.search(text) or _PSS_OLD.search(text)
    return int(m.group(1)) if m else None


def parse_gfxinfo(text: str) -> dict:
    out = {}
    for key, rx in _GFX.items():
        m = rx.search(text)
        if m:
            out[key] = float(m.group(1))
    return out


def parse_proc_stat_ticks(text: str) -> int | None:
    """utime + stime from /proc/<pid>/stat (fields 14, 15; comm may contain spaces)."""
    try:
        fields = text[text.rindex(")") + 2:].split()
        return int(fields[11]) + int(fields[12])
    except (ValueError, IndexError):
        return None


class Adb:
    def __init__(self, serial: str, adb: str | list[str] | None = None):
        self.serial = serial
        self.base = adb_command(adb) + ["-s", serial]

    def run(self, *args: str, timeout: int = 60) -> str:
        try:
            p = subprocess.run(self.base + list(args), capture_output=True, text=True, timeout=timeout,
                               encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"adb {' '.join(args)} timed out after {timeout}s") from e
        return p.stdout + p.stderr

    def sh(self, command: str, timeout: int = 60) -> str:
        return self.run("shell", command, timeout=timeout)

    def prop(self, name: str) -> str:
        return self.sh(f"getprop {name}").strip()

    def info(self) -> dict:
        return {"serial": self.serial, "model": self.prop("ro.product.model"),
                "android": self.prop("ro.build.version.release"), "sdk": self.prop("ro.build.version.sdk")}

    def is_installed(self, package: str) -> bool:
        return "package:" in self.sh(f"pm path {shlex.quote(package)}")

    def install(self, apk: str) -> None:
        out = self.run("install", "-r", "-g", apk, timeout=600)
        if "Success" not in out:
            raise AdbError(f"install failed on {self.serial}: {out.strip()[-300:]}")

    def launcher_activity(self, package: str) -> str:
        out = self.sh(f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {shlex.quote(package)}")
        for line in reversed(out.strip().splitlines()):
            if "/" in line:
                return line.strip()
        raise AdbError(f"couldn't resolve launcher activity for {package} on {self.serial}; set 'activity'")

    def am_start(self, component: str, cold: bool) -> dict:
        flags = "-W -S" if cold else "-W"
        return parse_am_start(self.sh(f"am start {flags} -n {shlex.quote(component)}", timeout=120))

    def home(self) -> None:
        self.sh("input keyevent KEYCODE_HOME")

    def force_stop(self, package: str) -> None:
        self.sh(f"am force-stop {shlex.quote(package)}")

    def pid(self, package: str) -> int | None:
        out = self.sh(f"pidof {shlex.quote(package)}").strip().split()
        return int(out[0]) if out and out[0].isdigit() else None

    def pss_kb(self, package: str) -> int | None:
        return parse_meminfo_pss_kb(self.sh(f"dumpsys meminfo {shlex.quote(package)}"))

    def cpu_ticks(self, pid: int) -> int | None:
        return parse_proc_stat_ticks(self.sh(f"cat /proc/{pid}/stat"))

    def clk_tck(self) -> int:
        out = self.sh("getconf CLK_TCK").strip()
        return int(out) if out.isdigit() else 100

    def gfx_reset(self, package: str) -> None:
        self.sh(f"dumpsys gfxinfo {shlex.quote(package)} reset")

    def gfxinfo(self, package: str) -> dict:
        return parse_gfxinfo(self.sh(f"dumpsys gfxinfo {shlex.quote(package)}"))
