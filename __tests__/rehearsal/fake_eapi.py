"""Loopback-only EAPI contract double. Never forwards a request upstream."""

from collections import Counter
from fake_catalogue import (
    ALL,
    HISTORY,
    E_HISTORY,
    E_HISTORY_PAGING,
    E_MODES,
    LIBGEN,
    E2_LIBGEN,
    select,
    payload,
    libgen_html,
)
from fake_annas import EMPTY_SEARCH
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import ssl
import socketserver
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

STARTED = time.monotonic()


def startup_stage(phase):
    if __name__ == "__main__":
        print(
            json.dumps(
                {
                    "fakeStartup": phase,
                    "python": sys.version.split()[0],
                    "elapsedMs": round((time.monotonic() - STARTED) * 1000, 3),
                }
            ),
            file=sys.stderr,
            flush=True,
        )


USER_ID = "rehearsal-cookie-id"
USER_KEY = "rehearsal-cookie-key"
MODES = {
    "e_person",
    "e_transient",
    "e_pacing",
    "e_runtime",
    "e_ceiling",
    "e_malformed",
    "healthy",
    "e_history",
    "e_outcome_unknown",
    "drop_transfer",
    "empty",
    "quota_exhausted",
    "login400",
    "unauthorized",
    "profile401",
    "operation401",
    "success0",
    "timeout",
    "server503",
    "wall",
    "slow",
    "ambiguous400",
}
MODES |= E_MODES


