"""Helpers shared by the built-in locustfiles.

The runner writes the resolved test config to JSON and points QAPERF_TEST_CONFIG at it,
so locustfiles never parse YAML or read secrets themselves.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
from pathlib import Path

from locust import LoadTestShape, between, constant
from locust.exception import StopUser
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

log = logging.getLogger("qaperf")


def load_config() -> dict:
    p = os.environ.get("QAPERF_TEST_CONFIG")
    return json.loads(Path(p).read_text(encoding="utf-8")) if p else {}


def load_credentials(cfg: dict) -> list[dict]:
    """Rows from users_file (CSV with at least a 'username' column), else the single auth user."""
    if cfg.get("users_file"):
        with open(cfg["users_file"], newline="", encoding="utf-8-sig") as f:
            rows = [{k.strip(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(f)]
        rows = [r for r in rows if r.get("username")]
        if not rows:
            raise RuntimeError(f"users_file {cfg['users_file']} has no rows with a 'username'")
        return rows
    auth = cfg.get("auth") or {}
    if auth.get("username"):
        return [{k: auth.get(k, "") for k in ("username", "password", "api_key")}]
    return [{}]


class CredentialPool:
    """Gives each virtual user its own credential row.

    With N Locust workers, worker w takes rows w, w+N, w+2N... so workers don't
    collide on the same user. If there are more virtual users than rows, rows are
    reused (with one warning) - realistic multi-user tests need >= as many rows as users.
    """

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.count = 0
        self.worker_index = int(os.environ.get("QAPERF_WORKER_INDEX", "0"))
        self.worker_count = max(1, int(os.environ.get("QAPERF_WORKER_COUNT", "1")))
        self._warned = False

    def next(self) -> dict:
        idx = self.worker_index + self.count * self.worker_count
        self.count += 1
        if idx >= len(self.rows) and len(self.rows) > 1 and not self._warned:
            log.warning("more virtual users than credential rows (%d); reusing users", len(self.rows))
            self._warned = True
        return dict(self.rows[idx % len(self.rows)])


class _StrictCtx(dict):
    def __missing__(self, key):
        raise KeyError(f"unknown placeholder {{{key}}} (available: {', '.join(sorted(self))})")


def fmt(value, ctx: dict):
    """Fill {placeholders} in strings, recursively through dicts/lists."""
    if isinstance(value, str):
        return value.format_map(_StrictCtx(ctx))
    if isinstance(value, dict):
        return {k: fmt(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [fmt(v, ctx) for v in value]
    return value


def wait_time_from(spec):
    if spec is None:
        return between(1, 3)
    if isinstance(spec, (int, float)):
        return constant(spec)
    lo, hi = spec
    return between(lo, hi)


def build_stages_shape(stages: list[dict]):
    """Cumulative stages: [{duration_s: 120, users: 10, spawn_rate: 2}, {duration_s: 300, users: 50}, ...]"""

    class StagesShape(LoadTestShape):
        def tick(self):
            elapsed = 0
            run_time = self.get_run_time()
            for s in stages:
                elapsed += s["duration_s"]
                if run_time < elapsed:
                    return int(s["users"]), float(s.get("spawn_rate") or max(1, int(s["users"]) / 10))
            return None

    return StagesShape


def apply_auth(user, auth: dict | None, creds: dict, ctx: dict) -> None:
    auth = auth or {}
    kind = auth.get("type", "none")
    if kind == "none":
        return
    if kind == "basic":
        user.client.auth = HTTPBasicAuth(creds["username"], creds.get("password", ""))
    elif kind == "digest":
        user.client.auth = HTTPDigestAuth(creds["username"], creds.get("password", ""))
    elif kind == "api_key":
        key = creds.get("api_key") or auth.get("api_key")
        user.client.headers["Authorization"] = f"ApiKey {creds['username']}:{key}"
    elif kind == "hq_login":
        hq_login(user, auth, creds, ctx)
    else:
        raise StopUser(f"unknown auth type {kind!r}")


def hq_login(user, auth: dict, creds: dict, ctx: dict) -> None:
    """CommCare HQ session login - same form post dimagi-qa's Locust scripts use.

    Doesn't work for accounts with 2FA enforced; use dedicated perf test users.
    """
    path = fmt(auth.get("login_path", "/a/{domain}/login/"), ctx)
    step_field = auth.get("step_field", "cloud_care_login_view-current_step")
    with user.client.get(path, name="auth: login page", catch_response=True) as r:
        if r.status_code != 200:
            r.failure(f"login page status {r.status_code}")
            raise StopUser()
        r.success()
    data = {"auth-username": creds["username"], "auth-password": creds.get("password", ""), step_field: "auth"}
    headers = {"X-CSRFToken": user.client.cookies.get("csrftoken", ""), "Referer": user.host.rstrip("/") + path}
    failure_text = auth.get("failure_text", "Sign In")
    with user.client.post(path, data=data, headers=headers, name="auth: login", catch_response=True) as r:
        if r.status_code != 200 or (failure_text and failure_text in r.text):
            r.failure(f"login failed for {creds['username']} (status {r.status_code})")
            raise StopUser()
        r.success()


def run_request(user, step: dict, ctx: dict):
    """One declarative request: method/path/params/json/data/headers, checked by
    expect_status + contains, with optional regex 'extract' into ctx for later steps."""
    try:
        path = fmt(step["path"], ctx)
        kwargs = {k: fmt(step[k], ctx) for k in ("params", "headers", "json", "data") if k in step}
    except KeyError as e:
        _fire_failure(user, step.get("name", step["path"]), f"config error: {e}")
        return None
    expect = step.get("expect_status", [200])
    expect = expect if isinstance(expect, list) else [expect]
    name = step.get("name", step["path"])
    with user.client.request(step.get("method", "GET").upper(), path, name=name,
                             catch_response=True, **kwargs) as resp:
        if resp.status_code not in expect:
            resp.failure(f"status {resp.status_code}, expected {expect}")
            return resp
        if step.get("contains") and step["contains"] not in resp.text:
            resp.failure(f"response doesn't contain {step['contains']!r}")
            return resp
        for var, pattern in (step.get("extract") or {}).items():
            m = re.search(pattern, resp.text)
            if not m:
                resp.failure(f"extract {var}: /{pattern}/ not found")
                return resp
            ctx[var] = m.group(1) if m.groups() else m.group(0)
        resp.success()
    return resp


def _fire_failure(user, name: str, message: str) -> None:
    user.environment.events.request.fire(
        request_type="ERROR", name=name, response_time=0, response_length=0,
        exception=Exception(message), context={})
