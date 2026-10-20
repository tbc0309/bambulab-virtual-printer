"""Synthetic Bambu camera transports. No connection to real printers is made."""

from __future__ import annotations

import base64
import collections
import copy
import hashlib
import hmac
import json
import pathlib
import re
import secrets
import select
import socket
import socketserver
import ssl
import struct
import threading
import time

ASSETS = pathlib.Path(__file__).parent / "assets" / "camera"
MODES = (
    "normal",
    "fixed-timestamps",
    "delayed",
    "drop",
    "stall",
    "wrong-code",
    "oversize",
    "truncated-header",
    "truncated-frame",
)
DEFAULTS = dict(
    mode="normal",
    auth="digest",
    fps=3,
    delaySeconds=2,
    dropAfter=10,
    fragmentBytes=0,
    aggregateParameterSets=False,
    h264Source="fixture",
    jpegSource="fixture",
)


def annex_b(data):
    nals = [n for n in re.split(b"\x00\x00\x00?\x01", data) if n]
    if (
        not nals
        or not any(n[0] & 31 == 7 for n in nals)
        or not any(n[0] & 31 == 8 for n in nals)
    ):
        raise ValueError("H.264 Annex B with SPS and PPS required")
    return nals


def first_slice(nal):
    # first_mb_in_slice is Exp-Golomb: zero is represented by the first bit 1.
    return len(nal) > 1 and bool(nal[1] & 128)


def access_units(nals):
    frames, current, has_slice = [], [], False
    for nal in nals:
        kind = nal[0] & 31
        if has_slice and (kind in (6, 7, 8, 9) or kind in (1, 5) and first_slice(nal)):
            frames.append(current)
            current, has_slice = [], False
        current.append(nal)
        has_slice |= kind in (1, 5)
    if has_slice:
        frames.append(current)
    if not frames:
        raise ValueError("H.264 stream contains no video frames")
    return frames


def payloads(nal, limit=1000):
    if len(nal) <= limit:
        return [nal]
    result = []
    for start in range(1, len(nal), limit - 2):
        end = min(start + limit - 2, len(nal))
        result.append(
            bytes(
                [
                    (nal[0] & 224) | 28,
                    (nal[0] & 31)
                    | (128 if start == 1 else 0)
                    | (64 if end == len(nal) else 0),
                ]
            )
            + nal[start:end]
        )
    return result


class Camera:
    def __init__(self, data_dir, logs=None):
        self.logs = logs
        self.directory = pathlib.Path(data_dir) / "camera"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.options = copy.deepcopy(DEFAULTS)
        self.events = collections.deque(maxlen=100)
        self.counters = collections.Counter()
        self.clients = set()
        self.cancellations = {}
        self.ports = {"rtsps": 0, "jpeg": 0}
        saved = self.directory / "settings.json"
        if saved.exists():
            try:
                self.configure(json.loads(saved.read_text("utf-8")), save=False)
            except (OSError, ValueError, TypeError):
                # Keep the invalid file for inspection; MQTT and the repair UI
                # must remain available when a selected media file disappears.
                self.event(
                    "settings-reset",
                    reason="Invalid settings or unavailable camera source; using fixtures",
                )

    def source(self, name, suffix):
        if name == "fixture":
            return ASSETS / ("test" + suffix)
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", name)
            or not name.endswith(suffix)
        ):
            raise ValueError("Use a camera data filename, not a path")
        path = (self.directory / name).resolve()
        if (
            path.parent != self.directory.resolve()
            or not path.is_file()
            or not 0 < path.stat().st_size <= 16 * 1024 * 1024
        ):
            raise ValueError("Camera source must be a nonempty local file up to 16 MiB")
        return path

    def configure(self, patch, save=True):
        if not isinstance(patch, dict) or set(patch) - set(DEFAULTS):
            raise ValueError("Unknown camera option")
        with self.lock:
            cfg = {**self.options, **patch}
            if cfg["mode"] not in MODES or cfg["auth"] not in ("basic", "digest"):
                raise ValueError("Unknown camera scenario/authentication")
            for key, low, high in [
                ("fps", 1, 30),
                ("delaySeconds", 0, 30),
                ("dropAfter", 1, 300),
                ("fragmentBytes", 0, 4096),
            ]:
                v = cfg[key]
                if (
                    isinstance(v, bool)
                    or not isinstance(v, int)
                    or not low <= v <= high
                ):
                    raise ValueError(f"{key} must be an integer {low}..{high}")
            if not isinstance(cfg["aggregateParameterSets"], bool):
                raise ValueError("aggregateParameterSets must be boolean")
            access_units(annex_b(self.source(cfg["h264Source"], ".h264").read_bytes()))
            jpeg = self.source(cfg["jpegSource"], ".jpg").read_bytes()
            if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
                raise ValueError("JPEG source required")
            if save:
                temp = self.directory / "settings.tmp"
                temp.write_text(json.dumps(cfg, indent=2), "utf-8")
                temp.replace(self.directory / "settings.json")
            self.options = cfg
            clients = tuple(self.clients)
            for client in clients:
                self.cancellations[client].set()
        if save:
            self.event("configured", options=cfg)
        # Apply scenarios on reconnect without interleaving old and new media.
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return self.status()

    def event(self, kind, **values):
        with self.lock:
            self.counters[kind] += 1
            if kind != "frame":
                self.events.append(dict(time=time.time(), event=kind, **values))
            if self.logs:
                self.logs.record(
                    "camera",
                    kind,
                    {**values, "count": self.counters[kind]},
                    level=(
                        "warning"
                        if kind in ("closed", "unauthorized", "settings-reset")
                        else "info"
                    ),
                    sample=(
                        (values.get("transport", "frame"), 1)
                        if kind == "frame"
                        else None
                    ),
                )

    def status(self):
        with self.lock:
            return dict(
                options=copy.deepcopy(self.options),
                ports=dict(self.ports),
                clients=len(self.clients),
                counters=dict(self.counters),
                events=list(self.events),
                modes=list(MODES),
                sources={
                    ext: ["fixture"]
                    + sorted(
                        p.name for p in self.directory.glob("*" + ext) if p.is_file()
                    )
                    for ext in (".h264", ".jpg")
                },
            )


class CameraServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = False

    def __init__(self, camera, bind, port, context, kind):
        self.camera, self.context, self.kind = camera, context, kind
        self.slots = threading.BoundedSemaphore(8)
        self.clients = set()
        self.stopped = threading.Event()
        self.published_port = None
        super().__init__((bind, port), CameraHandler)
        self.published_port = self.server_address[1]
        camera.ports[kind] = self.published_port

    def server_close(self):
        self.stopped.set()
        with self.camera.lock:
            clients = tuple(self.clients)
            for client in clients:
                self.camera.cancellations[client].set()
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        super().server_close()
        if self.camera.ports[self.kind] == self.published_port:
            self.camera.ports[self.kind] = 0


class CameraHandler(socketserver.BaseRequestHandler):
    def handle(self):
        if not self.server.slots.acquire(blocking=False):
            return
        camera = self.server.camera
        try:
            self.request.settimeout(3)
            with self.server.context.wrap_socket(
                self.request, server_side=True
            ) as self.stream:
                self.stream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with camera.lock:
                    camera.clients.add(self.stream)
                    self.server.clients.add(self.stream)
                    self.cancelled = threading.Event()
                    if self.server.stopped.is_set():
                        self.cancelled.set()
                    camera.cancellations[self.stream] = self.cancelled
                    self.cfg = dict(camera.options)
                camera.event("connected", transport=self.server.kind)
                try:
                    if self.server.kind == "rtsps":
                        self.rtsp()
                    else:
                        self.jpeg()
                finally:
                    with camera.lock:
                        camera.clients.discard(self.stream)
                        camera.cancellations.pop(self.stream, None)
                        self.server.clients.discard(self.stream)
                    camera.event("disconnected", transport=self.server.kind)
        except (OSError, ssl.SSLError, EOFError, ValueError, UnicodeError) as exc:
            camera.event("closed", transport=self.server.kind, error=type(exc).__name__)
        finally:
            self.server.slots.release()

    def send(self, data):
        size = self.cfg["fragmentBytes"] or len(data)
        for start in range(0, len(data), size):
            self.stream.sendall(data[start : start + size])

    def exact(self, count):
        data = b""
        while len(data) < count:
            chunk = self.stream.recv(count - len(data))
            if not chunk:
                raise EOFError()
            data += chunk
        return data

    def jpeg(self):
        auth = self.exact(80)
        expected = struct.pack("<IIII32s32s", 0x40, 0x3000, 0, 0, b"bblp", b"12345678")
        if not hmac.compare_digest(auth, expected) or self.cfg["mode"] == "wrong-code":
            self.server.camera.event("unauthorized", transport="jpeg")
            return
        if self.cfg["mode"] == "oversize":
            self.send(struct.pack("<IIII", 1048577, 0, 0, 0))
            return
        if self.cfg["mode"] == "truncated-header":
            self.send(b"\x00\x01")
            self.server.camera.event("intentional-drop", transport="jpeg")
            return
        frame = self.server.camera.source(self.cfg["jpegSource"], ".jpg").read_bytes()
        if self.cfg["mode"] == "truncated-frame":
            self.send(
                struct.pack("<IIII", len(frame), 0, 0, 0) + frame[: len(frame) // 2]
            )
            self.server.camera.event("intentional-drop", transport="jpeg")
            return
        expires = time.monotonic() + 300
        if self.cfg["mode"] == "delayed":
            if self.cancelled.wait(self.cfg["delaySeconds"]):
                return
        for i in range(900):
            if time.monotonic() >= expires:
                return
            if self.cfg["mode"] == "stall":
                while time.monotonic() < expires:
                    try:
                        self.exact(1)
                        return
                    except socket.timeout:
                        continue
                return
            self.send(struct.pack("<IIII", len(frame), i, 0, 0) + frame)
            self.server.camera.event("frame", transport="jpeg")
            if self.cfg["mode"] == "drop" and i + 1 >= self.cfg["dropAfter"]:
                self.server.camera.event("intentional-drop", transport="jpeg")
                return
            if self.cancelled.wait(1 / self.cfg["fps"]):
                return

    def extract_request(self):
        # Skip RTCP interleaved records before parsing subsequent keepalives.
        while self.pending.startswith(b"$"):
            if len(self.pending) < 4:
                return None
            length = struct.unpack("!H", self.pending[2:4])[0] + 4
            if len(self.pending) < length:
                return None
            self.pending = self.pending[length:]
        if len(self.pending) > 65536:
            raise ValueError("RTSP request too large")
        if b"\r\n\r\n" not in self.pending:
            return None
        raw, rest = self.pending.split(b"\r\n\r\n", 1)
        lines = raw.decode("ascii").split("\r\n")
        method, url, version = lines[0].split()
        if version != "RTSP/1.0":
            raise ValueError("RTSP version")
        headers = dict(
            (k.lower().strip(), v.strip())
            for k, v in (line.split(":", 1) for line in lines[1:])
        )
        length = int(headers.get("content-length", "0"))
        if not 0 <= length <= 65536:
            raise ValueError("RTSP body too large")
        if len(rest) < length:
            return None
        self.pending = rest[length:]
        return method, url, headers

    def receive_request(self, block):
        request = self.extract_request()
        while request is None:
            if (
                not block
                and not self.stream.pending()
                and not select.select([self.stream], [], [], 0)[0]
            ):
                return None
            self.stream.settimeout(3 if block else 0.01)
            try:
                chunk = self.stream.recv(4096)
            except (socket.timeout, ssl.SSLWantReadError):
                if block:
                    raise
                return None
            finally:
                self.stream.settimeout(3)
            if not chunk:
                raise EOFError()
            self.pending += chunk
            request = self.extract_request()
        return request

    def authenticated(self, method, url, headers):
        if self.cfg["mode"] == "wrong-code":
            return False
        auth = headers.get("authorization", "")
        if self.cfg["auth"] == "basic":
            return hmac.compare_digest(
                auth, "Basic " + base64.b64encode(b"bblp:12345678").decode()
            )
        if not auth.startswith("Digest "):
            return False
        fields = {
            m[0]: m[1] or m[2]
            for m in re.findall(r'(\w+)=(?:"([^"]*)"|([^, ]+))', auth[7:])
        }
        if (
            fields.get("username") != "bblp"
            or fields.get("realm") != "fixture"
            or fields.get("nonce") != self.nonce
            or fields.get("uri") != url
        ):
            return False
        md5 = lambda value: hashlib.md5(value.encode()).hexdigest()
        ha1, ha2 = md5("bblp:fixture:12345678"), md5(method + ":" + url)
        if fields.get("qop") == "auth":
            if not re.fullmatch(
                "[0-9a-fA-F]{8}", fields.get("nc", "")
            ) or not fields.get("cnonce"):
                return False
            expected = md5(
                ":".join([ha1, self.nonce, fields["nc"], fields["cnonce"], "auth", ha2])
            )
        elif "qop" not in fields:
            expected = md5(":".join([ha1, self.nonce, ha2]))
        else:
            return False
        return hmac.compare_digest(fields.get("response", ""), expected)

    def reply(self, request, status=200, extra="", body=b""):
        code = request[2].get("cseq", "0")
        if not code.isdigit() or len(code) > 10:
            raise ValueError("CSeq required")
        reason = {
            200: "OK",
            401: "Unauthorized",
            461: "Unsupported Transport",
            405: "Method Not Allowed",
        }[status]
        self.send(
            f"RTSP/1.0 {status} {reason}\r\nCSeq: {code}\r\n{extra}Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )

    def rtsp(self):
        self.pending, self.nonce = b"", secrets.token_hex(16)
        camera = self.server.camera
        nals = annex_b(camera.source(self.cfg["h264Source"], ".h264").read_bytes())
        frames = access_units(nals)
        parameters = [next(n for n in nals if n[0] & 31 == kind) for kind in (7, 8)]
        setup = False
        while True:
            request = self.receive_request(True)
            method, url, headers = request
            camera.event("request", method=method)
            if not self.authenticated(*request):
                challenge = (
                    'Basic realm="fixture"'
                    if self.cfg["auth"] == "basic"
                    else f'Digest realm="fixture", nonce="{self.nonce}", qop="auth", algorithm=MD5'
                )
                self.reply(request, 401, "WWW-Authenticate: " + challenge + "\r\n")
                camera.event(
                    "unauthorized" if headers.get("authorization") else "challenge",
                    transport="rtsps",
                )
                continue
            if method == "DESCRIBE":
                sprop = ",".join(base64.b64encode(n).decode() for n in parameters)
                body = f"v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Synthetic camera\r\nt=0 0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:trackID=0\r\na=fmtp:96 packetization-mode=1;sprop-parameter-sets={sprop}\r\n".encode()
                self.reply(
                    request,
                    extra=f'Content-Type: application/sdp\r\nContent-Base: {url.rstrip("/")}/\r\n',
                    body=body,
                )
            elif method == "SETUP":
                if "RTP/AVP/TCP" not in headers.get(
                    "transport", ""
                ) or "interleaved=0-1" not in headers.get("transport", ""):
                    self.reply(request, 461)
                    continue
                setup = True
                self.reply(
                    request,
                    extra="Session: synthetic;timeout=60\r\nTransport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n",
                )
            elif method == "PLAY" and setup:
                self.reply(request, extra="Session: synthetic\r\n")
                expires = time.monotonic() + 300
                if self.cfg["mode"] == "delayed":
                    if self.cancelled.wait(self.cfg["delaySeconds"]):
                        return
                sequence, timestamp, count = 65530, 0, 0
                while count < 900 and time.monotonic() < expires:
                    start = time.monotonic()
                    if self.cfg["mode"] != "stall":
                        frame = frames[count % len(frames)]
                        units = list(frame)
                        if self.cfg["aggregateParameterSets"] and all(
                            p in units for p in parameters
                        ):
                            stap = bytes([(parameters[0][0] & 224) | 24]) + b"".join(
                                struct.pack("!H", len(p)) + p for p in parameters
                            )
                            units = [stap] + [p for p in units if p not in parameters]
                        timestamp = (
                            6000
                            if self.cfg["mode"] == "fixed-timestamps"
                            else (timestamp + 90000 // self.cfg["fps"]) & 0xFFFFFFFF
                        )
                        for j, nal in enumerate(units):
                            parts = payloads(nal)
                            for i, part in enumerate(parts):
                                sequence = (sequence + 1) & 65535
                                rtp = (
                                    struct.pack(
                                        "!BBHII",
                                        128,
                                        96
                                        | (
                                            128
                                            if j == len(units) - 1
                                            and i == len(parts) - 1
                                            else 0
                                        ),
                                        sequence,
                                        timestamp,
                                        42,
                                    )
                                    + part
                                )
                                self.send(b"$\x00" + struct.pack("!H", len(rtp)) + rtp)
                        camera.event("frame", transport="rtsps")
                        count += 1
                        if (
                            self.cfg["mode"] == "drop"
                            and count >= self.cfg["dropAfter"]
                        ):
                            camera.event("intentional-drop", transport="rtsps")
                            return
                    deadline = start + 1 / self.cfg["fps"]
                    while time.monotonic() < deadline:
                        control = self.receive_request(False)
                        if control:
                            camera.event("request", method=control[0])
                            if not self.authenticated(*control):
                                self.reply(control, 401)
                            else:
                                self.reply(control, extra="Session: synthetic\r\n")
                                if control[0] == "TEARDOWN":
                                    return
                        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
            elif method in ("OPTIONS", "GET_PARAMETER", "TEARDOWN"):
                self.reply(
                    request,
                    extra="Public: OPTIONS, DESCRIBE, SETUP, PLAY, GET_PARAMETER, TEARDOWN\r\n",
                )
                if method == "TEARDOWN":
                    return
            else:
                self.reply(request, 405)


def start_services(camera, bind, rtsps_port, jpeg_port, cert, key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    servers = []
    try:
        for kind, port in [("rtsps", rtsps_port), ("jpeg", jpeg_port)]:
            if port:
                servers.append(CameraServer(camera, bind, port, context, kind))
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        return servers
    except Exception:
        for server in servers:
            server.server_close()
        raise
