"""Built-in web load locustfile: virtual users run the YAML 'tasks' list.

mode: weighted   (default) each task is picked at random by 'weight' - a traffic mix
mode: sequential every user walks the tasks in order, then loops - a user journey
"""
import re

from locust import HttpUser, SequentialTaskSet

from qaperf.locustfiles._shared import (CredentialPool, apply_auth, build_stages_shape, load_config,
                                        load_credentials, run_request, wait_time_from)

CONFIG = load_config()
POOL = CredentialPool(load_credentials(CONFIG)) if CONFIG else None


def _make_task(step: dict):
    def _task(parent):
        user = parent if isinstance(parent, HttpUser) else parent.user
        run_request(user, step, user.ctx)

    _task.__name__ = "task_" + re.sub(r"\W+", "_", step.get("name", step["path"]))
    return _task


_STEPS = CONFIG.get("tasks") or []

if CONFIG.get("mode") == "sequential":
    class Journey(SequentialTaskSet):
        tasks = [_make_task(s) for s in _STEPS]

    _TASKS = [Journey]
else:
    _TASKS = {_make_task(s): int(s.get("weight", 1)) for s in _STEPS}


class WebUser(HttpUser):
    wait_time = wait_time_from(CONFIG.get("wait_time"))
    tasks = _TASKS

    def on_start(self):
        self.creds = POOL.next()
        self.ctx = {**CONFIG.get("vars", {}), **self.creds}
        apply_auth(self, CONFIG.get("auth"), self.creds, self.ctx)
        for step in CONFIG.get("on_start") or []:
            run_request(self, step, self.ctx)


if CONFIG.get("stages"):
    StagesShape = build_stages_shape(CONFIG["stages"])
