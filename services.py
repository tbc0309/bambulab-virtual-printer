"""Independent listener lifecycle, runtime checks and service event summaries."""

import collections
import importlib.metadata
import platform
import socket
import ssl
import sys
import threading
import time

from bootstrap import dependencies_ready
from camera import start_services
from model import ROOT
from mqtt import LOCK, certificate, mqtt_server


class Services:
    """Runtime listeners controlled independently of the management server."""

    def __init__(self, lab):
        self.lab, self.bind = lab, "127.0.0.1"
        self.lock = threading.RLock()
        self.running = {}
        self.ports = {"mqtt": 8883, "rtsps": 322, "jpeg": 6000}
        self.events = collections.deque(maxlen=100)
        self.failures = {}

    def record(self, service, action, message):
        if action == "error":
            self.failures[service] = message
        elif action == "started":
            self.failures.pop(service, None)
        self.events.append(
            dict(time=time.time(), service=service, action=action, message=message)
        )
        self.lab.logs.record(
            "runtime",
            "service-" + action,
            {"service": service, "message": message},
            level="error" if action == "error" else "info",
        )

    def status(self):
        with self.lock:
            return {
                "bind": self.bind,
                "services": {
                    name: {
                        "enabled": name in self.running,
                        "port": self.ports[name],
                        "state": (
                            "running"
                            if name in self.running
                            else "error" if name in self.failures else "stopped"
                        ),
                        "error": self.failures.get(name, ""),
                    }
                    for name in self.ports
                },
                "events": list(self.events),
                "logStorage": {
                    "lastError": self.lab.logs.last_error,
                    "failedRecords": self.lab.logs.failed_records,
                },
                "warnings": list(self.failures.values())
                + [row["error"] for row in list(self.lab.errors)[-3:]]
                + list(self.lab.logs.alerts)[-3:],
                "environment": {
                    "python": platform.python_version(),
                    "pythonSupported": sys.version_info >= (3, 10),
                    "tls": ssl.OPENSSL_VERSION,
                    "cryptography": importlib.metadata.version("cryptography"),
                    "dependenciesReady": dependencies_ready(),
                    "mediaReady": all(
                        (ROOT / "assets/camera" / name).is_file()
                        for name in ("test.h264", "test.jpg")
                    ),
                },
            }

    def change(self, request):
        if (
            set(request) != {"service", "enabled", "port"}
            or request["service"] not in self.ports
        ):
            raise ValueError("Expected service, enabled and port")
        name, enabled, port = request["service"], request["enabled"], request["port"]
        if (
            not isinstance(enabled, bool)
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise ValueError("enabled must be boolean; port must be 1..65535")
        with self.lock:
            if name in self.running:
                if enabled and port == self.ports[name]:
                    return self.status()
                if enabled:
                    raise ValueError("Stop the service before changing its port")
                server = self.running.pop(name)
                if name == "mqtt":
                    try:
                        server.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    server.close()
                    with LOCK:
                        clients = tuple(self.lab.clients)
                    for client in clients:
                        client.close()
                else:
                    server.shutdown()
                    server.server_close()
                self.record(
                    name, "stopped", "Listener stopped and clients disconnected"
                )
            if enabled:
                try:
                    if any(
                        other != name and self.ports[other] == port
                        for other in self.running
                    ):
                        raise ValueError(
                            "Port already used by another simulator service"
                        )
                    if name == "mqtt":
                        server = mqtt_server(self.lab, self.bind, port)
                        self.lab.port = port
                    else:
                        server = start_services(
                            self.lab.camera,
                            self.bind,
                            port if name == "rtsps" else 0,
                            port if name == "jpeg" else 0,
                            *certificate(self.lab.data_dir),
                        )[0]
                    self.running[name] = server
                    self.ports[name] = port
                    self.record(name, "started", f"Listening on {self.bind}:{port}")
                except (OSError, ValueError, ImportError) as exc:
                    self.record(name, "error", str(exc))
                    raise ValueError(
                        f"{name} could not start: {exc}. Check port usage and permissions."
                    ) from exc
            return self.status()

    def close(self):
        for name in tuple(self.running):
            self.change({"service": name, "enabled": False, "port": self.ports[name]})
