"""qa-perf command line.

  python -m qaperf run scenarios/hq_web_load.yaml --env staging
  python -m qaperf run scenarios/commcare_mobile_load.yaml --users 200 --run-time 10m
  python -m qaperf validate scenarios/*.yaml
  python -m qaperf devices
  python -m qaperf report results/<run_id>

Exit codes: 0 pass (or warn without --strict), 1 budget failure / regression failure /
test error, 2 config or safety problem.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from qaperf.config import ConfigError, load_scenario
from qaperf.history import History
from qaperf.reporters import console, html, slack
from qaperf.results import RunResult
from qaperf.runners import RUNNERS
from qaperf.runners.base import RunOptions
from qaperf.safety import check_safety

ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv():
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env")
    load_dotenv()  # cwd/.env too, without overriding


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qaperf", description="One-stop performance testing: web load, "
                                "mobile device performance, multi-user mobile load.")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a scenario")
    run.add_argument("scenario")
    run.add_argument("--only", action="append", help="run only this test id (repeatable)")
    run.add_argument("--env", dest="environment", help="environment name (overrides the scenario's)")
    run.add_argument("--host", help="target base URL (overrides the scenario's)")
    run.add_argument("--users", type=int, help="flat load: virtual users for every load test")
    run.add_argument("--spawn-rate", type=float)
    run.add_argument("--run-time", help="e.g. 90s, 5m, 1h")
    run.add_argument("--results-dir", default=str(ROOT / "results"))
    run.add_argument("--allow-production", action="store_true",
                     help="permit load tests against production hosts (get the env owner's OK first)")
    run.add_argument("--allow-high-load", action="store_true", help="permit more users than safety.max_users")
    run.add_argument("--dry-run", action="store_true", help="validate + print the plan; send no traffic")
    run.add_argument("--slack", dest="slack", action="store_true", default=None, help="post summary to Slack")
    run.add_argument("--no-slack", dest="slack", action="store_false")
    run.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    run.add_argument("--no-history", action="store_true", help="don't record this run in history")
    run.add_argument("--report-url", help="link to put in the Slack message (e.g. CI artifact URL)")
    run.add_argument("--adb")
    run.add_argument("--maestro")

    val = sub.add_parser("validate", help="check scenario files without running anything")
    val.add_argument("scenarios", nargs="+")

    dev = sub.add_parser("devices", help="list attached Android devices")
    dev.add_argument("--adb")

    rep = sub.add_parser("report", help="re-render a run's HTML report")
    rep.add_argument("run_dir")
    return p


def cmd_run(args) -> int:
    overrides = {"environment": args.environment, "host": args.host, "users": args.users,
                 "spawn_rate": args.spawn_rate, "run_time": args.run_time}
    try:
        scenario = load_scenario(args.scenario, only=args.only, overrides=overrides, strict_env=not args.dry_run)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if scenario.missing_env:
        print(f"note: unset env vars (would fail a real run): {', '.join(sorted(scenario.missing_env))}")

    problems = check_safety(scenario, args.allow_production, args.allow_high_load)
    if problems:
        print("refusing to run:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        return 2

    options = RunOptions(results_dir=Path(args.results_dir).resolve(), allow_production=args.allow_production,
                         allow_high_load=args.allow_high_load, dry_run=args.dry_run, slack=args.slack,
                         strict=args.strict, record_history=not args.no_history, adb=args.adb,
                         maestro=args.maestro)
    if args.dry_run:
        print(f"scenario {scenario.name!r} [{scenario.environment}] - {len(scenario.tests)} test(s), "
              f"{len(scenario.budgets)} budget(s). Nothing will be sent.")
        for test in scenario.tests:
            for line in RUNNERS[test["type"]](scenario, test, options.results_dir, options).plan():
                print(f"  {line}")
        _preflight(scenario)
        return 0

    from qaperf.engine import run_scenario

    run, run_dir = run_scenario(scenario, options)
    past = [e for e in History(options.results_dir / "history.jsonl").entries(run.scenario, run.environment)
            if e["run_id"] != run.run_id]
    if scenario.reporting.get("html", True):
        (run_dir / "report.html").write_text(html.render(run, past), encoding="utf-8")
    print(console.render(run))
    print(f"\nresults: {run_dir}")

    want_slack = scenario.reporting.get("slack", False) if args.slack is None else args.slack
    slack_on = scenario.reporting.get("slack_on", "always")
    if want_slack and (slack_on == "always" or run.status != "pass"):
        try:
            print("slack:", slack.post(run, args.report_url))
        except Exception as e:
            print(f"slack: failed ({e})", file=sys.stderr)

    if run.status in ("fail", "error"):
        return 1
    return 1 if (args.strict and run.status == "warn") else 0


def _preflight(scenario) -> None:
    types = {t["type"] for t in scenario.tests}
    checks = []
    if types & {"web_load", "mobile_load"}:
        # Don't import locust here: it gevent-monkey-patches ssl, which breaks this process.
        from importlib.metadata import PackageNotFoundError, version
        try:
            checks.append(("locust", True, version("locust")))
        except PackageNotFoundError:
            checks.append(("locust", False, "pip install -r requirements.txt"))
    if "mobile_perf" in types or any(t.get("device_probe") for t in scenario.tests):
        adb = shutil.which("adb")
        checks.append(("adb", bool(adb), adb or "install Android platform-tools"))
        if adb:
            from qaperf.mobile.adb import list_devices
            devs = list_devices(adb)
            checks.append(("devices", bool(devs), ", ".join(devs) or "none attached"))
        if any(t.get("flows") or (t.get("device_probe") or {}).get("flows") for t in scenario.tests):
            m = shutil.which("maestro")
            checks.append(("maestro", bool(m), m or "install Maestro"))
    for name, ok, detail in checks:
        print(f"  [{'ok' if ok else '!!'}] {name}: {detail}")


def cmd_validate(args) -> int:
    rc = 0
    for path in args.scenarios:
        try:
            s = load_scenario(path, strict_env=False)
            extra = f" (needs env: {', '.join(sorted(s.missing_env))})" if s.missing_env else ""
            print(f"ok   {path}: {len(s.tests)} test(s), {len(s.budgets)} budget(s){extra}")
        except ConfigError as e:
            print(f"FAIL {e}")
            rc = 2
    return rc


def cmd_devices(args) -> int:
    from qaperf.mobile.adb import Adb, AdbError, list_devices
    try:
        serials = list_devices(args.adb)
    except AdbError as e:
        print(e, file=sys.stderr)
        return 2
    if not serials:
        print("no devices attached")
    for s in serials:
        i = Adb(s, args.adb).info()
        print(f"{s}\t{i['model']}\tAndroid {i['android']} (sdk {i['sdk']})")
    return 0


def cmd_report(args) -> int:
    run_dir = Path(args.run_dir)
    run = RunResult.read(run_dir / "results.json")
    past = [e for e in History(run_dir.parent / "history.jsonl").entries(run.scenario, run.environment)
            if e["run_id"] != run.run_id]
    (run_dir / "report.html").write_text(html.render(run, past), encoding="utf-8")
    print(console.render(run))
    print(f"\nwrote {run_dir / 'report.html'}")
    return 0


def main(argv=None) -> int:
    _load_dotenv()
    args = _parser().parse_args(argv)
    return {"run": cmd_run, "validate": cmd_validate, "devices": cmd_devices, "report": cmd_report}[args.command](args)
