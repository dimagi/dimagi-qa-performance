# qa-perf: one-stop performance testing

One CLI, one YAML format and one report for three kinds of test:

| Test type | What it measures | Engine |
|---|---|---|
| `web_load` | Many users hitting a web app (CommCare HQ, Web Apps, APIs): latency percentiles, errors, throughput | Locust |
| `mobile_perf` | A real Android app on one or many devices: cold/warm start, memory, CPU, frame jank, timed Maestro flows | adb + Maestro |
| `mobile_load` | Many **mobile users at once**: each virtual user is a separate CommCare mobile worker on its own device ID, doing restore / form submit / sync / heartbeat | Locust (CommCare mobile protocol) |

You can mix test types in one scenario. All three share the same pieces:

- **Budgets** with warn/fail thresholds.
- **Regression detection** against your own recent runs.
- **Run history** for trends.
- A **self-contained HTML report**, a **console summary** and an optional **Slack post**.
- **CI exit codes**.

`mobile_load` can also attach a `device_probe`, which measures a real phone *while* the simulated users load the server. That shows what a field worker experiences at peak.

## Quick start

```bash
cd qa-perf
python -m venv .venv && .venv\Scripts\activate      # (source .venv/bin/activate on mac/linux)
pip install -r requirements.txt
copy .env.example .env                               # fill in credentials - never commit .env

python -m qaperf validate scenarios/*.yaml
python -m qaperf run scenarios/hq_web_load.yaml --dry-run          # prints the plan, sends nothing
python -m qaperf run scenarios/hq_web_load.yaml --users 5 --run-time 2m   # small smoke first
python -m qaperf devices                                          # attached Android devices
python -m qaperf run scenarios/commcare_mobile_perf.yaml
python -m qaperf run scenarios/commcare_mobile_load.yaml --users 10 --run-time 3m
```

Each run writes `results/<run_id>/` containing `report.html`, `results.json`, the Locust HTML/CSV files and device logs. It also appends a line to `results/history.jsonl`.

**Exit codes:** `0` = pass (warnings allowed unless `--strict`); `1` = a budget failed, a regression failed or a test errored; `2` = a config or safety problem.

## Scenario files

```yaml
name: hq-web-load
environment: ${QAPERF_ENV:-staging}
host: ${HQ_BASE_URL:-https://staging.commcarehq.org}
vars: {domain: qateam}          # usable as {domain} in paths
tests:
  - id: hq_browse
    type: web_load
    stages:                      # or flat: users: 20, run_time: 5m
      - {duration: 2m, users: 10, spawn_rate: 2}
      - {duration: 5m, users: 50, spawn_rate: 5}
    auth: {type: hq_login, username: "${HQ_WEB_USERNAME}", password: "${HQ_WEB_PASSWORD}"}
    users_file: data/web_users.csv     # optional: one row per user
    tasks:
      - {name: Dashboard, path: "/a/{domain}/dashboard/project/", weight: 3}
budgets:
  "web_load/hq_browse/Aggregated/p95_ms": {warn: 2500, fail: 5000}
```

- **Secrets** go in env vars only: use `${VAR}` or `${VAR:-default}`. Inside `{ ... }` flow-style YAML, quote them, e.g. `"${VAR}"`. A missing variable fails the run loudly, but only for the tests you actually selected with `--only`.
- **Paths** in a scenario are relative to the scenario file.
- **CLI overrides:** `--users`, `--run-time`, `--spawn-rate`, `--host`, `--env`, `--only <test id>`.

### Metric keys, budgets and regressions

Every number is reported under the key `runner/test_id/subject/stat`. For example:

- `web_load/hq_browse/Dashboard/p95_ms`
- `mobile_load/mobile_workers/restore: initial (total)/p95_ms`
- `mobile_perf/device_perf/cold_start/p50_ms`
- `mobile_perf/device_perf/flow:login/janky_frames_pct`

**Budgets** are glob patterns over these keys. A budget whose pattern matches nothing is reported as **MISSING** (a warning). That way a renamed request or a test that didn't run can't pass silently. Stats where a higher number is better (`rps`, `success_rate_pct`) are treated as floors automatically.

**Regressions** compare each run with the median of the last 5 runs on the same scenario and environment. By default a metric is flagged when it is more than 25% worse *and* the change is above a noise floor (50 ms, 1 s, 10 MB or 2 percentage points). This catches slowdowns that are still under budget. You can tune this in the `regression:` block; `fail: true` turns regressions into failures.

**Stats:**

- **Load tests:** `requests failures error_rate_pct rps avg_ms p50_ms p90_ms p95_ms p99_ms max_ms`.
- **Devices:** `cold_start` and `warm_start` report `p50_ms p90_ms max_ms samples`; `idle_memory` reports `pss_mb`.
- **Each flow:** `flow:<name>` reports `success_rate_pct p50_s max_s peak_pss_mb avg_cpu_pct peak_cpu_pct janky_frames_pct frame_p90_ms`.
- **Several devices:** each metric is also reported per device as `<subject>@<serial>`.

## Web load (`web_load`)

