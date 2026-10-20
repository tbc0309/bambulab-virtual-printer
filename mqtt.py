"""Bounded TLS MQTT transport and local fixture certificates."""

import datetime
import ipaddress
import json
import queue
import re
import socket
import ssl
import threading
import time
import traceback

LOCK = threading.RLock()


def mqtt_string(s):
    b = s.encode()
    return len(b).to_bytes(2, "big") + b


def packet(kind, body=b""):
    n, out = len(body), bytearray([kind])
    while True:
        d, n = n % 128, n // 128
        out.append(d | (128 if n else 0))
        if not n:
            return bytes(out) + body


def exact(c, n):
    b = bytearray()
    while len(b) < n:
        part = c.recv(n - len(b))
        if not part:
            raise EOFError()
        b.extend(part)
    return bytes(b)


def receive(c):
    kind, size, factor = exact(c, 1)[0], 0, 1
    for _ in range(4):
        n = exact(c, 1)[0]
        size += (n & 127) * factor
        if size > 65536:
            raise ValueError("MQTT frame >64KiB")
        if not n & 128:
            return kind, exact(c, size)
        factor *= 128
    raise ValueError("Malformed remaining length")


def read_string(b, offset):
    if offset + 2 > len(b):
        raise ValueError("Truncated MQTT string")
    size = int.from_bytes(b[offset : offset + 2], "big")
    end = offset + 2 + size
    if end > len(b):
        raise ValueError("Truncated MQTT string")
    return b[offset + 2 : end].decode("utf-8"), end


class Client:
    WRITE_TIMEOUT = 5.0

    def __init__(self, lab, connection):
        self.lab, self.c = lab, connection
        self.lock = threading.Lock()
        self.close_lock = threading.Lock()
        self.subscriptions = set()
        self.authenticated = False
        self.closed = threading.Event()
        self.reports = queue.Queue(maxsize=8)
        threading.Thread(target=self._write_reports, daemon=True).start()

    def send(self, kind, body=b""):
        with self.lock:
            self.c.sendall(packet(kind, body))

    def enqueue_report(self, body):
        if self.closed.is_set():
            raise OSError("Client closed")
        if len(body) > 65536:
            raise ValueError("Report >64KiB")
        try:
            self.reports.put_nowait(body)
        except queue.Full:
            self.close("report-queue-full")
            raise OSError("Slow MQTT client: report queue full")

    def _write_reports(self):
        while not self.closed.is_set():
            try:
                body = self.reports.get(timeout=0.25)
            except queue.Empty:
                continue
            deadline = threading.Timer(
                self.WRITE_TIMEOUT, lambda: self.close("write-timeout")
            )
            deadline.daemon = True
            deadline.start()
            try:
                self.send(0x30, body)
            except OSError:
                self.close()
                break
            finally:
                deadline.cancel()

    def close(self, reason="closed"):
        with self.close_lock:
            if not self.closed.is_set():
                self.closed.set()
                self.lab.logs.record(
                    "mqtt",
                    "disconnected",
                    {"reason": reason, "subscriptions": sorted(self.subscriptions)},
                )
        with LOCK:
            self.lab.clients.discard(self)
        try:
            self.c.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.c.close()

    def run(self):
        try:
            while not self.closed.is_set():
                kind, b = receive(self.c)
                typ = kind >> 4
                if typ == 1:
                    if self.authenticated:
                        raise ValueError("Duplicate CONNECT")
                    protocol, offset = read_string(b, 0)
                    if protocol != "MQTT" or len(b) < offset + 4 or b[offset] != 4:
                        self.send(0x20, b"\x00\x01")
                        break
                    flags = b[offset + 1]
                    if flags & 1:
                        raise ValueError("Invalid CONNECT flags")
                    keep = int.from_bytes(b[offset + 2 : offset + 4], "big")
                    _, offset = read_string(b, offset + 4)
                    if flags & 4:
                        _, offset = read_string(b, offset)
                        _, offset = read_string(b, offset)
                    user, password = "", ""
                    if flags & 128:
                        user, offset = read_string(b, offset)
                    if flags & 64:
                        password, offset = read_string(b, offset)
                    if (user, password) not in (
                        ("bblp", "12345678"),
                        ("xtouch-lab", "xtouch-lab"),
                    ):
                        self.lab.logs.record(
                            "mqtt", "authentication-rejected", level="warning"
                        )
                        self.send(0x20, b"\x00\x05")
                        break
                    self.authenticated = True
                    self.lab.logs.record("mqtt", "connected", {"keepalive": keep})
                    self.c.settimeout(keep * 1.5 if keep else None)
                    self.send(0x20, b"\0\0")
                    with LOCK:
                        self.lab.clients.add(self)
                elif not self.authenticated:
                    raise ValueError("CONNECT required")
                elif typ == 8:
                    offset, result, new = 2, bytearray(), []
                    while offset < len(b):
                        topic, offset = read_string(b, offset)
                        qos = b[offset]
                        offset += 1
                        match = re.fullmatch(
                            r"device/(XTSIM[A-Za-z0-9_-]+)/report", topic
                        )
                        if not match or match[1] not in self.lab.printers or qos > 1:
                            result.append(128)
                        else:
                            result.append(0)
                            self.subscriptions.add(match[1])
                            new.append(match[1])
                    self.send(0x90, b[:2] + result)
                    with LOCK:
                        for serial in new:
                            self.lab.publish(serial, self.lab.printers[serial].status())
                elif typ == 10:
                    offset = 2
                    while offset < len(b):
                        topic, offset = read_string(b, offset)
                        self.subscriptions.discard(
                            topic.split("/")[1] if "/" in topic else ""
                        )
                    self.send(0xB0, b[:2])
                elif typ == 3:
                    topic, offset = read_string(b, 0)
                    qos = (kind >> 1) & 3
                    if qos > 1:
                        raise ValueError("QoS2 is not implemented")
                    pid = b[offset : offset + 2] if qos else b""
                    offset += 2 if qos else 0
                    if qos:
                        self.send(0x40, pid)
                    match = re.fullmatch(r"device/(XTSIM[A-Za-z0-9_-]+)/request", topic)
                    if not match or match[1] not in self.lab.printers:
                        raise ValueError("Unknown request topic")
                    data = json.loads(b[offset:])
                    if not isinstance(data, dict):
                        raise ValueError("MQTT JSON must be object")
                    with LOCK:
                        p = self.lab.printers[match[1]]
                        reply = p.command(data)
                        noack, delay = p.fault["noack"], p.fault["delay"]
                    if delay:
                        if self.closed.wait(delay):
                            break
                    with LOCK:
                        if not noack:
                            self.lab.publish(p.serial, reply)
                        self.lab.publish(p.serial, p.status())
                elif typ == 12:
                    self.send(0xD0)
                elif typ == 14:
                    break
                else:
                    raise ValueError("Unsupported MQTT packet")
        except (
            OSError,
            EOFError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            RecursionError,
        ) as e:
            if not self.closed.is_set() and not isinstance(
                e, (EOFError, socket.timeout)
            ):
                with LOCK:
                    self.lab.report_error(
                        type(e).__name__,
                        {"transport": "mqtt", "traceback": traceback.format_exc()},
                    )
        finally:
            self.close()


