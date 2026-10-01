"""Scenario loading: one YAML file -> a validated, env-substituted Scenario.

A scenario holds one or more tests (web_load / mobile_perf / mobile_load) plus the
budgets, regression rules and reporting settings that apply to all of them.
Secrets never live in the YAML: reference them as ${ENV_VAR} or ${ENV_VAR:-default}.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
TEST_TYPES = ("web_load", "mobile_perf", "mobile_load")
LOAD_TYPES = ("web_load", "mobile_load")
MOBILE_ACTION_KINDS = ("submit_form", "sync", "heartbeat", "request")

DEFAULT_SAFETY = {
    # Load tests against these hosts / environment names need --allow-production.
    "production_hosts": [
        "www.commcarehq.org",
        "india.commcarehq.org",
        "eu.commcarehq.org",
    ],
    "production_environments": ["production", "prod"],
    # Peak virtual users above this need --allow-high-load.
    "max_users": 100,
}

DEFAULT_REGRESSION = {
    "enabled": True,
    "baseline_runs": 5,
    "threshold_pct": 25,
    # Ignore regressions smaller than this absolute delta, per unit (noise floor).
    "min_abs_delta": {"ms": 50, "s": 1, "mb": 10, "pct": 2},
    "metrics": ["*/p95_ms", "*/p50_ms", "*/p50_s", "*/pss_mb", "*/peak_pss_mb"],
    "fail": False,
}

DEFAULT_REPORTING = {"html": True, "slack": False, "slack_on": "always"}


class ConfigError(Exception):
    pass


@dataclass
class Scenario:
    name: str
    path: Path
    environment: str
    host: str
    vars: dict
    tests: list[dict]
    budgets: dict
    regression: dict
    reporting: dict
    safety: dict
    missing_env: set[str] = field(default_factory=set)

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    def resolve(self, p: str | os.PathLike | None) -> Path | None:
        """Resolve a path from the YAML relative to the scenario file's folder."""
        if p in (None, ""):
            return None
        path = Path(os.path.expanduser(str(p)))
        return path if path.is_absolute() else (self.base_dir / path).resolve()


def _coerce_scalar(text: str):
    """'20' -> 20, 'true' -> True; anything YAML wouldn't turn into a scalar stays a string."""
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError:
        return text
    return value if isinstance(value, (int, float, bool)) else text


def substitute_env(value, missing: set[str]):
    if isinstance(value, str):
        whole = ENV_PATTERN.fullmatch(value.strip())

        def rep(m):
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            missing.add(name)
            return ""

        out = ENV_PATTERN.sub(rep, value)
        return _coerce_scalar(out) if whole else out
    if isinstance(value, dict):
        return {k: substitute_env(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_env(v, missing) for v in value]
    return value


def parse_duration(value) -> int:
    """'90' / 90 / '90s' / '5m' / '1h30m' -> seconds."""
    if value is None or value == "":
        raise ConfigError("duration is empty")
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if text.isdigit():
        return int(text)
    parts = re.findall(r"(\d+)\s*([hms])", text)
    if not parts or "".join(n + u for n, u in parts) != text.replace(" ", ""):
        raise ConfigError(f"can't parse duration {value!r} (use e.g. 90s, 5m, 1h30m)")
    mult = {"h": 3600, "m": 60, "s": 1}
    return sum(int(n) * mult[u] for n, u in parts)


def peak_users(test: dict) -> int:
    stages = test.get("stages") or []
    if stages:
        return max(int(s.get("users", 0)) for s in stages)
    return int(test.get("users", 0) or 0)


def _merge(defaults: dict, given: dict | None) -> dict:
    out = dict(defaults)
    out.update(given or {})
    return out


def load_scenario(path, only: list[str] | None = None, overrides: dict | None = None,
                  strict_env: bool = True) -> Scenario:
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"scenario file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path.name}: invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name}: top level must be a mapping")

    tests_raw = raw.get("tests") or []
    if not isinstance(tests_raw, list) or not tests_raw:
        raise ConfigError(f"{path.name}: 'tests' must be a non-empty list")
    if only:
        known = {t.get("id") for t in tests_raw if isinstance(t, dict)}
        unknown = [o for o in only if o not in known]
        if unknown:
            raise ConfigError(f"--only: unknown test id(s) {unknown}; known: {sorted(k for k in known if k)}")
        tests_raw = [t for t in tests_raw if t.get("id") in only]

    # Substitute only what will actually run, so an unselected test's secrets aren't required.
    missing: set[str] = set()
    top = substitute_env({k: v for k, v in raw.items() if k != "tests"}, missing)
    tests = substitute_env(tests_raw, missing)

    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    scenario = Scenario(
        name=str(top.get("name") or path.stem),
        path=path,
        environment=str(overrides.get("environment") or top.get("environment") or "unknown"),
        host=str(overrides.get("host") or top.get("host") or "").rstrip("/"),
        vars=top.get("vars") or {},
        tests=tests,
        budgets=top.get("budgets") or {},
        regression=_merge(DEFAULT_REGRESSION, top.get("regression")),
        reporting=_merge(DEFAULT_REPORTING, top.get("reporting")),
        safety=_merge(DEFAULT_SAFETY, top.get("safety")),
        missing_env=missing,
    )
    _apply_overrides(scenario, overrides)

    errors = validate(scenario)
    if missing and strict_env:
        errors.append("missing environment variables (set them or add a ${VAR:-default}): "
                      + ", ".join(sorted(missing)))
    if errors:
        raise ConfigError(f"{path.name}:\n  - " + "\n  - ".join(errors))
    return scenario


