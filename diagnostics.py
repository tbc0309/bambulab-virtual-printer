"""Bounded, redacted JSONL logs and a human-readable file catalogue."""

from __future__ import annotations

import datetime
import collections
import json
import os
import pathlib
import re
import secrets
import threading
import time

CHANNELS = {
    "runtime": (
        "运行与服务",
        "Runtime and services",
        "启动检查、服务启停、配置操作和异常；异常堆栈仅写入文件。",
    ),
    "mqtt": (
        "打印机通信",
        "Printer communication",
        "虚拟设备命令、参数校验、回复和发送到订阅者的状态报文。",
    ),
    "camera": (
        "摄像头传输",
        "Camera transport",
        "认证、协议请求、连接、断流和每秒最多一次的帧统计；不保存视频。",
    ),
    "http": (
        "管理页请求",
        "Management requests",
        "请求路径、状态码及耗时；不保存请求头、查询参数或请求正文。",
    ),
    "comparison": (
        "只读对照",
        "Read-only comparison",
        "手动运行 P1S 只读对照工具的状态摘要，不包含真实账号或设备身份。",
    ),
}
FILE_NAME = re.compile(
    r"\d{8}T\d{9}Z-\d+-[0-9a-f]{8}--(runtime|mqtt|camera|http|comparison)\.jsonl(?:\.[1-3])?"
)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds"
    )


def redact(value, depth=0):
    if depth > 8:
        return "[depth limit]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:64]:
            name = str(key)
            normalized = re.sub(r"[^a-z]", "", name.lower())
            secret = normalized in ("otp", "pin") or any(
                word in normalized
                for word in (
                    "password",
                    "passwd",
                    "token",
                    "accesscode",
                    "authorization",
                    "secret",
                    "credential",
                    "apikey",
                    "privatekey",
                    "verificationcode",
                )
            )
            result[name] = "[redacted]" if secret else redact(item, depth + 1)
        if len(value) > 64:
            result["__truncatedKeys"] = len(value) - 64
        return result
    if isinstance(value, (list, tuple)):
        items = [redact(item, depth + 1) for item in value[:64]]
        if len(value) > 64:
            items.append({"__truncatedItems": len(value) - 64})
        return items
    if isinstance(value, str):
        value = re.sub(
            r"((?:https?|rtsps?)://)[^/@\s]+@", r"\1[redacted]@", value, flags=re.I
        )
        value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [redacted]", value)
        value = re.sub(
            r"(?i)([?&](?:access_token|token|password|access_code|code)=)[^&#\s]+",
            r"\1[redacted]",
            value,
        )
        return value[:4096] + ("…[truncated]" if len(value) > 4096 else "")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    return redact(str(value), depth + 1)


