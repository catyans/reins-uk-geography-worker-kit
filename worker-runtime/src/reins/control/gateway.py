"""TLS gateway with a separate authoritative ledger and credentials per tenant.

Workers may run on different hosts. Each tenant still has one ledger authority;
this is not a replicated/high-availability database service.
"""

import hmac
import json
import secrets
import ssl
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .ledger import Ledger
from .server import ControlServer


class Gateway(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, manifest, certfile, keyfile, *, host="127.0.0.1", port=8843):
        path = Path(manifest).expanduser()
        if path.stat().st_mode & 0o077:
            raise ValueError("Tenant credential manifest must have permissions 0600")
        config = json.loads(path.read_text())
        tenants = config.get("tenants", [])
        if not tenants:
            raise ValueError("Configure at least one tenant")
        self.origin = config["origin"].rstrip("/")
        from urllib.parse import urlparse

        parsed = urlparse(self.origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Set the externally visible HTTPS origin")
        self.authority = parsed.netloc
        self.services = []
        self.routes = []
        ids = set()
        tokens = set()
        databases = set()
        # Validate the full manifest before opening any ledger or network listener.
        for t in tenants:
            if not isinstance(t.get("id"), str) or not t["id"] or t["id"] in ids:
                raise ValueError("Distinct tenant IDs required")
            ids.add(t["id"])
            db = str((path.parent / t["database"]).resolve())
            if db in databases:
                raise ValueError("Tenants must have separate databases")
            databases.add(db)
            for role in ("operator", "worker"):
                token = t.get(role + "_token")
                if not isinstance(token, str) or len(token) < 40 or token in tokens:
                    raise ValueError("Distinct strong tenant and role credentials required")
                tokens.add(token)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile, keyfile)
        try:
            for t in tenants:
                ledger = Ledger((path.parent / t["database"]).resolve())
                op, worker = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
                service = ControlServer(ledger, op, 0, worker_token=worker, local_ui=False)
                service.workspace_label = t["id"]
                self.services.append(service)
                threading.Thread(target=service.serve_forever, daemon=True).start()
                for role, internal in [("operator", op), ("worker", worker)]:
                    self.routes.append((t[role + "_token"].encode(), service, internal))
            super().__init__((host, port), GatewayHandler)
            self.socket = context.wrap_socket(self.socket, server_side=True)
        except BaseException:
            self.close_services()
            raise

    def close_services(self):
        for service in self.services:
            service.shutdown()
            service.server_close()
            service.ledger.close()
        self.services = []

    def server_close(self):
        super().server_close()
        self.close_services()


class GatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def error(self, code, message):
        content = json.dumps({"error": message}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        self.forward(False)

    def do_POST(self):
        self.forward(True)

    def forward(self, authenticated):
        if self.headers.get("Host") != self.server.authority:
            return self.error(403, "Unexpected host")
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            return self.error(403, "Unexpected origin")
        if not self.path.startswith("/") or self.path.startswith("//"):
            return self.error(400, "Invalid path")
        service = self.server.services[0]
        internal = None
        data = None
        if authenticated:
            auth = self.headers.get("Authorization", "").removeprefix("Bearer ").encode()
            matched = None
            for token, target, secret in self.server.routes:
                if hmac.compare_digest(auth, token):
                    matched = (target, secret)
            if matched is None:
                return self.error(403, "Workspace credential required")
            service, internal = matched
            if self.headers.get("Transfer-Encoding"):
                return self.error(400, "Chunked bodies not supported")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1048576:
                    raise ValueError()
                data = self.rfile.read(length)
                if len(data) != length:
                    raise ValueError()
            except (ValueError, TimeoutError):
                return self.error(400, "Invalid body size")
        headers = {"Content-Type": "application/json"}
        if internal:
            headers["Authorization"] = "Bearer " + internal
        req = urllib.request.Request(
            "http://127.0.0.1:" + str(service.server_port) + self.path, data=data, headers=headers
        )
        try:
            response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                req, timeout=15
            )
        except urllib.error.HTTPError as exc:
            response = exc
        except OSError:
            return self.error(
                503, "Workspace unavailable; inspect state before retrying a mutation"
            )
        with response:
            content = response.read()
            self.send_response(response.code)
            for name in (
                "Content-Type",
                "Cache-Control",
                "Content-Security-Policy",
                "X-Content-Type-Options",
            ):
                if response.headers.get(name):
                    self.send_header(name, response.headers[name])
            self.send_header("Strict-Transport-Security", "max-age=31536000")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
