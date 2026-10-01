"""A tiny stand-in for CommCare HQ, so web_load and mobile_load can be exercised end to end
without sending any traffic to a real environment."""
from __future__ import annotations

import base64
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

RESTORE_XML = """<OpenRosaResponse xmlns="http://openrosa.org/http/response" items="3">
<message nature="ota_restore_success">Successfully restored account {user}!</message>
<Sync xmlns="http://commcarehq.org/sync"><restore_id>sync-{n}</restore_id></Sync>
<Registration xmlns="http://openrosa.org/user/registration"><username>{user}</username>
<password>x</password><uuid>uid-{user}</uuid><date>2026-01-01</date></Registration>
<case case_id="case-a-{user}" date_modified="2026-01-01" user_id="uid-{user}" xmlns="http://commcarehq.org/case/transaction/v2"/>
<case case_id="case-b-{user}" date_modified="2026-01-01" user_id="uid-{user}" xmlns="http://commcarehq.org/case/transaction/v2"/>
</OpenRosaResponse>"""


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.counts: dict[str, int] = {}
        self.async_devices: set[str] = set()
        self.submissions: list[str] = []
        self.users: set[str] = set()

    def hit(self, key):
        with self.lock:
            self.counts[key] = self.counts.get(key, 0) + 1
            return self.counts[key]


class Handler(BaseHTTPRequestHandler):
    state: State

    def log_message(self, *a):
        pass

    def _send(self, code, body="", ctype="text/html", headers=None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _basic_user(self):
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return None
        return base64.b64decode(h[6:]).decode().split(":", 1)[0]

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        p = url.path
        if re.fullmatch(r"/a/[^/]+/login/", p):
            self.state.hit("login_page")
            return self._send(200, "<form>Sign In</form>", headers={"Set-Cookie": "csrftoken=tok123; Path=/"})
        if p.endswith("/dashboard/project/"):
            time.sleep(0.01)
            return self._send(200, "<h1>Dashboard</h1><span id='uid'>abc-42</span>")
        if p == "/slow":
            time.sleep(0.3)
            return self._send(200, "slow ok")
        if "/phone/restore/" in p:
            user = self._basic_user()
            if not user:
                return self._send(401, "auth required")
            self.state.users.add(user)
            device = q.get("device_id", [""])[0]
            if "since" not in q and device not in self.state.async_devices:
                self.state.async_devices.add(device)
                self.state.hit("restore_202")
                return self._send(202, '<OpenRosaResponse><Sync><progress done="1" total="5" retry-after="1"/>'
                                       '</Sync></OpenRosaResponse>', "text/xml", {"Retry-After": "1"})
            n = self.state.hit("restore_incremental" if "since" in q else "restore_initial")
            time.sleep(0.02)
            return self._send(200, RESTORE_XML.format(user=user, n=n), "text/xml")
        if "/phone/heartbeat/" in p:
            self.state.hit("heartbeat")
            return self._send(200, '{"app_id": "x"}', "application/json")
        return self._send(404, "not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode(errors="replace")
        p = urlparse(self.path).path
        if re.fullmatch(r"/a/[^/]+/login/", p):
            ok = "auth-password=good" in body and self.headers.get("X-CSRFToken") == "tok123"
            self.state.hit("login_ok" if ok else "login_bad")
            return self._send(200, "Welcome" if ok else "<form>Sign In</form>")
        if "/receiver/" in p:
            if not self._basic_user():
                return self._send(401, "auth required")
            if "{{" in body or "<n1:instanceID>" not in body:
                return self._send(400, "bad form")
            self.state.submissions.append(body)
            return self._send(201, "<OpenRosaResponse><message>Thanks</message></OpenRosaResponse>", "text/xml")
        return self._send(404, "not found")


def start() -> tuple[ThreadingHTTPServer, State, str]:
    state = State()
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state, f"http://127.0.0.1:{server.server_address[1]}"