def certificate(data_dir):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cert, key = data_dir / "server.pem", data_dir / "server.key"
    if not cert.exists() or not key.exists():
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "BambuLab Virtual Printer")]
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        public = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(private.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("localhost"),
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    ]
                ),
                False,
            )
            .sign(private, hashes.SHA256())
        )
        key.write_bytes(
            private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        key.chmod(0o600)
        cert.write_bytes(public.public_bytes(serialization.Encoding.PEM))
    return cert, key


class MQTTListener(socket.socket):
    def __init__(self):
        super().__init__()
        self.connections = set()
        self.workers = set()
        self.handlers = set()
        self.guard = threading.Lock()
        self.stopping = False

    def close(self):
        with self.guard:
            self.stopping = True
            connections = tuple(self.connections)
            workers = tuple(self.workers)
            handlers = tuple(self.handlers)
        try:
            self.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        super().close()
        for client in handlers:
            client.close("service-stopped")
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        deadline = time.monotonic() + 7
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=max(0, deadline - time.monotonic()))


def mqtt_server(lab, bind, port):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    cert, key = certificate(lab.data_dir)
    context.load_cert_chain(cert, key)
    server = MQTTListener()
    server.setsockopt(
        socket.SOL_SOCKET,
        (
            socket.SO_EXCLUSIVEADDRUSE
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE")
            else socket.SO_REUSEADDR
        ),
        1,
    )
    try:
        server.bind((bind, port))
        server.listen(16)
    except OSError:
        server.close()
        raise
    slots = threading.BoundedSemaphore(24)

    def run(c):
        stream = None
        client = None
        try:
            c.settimeout(6)
            # Track TLS sockets before the handshake, so stop also cancels
            # clients that have connected but not sent authentication yet.
            stream = context.wrap_socket(
                c, server_side=True, do_handshake_on_connect=False
            )
            with server.guard:
                server.connections.discard(c)
                if server.stopping:
                    stream.close()
                    return
                server.connections.add(stream)
            stream.do_handshake()
            client = Client(lab, stream)
            with server.guard:
                if server.stopping:
                    client.close("service-stopped")
                    return
                server.handlers.add(client)
            client.run()
        except (OSError, ValueError):
            c.close()
        finally:
            if stream is not None:
                stream.close()
            with server.guard:
                server.connections.discard(c)
                server.connections.discard(stream)
                server.workers.discard(threading.current_thread())
                server.handlers.discard(client)
            slots.release()

    def accept():
        while True:
            try:
                c, _ = server.accept()
            except OSError:
                return
            if slots.acquire(blocking=False):
                with server.guard:
                    if server.stopping:
                        c.close()
                        slots.release()
                        return
                    server.connections.add(c)
                    worker = threading.Thread(
                        target=run, args=(c,), daemon=True, name="mqtt-client"
                    )
                    server.workers.add(worker)
                    worker.start()
            else:
                c.close()

    threading.Thread(target=accept, daemon=True).start()
    return server
