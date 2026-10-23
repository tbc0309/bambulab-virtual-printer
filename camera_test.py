"""Portable camera/API regression tests; synthetic fixtures, no hardware needed."""

import base64
import hashlib
import json
import pathlib
import re
import socket
import ssl
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock
from http.server import ThreadingHTTPServer

import camera
import lab
import management
import mqtt
import bootstrap


class Client:
    def __init__(self, port, auth="digest", password="12345678"):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.socket = ctx.wrap_socket(
            socket.create_connection(("127.0.0.1", port)), server_hostname="localhost"
        )
        self.socket.settimeout(3)
        self.url = f"rtsps://127.0.0.1:{port}/streaming/live/1"
        self.auth, self.password, self.nonce, self.sequence = auth, password, "", 0

    def exact(self, count):
        data = b""
        while len(data) < count:
            chunk = self.socket.recv(count - len(data))
            if not chunk:
                raise EOFError()
            data += chunk
        return data

    def message(self):
        first = self.exact(1)
        if first == b"$":
            head = self.exact(3)
            return "rtp", self.exact(struct.unpack("!H", head[1:])[0])
        raw = first
        while not raw.endswith(b"\r\n\r\n"):
            raw += self.exact(1)
        length = int(re.search(rb"Content-Length: (\d+)", raw).group(1))
        return "rtsp", (raw, self.exact(length))

    def send_request(self, method, extra="", authenticated=True):
        self.sequence += 1
        header = ""
        if authenticated:
            if self.auth == "basic":
                header = (
                    "Basic "
                    + base64.b64encode(("bblp:" + self.password).encode()).decode()
                )
            elif self.nonce:
                md5 = lambda value: hashlib.md5(value.encode()).hexdigest()
                response = md5(
                    ":".join(
                        [
                            md5("bblp:fixture:" + self.password),
                            self.nonce,
                            "00000001",
                            "fixture-client",
                            "auth",
                            md5(method + ":" + self.url),
                        ]
                    )
                )
                header = f'Digest username="bblp", realm="fixture", nonce="{self.nonce}", uri="{self.url}", qop=auth, nc=00000001, cnonce="fixture-client", response="{response}"'
        self.socket.sendall(
            f'{method} {self.url} RTSP/1.0\r\nCSeq: {self.sequence}\r\n{("Authorization: " + header + chr(13) + chr(10)) if header else ""}{extra}\r\n'.encode()
        )

    def request(self, method, extra="", authenticated=True):
        self.send_request(method, extra, authenticated)
        while True:
            kind, value = self.message()
            if kind == "rtsp":
                return value

    def start(self):
        head, _ = self.request("DESCRIBE", authenticated=False)
        assert b"401 Unauthorized" in head
        if self.auth == "digest":
            self.nonce = re.search(rb'nonce="([^"]+)"', head).group(1).decode()
        head, body = self.request("DESCRIBE")
        assert b"200 OK" in head and b"sprop-parameter-sets=" in body
        head, _ = self.request(
            "SETUP", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n"
        )
        assert b"200 OK" in head
        head, _ = self.request("PLAY")
        assert b"200 OK" in head

    def frame(self):
        packets = []
        while True:
            kind, value = self.message()
            if kind == "rtp":
                packets.append(value)
                if value[1] & 128:
                    return packets

    def close(self):
        self.socket.close()


class CameraTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certs = tempfile.TemporaryDirectory()
        cert, key = mqtt.certificate(pathlib.Path(cls.certs.name))
        cls.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.context.load_cert_chain(cert, key)

    @classmethod
    def tearDownClass(cls):
        cls.certs.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.camera = camera.Camera(self.temp.name)
        self.servers, self.clients = [], []

    def tearDown(self):
        for client in self.clients:
            client.close()
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.temp.cleanup()

    def server(self, kind="rtsps", **options):
        self.camera.configure(options)
        server = camera.CameraServer(self.camera, "127.0.0.1", 0, self.context, kind)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return server.server_address[1]

    def client(self, port, auth="digest", password="12345678"):
        c = Client(port, auth, password)
        self.clients.append(c)
        return c

    def test_basic_and_digest_fu_a_and_sequence_wrap(self):
        for auth in ("basic", "digest"):
            with self.subTest(auth=auth):
                c = self.client(self.server(auth=auth), auth)
                c.start()
                packets = c.frame()
                self.assertTrue(any(p[12] & 31 == 28 for p in packets))
                sequence = [struct.unpack("!H", p[2:4])[0] for p in packets]
                self.assertIn(0, sequence)
                self.assertTrue(
                    all(b == (a + 1) & 65535 for a, b in zip(sequence, sequence[1:]))
                )
                self.assertEqual(len({p[4:8] for p in packets}), 1)
                parts = [p[12:] for p in packets if p[12] & 31 == 28]
                self.assertTrue(parts[0][1] & 128 and parts[-1][1] & 64)
                reconstructed = bytes(
                    [(parts[0][0] & 224) | (parts[0][1] & 31)]
                ) + b"".join(p[2:] for p in parts)
                source = camera.annex_b((camera.ASSETS / "test.h264").read_bytes())
                self.assertIn(reconstructed, source)
                c.close()

    def test_stap_and_tcp_fragmentation(self):
        c = self.client(self.server(fragmentBytes=7, aggregateParameterSets=True))
        c.start()
        packets = c.frame()
        stap = next(p[12:] for p in packets if p[12] & 31 == 24)
        position, kinds = 1, []
        while position < len(stap):
            length = struct.unpack("!H", stap[position : position + 2])[0]
            kinds.append(stap[position + 2] & 31)
            position += 2 + length
        self.assertEqual(kinds, [7, 8])
        self.assertEqual(position, len(stap))

    def test_fixed_timestamps_and_keepalive_with_rtcp(self):
        c = self.client(self.server(mode="fixed-timestamps"))
        c.start()
        self.assertEqual(c.frame()[0][4:8], c.frame()[0][4:8])
        c.socket.sendall(b"$\x01\x00\x04test")
        head, _ = c.request("GET_PARAMETER", "Session: synthetic\r\n")
        self.assertIn(b"200 OK", head)
        self.assertTrue(c.frame())
        head, _ = c.request("TEARDOWN", "Session: synthetic\r\n")
        self.assertIn(b"200 OK", head)
        with self.assertRaises(EOFError):
            c.message()

    def test_disconnect_and_reconnect(self):
        port = self.server(mode="drop", dropAfter=1)
        for _ in range(2):
            c = self.client(port)
            c.start()
            self.assertTrue(c.frame())
            with self.assertRaises(EOFError):
                c.message()
            c.close()
        self.assertEqual(self.camera.status()["counters"]["intentional-drop"], 2)

    def test_stall_keeps_control_channel_responsive(self):
        c = self.client(self.server(mode="stall"))
        c.start()
        c.socket.settimeout(0.15)
        with self.assertRaises(socket.timeout):
            c.message()
        c.socket.settimeout(3)
        head, _ = c.request("GET_PARAMETER")
        self.assertIn(b"200 OK", head)
        self.assertNotIn("frame", self.camera.status()["counters"])

    def test_long_delay_can_be_cancelled_without_lingering_clients(self):
        c = self.client(self.server(mode="delayed", delaySeconds=30))
        c.start()
        began = time.monotonic()
        self.camera.configure({"mode": "normal"})
        with self.assertRaises((EOFError, OSError)):
            c.message()
        while self.camera.status()["clients"] and time.monotonic() - began < 1:
            time.sleep(0.01)
        self.assertEqual(self.camera.status()["clients"], 0)
        self.assertLess(time.monotonic() - began, 1)

    def test_delayed_first_frame(self):
        c = self.client(self.server(mode="delayed", delaySeconds=1))
        c.start()
        began = time.monotonic()
        self.assertTrue(c.frame())
        self.assertGreaterEqual(time.monotonic() - began, 0.8)

    def test_wrong_code_and_unsupported_transport(self):
        c = self.client(self.server(auth="basic"), "basic", "bad-code")
        head, _ = c.request("DESCRIBE")
        self.assertIn(b"401 Unauthorized", head)
        c.password = "12345678"
        head, _ = c.request(
            "SETUP", "Transport: RTP/AVP;unicast;client_port=1234-1235\r\n"
        )
        self.assertIn(b"461 Unsupported Transport", head)
        port = self.server(auth="basic", mode="wrong-code")
        c = self.client(port, "basic")
        head, _ = c.request("DESCRIBE")
        self.assertIn(b"401 Unauthorized", head)

    def test_jpeg_auth_framing_fragmentation_and_oversize(self):
        port = self.server("jpeg", fragmentBytes=13, mode="drop", dropAfter=1)
        c = self.client(port)
        c.socket.sendall(
            struct.pack("<IIII32s32s", 0x40, 0x3000, 0, 0, b"bblp", b"12345678")
        )
        size, _, _, _ = struct.unpack("<IIII", c.exact(16))
        frame = c.exact(size)
        self.assertEqual(frame, (camera.ASSETS / "test.jpg").read_bytes())
        with self.assertRaises(EOFError):
            c.exact(1)
        port = self.server("jpeg", mode="oversize")
        c = self.client(port)
        c.socket.sendall(
            struct.pack("<IIII32s32s", 0x40, 0x3000, 0, 0, b"bblp", b"12345678")
        )
        self.assertGreater(struct.unpack("<IIII", c.exact(16))[0], 1048576)
        c = self.client(port)
        c.socket.sendall(b"\0" * 80)
        with self.assertRaises(EOFError):
            c.exact(1)

    def test_validation_persistence_and_active_disconnect(self):
        for patch in [
            {"fps": 0},
            {"fps": True},
            {"fragmentBytes": 4097},
            {"mode": "bad"},
            {"h264Source": "../secret.h264"},
            {"jpegSource": "missing.jpg"},
            {"unknown": 1},
        ]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.camera.configure(patch)
        c = self.client(self.server())
        c.start()
        self.camera.configure({"fps": 5})
        try:
            while c.message():
                pass
        except (EOFError, OSError):
            pass
        self.assertEqual(camera.Camera(self.temp.name).options["fps"], 5)

    def test_truncated_jpeg_and_recovery(self):
        auth = struct.pack("<IIII32s32s", 0x40, 0x3000, 0, 0, b"bblp", b"12345678")
        for mode in ("truncated-header", "truncated-frame"):
            with self.subTest(mode=mode):
                port = self.server("jpeg", mode=mode)
                c = self.client(port)
                c.socket.sendall(auth)
                if mode == "truncated-header":
                    self.assertEqual(c.exact(2), b"\x00\x01")
                    with self.assertRaises(EOFError):
                        c.exact(14)
                else:
                    size = struct.unpack("<IIII", c.exact(16))[0]
                    self.assertEqual(
                        c.exact(size // 2),
                        (camera.ASSETS / "test.jpg").read_bytes()[: size // 2],
                    )
                    with self.assertRaises(EOFError):
                        c.exact(size - size // 2)
                self.camera.configure({"mode": "drop", "dropAfter": 1})
                c = self.client(port)
                c.socket.sendall(auth)
                size = struct.unpack("<IIII", c.exact(16))[0]
                self.assertEqual(
                    c.exact(size), (camera.ASSETS / "test.jpg").read_bytes()
                )

    def test_invalid_saved_settings_do_not_disable_lab(self):
        directory = pathlib.Path(self.temp.name) / "camera"
        for value in ("{broken", "[]", '{"h264Source":"deleted.h264"}', '{"fps":true}'):
            with self.subTest(value=value):
                (directory / "settings.json").write_text(value, "utf-8")
                restored = camera.Camera(self.temp.name)
                self.assertEqual(restored.options, camera.DEFAULTS)
                self.assertEqual(restored.status()["counters"]["settings-reset"], 1)
                self.assertEqual(
                    (directory / "settings.json").read_text("utf-8"), value
                )
                restored.configure({"fps": 6})
                self.assertEqual(camera.Camera(self.temp.name).options["fps"], 6)

    def test_camera_http_api(self):
        model = lab.Lab(pathlib.Path(self.temp.name) / "http")
        server = ThreadingHTTPServer(("127.0.0.1", 0), management.handler(model))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:" + str(server.server_address[1]) + "/api/camera"
        try:
            self.assertEqual(
                json.load(urllib.request.urlopen(url))["ports"], {"rtsps": 0, "jpeg": 0}
            )

            def post(body, origin=None):
                headers = {"Content-Type": "application/json"}
                if origin:
                    headers["Origin"] = origin
                return urllib.request.urlopen(
                    urllib.request.Request(
                        url, data=json.dumps(body).encode(), headers=headers
                    )
                )

            self.assertEqual(
                json.load(post({"auth": "basic"}))["options"]["auth"], "basic"
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                post({"fps": 99})
            self.assertEqual(ctx.exception.code, 400)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                post({"fps": 3}, "http://evil.example")
            self.assertEqual(ctx.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()

    def test_runtime_services_start_stop_conflict_and_recovery(self):
        model = lab.Lab(pathlib.Path(self.temp.name) / "services")
        services = model.services

        def free_port():
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]

        try:
            self.assertFalse(
                any(s["enabled"] for s in services.status()["services"].values())
            )
            for name in ("mqtt", "rtsps", "jpeg"):
                port = free_port()
                for _ in range(2):
                    services.change(dict(service=name, enabled=True, port=port))
                    self.assertTrue(services.status()["services"][name]["enabled"])
                    pending = None
                    if name == "mqtt":
                        pending = socket.create_connection(("127.0.0.1", port))
                        pending.settimeout(2)
                        c = self.client(port)
                        body = (
                            mqtt.mqtt_string("MQTT")
                            + b"\x04\xc2\x00\x1e"
                            + mqtt.mqtt_string("service-test")
                            + mqtt.mqtt_string("bblp")
                            + mqtt.mqtt_string("12345678")
                        )
                        c.socket.sendall(mqtt.packet(0x10, body))
                        self.assertEqual(mqtt.receive(c.socket), (0x20, b"\0\0"))
                    with self.assertRaises(ValueError):
                        services.change(
                            dict(service=name, enabled=True, port=free_port())
                        )
                    services.change(dict(service=name, enabled=False, port=port))
                    self.assertFalse(services.status()["services"][name]["enabled"])
                    if pending is not None:
                        try:
                            self.assertEqual(pending.recv(1), b"")
                            self.assertEqual(c.socket.recv(1), b"")
                        except ConnectionResetError:
                            pass
                        finally:
                            pending.close()
            with socket.socket() as occupied:
                occupied.bind(("127.0.0.1", 0))
                occupied.listen()
                with self.assertRaises(ValueError):
                    services.change(
                        dict(
                            service="rtsps",
                            enabled=True,
                            port=occupied.getsockname()[1],
                        )
                    )
            self.assertEqual(services.status()["events"][-1]["action"], "error")
            failed = services.status()["services"]["rtsps"]
            self.assertEqual(failed["state"], "error")
            self.assertTrue(failed["error"])
            for _ in range(101):
                services.record("jpeg", "stopped", "event retention test")
            self.assertEqual(services.status()["services"]["rtsps"]["state"], "error")
            port = free_port()
            services.change(dict(service="rtsps", enabled=True, port=port))
            self.assertEqual(services.status()["services"]["rtsps"]["state"], "running")
            self.assertEqual(services.status()["services"]["rtsps"]["error"], "")
            c = self.client(port)
            c.start()
            self.assertTrue(c.frame())
            services.change(dict(service="rtsps", enabled=False, port=port))
            self.assertEqual(services.status()["services"]["rtsps"]["state"], "stopped")
            with self.assertRaises((EOFError, OSError)):
                while c.message():
                    pass
            self.assertEqual(model.camera.ports["rtsps"], 0)
            self.assertTrue(services.status()["environment"]["mediaReady"])
        finally:
            services.close()

    def test_services_http_validation_and_same_origin(self):
        model = lab.Lab(pathlib.Path(self.temp.name) / "http-services")
        server = ThreadingHTTPServer(("127.0.0.1", 0), management.handler(model))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/api/services"
        try:
            self.assertIn("environment", json.load(urllib.request.urlopen(url)))
            for payload, origin, expected in [
                ({"service": "jpeg", "enabled": True, "port": 0}, None, 400),
                ({"service": "invalid", "enabled": True, "port": 6000}, None, 400),
                ({"service": "jpeg", "enabled": "false", "port": 6000}, None, 400),
                (
                    {"service": "jpeg", "enabled": False, "port": 6000},
                    "http://evil.example",
                    403,
                ),
            ]:
                headers = {"Content-Type": "application/json"}
                if origin:
                    headers["Origin"] = origin
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(
                        urllib.request.Request(
                            url, data=json.dumps(payload).encode(), headers=headers
                        )
                    )
                self.assertEqual(ctx.exception.code, expected)
        finally:
            server.shutdown()
            server.server_close()
            model.services.close()

    def test_stopping_mqtt_cancels_injected_reply_delay(self):
        model = lab.Lab(pathlib.Path(self.temp.name) / "mqtt-delay")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        services = model.services
        try:
            services.change(dict(service="mqtt", enabled=True, port=port))
            c = self.client(port)
            c.socket.sendall(
                mqtt.packet(
                    0x10,
                    mqtt.mqtt_string("MQTT")
                    + b"\x04\xc2\x00\x1e"
                    + mqtt.mqtt_string("delay-test")
                    + mqtt.mqtt_string("bblp")
                    + mqtt.mqtt_string("12345678"),
                )
            )
            self.assertEqual(mqtt.receive(c.socket), (0x20, b"\0\0"))
            model.printers["XTSIMP1S"].fault["delay"] = 20
            c.socket.sendall(
                mqtt.packet(
                    0x30,
                    mqtt.mqtt_string("device/XTSIMP1S/request")
                    + json.dumps(
                        {
                            "system": {
                                "command": "ledctrl",
                                "led_node": "chamber_light",
                                "led_mode": "on",
                            }
                        }
                    ).encode(),
                )
            )
            deadline = time.monotonic() + 2
            while not model.printers["XTSIMP1S"].rx and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(model.printers["XTSIMP1S"].rx, 1)
            began = time.monotonic()
            services.change(dict(service="mqtt", enabled=False, port=port))
            self.assertLess(time.monotonic() - began, 2)
            self.assertEqual(model.clients, set())
        finally:
            services.close()

    def test_bootstrap_dependency_check_and_offline_start(self):
        self.assertTrue(bootstrap.dependencies_ready())
        with mock.patch.object(
            bootstrap.importlib.metadata, "version", return_value="43.0.0"
        ):
            self.assertFalse(bootstrap.dependencies_ready())
        with mock.patch.object(
            bootstrap.importlib.metadata, "version", side_effect=ImportError
        ):
            self.assertFalse(bootstrap.dependencies_ready())
        with (
            mock.patch.object(
                bootstrap.sys,
                "argv",
                [
                    "bootstrap.py",
                    "--data-dir",
                    str(pathlib.Path(self.temp.name) / "bootstrap"),
                ],
            ),
            mock.patch.object(bootstrap.subprocess, "run") as install,
            mock.patch.object(lab, "main") as start,
        ):
            bootstrap.main()
            install.assert_not_called()
            start.assert_called_once()
        with (
            mock.patch.object(
                bootstrap.sys,
                "argv",
                [
                    "bootstrap.py",
                    "--data-dir",
                    str(pathlib.Path(self.temp.name) / "bootstrap"),
                ],
            ),
            mock.patch.object(bootstrap, "dependencies_ready", return_value=False),
            mock.patch.object(
                bootstrap.subprocess, "run", side_effect=OSError("offline")
            ),
        ):
            with self.assertRaisesRegex(SystemExit, "Dependency installation failed"):
                bootstrap.main()


if __name__ == "__main__":
    unittest.main(verbosity=2)
