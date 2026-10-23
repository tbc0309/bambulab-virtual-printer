"""Log privacy, retention, storage failures and HTTP/file integration tests."""

import json
import os
import pathlib
import tempfile
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import diagnostics
import compare_p1s
import lab
import management


class LogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = diagnostics.LogStore(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_redaction_catalogue_and_valid_json(self):
        self.store.record(
            "mqtt",
            "command",
            {
                "password": "do-not-save",
                "nested": {"access_token": "secret-token"},
                "url": "rtsps://user:private@localhost/test",
                "api_key": "api-value",
                "query": "https://localhost/?token=query-value",
                "header": "Bearer hidden",
                "number": float("nan"),
            },
        )
        catalogue = self.store.catalogue()
        entry = catalogue["files"][0]
        self.assertEqual(entry["titleEn"], "Printer communication")
        self.assertTrue(entry["description"])
        data = self.store.read(entry["name"]).decode()
        for secret in (
            "do-not-save",
            "secret-token",
            "private",
            "Bearer hidden",
            "api-value",
            "query-value",
        ):
            self.assertNotIn(secret, data)
        record = json.loads(data)
        self.assertEqual(record["details"]["number"], "nan")
        self.assertIn("[redacted]", data)
        self.store.record("runtime", "invalid-unicode", {"value": "\ud800"})
        index = json.loads((self.store.directory / "index.json").read_text("utf-8"))
        self.assertIn(entry["name"], [file["name"] for file in index["files"]])

    def test_logging_preserves_malformed_delta_and_running_state(self):
        model = lab.Lab(pathlib.Path(self.temp.name) / "delta")

        class Subscriber:
            subscriptions = {"XTSIMP1S"}
            reports = []

            def enqueue_report(self, value):
                self.reports.append(value)

        subscriber = Subscriber()
        model.clients.add(subscriber)
        before = model.printers["XTSIMP1S"].status()
        for payload in ({"print": 42}, {"print": None}, {"print": {"value": "ok"}}):
            model.publish("XTSIMP1S", payload)
        self.assertEqual(len(subscriber.reports), 3)
        self.assertEqual(model.printers["XTSIMP1S"].status(), before)

    def test_comparison_summary_uses_log_catalogue_without_real_network(self):
        config = pathlib.Path(self.temp.name) / "private-config.json"
        config.write_text(
            json.dumps(
                {
                    "printers": [
                        {
                            "serialNumber": "DUMMY",
                            "printerModel": "C12",
                            "mode": "lan",
                            "host": "127.0.0.1",
                            "accessCode": "dummy-code",
                        }
                    ]
                }
            ),
            "utf-8",
        )
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "compare_p1s.py",
                    "--config",
                    str(config),
                    "--data-dir",
                    self.temp.name,
                ],
            ),
            mock.patch.object(
                compare_p1s, "snapshot", return_value={"received": True}
            ) as snapshot,
        ):
            compare_p1s.main()
            snapshot.assert_called_once()
        entries = self.store.catalogue()["files"]
        comparison = next(row for row in entries if row["channel"] == "comparison")
        self.assertIn("Read-only", comparison["titleEn"])
        record = self.store.read(comparison["name"]).decode()
        self.assertNotIn("dummy-code", record)
        self.assertNotIn("DUMMY", record)

    def test_rotation_and_tail_remain_parseable(self):
        self.store.MAX_BYTES = 1024
        for i in range(30):
            self.store.record("runtime", "message", {"i": i, "text": "x" * 100})
        files = self.store.catalogue()["files"]
        self.assertEqual(len(files), 4)
        for entry in files:
            self.assertLessEqual(entry["bytes"], 1024)
            for line in self.store.read(entry["name"], tail=True):
                self.assertEqual(json.loads(line)["event"], "message")
        newest = next(f for f in files if f["name"].endswith(".jsonl"))
        self.assertEqual(
            json.loads(self.store.read(newest["name"], tail=True)[-1])["details"]["i"],
            29,
        )

    def test_retention_and_unrelated_files(self):
        self.store.record("runtime", "old")
        path = next(iter(self.store.files()))
        old = time.time() - 8 * 86400
        os.utime(path, (old, old))
        unrelated = self.store.directory / "user-notes.txt"
        unrelated.write_text("keep", "utf-8")
        self.store.prune()
        self.assertFalse(path.exists())
        self.assertTrue(unrelated.exists())
        malformed = (
            self.store.directory / "00000000T000000000Z-1-12345678--runtime.jsonl"
        )
        malformed.write_text("{}\n", "utf-8")
        self.assertTrue(self.store.catalogue()["files"])
        self.store.MAX_TOTAL = 800
        for channel in diagnostics.CHANNELS:
            self.store.record(channel, "bounded", {"message": "x" * 100})
        self.store.prune()
        self.assertLessEqual(sum(p.stat().st_size for p in self.store.files()), 800)

    def test_storage_failure_does_not_stop_operations(self):
        with mock.patch.object(
            pathlib.Path, "open", side_effect=PermissionError("read-only")
        ):
            self.store.record("runtime", "unwritable")
        self.assertEqual(self.store.failed_records, 1)
        self.assertTrue(self.store.last_error)
        self.store.record("runtime", "recovered")
        self.assertEqual(self.store.last_error, "")
        self.assertTrue(self.store.catalogue()["files"])

    def test_file_paths_and_symlinks_are_rejected(self):
        for name in ("../server.key", "index.json", "/etc/passwd", "no.jsonl"):
            with self.assertRaises(ValueError):
                self.store.read(name)
        outside = pathlib.Path(self.temp.name) / "private.txt"
        outside.write_text("private", "utf-8")
        link = self.store.directory / f"{self.store.session}--runtime.jsonl"
        try:
            link.symlink_to(outside)
        except OSError:
            return  # Windows may not grant symbolic-link creation privileges.
        self.assertEqual(self.store.catalogue()["files"], [])
        with self.assertRaises(ValueError):
            self.store.read(link.name)

    def test_frame_sampling_preserves_significant_events(self):
        model = lab.Lab(pathlib.Path(self.temp.name) / "model")
        for _ in range(200):
            model.camera.event("frame", transport="rtsps")
        model.camera.event("unauthorized", transport="rtsps")
        self.assertEqual(model.camera.status()["counters"]["frame"], 200)
        self.assertEqual([e["event"] for e in model.camera.events], ["unauthorized"])
        entry = next(
            f for f in model.logs.catalogue()["files"] if f["channel"] == "camera"
        )
        events = [
            json.loads(line)["event"]
            for line in model.logs.read(entry["name"], tail=True)
        ]
        self.assertEqual(events.count("frame"), 1)
        self.assertIn("unauthorized", events)

    def test_http_logs_and_downloads(self):
        model = lab.Lab(pathlib.Path(self.temp.name) / "http")
        server = management.ManagementServer(("127.0.0.1", 0), model)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            model.printers["XTSIMP1S"].command(
                {"system": {"command": "unsupported", "password": "not-for-disk"}}
            )
            urllib.request.urlopen(base + "/api/state?token=not-for-disk").read()
            catalogue = json.load(urllib.request.urlopen(base + "/api/logs"))
            self.assertTrue(
                {"mqtt", "http", "runtime"}
                <= {f["channel"] for f in catalogue["files"]}
            )
            before = dict(model.printers["XTSIMP1S"].fault)
            request = urllib.request.Request(
                base + "/api/logs/client",
                data=json.dumps(
                    {
                        "message": "Synthetic error",
                        "source": "/app.js?token=not-for-disk",
                        "line": 8,
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            self.assertTrue(json.load(urllib.request.urlopen(request))["ok"])
            self.assertTrue(model.logs.alerts)
            for fault in ({"noack": "false"}, {"unknown": True}):
                request = urllib.request.Request(
                    base + "/api/action",
                    data=json.dumps(
                        {"serial": "XTSIMP1S", "action": "fault", "payload": fault}
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(request)
                self.assertEqual(ctx.exception.code, 400)
                self.assertEqual(model.printers["XTSIMP1S"].fault, before)
            for entry in catalogue["files"]:
                with urllib.request.urlopen(
                    base + "/api/logs/file?name=" + entry["name"]
                ) as response:
                    data = response.read().decode()
                    self.assertIn(
                        "attachment;", response.headers["Content-Disposition"]
                    )
                    self.assertNotIn("not-for-disk", data)
                tail = json.load(
                    urllib.request.urlopen(
                        base + "/api/logs/tail?name=" + entry["name"]
                    )
                )
                self.assertTrue(tail["lines"])
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + "/api/logs/file?name=../server.key")
            self.assertEqual(ctx.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
