"""Unit tests: config, budgets, regressions, Locust CSV parsing, Android output parsers."""
import textwrap

import pytest

from qaperf.budgets import evaluate_budgets
from qaperf.config import ConfigError, load_scenario, parse_duration
from qaperf.engine import overall_status
from qaperf.history import find_regressions
from qaperf.mobile.adb import parse_am_start, parse_gfxinfo, parse_meminfo_pss_kb, parse_proc_stat_ticks
from qaperf.results import Metric, RunResult, TestResult, percentile
from qaperf.runners.locust_runner import metrics_from_stats
from qaperf.safety import check_safety


def write(tmp_path, text, name="s.yaml"):
    p = tmp_path / name
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


WEB = """
name: demo
host: ${DEMO_HOST:-http://example.test}
environment: staging
vars: {domain: d}
tests:
  - id: web
    type: web_load
    users: ${DEMO_USERS:-7}
    run_time: 1m
    stages: [{duration: 30s, users: 5}]
    tasks: [{path: "/a/{domain}/x/"}]
  - id: other
    type: web_load
    users: 1
    run_time: 10s
    auth: {type: basic, username: "${OTHER_USER}", password: x}
    tasks: [{path: /}]
"""


def test_env_substitution_defaults_and_coercion(tmp_path, monkeypatch):
    monkeypatch.setenv("OTHER_USER", "bob")
    s = load_scenario(write(tmp_path, WEB))
    assert s.host == "http://example.test"
    assert s.tests[0]["users"] == 7  # "${DEMO_USERS:-7}" became an int
    assert s.tests[1]["auth"]["username"] == "bob"


def test_missing_env_only_counts_for_selected_tests(tmp_path, monkeypatch):
    monkeypatch.delenv("OTHER_USER", raising=False)
    with pytest.raises(ConfigError, match="OTHER_USER"):
        load_scenario(write(tmp_path, WEB))
    s = load_scenario(write(tmp_path, WEB), only=["web"])
    assert [t["id"] for t in s.tests] == ["web"]


def test_cli_users_override_replaces_stages(tmp_path):
    s = load_scenario(write(tmp_path, WEB), only=["web"], overrides={"users": 3, "run_time": "20s"})
    assert s.tests[0]["users"] == 3 and "stages" not in s.tests[0]


def test_validation_reports_every_problem(tmp_path):
    bad = """
    tests:
      - {id: a, type: web_load, users: 1, run_time: 5x}
      - {id: a, type: nope}
      - {id: m, type: mobile_load, users: 1, run_time: 1m, actions: [{kind: submit_form, template: missing.xml}]}
      - {id: p, type: mobile_perf, measures: [teleport]}
    budgets: {"x/*": {limit: 3}}
    """
    with pytest.raises(ConfigError) as e:
        load_scenario(write(tmp_path, bad))
    msg = str(e.value)
    for needle in ["run_time", "duplicate id", "'type' must be", "domain", "template not found",
                   "'package' is required", "teleport", "needs 'warn'", "no host"]:
        assert needle in msg, needle


def test_parse_duration():
    assert parse_duration("90") == 90
    assert parse_duration("1h30m") == 5400
    assert parse_duration("2m 5s") == 125
    with pytest.raises(ConfigError):
        parse_duration("5 minutes")


def test_safety_blocks_production_and_high_load(tmp_path):
    s = load_scenario(write(tmp_path, WEB), only=["web"],
                      overrides={"host": "https://www.commcarehq.org", "users": 500, "run_time": "1m"})
    problems = check_safety(s, allow_production=False, allow_high_load=False)
    assert len(problems) == 2
    assert check_safety(s, allow_production=True, allow_high_load=True) == []


def M(key, value, unit="ms"):
    runner, tid, subject, stat = key.split("/")
    return Metric(runner, tid, subject, stat, value, unit)


def test_budgets_directions_and_missing():
    metrics = [M("web_load/t/Aggregated/p95_ms", 2500), M("web_load/t/Login/p95_ms", 900),
               M("web_load/t/Aggregated/rps", 3.0, "req/s")]
    res = {b.key: b.status for b in evaluate_budgets(metrics, {
        "web_load/t/*/p95_ms": {"warn": 2000, "fail": 3000},
        "web_load/t/Aggregated/rps": {"fail": 5},           # higher-is-better -> floor
        "web_load/t/Renamed/p95_ms": {"fail": 1000},
    })}
    assert res == {"web_load/t/Aggregated/p95_ms": "warn", "web_load/t/Login/p95_ms": "pass",
                   "web_load/t/Aggregated/rps": "fail", "web_load/t/Renamed/p95_ms": "missing"}


def test_regression_uses_median_baseline_and_noise_floor():
    past = [{"status": "pass", "metrics": {"web_load/t/A/p95_ms": v, "web_load/t/B/p95_ms": 100}}
            for v in (600, 620, 580, 5000)]  # one outlier run doesn't move the median much
    cfg = {"enabled": True, "baseline_runs": 5, "threshold_pct": 25, "min_abs_delta": {"ms": 50},
           "metrics": ["*/p95_ms"]}
    regs = find_regressions([M("web_load/t/A/p95_ms", 1000), M("web_load/t/B/p95_ms", 140)], past, cfg)
    assert [r.key for r in regs] == ["web_load/t/A/p95_ms"]  # B is +40% but only +40ms (< floor)
    assert regs[0].baseline == 610


def test_overall_status():
    run = RunResult("r", "s", "p", "e", "h", "t", tests=[TestResult("a", "web_load")])
    assert overall_status(run) == "pass"
    run.tests.append(TestResult("b", "mobile_perf", status="error"))
    assert overall_status(run) == "error"


def test_percentile_matches_numpy_linear():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([10], 95) == 10
    assert round(percentile(list(range(1, 101)), 95), 2) == 95.05


def test_locust_stats_to_metrics():
    rows = [{"Type": "GET", "Name": "Dashboard", "Request Count": "200", "Failure Count": "4",
             "Average Response Time": "123.4", "Max Response Time": "900", "Requests/s": "3.3",
             "50%": "110", "90%": "200", "95%": "250", "99%": "600"},
            {"Type": "", "Name": "Aggregated", "Request Count": "0", "Failure Count": "0"}]
    ms = {m.key: m.value for m in metrics_from_stats(rows, "web_load", "t")}
    assert ms["web_load/t/Dashboard/p95_ms"] == 250
    assert ms["web_load/t/Dashboard/error_rate_pct"] == 2.0
    assert not any("Aggregated" in k for k in ms)  # zero-request rows are skipped


def test_android_parsers():
    am = parse_am_start("Status: ok\nLaunchState: COLD\nTotalTime: 1234\nWaitTime: 1300\nComplete")
    assert am == {"total_ms": 1234, "wait_ms": 1300, "launch_state": "COLD"}
    assert parse_meminfo_pss_kb("           TOTAL PSS:   145373            TOTAL RSS: 1") == 145373
    assert parse_meminfo_pss_kb("            TOTAL    98765    50000    1000") == 98765  # Android <= 9
    gfx = parse_gfxinfo("Total frames rendered: 820\nJanky frames: 41 (5.00%)\n90th percentile: 14ms")
    assert gfx == {"frames": 820, "janky_frames": 41, "janky_pct": 5.0, "p90_ms": 14}
    assert parse_proc_stat_ticks("4321 (my app (x)) S 1 1 0 0 -1 1 1 0 0 0 150 50 0 0") == 200
