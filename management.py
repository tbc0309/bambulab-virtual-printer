"""Management HTTP routes, same-origin guards and diagnostic downloads."""

import ipaddress
import json
import socket
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from model import ROOT, CATALOG, integer, merge
from mqtt import LOCK


def handler(lab):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def allowed_host(self):
            host = self.headers.get("Host", "").split(":")[0]
            try:
                ipaddress.IPv4Address(host)
                return True
            except ValueError:
                return host == "localhost"

        def log_message(self, *args):
            # Paths only: headers, bodies and query values never enter access logs.
            status = getattr(self, "response_status", 0)
            lab.logs.record(
                "http",
                "request",
                {
                    "method": getattr(self, "command", ""),
                    "path": urlsplit(getattr(self, "path", "")).path,
                    "status": status,
                    "elapsedMs": round(
                        (time.monotonic() - self.request_started) * 1000, 2
                    ),
                },
                level="warning" if status >= 400 else "info",
            )

        def handle_one_request(self):
            self.request_started = time.monotonic()
            return super().handle_one_request()

        def send_response(self, code, message=None):
            self.response_status = code
            super().send_response(code, message)

        def reply(self, obj, status=200):
            b = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if not self.allowed_host():
                return self.reply({"error": "host_not_allowed"}, 403)
            path = urlsplit(self.path).path
            if path == "/api/logs":
                return self.reply(lab.logs.catalogue())
            if path in ("/api/logs/tail", "/api/logs/file"):
                name = parse_qs(urlsplit(self.path).query).get("name", [""])[0]
                try:
                    data = lab.logs.read(name, tail=path.endswith("/tail"))
                except (OSError, ValueError):
                    return self.reply(
                        {"error": "Log file unavailable; refresh the list"}, 404
                    )
                if isinstance(data, list):
                    return self.reply({"name": name, "lines": data})
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{name}"'
                )
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(data)
            if path == "/api/services":
                return self.reply(lab.services.status())
            with LOCK:
                if path == "/api/state":
                    return self.reply(lab.summary())
                if path == "/api/camera":
                    return self.reply(lab.camera.status())
                if path == "/api/catalog":
                    return self.reply(CATALOG)
                if path == "/api/discovery":
                    return self.reply(
                        {
                            "devices": [
                                {
                                    "serialNumber": p.serial,
                                    "name": p.name,
                                    "printerModel": p.model,
                                    "online": True,
                                }
                                for p in lab.printers.values()
                            ]
                        }
                    )
            if path.startswith("/assets/"):
                file = ROOT / "web" / path.lstrip("/")
            else:
                file = (
                    ROOT / "web" / ("index.html" if path == "/" else path.lstrip("/"))
                )
            resolved = file.resolve()
            if (
                not resolved.is_relative_to((ROOT / "web").resolve())
                or not resolved.is_file()
            ):
                return self.reply({"error": "not_found"}, 404)
            content = file.read_bytes()
            mime = {
                ".html": "text/html; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".css": "text/css",
                ".png": "image/png",
                ".svg": "image/svg+xml",
            }
            self.send_response(200)
            self.send_header(
                "Content-Type", mime.get(file.suffix, "application/octet-stream")
            )
            self.send_header("Content-Length", str(len(content)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            # No password, but reject cross-site writes and DNS rebinding hosts.
            host = self.headers.get("Host", "")
            if not self.allowed_host():
                return self.reply({"error": "host_not_allowed"}, 403)
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + host:
                return self.reply({"error": "cross_origin"}, 403)
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self.reply({"error": "cross_site"}, 403)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    raise ValueError("Body must be 1..65536 bytes")
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("JSON required")
                d = json.loads(
                    self.rfile.read(size),
                    parse_constant=lambda x: (_ for _ in ()).throw(
                        ValueError("Non-finite JSON")
                    ),
                )
                if not isinstance(d, dict):
                    raise ValueError("Object required")
                # Listener shutdown must not hold the printer state lock.
                if self.path == "/api/services":
                    return self.reply(lab.services.change(d))
                if self.path == "/api/logs/client":
                    if not isinstance(d.get("message"), str) or not isinstance(
                        d.get("source", ""), str
                    ):
                        raise ValueError("Expected browser error message and source")
                    details = {
                        "type": "JavaScript",
                        "message": d["message"][:2048],
                        "source": urlsplit(d.get("source", "")).path[:256],
                        "line": integer(d.get("line", 0), 0, 1000000),
                    }
                    lab.logs.record(
                        "runtime",
                        "browser-error",
                        details,
                        level="error",
                        sample=("browser:" + details["message"][:120], 10),
                    )
                    return self.reply({"ok": True})
                extra = {}
                with LOCK:
                    if self.path == "/api/camera":
                        return self.reply(lab.camera.configure(d))
                    if self.path == "/api/printer":
                        lab.add(d)
                    elif self.path == "/api/delete":
                        del lab.printers[d["serial"]]
                        lab.logs.record(
                            "runtime", "printer-deleted", {"serial": d["serial"]}
                        )
                        lab.save()
                        for client in tuple(lab.clients):
                            client.subscriptions.discard(d["serial"])
                    else:
                        p = lab.printers[d["serial"]]
                        if self.path == "/api/action":
                            action = d["action"]
                            if action == "publish":
                                lab.publish(p.serial, p.status())
                            elif action == "auto_report":
                                if not isinstance(d.get("payload"), bool):
                                    raise ValueError("auto_report must be boolean")
                                p.auto_report = d["payload"]
                                lab.logs.record(
                                    "runtime",
                                    "auto-report",
                                    {"serial": p.serial, "enabled": p.auto_report},
                                )
                            elif action == "reset":
                                lab.logs.record(
                                    "runtime", "printer-reset", {"serial": p.serial}
                                )
                                p.p = p.initial()
                                p.dry_seconds.clear()
                            elif action == "command":
                                command_reply = p.command(d["payload"], "WEB")
                                lab.publish(p.serial, command_reply)
                                extra["commandReply"] = command_reply
                            elif action == "patch":
                                if not isinstance(d["payload"], dict):
                                    raise ValueError("Patch must be object")
                                merge(p.p, d["payload"].get("print", d["payload"]))
                                p.event(
                                    "PATCH", d["payload"], "Merged into running state"
                                )
                            elif action == "delta":
                                payload = d["payload"]
                                if not isinstance(payload, dict):
                                    raise ValueError("Delta must be object")
                                lab.publish(p.serial, payload)
                                p.event(
                                    "TX DELTA",
                                    payload,
                                    "One-shot; does not change stored state",
                                )
                            elif action == "fault":
                                f = d["payload"]
                                if not isinstance(f, dict) or set(f) - {
                                    "error",
                                    "noack",
                                    "drop",
                                    "delay",
                                    "noVersion",
                                }:
                                    raise ValueError("Unknown fault option")
                                if any(
                                    not isinstance(f.get(key, False), bool)
                                    for key in ("noack", "drop", "noVersion")
                                ):
                                    raise ValueError("Fault switches must be boolean")
                                p.fault = {
                                    "error": integer(f.get("error", 0), 0, 4294967295),
                                    "noack": bool(f.get("noack", False)),
                                    "drop": bool(f.get("drop", False)),
                                    "delay": integer(f.get("delay", 0), 0, 20),
                                    "noVersion": bool(f.get("noVersion", False)),
                                }
                                lab.logs.record(
                                    "runtime",
                                    "fault-configured",
                                    {"serial": p.serial, "options": p.fault},
                                )
                            else:
                                raise ValueError("Unknown action")
                            if action not in ("delta", "publish", "auto_report"):
                                lab.publish(p.serial, p.status())
                        else:
                            return self.reply({"error": "not_found"}, 404)
                    self.reply({"ok": True, **extra})
            except (ValueError, KeyError, TypeError, IndexError, RecursionError) as exc:
                lab.logs.record(
                    "runtime",
                    "request-rejected",
                    {"path": urlsplit(self.path).path, "reason": str(exc)},
                    level="warning",
                )
                self.reply({"error": str(exc)}, 400)

    return Handler


class ManagementServer(ThreadingHTTPServer):
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def __init__(self, address, lab):
        self.lab = lab
        super().__init__(address, handler(lab))

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        self.lab.report_error(
            "Management request failed", {"traceback": traceback.format_exc()}
        )