class LogStore:
    MAX_BYTES = 2 * 1024 * 1024
    MAX_TOTAL = 64 * 1024 * 1024
    MAX_FILES = 128
    MAX_AGE = 7 * 86400

    def __init__(self, data_dir):
        self.directory = pathlib.Path(data_dir) / "logs"
        self.lock = threading.RLock()
        self.session = (
            datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%f")[
                :-3
            ]
            + f"Z-{os.getpid()}-{secrets.token_hex(4)}"
        )
        self.last_error = ""
        self.failed_records = 0
        self.samples = {}
        self.alerts = collections.deque(maxlen=10)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.prune()
            self.catalogue()
        except OSError as exc:
            self.failure(exc)

    def failure(self, exc):
        self.failed_records += 1
        self.last_error = f"Log storage unavailable: {type(exc).__name__}; check disk space and directory permissions"

    def record(self, channel, event, details=None, level="info", sample=None):
        if channel not in CHANNELS:
            raise ValueError("Unknown log channel")
        with self.lock:
            if sample is not None:
                now = time.monotonic()
                key, interval = sample
                if now - self.samples.get((channel, key), -float("inf")) < interval:
                    return
                self.samples[(channel, key)] = now
                if len(self.samples) > 1024:
                    self.samples.pop(next(iter(self.samples)))
            if channel == "runtime" and event in (
                "thread-error",
                "startup-failed",
                "browser-error",
            ):
                self.alerts.append(f'{event}: {(details or {}).get("type", "error")}')
            row = dict(
                time=utc_now(),
                session=self.session,
                channel=channel,
                level=level,
                event=event,
                details=redact(details or {}),
            )
            encoded = (
                json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
            ).encode("utf-8", errors="backslashreplace")
            if len(encoded) > 32768:
                row["details"] = {"truncated": True, "originalBytes": len(encoded)}
                encoded = (json.dumps(row, ensure_ascii=False) + "\n").encode(
                    "utf-8", errors="backslashreplace"
                )
            path = self.directory / f"{self.session}--{channel}.jsonl"
            try:
                changed = not path.exists()
                if (
                    path.exists()
                    and path.stat().st_size + len(encoded) > self.MAX_BYTES
                ):
                    changed = True
                    for index in (3, 2, 1):
                        old = path.with_name(path.name + f".{index}")
                        if index == 3:
                            old.unlink(missing_ok=True)
                        elif old.exists():
                            old.replace(path.with_name(path.name + f".{index + 1}"))
                    path.replace(path.with_name(path.name + ".1"))
                    self.prune()
                with path.open("ab") as stream:
                    stream.write(encoded)
                self.last_error = ""
                if changed:
                    self.prune()
                    self.catalogue()
            except OSError as exc:
                self.failure(exc)

    def files(self):
        return [
            p
            for p in self.directory.iterdir()
            if FILE_NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink()
        ]

    def prune(self):
        files = sorted(self.files(), key=lambda p: p.stat().st_mtime, reverse=True)
        total = 0
        for index, path in enumerate(files):
            total += path.stat().st_size
            if (
                index >= self.MAX_FILES
                or total > self.MAX_TOTAL
                or time.time() - path.stat().st_mtime > self.MAX_AGE
            ):
                path.unlink(missing_ok=True)

    def catalogue(self):
        with self.lock:
            rows = []
            try:
                for path in sorted(
                    self.files(), key=lambda p: p.stat().st_mtime, reverse=True
                ):
                    channel = FILE_NAME.fullmatch(path.name)[1]
                    try:
                        started = (
                            datetime.datetime.strptime(
                                path.name.split("-")[0], "%Y%m%dT%H%M%S%fZ"
                            )
                            .replace(tzinfo=datetime.timezone.utc)
                            .timestamp()
                        )
                    except ValueError:
                        started = path.stat().st_mtime
                    rows.append(
                        dict(
                            name=path.name,
                            channel=channel,
                            title=CHANNELS[channel][0],
                            titleEn=CHANNELS[channel][1],
                            description=CHANNELS[channel][2],
                            bytes=path.stat().st_size,
                            modified=path.stat().st_mtime,
                            session=path.name.split("--")[0],
                            started=started,
                            current=path.name.startswith(self.session + "--"),
                        )
                    )
                result = dict(
                    directory="logs/",
                    session=self.session,
                    files=rows,
                    lastError=self.last_error,
                    failedRecords=self.failed_records,
                    retention=dict(
                        days=7,
                        maxFiles=self.MAX_FILES,
                        maxBytes=self.MAX_TOTAL,
                        fileBytes=self.MAX_BYTES,
                        backups=3,
                    ),
                )
                temp = self.directory / f"index-{self.session}.tmp"
                temp.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
                )
                temp.replace(self.directory / "index.json")
                return result
            except OSError as exc:
                self.failure(exc)
                return dict(
                    directory="logs/",
                    session=self.session,
                    files=rows,
                    lastError=self.last_error,
                    failedRecords=self.failed_records,
                )

    def read(self, name, tail=False):
        if not isinstance(name, str) or not FILE_NAME.fullmatch(name):
            raise ValueError("Select a listed log filename")
        path = self.directory / name
        if (
            path.is_symlink()
            or not path.is_file()
            or path.resolve().parent != self.directory.resolve()
        ):
            raise ValueError("Log file unavailable")
        with self.lock, path.open("rb") as stream:
            if tail:
                stream.seek(max(0, path.stat().st_size - 65536))
                if stream.tell():
                    stream.readline()
                return (
                    stream.read().decode("utf-8", errors="replace").splitlines()[-100:]
                )
            return stream.read(self.MAX_BYTES + 32768)
