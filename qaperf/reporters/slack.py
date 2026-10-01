"""Posts a run summary to Slack - incoming webhook (SLACK_WEBHOOK_URL) or bot token
(SLACK_BOT_TOKEN + SLACK_CHANNEL_ID), whichever is set."""
from __future__ import annotations

import os

import requests

from qaperf.reporters.console import fmt_value
from qaperf.results import RunResult

EMOJI = {"pass": ":white_check_mark:", "warn": ":warning:", "fail": ":x:", "error": ":rotating_light:"}


def build_blocks(run: RunResult, report_url: str | None = None) -> list[dict]:
    bad = [b for b in run.budgets if b.status in ("fail", "warn", "missing")]
    lines = [f"{'FAIL' if b.status == 'fail' else 'WARN'} `{b.key}` = {fmt_value(b.value, b.unit)}"
             f" (fail {fmt_value(b.fail, b.unit)})" for b in bad[:10]]
    lines += [f"REGRESSION `{r.key}` {fmt_value(r.value, r.unit)} vs {fmt_value(r.baseline, r.unit)}"
              f" ({r.change_pct:+.0f}%)" for r in run.regressions[:5]]
    lines += [f"ERROR test `{t.test_id}`: {t.error.splitlines()[0][:200]}" for t in run.tests if t.error]
    passed = sum(b.status == "pass" for b in run.budgets)
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"{EMOJI[run.status]} *Perf: {run.scenario}* on *{run.environment}* - *{run.status.upper()}*\n"
            f"{len(run.tests)} test(s), {passed}/{len(run.budgets)} budgets passed, "
            f"{len(run.regressions)} regression(s)  ·  `{run.run_id}`"}},
    ]
    if lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}})
    if report_url:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{report_url}|Full report>"}]})
    return blocks


def post(run: RunResult, report_url: str | None = None) -> str:
    blocks = build_blocks(run, report_url)
    text = f"Perf {run.scenario} [{run.environment}]: {run.status.upper()}"
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if webhook:
        r = requests.post(webhook, json={"text": text, "blocks": blocks}, timeout=30)
        r.raise_for_status()
        return "posted via webhook"
    token, channel = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("SLACK_CHANNEL_ID")
    if token and channel:
        r = requests.post("https://slack.com/api/chat.postMessage", timeout=30,
                          headers={"Authorization": f"Bearer {token}"},
                          json={"channel": channel, "text": text, "blocks": blocks})
        r.raise_for_status()
        if not r.json().get("ok"):
            raise RuntimeError(f"Slack error: {r.json().get('error')}")
        return f"posted to {channel}"
    return "skipped (set SLACK_WEBHOOK_URL, or SLACK_BOT_TOKEN + SLACK_CHANNEL_ID)"
