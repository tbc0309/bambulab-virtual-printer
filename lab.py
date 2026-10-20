"""BambuLab Virtual Printer application and same-origin management API."""

from __future__ import annotations
import argparse
import collections
import hashlib
import json
import pathlib
import threading
import time

from camera import Camera
from diagnostics import LogStore
from model import ROOT, Printer
from mqtt import LOCK, mqtt_string
from services import Services
from management import ManagementServer

VERSION = (ROOT / "VERSION").read_text("utf-8").strip()


class Lab:
    def __init__(self, data_dir, logs=None):
        self.data_dir = pathlib.Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs = logs or LogStore(self.data_dir)
        self.printers = {}
        self.clients = set()
        self.started = time.time()
        self.build = hashlib.sha256(
            b"".join(
                (ROOT / name).read_bytes()
                for name in (
                    "VERSION",
                    "lab.py",
                    "model.py",
                    "mqtt.py",
                    "services.py",
                    "management.py",
                    "diagnostics.py",
                    "camera.py",
                    "bootstrap.py",
                    "catalog.json",
                    "web/index.html",
                    "web/app.js",
                    "web/activity.js",
                    "web/services.js",
                    "web/audit.html",
                    "web/audit.js",
                    "web/logs.js",
                    "web/ui.js",
                    "web/i18n.js",
                    "web/locales.js",
                    "web/style.css",
                    "assets/camera/test.h264",
                    "assets/camera/test.jpg",
                )
            )
        ).hexdigest()[:12]
        self.errors = collections.deque(maxlen=40)
        self.port = 8883
        self.camera = Camera(self.data_dir, self.logs)
        self.services = Services(self)
        saved = self.data_dir / "combinations.json"
        specs = (
            json.loads(saved.read_text("utf-8"))
            if saved.exists()
            else [
                {
                    "serial": "XTSIMP1S",
                    "name": "P1S baseline",
                    "model": "C12",
                    "units": [{"id": 0, "kind": "ams"}],
                    "external": 0,
                },
                {
                    "serial": "XTSIMH2C",
                    "name": "H2C mixed",
                    "model": "O1C",
                    "units": [
                        {"id": 0, "kind": "pro"},
                        {"id": 128, "kind": "ht", "bound": 1},
                    ],
                    "external": 2,
                },
            ]
        )
        for spec in specs:
            self.add(spec, save=False)
        self.logs.record(
            "runtime",
            "application-ready",
            {"build": self.build, "printers": len(self.printers)},
        )

    def report_error(self, summary, details=None):
        self.errors.append({"time": time.time(), "error": summary})
        self.logs.record(
            "runtime", "error", {"summary": summary, "details": details}, level="error"
        )

    def add(self, spec, save=True):
        p = Printer(spec)
        p.log = self.logs
        if p.serial not in self.printers and len(self.printers) >= 100:
            raise ValueError("Maximum 100 virtual printers")
        self.printers[p.serial] = p
        if save:
            self.logs.record(
                "runtime", "printer-saved", {"serial": p.serial, "model": p.model}
            )
            self.save()
            self.publish(
                p.serial,
                {
                    "info": {
                        "command": "get_version",
                        "module": [
                            {
                                "name": "ota",
                                "product_name": p.model,
                                "sw_ver": "99.00.00.00",
                            }
                        ],
                    }
                },
            )
            self.publish(p.serial, p.status())
        return p

    def save(self):
        specs = [
            {
                "serial": p.serial,
                "name": p.name,
                "model": p.model,
                "units": p.units,
                "external": p.external,
            }
            for p in self.printers.values()
        ]
        temp = self.data_dir / "combinations.tmp"
        temp.write_text(json.dumps(specs, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(self.data_dir / "combinations.json")

    def publish(self, serial, payload):
        p = self.printers[serial]
        if p.fault["drop"]:
            return
        recipients = 0
        body = None
        for c in tuple(self.clients):
            if serial in c.subscriptions:
                try:
                    if body is None:
                        body = (
                            mqtt_string("device/" + serial + "/report")
                            + json.dumps(payload, separators=(",", ":")).encode()
                        )
                    c.enqueue_report(body)
                    p.tx += 1
                    recipients += 1
                except OSError:
                    c.close()
        if recipients:
            status = (
                isinstance(payload.get("print"), dict)
                and "command" not in payload["print"]
            )
            self.logs.record(
                "mqtt",
                "report",
                {"serial": serial, "recipients": recipients, "payload": payload},
                sample=(serial, 2) if status else None,
            )

    def summary(self):
        return {
            "version": VERSION,
            "build": self.build,
            "uptime": int(time.time() - self.started),
            "mqttPort": self.port,
            "clients": len(self.clients),
            "errors": list(self.errors),
            "printers": [
                {
                    "serial": p.serial,
                    "name": p.name,
                    "model": p.model,
                    "cap": p.cap,
                    "units": p.units,
                    "external": p.external,
                    "warnings": p.warnings,
                    "autoReport": p.auto_report,
                    "state": p.p,
                    "mqtt": {
                        "connected": sum(
                            p.serial in c.subscriptions for c in self.clients
                        ),
                        "rx": p.rx,
                        "tx": p.tx,
                        "rxWeb": p.rx_web,
                        "rxMqtt": p.rx - p.rx_web,
                        "lastRx": p.last_rx,
                        "failures": p.failures,
                        "commands": dict(p.commands),
                    },
                    "fault": p.fault,
                    "events": list(p.events)[-40:],
                    "activity": list(p.activity),
                }
                for p in self.printers.values()
            ],
        }


def main(logs=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="Use 0.0.0.0 for physical xTouch access on a trusted LAN",
    )
    parser.add_argument("--http-port", type=int, default=8894)
    parser.add_argument("--mqtt-port", type=int, default=8883)
    parser.add_argument(
        "--camera-port",
        type=int,
        default=0,
        help="Enable RTSPS, typically 322; 0 disables",
    )
    parser.add_argument(
        "--jpeg-port",
        type=int,
        default=0,
        help="Enable Bambu JPEG/TLS, typically 6000; 0 disables",
    )
    parser.add_argument("--data-dir", type=pathlib.Path, default=ROOT / "data")
    args = parser.parse_args()
    lab = Lab(args.data_dir, logs=logs)
    lab.port = args.mqtt_port
    lab.services.bind = args.bind
    lab.services.ports["mqtt"] = args.mqtt_port
    for name, port in [
        ("mqtt", args.mqtt_port),
        ("rtsps", args.camera_port),
        ("jpeg", args.jpeg_port),
    ]:
        if port:
            try:
                lab.services.change({"service": name, "enabled": True, "port": port})
            except ValueError as exc:
                print(exc, flush=True)  # Keep the repair/control page available.

    stopped = threading.Event()

    def periodic():
        last = time.monotonic()
        while not stopped.wait(2):
            now = time.monotonic()
            with LOCK:
                for p in lab.printers.values():
                    try:
                        p.tick(now - last)
                        if p.auto_report:
                            lab.publish(p.serial, p.status())
                    except (ValueError, TypeError, KeyError, OverflowError):
                        lab.report_error(
                            "Invalid injected state: reset this combination",
                            {"serial": p.serial},
                        )
                last = now

    ticker = threading.Thread(target=periodic, daemon=True, name="virtual-printer-tick")
    ticker.start()
    print(
        f"BambuLab Virtual Printer: http://{args.bind}:{args.http_port} | TLS MQTT :{args.mqtt_port}",
        flush=True,
    )
    server = None
    try:
        server = ManagementServer((args.bind, args.http_port), lab)
        server.serve_forever()
    finally:
        stopped.set()
        if server:
            server.server_close()
        lab.services.close()
        ticker.join(timeout=3)
        lab.logs.record("runtime", "application-stopped")
        lab.logs.catalogue()


if __name__ == "__main__":
    main()
