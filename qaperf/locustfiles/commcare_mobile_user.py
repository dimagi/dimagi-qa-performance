"""Built-in mobile load locustfile: each virtual user is one CommCare mobile worker on its own device.

It speaks the same HTTP protocol the Android app does, so hundreds of "phones" can be
simulated from one machine:

  on start   initial OTA restore (handles HQ's 202 async-restore polling)
  actions    submit_form - POST an XForm to the receiver (template rendered per submission)
             sync        - incremental restore using the user's last sync token
             heartbeat   - the app's periodic heartbeat call
             request     - any other endpoint (case search, app download, ...), same shape as web tasks

Each restore's end-to-end time including async waiting - what the user actually waits
for - is recorded separately (not as a Locust request, so the Aggregated row isn't
double-counted) and reported as '<name> (total)'. Budgets should usually target that.
"""
import json
import os
import random
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from collections import defaultdict

from locust import HttpUser, events
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

from qaperf.locustfiles._shared import (CredentialPool, build_stages_shape, fmt, load_config,
                                        load_credentials, run_request, wait_time_from)

CONFIG = load_config()
POOL = CredentialPool(load_credentials(CONFIG)) if CONFIG else None
DOMAIN = CONFIG.get("domain", "")
APP_ID = CONFIG.get("app_id", "")
TEMPLATES = {a["template"]: Path(a["template"]).read_text(encoding="utf-8")
             for a in CONFIG.get("actions", []) if a.get("kind") == "submit_form"}

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
RESTORE_ID = re.compile(r"<restore_id>([^<]+)</restore_id>")
USER_UUID = re.compile(r"<Registration\b.*?<uuid>([^<]+)</uuid>", re.S)
CASE_ID = re.compile(r"<case\b[^>]*\bcase_id=\"([^\"]+)\"")
RETRY_AFTER = re.compile(r"retry-after=\"(\d+)\"")

# name -> end-to-end restore durations (ms) / failure count, written out when the test stops
TOTALS: dict[str, list[float]] = defaultdict(list)
TOTAL_FAILURES: dict[str, int] = defaultdict(int)


@events.test_stop.add_listener
def _write_totals(environment, **kw):
    out_dir = os.environ.get("QAPERF_OUT_DIR")
    if not out_dir or not (TOTALS or TOTAL_FAILURES):
        return
    tag = os.environ.get("QAPERF_ROLE", "standalone") + os.environ.get("QAPERF_WORKER_INDEX", "")
    Path(out_dir, f"restore_totals_{tag}.json").write_text(
        json.dumps({"durations_ms": TOTALS, "failures": TOTAL_FAILURES}), encoding="utf-8")


def render_template(template: str, ctx: dict) -> str:
    def rep(m):
        key = m.group(1)
        if key not in ctx or ctx[key] in (None, ""):
            raise KeyError(f"form template needs {{{{{key}}}}} but it has no value")
        return str(ctx[key])

    return PLACEHOLDER.sub(rep, template)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _make_action(action: dict):
    kind = action["kind"]

    def _task(user):
        if kind == "submit_form":
            user.submit_form(action)
        elif kind == "sync":
            user.restore(action.get("name", "restore: incremental"), incremental=True)
        elif kind == "heartbeat":
            user.heartbeat(action)
        else:
            run_request(user, action, user.ctx)

    _task.__name__ = "action_" + re.sub(r"\W+", "_", action.get("name", kind))
    return _task


