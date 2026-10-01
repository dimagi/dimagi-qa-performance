"""Guard rails so a load test can't be pointed at production (or at 5,000 users) by accident."""
from __future__ import annotations

from urllib.parse import urlparse

from qaperf.config import LOAD_TYPES, Scenario, peak_users


def check_safety(scenario: Scenario, allow_production: bool, allow_high_load: bool) -> list[str]:
    errors = []
    prod_hosts = {h.lower() for h in scenario.safety.get("production_hosts", [])}
    prod_envs = {e.lower() for e in scenario.safety.get("production_environments", [])}
    max_users = int(scenario.safety.get("max_users", 100))
    for test in scenario.tests:
        if test["type"] not in LOAD_TYPES:
            continue
        host = (urlparse(test.get("host") or scenario.host).hostname or "").lower()
        is_prod = host in prod_hosts or scenario.environment.lower() in prod_envs
        if is_prod and not allow_production:
            errors.append(
                f"test {test['id']!r} would load-test production ({host or scenario.environment}). "
                "Get sign-off from the environment owner, then re-run with --allow-production.")
        peak = peak_users(test)
        if peak > max_users and not allow_high_load:
            errors.append(
                f"test {test['id']!r} peaks at {peak} users (> safety.max_users={max_users}). "
                "Re-run with --allow-high-load if that's intended.")
    return errors
