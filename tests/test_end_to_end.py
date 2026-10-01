"""End-to-end: the real CLI drives real Locust (and the mobile_perf runner drives fake adb/Maestro)
against local fakes. Nothing leaves 127.0.0.1."""
import json
import sys
import textwrap
from pathlib import Path

import pytest

from qaperf.cli import main
from qaperf.config import load_scenario
from qaperf.engine import run_scenario
from qaperf.results import RunResult
from qaperf.runners.base import RunOptions
from tests.fakes import fake_server

FAKES = Path(__file__).parent / "fakes"
DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture()
def server():
    srv, state, url = fake_server.start()
    yield state, url
    srv.shutdown()


def write(tmp_path, text):
    p = tmp_path / "scenario.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


def only_run_dir(results: Path) -> Path:
    dirs = [d for d in results.iterdir() if d.is_dir()]
    assert len(dirs) == 1
    return dirs[0]


def test_web_load_end_to_end(tmp_path, server):
    state, url = server
    users = tmp_path / "users.csv"
    users.write_text("username,password\nweb1,good\nweb2,good\n", encoding="utf-8")
    scenario = write(tmp_path, f"""
    name: web-e2e
    host: {url}
    environment: local
    vars: {{domain: demo}}
    tests:
      - id: browse
        type: web_load
        users: 4
        spawn_rate: 4
        run_time: 6s
        wait_time: 0.2
        users_file: {users.as_posix()}
        auth: {{type: hq_login}}
        mode: sequential
        tasks:
          - name: Dashboard
            path: /a/{{domain}}/dashboard/project/
            extract: {{uid: "id='uid'>([^<]+)<"}}
          - {{name: Slow page, path: "/slow?u={{uid}}"}}
          - {{name: Missing page, path: /nope, weight: 1}}
    budgets:
      "web_load/browse/Dashboard/p95_ms": {{fail: 1000}}
      "web_load/browse/Slow page/p50_ms": {{warn: 100, fail: 5000}}
      "web_load/browse/Missing page/error_rate_pct": {{fail: 50}}
      "web_load/browse/Aggregated/requests": {{fail: 5}}
    """)
    results = tmp_path / "results"
    rc = main(["run", str(scenario), "--results-dir", str(results), "--no-slack"])
    assert rc == 1  # the 404 page blows its error budget
    run_dir = only_run_dir(results)
    run = RunResult.read(run_dir / "results.json")
    status = {b.key: b.status for b in run.budgets}
    assert status["web_load/browse/Dashboard/p95_ms"] == "pass"
    assert status["web_load/browse/Slow page/p50_ms"] == "warn"  # ~300ms > 100ms warn
    assert status["web_load/browse/Missing page/error_rate_pct"] == "fail"
    assert status["web_load/browse/Aggregated/requests"] == "pass"
    assert state.counts.get("login_ok", 0) >= 2 and not state.counts.get("login_bad")
    assert (run_dir / "report.html").exists() and (run_dir / "browse" / "locust_report.html").exists()
    assert (results / "history.jsonl").exists()


def test_mobile_load_multi_user_end_to_end(tmp_path, server):
    state, url = server
    users = tmp_path / "workers.csv"
    users.write_text("username,password\n" + "".join(f"mw{i},pw\n" for i in range(5)), encoding="utf-8")
    scenario = write(tmp_path, f"""
    name: mobile-load-e2e
    host: {url}
    environment: local
    vars: {{case_type: perf}}
    tests:
      - id: workers
        type: mobile_load
        domain: demo
        app_id: app123
        users: 5
        spawn_rate: 5
        run_time: 8s
        wait_time: 0.3
        users_file: {users.as_posix()}
        actions:
          - {{kind: submit_form, name: "form: register", weight: 3,
              template: {(DATA / 'forms' / 'register_case.xml').as_posix()}, vars: {{xmlns: "http://x/reg"}}}}
          - {{kind: submit_form, name: "form: update", weight: 2,
              template: {(DATA / 'forms' / 'update_case.xml').as_posix()}, vars: {{xmlns: "http://x/upd"}}}}
          - {{kind: sync, weight: 1}}
          - {{kind: heartbeat, weight: 1}}
    budgets:
      "mobile_load/workers/restore: initial (total)/p50_ms": {{warn: 500, fail: 10000}}
      "mobile_load/workers/form: */error_rate_pct": {{fail: 0}}
    """)
    results = tmp_path / "results"
    rc = main(["run", str(scenario), "--results-dir", str(results), "--no-slack"])
    run = RunResult.read(only_run_dir(results) / "results.json")
    keys = {m.key: m.value for m in run.metrics}
    assert run.tests[0].status == "ok", run.tests[0].error
    assert state.users == {f"mw{i}@demo.commcarehq.org" for i in range(5)}  # 5 distinct workers
    assert state.counts["restore_202"] == 5  # every device hit the async-restore path and polled
    # the total includes the ~1s async wait; the single request doesn't
    assert keys["mobile_load/workers/restore: initial (total)/p50_ms"] >= 900
    # totals are reported, but not folded into Locust's Aggregated row (no double counting)
    real = [m for m in run.metrics if m.stat == "requests" and m.subject != "Aggregated"
            and not m.subject.endswith("(total)")]
    assert keys["mobile_load/workers/Aggregated/requests"] == sum(m.value for m in real)
    assert keys["mobile_load/workers/form: register/requests"] > 0
    assert keys["mobile_load/workers/form: update/requests"] > 0
    assert keys["mobile_load/workers/form: register/error_rate_pct"] == 0
    sub = state.submissions[0]
    assert "uid-mw" in sub and "{{" not in sub  # user_id came from the restore
    statuses = {b.key: b.status for b in run.budgets}
    assert statuses["mobile_load/workers/restore: initial (total)/p50_ms"] == "warn"
    assert rc == 0  # warn only