class CommCareMobileUser(HttpUser):
    wait_time = wait_time_from(CONFIG.get("wait_time", [5, 15]))
    tasks = {_make_action(a): int(a.get("weight", 1)) for a in CONFIG.get("actions", [])}

    def on_start(self):
        creds = POOL.next()
        username = creds["username"]
        if "@" not in username:
            username = f"{username}@{DOMAIN}.{CONFIG.get('user_domain_suffix', 'commcarehq.org')}"
        self.username = username
        self.device_id = f"qaperf-{uuid.uuid4().hex[:12]}"
        self.restore_id = None
        self.user_id = creds.get("user_id", "")
        self.case_ids: list[str] = []
        auth_type = (CONFIG.get("auth") or {}).get("type", "basic")
        auth_cls = HTTPDigestAuth if auth_type == "digest" else HTTPBasicAuth
        self.client.auth = auth_cls(username, creds.get("password", ""))
        self.client.headers["X-OpenRosa-Version"] = "1.0"
        self.ctx = {**CONFIG.get("vars", {}), **creds, "domain": DOMAIN, "app_id": APP_ID,
                    "username": username, "username_short": username.split("@")[0],
                    "device_id": self.device_id, "app_version": CONFIG.get("app_version", "")}
        if CONFIG.get("initial_restore", True):
            self.restore("restore: initial", incremental=False)

    # --- protocol calls -------------------------------------------------------------

    def restore(self, name: str, incremental: bool) -> bool:
        default = "/a/{domain}/phone/restore/{app_id}/" if APP_ID else "/a/{domain}/phone/restore/"
        path = fmt(CONFIG.get("restore_path", default), self.ctx)
        params = {"version": "2.0", "device_id": self.device_id}
        if CONFIG.get("app_version"):
            params["app_version"] = CONFIG["app_version"]
        if incremental and self.restore_id:
            params["since"] = self.restore_id
        max_wait = float(CONFIG.get("restore_max_wait_s", 300))
        start = time.perf_counter()
        polls = 0
        while True:
            label = name if polls == 0 else f"{name} (async poll)"
            with self.client.get(path, params=params, name=label, catch_response=True) as r:
                status = r.status_code
                if status == 200:
                    r.success()
                    body = r.text
                    break
                if status == 202:
                    r.success()
                    header = r.headers.get("Retry-After", "")
                    m = RETRY_AFTER.search(r.text)
                    wait = int(header) if header.isdigit() else int(m.group(1)) if m else 5
                    wait = min(max(wait, 1), 30)
                else:
                    r.failure(f"restore status {status}")
                    self._total(name, start, ok=False)
                    return False
            polls += 1
            if time.perf_counter() - start + wait > max_wait:
                self._total(name, start, ok=False)
                self._fire(f"{name} (async timeout)", "RESTORE", 0, 0,
                           f"async restore not done after {max_wait:.0f}s")
                return False
            time.sleep(wait)

        self._total(name, start, ok=True)
        m = RESTORE_ID.search(body)
        if m:
            self.restore_id = m.group(1)
        m = USER_UUID.search(body)
        if m and not self.user_id:
            self.user_id = m.group(1)
            self.ctx["user_id"] = self.user_id
        if not incremental:
            self.case_ids = CASE_ID.findall(body)[:500]
        return True

    def submit_form(self, action: dict) -> None:
        name = action.get("name", "form: submit")
        ctx = {**self.ctx, **(action.get("vars") or {}),
               "instance_id": str(uuid.uuid4()), "case_id": str(uuid.uuid4()), "now": _now(),
               "user_id": self.user_id, "random_name": f"qaperf-{uuid.uuid4().hex[:8]}",
               "existing_case_id": random.choice(self.case_ids) if self.case_ids else ""}
        try:
            xml = render_template(TEMPLATES[action["template"]], ctx)
            path = fmt(action.get("path") or CONFIG.get("submit_path") or
                       ("/a/{domain}/receiver/{app_id}/" if APP_ID else "/a/{domain}/receiver/"), ctx)
        except KeyError as e:
            self._fire(name, "SUBMIT", 0, 0, f"{e.args[0] if e.args else e}")
            return
        expect = action.get("expect_status", [201])
        expect = expect if isinstance(expect, list) else [expect]
        files = {"xml_submission_file": ("xform.xml", xml.encode("utf-8"), "text/xml")}
        with self.client.post(path, files=files, name=name, catch_response=True) as r:
            if r.status_code not in expect:
                r.failure(f"submit status {r.status_code}, expected {expect}")
                return
            r.success()
        if action.get("creates_case", "{{case_id}}" in TEMPLATES[action["template"]]):
            self.case_ids.append(ctx["case_id"])
            del self.case_ids[:-500]

    def heartbeat(self, action: dict) -> None:
        path = fmt(action.get("path", "/a/{domain}/phone/heartbeat/{app_id}/"), self.ctx)
        params = {"app_id": APP_ID, "device_id": self.device_id,
                  "app_version": CONFIG.get("app_version", ""), "cc_version": CONFIG.get("cc_version", ""),
                  "last_sync": _now(), "unsent_forms": 0, "quarantined_forms": 0}
        with self.client.get(path, params=params, name=action.get("name", "heartbeat"),
                             catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"heartbeat status {r.status_code}")
            else:
                r.success()

    # --- locust plumbing -----------------------------------------------------------

    def _total(self, name, start, ok: bool):
        if ok:
            TOTALS[f"{name} (total)"].append(round((time.perf_counter() - start) * 1000, 1))
        else:
            TOTAL_FAILURES[f"{name} (total)"] += 1

    def _fire(self, name, rtype, ms, length, error):
        self.environment.events.request.fire(
            request_type=rtype, name=name, response_time=ms, response_length=length,
            exception=Exception(error) if error else None, context={})


if CONFIG.get("stages"):
    StagesShape = build_stages_shape(CONFIG["stages"])