- `mode: weighted` (the default) picks tasks at random by weight, so you get a traffic mix. `mode: sequential` walks each user through the tasks in order (a user journey). In sequential mode, `extract: {var: "regex(group)"}` captures a value from one response into a later step's `{var}`.
- **Auth types:**
  - `none`
  - `basic` / `digest`
  - `api_key` (HQ's `ApiKey user:key`)
  - `hq_login`: the same session login as the dimagi-qa Locust scripts. It doesn't work for accounts with 2FA enforced, so use dedicated perf users.
- **Existing Locust scripts:** set `locustfile:` (plus `cwd`, `pythonpath`, `locust_args`) to run a dimagi-qa `LocustScripts/...py` unchanged. You still get budgets, history and reports. See the commented example in `scenarios/hq_web_load.yaml`.
- **Large user counts:** `workers: N` starts a Locust master plus N local worker processes. A single Locust process tops out at roughly one CPU core.

## Mobile load with multiple users (`mobile_load`)

Each virtual user is one CommCare mobile worker, with a unique `device_id` and its own sync token. It does what the app does over HTTP:

- **On start:** an initial OTA restore (`/a/<domain>/phone/restore/[<app_id>/]`). HQ's `202` async-restore responses are polled until the restore finishes.
- **`submit_form`:** posts an XForm to `/a/<domain>/receiver/[<app_id>/]`. The form is rendered per submission from a template; see `data/forms/`. Placeholders include `{{instance_id}}`, `{{case_id}}`, `{{existing_case_id}}`, `{{user_id}}` (taken from the restore), `{{now}}` and `{{device_id}}`.
- **`sync`:** an incremental restore (`since=<last restore_id>`).
- **`heartbeat`:** the app's heartbeat call. Needs `app_id`.
- **`request`:** any other endpoint, such as case search.

`restore: … (total)` is the full time the user waits, *including* async polling. It's kept separate from Locust's `Aggregated` row so nothing is counted twice. It's usually the restore metric to put a budget on.

**Users:** `users_file` is a CSV of `username,password` (short names get `@<domain>.commcarehq.org` appended). Give it at least as many rows as your peak user count. If there are fewer rows, users are reused and a warning is logged.

> This **creates real forms and cases**. Point it only at a dedicated perf domain with perf mobile workers.

For realistic payloads, replace the sample templates with real submissions from your perf app (same `xmlns`, case structure and sizes).

## Mobile device performance (`mobile_perf`)

**Needs:**

- `adb` and at least one device or emulator in `adb devices`: USB, Wi-Fi adb, or an emulator.
- The app installed, or `apk:` set so qa-perf installs it.
- Maestro, if you use `flows`.

**What it does on each device:**

- **Cold start:** `am start -W -S`, repeated `iterations` times.
- **Warm start:** HOME, then `am start -W`.
- **Idle memory:** PSS after launch.
- **Flows:** each Maestro flow from `dimagi-qa-commcare-mobile/flows` is timed. While it runs, the app's PSS and CPU are sampled in the background (CPU from `/proc/<pid>/stat` as a % of one core). Frame stats come from `dumpsys gfxinfo`, which is reset before each run.

**Multi-device, multi-user:**

- Measuring devices in parallel is on by default (`devices: auto`).
- With a `users_file`, each device logs in as the next user in the file. Flow env values can use `{username}` and `{password}`.
- Maestro flows run one after another across devices unless you set `maestro_parallel: true`. Only do that once you've confirmed parallel Maestro works on your machine.

**Caveats:**

- Flow durations include Maestro's own overhead. Use them for trends and budgets, not as exact UI timings.
- BrowserStack App Automate doesn't expose adb, so device metrics need a local device, emulator or self-hosted runner.

## Safety rails

Load tests refuse to run in these cases unless you pass a flag:

- **Production:** the target host is a production host (`www.`, `india.` or `eu.commcarehq.org`) or the environment is named `production`. Needs `--allow-production`; get the environment owner's sign-off first.
- **High load:** more than `safety.max_users` (default 100) peak users. Needs `--allow-high-load`.

Both thresholds can be changed in each scenario's `safety:` block. `--dry-run` validates everything and checks tooling (locust, adb, devices, maestro) without sending any traffic.

## Slack and CI

- **Slack:** `--slack` (or `reporting.slack: true`) posts a summary using `SLACK_WEBHOOK_URL`, or `SLACK_BOT_TOKEN` + `SLACK_CHANNEL_ID`. `reporting.slack_on: problems` posts only on warn or fail.
- **CI:** `.github/workflows/qa-perf.yml` is a `workflow_dispatch` job. It caches `history.jsonl` between runs and uploads `results/`. Set the `QAPERF_*` repo secrets it reads first.

## Tests

```bash
python -m pytest -q
```

The suite has unit tests plus end-to-end runs of real Locust against a local fake HQ: web load with HQ login, and mobile load with 5 distinct mobile workers, async restore, and form register/update. A multi-device `mobile_perf` run is driven by a fake adb and fake Maestro. Nothing leaves `127.0.0.1`.

## Layout

```
qaperf/
  cli.py  engine.py  config.py  results.py  budgets.py  history.py  safety.py
  runners/      web_load, mobile_load, mobile_perf (+ shared Locust process manager)
  locustfiles/  web_user.py (YAML tasks), commcare_mobile_user.py (CommCare mobile protocol)
  mobile/adb.py adb wrapper + Android output parsers
  reporters/    console, html, slack
scenarios/      hq_web_load, commcare_mobile_perf, commcare_mobile_load
data/           users CSV example, XForm templates
tests/          unit + end-to-end (fakes/ = fake HQ server, fake adb, fake maestro)
```