def _shim(tmp_path, name, script):
    """A launcher for a fake tool. Lists work for adb (Adb accepts a command list) and Maestro."""
    return [sys.executable, str(FAKES / script)]


def test_mobile_perf_multi_device(tmp_path):
    (tmp_path / "flow_ok.yaml").write_text("appId: org.commcare.dalvik\n---\n- launchApp\n", encoding="utf-8")
    (tmp_path / "flow_fail.yaml").write_text("appId: org.commcare.dalvik\n---\n- launchApp\n", encoding="utf-8")
    (tmp_path / "devices.csv").write_text("username,password\nalice,a1\nbob,b2\n", encoding="utf-8")
    scenario_path = write(tmp_path, """
    name: device-e2e
    environment: local
    tests:
      - id: phones
        type: mobile_perf
        package: org.commcare.dalvik
        iterations: 3
        settle_s: 0
        sample_interval_s: 0.3
        users_file: devices.csv
        maestro_parallel: true
        flows:
          - {name: login, path: flow_ok.yaml, iterations: 2, env: {CC_TEST_USERNAME: "{username}"}}
          - {name: broken, path: flow_fail.yaml}
    budgets:
      "mobile_perf/phones/cold_start/p50_ms": {warn: 1500, fail: 3000}
      "mobile_perf/phones/flow:*/success_rate_pct": {fail: 100}
    """)
    scenario = load_scenario(scenario_path)
    options = RunOptions(results_dir=tmp_path / "results", adb=_shim(tmp_path, "adb", "fake_adb.py"),
                         maestro=_shim(tmp_path, "maestro", "fake_maestro.py"))
    run, run_dir = run_scenario(scenario, options, log=lambda *_: None)
    t = run.tests[0]
    assert t.status == "ok", t.error
    keys = {m.key: m.value for m in t.metrics}
    assert keys["mobile_perf/phones/cold_start/samples"] == 6  # 3 iterations x 2 devices
    assert 1400 <= keys["mobile_perf/phones/cold_start@emulator-5554/p50_ms"] < 1500
    assert keys["mobile_perf/phones/cold_start@emulator-5556/p50_ms"] >= 1700  # the slower phone
    assert keys["mobile_perf/phones/idle_memory/pss_mb"] == pytest.approx(142.0, abs=0.1)
    assert keys["mobile_perf/phones/flow:login/success_rate_pct"] == 100
    assert keys["mobile_perf/phones/flow:broken/success_rate_pct"] == 0
    assert keys["mobile_perf/phones/flow:login/janky_frames_pct"] == 5.0
    assert 20 <= keys["mobile_perf/phones/flow:login/avg_cpu_pct"] <= 60
    assert keys["mobile_perf/phones/flow:login/p50_s"] >= 1.5
    status = {b.key: b.status for b in run.budgets}
    assert status["mobile_perf/phones/flow:broken/success_rate_pct"] == "fail"
    assert run.status == "fail"
    # each device ran the flow as its own user
    logs = {p.parent.name: p.read_text() for p in run_dir.rglob("login_1.log")}
    assert "CC_TEST_USERNAME=alice" in logs["emulator-5554"]
    assert "CC_TEST_USERNAME=bob" in logs["emulator-5556"]


def test_dry_run_sends_nothing_and_validate_ships_scenarios(capsys, tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    # The device scenario points at dimagi-qa-commcare-mobile's flows; stand them in so this
    # passes on a fresh clone without that repo checked out next to this one.
    for rel in ("login/commcare_odk_01_login.yaml", "form_submissions/multiple_submissions_10x_background_sync.yaml"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("appId: org.commcare.dalvik\n---\n- launchApp\n", encoding="utf-8")
    monkeypatch.setenv("CC_MOBILE_FLOWS", tmp_path.as_posix())
    rc = main(["validate", *[str(p) for p in sorted((root / "scenarios").glob("*.yaml"))]])
    out = capsys.readouterr().out
    assert rc == 0, out
    rc = main(["run", str(root / "scenarios" / "commcare_mobile_load.yaml"), "--dry-run"])
    assert rc == 0
    assert "Nothing will be sent" in capsys.readouterr().out