class Fake(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind calls getfqdn even for a numeric loopback bind.
        # This fake has no need for DNS; resolver availability must not gate it.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def __init__(self, cert, key):
        startup_stage("binding")
        super().__init__(("127.0.0.1", 0), Handler)
        startup_stage("bound")
        self.lock = threading.Lock()
        self.calls = Counter()
        self.mode = "healthy"
        self.epoch = 1
        self.successes = 0
        self.inflight = 0
        self.max_inflight = 0
        self.drop_cookies = False
        self.remaining = 7
        self.quota_spent = 0
        self.events = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        startup_stage("loading-certificate")
        context.load_cert_chain(cert, key)
        self.socket = context.wrap_socket(self.socket, server_side=True)
        startup_stage("tls-ready")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # Request bodies and auth headers must not reach logs.

    def reply(self, payload, status=200, content_type="application/json", headers=None):
        data = (
            json.dumps(payload).encode()
            if content_type == "application/json"
            else payload
        )
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass

    def do_POST(self):
        self.handle_request()

    def do_GET(self):
        self.handle_request()

    def handle_request(self):
        with self.server.lock:
            self.server.inflight += 1
            self.server.max_inflight = max(
                self.server.max_inflight, self.server.inflight
            )
        try:
            self.dispatch_request()
        finally:
            with self.server.lock:
                self.server.inflight -= 1

    def dispatch_request(self):
        route = urlsplit(self.path).path
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if route == "/__mode":
            value = json.loads(body)
            if value.get("mode", "healthy") not in MODES:
                return self.reply({"error": "unknown mode"}, 400)
            with self.server.lock:
                self.server.mode = value.get("mode", "healthy")
                self.server.drop_cookies = value.get("drop_cookies", False)
                if value.get("reject_epoch"):
                    self.server.epoch += 1
                if value.get("reset"):
                    self.server.calls.clear()
                    self.server.epoch = 1
                    self.server.successes = 0
                    self.server.max_inflight = 0
                    self.server.quota_spent = 0
                    self.server.events.clear()
                if "remaining" in value:
                    self.server.remaining = int(value["remaining"])
            return self.reply({"ok": True})
        if route == "/__stats":
            with self.server.lock:
                calls = dict(self.server.calls)
            return self.reply(
                {
                    "logins": calls.get("/eapi/user/login", 0),
                    "loginSuccesses": self.server.successes,
                    "epoch": self.server.epoch,
                    "maxInflight": self.server.max_inflight,
                    "calls": calls,
                    "quotaSpent": self.server.quota_spent,
                    "remaining": self.server.remaining,
                    "events": list(self.server.events),
                }
            )
        with self.server.lock:
            self.server.calls[route] += 1
            mode = self.server.mode
            drop_cookies = self.server.drop_cookies
        params = parse_qs(urlsplit(self.path).query)
        fields = parse_qs(body.decode()) if body else params
        with self.server.lock:
            # Only synthetic operation parameters; never login secrets/cookies.
            if route != "/eapi/user/login":
                self.server.events.append({"route": route, "arguments": fields})
        if route == "/annas/search":
            if mode == "e_annas_rows":
                return self.reply(
                    b'<html><title>Offline Anna search</title><div><a href="/md5/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">Persuasion</a></div></html>',
                    content_type="text/html",
                )
            if mode == "e_person":
                return self.reply(
                    b"<html><title>DDoS-Guard</title><h1>Checking your browser</h1></html>",
                    403,
                    "text/html",
                    {"Server": "ddos-guard"},
                )
            if mode in {"e_pacing", "e_runtime", "e_ceiling"}:
                return self.reply(
                    {
                        "reason": mode,
                        "retry_after_s": 2 if mode == "e_pacing" else 3600,
                    },
                    503 if mode == "e_runtime" else 429,
                    headers={"Retry-After": "2" if mode == "e_pacing" else "3600"},
                )
            if mode == "e_transient":
                if self.server.calls[route] == 1:
                    return self.reply(
                        b"",
                        content_type="text/html",
                        headers={"X-Eval-Outcome": "read-timeout"},
                    )
                return self.reply(
                    b'<html><title>Offline Anna search</title><div><a href="/md5/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee">E Transient Recovery</a></div></html>',
                    content_type="text/html",
                )
            return self.reply(EMPTY_SEARCH, content_type="text/html")
        if route == "/libgen/index.php":
            query = params.get("req", params.get("q", [""]))[0]
            return self.reply(
                libgen_html(select(query, "libgen", {"limit": ["50"]}, mode)),
                content_type="text/html",
            )
        if route in {"/libgen/ads.php", "/libgen/get.php"}:
            md5 = params.get("md5", [""])[0]
            row = next((b for b in LIBGEN + E2_LIBGEN if b["md5"] == md5), None)
            if not row:
                return self.reply({"error": "unknown md5"}, 404)
            if route.endswith("ads.php"):
                return self.reply(
                    f'<html><a href="get.php?md5={md5}&amp;key=fixture">GET</a></html>'.encode(),
                    content_type="text/html",
                )
            if mode == "drop_transfer":
                return self.reply(b"", 503, "application/octet-stream")
            if mode == "e_outcome_unknown":
                return self.reply(
                    b"",
                    content_type="application/octet-stream",
                    headers={"X-Eval-Outcome": "issued-unknown"},
                )
            return self.reply(
                payload(row),
                content_type="application/octet-stream",
                headers={
                    "Content-Disposition": f'attachment; filename="{row["id"]}.{row["extension"]}"'
                },
            )
        login = route == "/eapi/user/login"
        profile = route == "/eapi/user/profile"
        if (
            mode == "unauthorized"
            or (mode == "profile401" and profile)
            or (mode == "operation401" and not login and not profile)
        ):
            return self.reply({"success": 0, "error": "authentication rejected"}, 401)
        if mode == "login400" and login:
            return self.reply({"success": 0, "error": "login rejected"}, 400)
        if mode == "ambiguous400" and not login:
            return self.reply(
                {"success": 0, "error": "ambiguous operation failure"}, 400
            )
        if mode == "server503" and not login:
            return self.reply({"error": "fixture service unavailable"}, 503)
        if mode == "wall" and not login:
            return self.reply(b"<html>DiamWall block page</html>", 403, "text/html")
        if mode == "success0" and not login:
            return self.reply({"success": 0, "user": {"id": "populated-but-rejected"}})
        if mode == "timeout" and not login:
            time.sleep(35)  # Longer than httpx's 30s; MCP bridge budget is 15s.
        if mode == "slow":
            time.sleep(0.4)
        if login:
            fields = parse_qs(body.decode())
            if fields.get("email") != [
                "rehearsal-account@example.invalid"
            ] or fields.get("password") != ["rehearsal-password-canary"]:
                return self.reply({"success": 0}, 400)
            with self.server.lock:
                self.server.successes += 1
                cookie_key = f"{USER_KEY}-epoch-{self.server.epoch}"
            user = {} if drop_cookies else {"id": USER_ID, "remix_userkey": cookie_key}
            return self.reply(
                {"success": 1, "user": user},
                headers={"Set-Cookie": f"remix_userid={USER_ID}; Secure; HttpOnly"},
            )
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        if any(
            cookies.get(k) is None or cookies[k].value != v
            for k, v in {
                "remix_userid": USER_ID,
                "remix_userkey": f"{USER_KEY}-epoch-{self.server.epoch}",
            }.items()
        ):
            return self.reply({"success": 0, "error": "missing session"}, 401)
        if profile:
            return self.reply(
                {
                    "success": 1,
                    "user": {
                        "id": USER_ID,
                        "downloads_limit": 10,
                        "downloads_today": 10 - self.server.remaining,
                        "isPremium": 0,
                    },
                }
            )
        if route == "/eapi/info/domains":
            return self.reply({"success": 1, "domains": []})
        if route == "/eapi/book/search":
            if mode == "e_malformed":
                return self.reply({"success": 1, "unexpected": "missing books list"})
            query = fields.get("message", [""])[0]
            if query.startswith("slow-"):
                time.sleep(0.6)
            return self.reply(
                {"success": 1, "books": select(query, fields=fields, mode=mode)}
            )
        if route in {"/eapi/user/book/downloaded", "/eapi/book/recently"}:
            limit = int(fields.get("limit", ["10"])[0])
            page = int(fields.get("page", ["1"])[0])
            return self.reply(
                {
                    "success": 1,
                    "books": (
                        ([E_HISTORY[0]] + E_HISTORY_PAGING + [E_HISTORY[1]])
                        if mode == "e_history"
                        else ([E_HISTORY[1], E_HISTORY[0]] + E_HISTORY_PAGING)
                        if mode == "e_history_first"
                        else HISTORY
                    )[(page - 1) * limit : page * limit],
                }
            )
        match = re.fullmatch(r"/eapi/book/(\d+)/(hash\d+)(/file)?", route)
        if match:
            row = ALL.get(match[1])
            if not row or row["hash"] != match[2]:
                return self.reply({"success": 0}, 404)
            if match[3]:
                with self.server.lock:
                    if mode == "quota_exhausted" or self.server.remaining <= 0:
                        return self.reply(
                            {"success": 0, "error": "quota exhausted"}, 429
                        )
                    self.server.quota_spent += 1
                    self.server.remaining -= 1
                return self.reply(
                    {
                        "success": 1,
                        "file": {
                            "downloadLink": f"/files/{row['id']}.{row['extension']}"
                        },
                    }
                )
            return self.reply({"success": 1, "book": row})
        match = re.fullmatch(r"/files/(\d+)\.\w+", route)
        if match and match[1] in ALL:
            row = ALL[match[1]]
            if mode == "drop_transfer":
                return self.reply(b"", 503, "application/octet-stream")
            if mode == "e_outcome_unknown":
                return self.reply(
                    b"",
                    content_type="application/octet-stream",
                    headers={"X-Eval-Outcome": "issued-unknown"},
                )
            return self.reply(
                payload(row),
                content_type="application/octet-stream",
                headers={
                    "Content-Disposition": f'attachment; filename="{row["id"]}.{row["extension"]}"'
                },
            )
        self.reply({"error": "unsupported fixture endpoint"}, 404)


if __name__ == "__main__":
    startup_stage("imports-ready")
    server = Fake(*sys.argv[1:])
    print(server.server_port, flush=True)

    def stop_at_eof():
        sys.stdin.buffer.read()
        server.shutdown()

    threading.Thread(target=stop_at_eof, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        server.server_close()