def _apply_overrides(scenario: Scenario, overrides: dict) -> None:
    for test in scenario.tests:
        if overrides.get("host"):
            test["host"] = overrides["host"]
        if test.get("type") not in LOAD_TYPES:
            continue
        if overrides.get("users") is not None:
            test["users"] = int(overrides["users"])
            test.pop("stages", None)  # an explicit --users means a flat load, not the staged ramp
        if overrides.get("spawn_rate") is not None:
            test["spawn_rate"] = float(overrides["spawn_rate"])
        if overrides.get("run_time"):
            test["run_time"] = overrides["run_time"]
            test.pop("stages", None)


def validate(scenario: Scenario) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for i, test in enumerate(scenario.tests):
        where = f"tests[{i}]"
        if not isinstance(test, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        tid = test.get("id")
        if not tid or not re.fullmatch(r"[A-Za-z0-9_.-]+", str(tid)):
            errors.append(f"{where}: 'id' is required (letters, digits, _ . -)")
        elif tid in seen:
            errors.append(f"{where}: duplicate id {tid!r}")
        seen.add(tid)
        where = f"test {tid!r}"
        ttype = test.get("type")
        if ttype not in TEST_TYPES:
            errors.append(f"{where}: 'type' must be one of {TEST_TYPES}")
            continue
        if ttype in LOAD_TYPES:
            errors += _validate_load(scenario, test, where)
        if ttype == "web_load":
            errors += _validate_web(scenario, test, where)
        elif ttype == "mobile_load":
            errors += _validate_mobile_load(scenario, test, where)
            if test.get("device_probe"):
                errors += _validate_mobile_perf(scenario, test["device_probe"], f"{where} device_probe")
        elif ttype == "mobile_perf":
            errors += _validate_mobile_perf(scenario, test, where)

    for pattern, spec in scenario.budgets.items():
        if not isinstance(spec, dict) or not ({"warn", "fail"} & set(spec)):
            errors.append(f"budget {pattern!r}: needs 'warn' and/or 'fail'")
        elif spec.get("direction") not in (None, "max", "min"):
            errors.append(f"budget {pattern!r}: direction must be 'max' (lower is better) or 'min'")
    return errors


def _validate_load(scenario, test, where) -> list[str]:
    errors = []
    if not (test.get("host") or scenario.host) and not test.get("locustfile"):
        errors.append(f"{where}: no host (set top-level 'host', the test's 'host', or --host)")
    stages = test.get("stages")
    if stages:
        for j, s in enumerate(stages):
            try:
                parse_duration(s.get("duration"))
                int(s["users"])
            except (ConfigError, KeyError, TypeError, ValueError):
                errors.append(f"{where}: stages[{j}] needs 'duration' (e.g. 2m) and 'users'")
    else:
        if not test.get("users"):
            errors.append(f"{where}: set 'users' (or 'stages')")
        try:
            parse_duration(test.get("run_time"))
        except ConfigError as e:
            errors.append(f"{where}: run_time: {e}")
    if test.get("users_file"):
        p = scenario.resolve(test["users_file"])
        if not p.exists():
            errors.append(f"{where}: users_file not found: {p}")
    return errors


def _validate_web(scenario, test, where) -> list[str]:
    errors = []
    if test.get("locustfile"):
        p = scenario.resolve(test["locustfile"])
        if not p.exists():
            errors.append(f"{where}: locustfile not found: {p}")
        return errors
    tasks = test.get("tasks") or []
    if not tasks:
        errors.append(f"{where}: needs 'tasks' (or an existing 'locustfile')")
    for j, t in enumerate(tasks):
        if not isinstance(t, dict) or not t.get("path"):
            errors.append(f"{where}: tasks[{j}] needs a 'path'")
    auth = (test.get("auth") or {}).get("type", "none")
    if auth not in ("none", "basic", "digest", "api_key", "hq_login"):
        errors.append(f"{where}: auth.type must be none|basic|digest|api_key|hq_login")
    return errors


def _validate_mobile_load(scenario, test, where) -> list[str]:
    errors = []
    domain = test.get("domain") or scenario.vars.get("domain")
    if not domain:
        errors.append(f"{where}: 'domain' is required")
    if not test.get("users_file") and not (test.get("auth") or {}).get("username"):
        errors.append(f"{where}: give mobile workers via 'users_file' (CSV username,password) or auth.username")
    actions = test.get("actions") or []
    if not actions:
        errors.append(f"{where}: needs at least one entry in 'actions'")
    for j, a in enumerate(actions):
        kind = a.get("kind") if isinstance(a, dict) else None
        if kind not in MOBILE_ACTION_KINDS:
            errors.append(f"{where}: actions[{j}].kind must be one of {MOBILE_ACTION_KINDS}")
            continue
        if kind == "submit_form":
            tpl = scenario.resolve(a.get("template"))
            if not tpl or not tpl.exists():
                errors.append(f"{where}: actions[{j}] template not found: {a.get('template')}")
        if kind == "heartbeat" and not test.get("app_id"):
            errors.append(f"{where}: actions[{j}] heartbeat needs the test's 'app_id'")
        if kind == "request" and not a.get("path"):
            errors.append(f"{where}: actions[{j}] request needs a 'path'")
    if (test.get("auth") or {}).get("type", "basic") not in ("basic", "digest"):
        errors.append(f"{where}: mobile auth.type must be basic or digest")
    return errors


def _validate_mobile_perf(scenario, test, where) -> list[str]:
    errors = []
    if not test.get("package"):
        errors.append(f"{where}: 'package' is required (e.g. org.commcare.dalvik)")
    measures = test.get("measures") or ["cold_start", "warm_start", "memory"]
    bad = set(measures) - {"cold_start", "warm_start", "memory"}
    if bad:
        errors.append(f"{where}: unknown measures {sorted(bad)} (cold_start, warm_start, memory)")
    for j, flow in enumerate(test.get("flows") or []):
        p = scenario.resolve(flow.get("path")) if isinstance(flow, dict) else None
        if not p or not p.exists():
            errors.append(f"{where}: flows[{j}] path not found: {flow.get('path') if isinstance(flow, dict) else flow}")
        if isinstance(flow, dict) and not flow.get("name"):
            errors.append(f"{where}: flows[{j}] needs a 'name'")
    if test.get("apk"):
        p = scenario.resolve(test["apk"])
        if not p.exists():
            errors.append(f"{where}: apk not found: {p}")
    return errors
