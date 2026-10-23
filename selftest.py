"""Protocol and state tests. Runs on Windows/Linux without printer hardware."""

import json
import socket
import ssl
import tempfile
import threading
import time
from unittest.mock import patch
import unittest
import urllib.error
import urllib.request
import lab
import management
import model as printer_model
import mqtt


class StateTests(unittest.TestCase):
    def make(self, model="O1C", units=None, external=0):
        return printer_model.Printer(
            {
                "serial": "XTSIMTEST",
                "model": model,
                "name": "Test",
                "units": units or [],
                "external": external,
            }
        )

    def test_device_picker_fixture(self):
        specs = json.loads((printer_model.ROOT / "fixtures/device-picker.json").read_text("utf-8"))
        self.assertEqual(len(specs), 16)
        printers = [printer_model.Printer(spec) for spec in specs]
        self.assertEqual(len({p.serial for p in printers}), 16)
        self.assertEqual({p.model for p in printers}, set(printer_model.CATALOG["models"]))
        for p in printers:
            self.assertEqual(p.status()["print"]["printer_type"], p.model)

    def test_all_models_and_kinds(self):
        for code in printer_model.CATALOG["models"]:
            for kind in printer_model.KINDS:
                with self.subTest(model=code, kind=kind):
                    raw = 128 if kind == "ht" else 16 if kind == "mixed" else 0
                    p = self.make(code, [{"id": raw, "kind": kind}])
                    s = p.status()["print"]
                    self.assertEqual(
                        len(s["ams"]["ams"][0]["tray"]), printer_model.KINDS[kind][1]
                    )
                    self.assertEqual(s["printer_type"], code)
                    self.assertEqual(
                        s["support_chamber_temp_edit"],
                        p.cap["support_chamber_temp_edit"],
                    )

    def test_activity_survives_status_event_rotation(self):
        p = self.make()
        p.command({"system": {"command": "ledctrl", "led_mode": "on"}})
        incoming = p.activity[-1]
        for _ in range(250):
            p.event("TX", p.status())
        self.assertNotIn(incoming, p.events)
        self.assertEqual(p.activity[-1], incoming)
        for _ in range(45):
            p.command({"info": {"command": "get_version"}})
        self.assertEqual(len(p.activity), 40)
        ids = [e["id"] for e in p.activity]
        self.assertEqual(ids, sorted(set(ids)))

    def test_progress_uses_configured_total_layers(self):
        p = self.make()
        p.p.update(gcode_state="RUNNING", mc_percent=50, total_layer_num=90)
        p.tick(0)
        self.assertEqual(p.p["layer_num"], 45)
        p.tick(1200)
        self.assertEqual(p.p["layer_num"], 90)
        self.assertEqual(p.p["gcode_state"], "FINISH")
        p.p.update(gcode_state="RUNNING", mc_percent=50, total_layer_num=0)
        p.tick(1)
        self.assertEqual(p.p["layer_num"], 0)

    def test_no_false_success(self):
        p = self.make()
        r = p.command({"print": {"command": "not_implemented", "sequence_id": "42"}})[
            "print"
        ]
        self.assertEqual(r["sequence_id"], "42")
        self.assertEqual(r["result"], "fail")
        self.assertEqual(r["err_code"], 9001)

    def test_p1s_sentinel_no_heater(self):
        p = self.make("C12")
        self.assertEqual(p.p["chamber_temper"], 5)
        self.assertFalse(p.p["support_chamber_temp_display"])
        self.assertFalse(p.p["support_chamber_temp_edit"])
        self.assertEqual(
            p.command({"print": {"command": "set_ctt", "ctt_val": 55}})["print"][
                "result"
            ],
            "fail",
        )

    def test_dry_limits_and_stop(self):
        for kind, raw, limit in [
            ("pro", 0, 65),
            ("ht", 128, 85),
            ("ams", 0, 0),
            ("lite", 0, 0),
        ]:
            p = self.make(units=[{"id": raw, "kind": kind}])
            command = {
                "command": "ams_filament_drying",
                "ams_id": raw,
                "mode": 1,
                "temp": limit or 55,
                "duration": 4,
            }
            r = p.command({"print": command})["print"]
            self.assertEqual(r["result"], "success" if limit else "fail")
            if limit:
                self.assertEqual(p.p["ams"]["ams"][0]["dry_time"], 240)
                command["temp"] = limit + 1
                self.assertEqual(
                    p.command({"print": command})["print"]["result"], "fail"
                )
                command.update(mode=0, temp=0, duration=0)
                self.assertEqual(
                    p.command({"print": command})["print"]["result"], "success"
                )
                self.assertEqual(p.p["ams"]["ams"][0]["dry_time"], 0)

    def test_external_rfid_and_source_mapping(self):
        p = self.make(units=[{"id": 128, "kind": "ht", "bound": 1}], external=2)
        self.assertEqual(
            p.command(
                {"print": {"command": "ams_get_rfid", "ams_id": 255, "slot_id": 0}}
            )["print"]["result"],
            "fail",
        )
        for raw, i in [(128, 1), (255, 0), (254, 1)]:
            c = {
                "command": "ams_change_filament",
                "ams_id": raw,
                "slot_id": 0,
                "target": raw,
                "extruder_id": i,
            }
            self.assertEqual(p.command({"print": c})["print"]["result"], "success")
            self.assertEqual(p.p["device"]["extruder"]["info"][i]["snow"], raw << 8)
        c.update(ams_id=128, target=128, extruder_id=0)
        self.assertEqual(p.command({"print": c})["print"]["result"], "fail")

    def test_busy_cold_and_atomic_batch(self):
        p = self.make()
        self.assertEqual(
            p.command(
                {
                    "print": {
                        "command": "set_extrusion_length",
                        "extruder_index": 0,
                        "length": 10,
                    }
                }
            )["print"]["result"],
            "fail",
        )
        before = p.p["bed_target_temper"]
        p.command({"print": {"command": "gcode_line", "param": "M140 S60\nUNKNOWN"}})
        self.assertEqual(p.p["bed_target_temper"], before)
        p.p["gcode_state"] = "RUNNING"
        self.assertEqual(
            p.command({"print": {"command": "select_extruder", "extruder_index": 1}})[
                "print"
            ]["result"],
            "fail",
        )

    def test_maximum_and_independence(self):
        units = (
            [{"id": i, "kind": "pro"} for i in range(4)]
            + [{"id": 128 + i, "kind": "ht"} for i in range(8)]
            + [{"id": 16, "kind": "mixed"}]
        )
        p = self.make(units=units)
        self.assertEqual(sum(len(u["tray"]) for u in p.p["ams"]["ams"]), 28)
        q = self.make()
        p.p["gcode_state"] = "RUNNING"
        self.assertEqual(q.p["gcode_state"], "IDLE")

    def test_invalid_combinations(self):
        for units in [
            [{"id": 0, "kind": "ht"}],
            [{"id": 0, "kind": "ams"}, {"id": 0, "kind": "pro"}],
            [{"id": 128, "kind": "ht", "bound": 2}],
        ]:
            with self.assertRaises(ValueError):
                self.make(units=units)

    def test_ring_bounds(self):
        p = self.make()
        for i in range(300):
            p.command({"print": {"command": "not_supported"}})
        self.assertEqual(len(p.events), 200)

    def test_all_model_control_capabilities(self):
        for code, cap in printer_model.CATALOG["models"].items():
            with self.subTest(model=code):
                p = self.make(code, [{"id": 0, "kind": "pro"}])

                def send(action, **kw):
                    return p.command({"print": dict(command=action, **kw)})["print"][
                        "result"
                    ]

                self.assertEqual(send("gcode_line", param="M140 S60"), "success")
                self.assertEqual(send("gcode_line", param="M106 P1 S128"), "success")
                self.assertEqual(
                    send("gcode_line", param="M106 P2 S128"),
                    "success" if cap["support_aux_fan"] else "fail",
                )
                self.assertEqual(
                    send("gcode_line", param="M106 P3 S128"),
                    "success" if cap["support_chamber_fan"] else "fail",
                )
                self.assertEqual(
                    send("set_ctt", ctt_val=55),
                    "success" if cap["support_chamber_temp_edit"] else "fail",
                )
                self.assertEqual(
                    send("holder_nozzle_refresh", id=255),
                    "success" if cap["rack"] else "fail",
                )
                if cap["newProtocol"]:
                    self.assertEqual(
                        send("set_nozzle_temp", extruder_index=0, target_temp=215),
                        "success",
                    )
                    self.assertEqual(
                        send(
                            "set_nozzle_temp",
                            extruder_index=cap["nozzles"],
                            target_temp=215,
                        ),
                        "fail",
                    )
                    p.tick(60)
                    self.assertEqual(
                        send("set_extrusion_length", extruder_index=0, length=10),
                        "success",
                    )
                else:
                    self.assertEqual(send("gcode_line", param="M104 S215"), "success")

    def test_print_lifecycle_fault_and_dry_progress(self):
        p = self.make(units=[{"id": 128, "kind": "ht"}])
        p.p["gcode_state"] = "RUNNING"
        p.tick(12)
        self.assertEqual(p.p["mc_percent"], 1)
        self.assertEqual(
            p.command({"print": {"command": "pause"}})["print"]["result"], "success"
        )
        self.assertEqual(p.p["gcode_state"], "PAUSE")
        p.command({"print": {"command": "resume"}})
        self.assertEqual(p.p["gcode_state"], "RUNNING")
        p.command({"print": {"command": "stop"}})
        p.command(
            {
                "print": {
                    "command": "ams_filament_drying",
                    "ams_id": 128,
                    "mode": 1,
                    "temp": 65,
                    "duration": 1,
                }
            }
        )
        p.tick(61)
        self.assertEqual(p.p["ams"]["ams"][0]["dry_time"], 59)
        self.assertGreater(float(p.p["ams"]["ams"][0]["temp"]), 26.5)
        p.fault["error"] = 84033543
        r = p.command(
            {
                "print": {
                    "command": "set_nozzle_temp",
                    "extruder_index": 0,
                    "target_temp": 300,
                }
            }
        )
        self.assertEqual(r["print"]["err_code"], 84033543)
        self.assertEqual(p.p["device"]["extruder"]["info"][0]["temp"] >> 16, 0)

    def test_control_screen_relative_extrusion_and_axis_batch(self):
        p = self.make("C12")
        c = {"print": {"command": "gcode_line", "param": "M83 \nG0 E-10.0 F900\n"}}
        self.assertEqual(p.command(c)["print"]["result"], "fail")
        p.p["nozzle_temper"] = 215
        self.assertEqual(p.command(c)["print"]["result"], "success")
        self.assertEqual(p.p["virtual_motion"]["position"]["E"], -10)
        c["print"]["param"] = "M83\nG0 E10.0 F900\n"
        self.assertEqual(p.command(c)["print"]["result"], "success")
        self.assertEqual(p.p["virtual_motion"]["position"]["E"], 0)
        c["print"][
            "param"
        ] = "M211 S\nM211 X1 Y1 Z1\nM1002 push_ref_mode\nG91\nG1 X1.0 F3000\nM1002 pop_ref_mode\nM211 R\n"
        self.assertEqual(p.command(c)["print"]["result"], "success")
        self.assertEqual(p.p["virtual_motion"]["position"]["X"], 1)

    def test_missing_version_and_port_exclusion(self):
        p = self.make("C12")
        p.fault["noVersion"] = True
        self.assertEqual(
            p.command({"info": {"command": "get_version"}})["info"]["module"], []
        )
        self.assertNotIn("printer_type", p.status()["print"])
        p.fault["noVersion"] = False
        self.assertEqual(
            p.command({"info": {"command": "get_version"}})["info"]["module"][0][
                "product_name"
            ],
            "C12",
        )


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.lab = lab.Lab(cls.temp.name)
        cls.server = mqtt.mqtt_server(cls.lab, "127.0.0.1", 0)
        cls.port = cls.server.getsockname()[1]
        cls.http = management.ThreadingHTTPServer(
            ("127.0.0.1", 0), management.handler(cls.lab)
        )
        cls.url = "http://127.0.0.1:" + str(cls.http.server_address[1])
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for c in tuple(cls.lab.clients):
            c.close()
        cls.server.close()
        cls.http.shutdown()
        cls.http.server_close()
        cls.temp.cleanup()

    def connect(self, user="xtouch-lab", password="xtouch-lab", keep=30):
        c = ssl._create_unverified_context().wrap_socket(
            socket.create_connection(("127.0.0.1", self.port), timeout=4),
            server_hostname="localhost",
        )
        c.sendall(
            mqtt.packet(
                0x10,
                mqtt.mqtt_string("MQTT")
                + b"\x04\xc2"
                + keep.to_bytes(2, "big")
                + mqtt.mqtt_string("selftest")
                + mqtt.mqtt_string(user)
                + mqtt.mqtt_string(password),
            )
        )
        return c, mqtt.receive(c)

    def test_negotiated_keepalive(self):
        for keep, expected in [(0, None), (2, 3.0), (600, 900.0)]:
            with self.subTest(keep=keep):
                before = set(self.lab.clients)
                c, r = self.connect(keep=keep)
                self.assertEqual(r, (0x20, b"\0\0"))
                deadline = time.monotonic() + 1
                while (
                    not set(self.lab.clients) - before and time.monotonic() < deadline
                ):
                    time.sleep(0.005)
                client = next(iter(set(self.lab.clients) - before))
                self.assertEqual(client.c.gettimeout(), expected)
                c.close()

    def test_idle_connection_keeps_alive_with_ping(self):
        # A sleeping display may stop drawing while its network loop still runs.
        c, response = self.connect(keep=1)
        self.assertEqual(response, (0x20, b"\0\0"))
        try:
            for _ in range(4):
                time.sleep(0.6)
                c.sendall(mqtt.packet(0xC0))
                self.assertEqual(mqtt.receive(c), (0xD0, b""))
        finally:
            c.close()

    def test_delta_and_auto_report_do_not_emit_full_status(self):
        def action(name, payload):
            data = json.dumps(
                dict(serial="XTSIMP1S", action=name, payload=payload)
            ).encode()
            req = urllib.request.Request(
                self.url + "/api/action",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as response:
                self.assertTrue(json.load(response)["ok"])

        with patch.object(self.lab, "publish") as publish:
            original = self.lab.printers["XTSIMP1S"].p["wifi_signal"]
            delta = {"print": {"wifi_signal": "-99dBm"}}
            action("auto_report", False)
            self.assertFalse(self.lab.printers["XTSIMP1S"].auto_report)
            publish.assert_not_called()
            action("delta", delta)
            publish.assert_called_once_with("XTSIMP1S", delta)
            self.assertEqual(self.lab.printers["XTSIMP1S"].p["wifi_signal"], original)
            publish.reset_mock()
            action("auto_report", True)
            publish.assert_not_called()
            self.assertTrue(self.lab.printers["XTSIMP1S"].auto_report)

    def test_slow_client_isolation_and_queue_limit(self):
        class Connection:
            def __init__(self, blocked=False):
                self.started = threading.Event()
                self.release = threading.Event()
                self.sent = threading.Event()
                if not blocked:
                    self.release.set()

            def sendall(self, data):
                self.started.set()
                self.release.wait(3)
                self.sent.set()

            def shutdown(self, *args):
                self.release.set()

            def close(self):
                self.release.set()

        slow_socket, fast_socket = Connection(True), Connection()
        slow, fast = mqtt.Client(self.lab, slow_socket), mqtt.Client(
            self.lab, fast_socket
        )
        for client in (slow, fast):
            client.subscriptions.add("XTSIMP1S")
            self.lab.clients.add(client)
        try:
            with mqtt.LOCK:
                self.lab.publish("XTSIMP1S", {"print": {"mc_percent": 1}})
            self.assertTrue(slow_socket.started.wait(1))
            self.assertTrue(
                fast_socket.sent.wait(1), "slow subscriber blocked healthy subscriber"
            )
            for i in range(9):
                try:
                    slow.enqueue_report(b"queued")
                except OSError:
                    break
            self.assertTrue(slow.closed.is_set(), "overflow must retire slow client")
            self.assertNotIn(slow, self.lab.clients)
            self.assertFalse(fast.closed.is_set())
        finally:
            slow.close()
            fast.close()
        # Even one blocked frame is retired, without waiting for queue overflow.
        blocked_socket = Connection(True)
        with patch.object(mqtt.Client, "WRITE_TIMEOUT", 0.05):
            client = mqtt.Client(self.lab, blocked_socket)
            try:
                client.enqueue_report(b"blocked")
                self.assertTrue(blocked_socket.started.wait(1))
                self.assertTrue(
                    client.closed.wait(1), "write deadline must close blocked socket"
                )
            finally:
                client.close()

    def test_auth(self):
        c, r = self.connect(password="wrong")
        self.assertEqual(r, (0x20, b"\x00\x05"))
        c.close()

    def test_read_only_lan_verification(self):
        """LAN verification needs authentication and a matching report, no commands."""
        c, result = self.connect(user="bblp", password="wrong")
        self.assertEqual(result, (0x20, b"\x00\x05"))
        c.close()
        c, result = self.connect(user="bblp", password="12345678")
        try:
            self.assertEqual(result, (0x20, b"\0\0"))
            with patch.object(printer_model.Printer, "command", autospec=True) as command:
                c.sendall(mqtt.packet(0x82, b"\x00\x01" + mqtt.mqtt_string("device/XTSIMP1S/report") + b"\x00"))
                self.assertEqual(mqtt.receive(c), (0x90, b"\x00\x01\x00"))
                typ, body = mqtt.receive(c)
                topic, offset = mqtt.read_string(body, 0)
                self.assertEqual(typ & 0xF0, 0x30)
                self.assertEqual(topic, "device/XTSIMP1S/report")
                self.assertIn("print", json.loads(body[offset:]))
                command.assert_not_called()
                c.sendall(mqtt.packet(0x82, b"\x00\x02" + mqtt.mqtt_string("device/XTSIMUNKNOWN/report") + b"\x00"))
                self.assertEqual(mqtt.receive(c), (0x90, b"\x00\x02\x80"))
        finally:
            c.close()

    def test_second_broker_cannot_bind_same_port(self):
        with self.assertRaises(OSError):
            mqtt.mqtt_server(self.lab, "127.0.0.1", self.port)

    def test_qos1_roundtrip_and_isolation(self):
        c, r = self.connect()
        self.assertEqual(r, (0x20, b"\0\0"))
        c.sendall(
            mqtt.packet(
                0x82, b"\x00\x01" + mqtt.mqtt_string("device/XTSIMP1S/report") + b"\x01"
            )
        )
        self.assertEqual(mqtt.receive(c), (0x90, b"\x00\x01\x00"))
        typ, b = mqtt.receive(c)
        topic, offset = mqtt.read_string(b, 0)
        self.assertEqual(topic, "device/XTSIMP1S/report")
        command = {
            "system": {"command": "ledctrl", "led_mode": "on", "sequence_id": "713"}
        }
        c.sendall(
            mqtt.packet(
                0x32,
                mqtt.mqtt_string("device/XTSIMP1S/request")
                + b"\x00\x07"
                + json.dumps(command).encode(),
            )
        )
        self.assertEqual(mqtt.receive(c), (0x40, b"\x00\x07"))
        typ, b = mqtt.receive(c)
        topic, offset = mqtt.read_string(b, 0)
        self.assertEqual(json.loads(b[offset:])["system"]["sequence_id"], "713")
        mqtt.receive(c)
        self.assertEqual(
            self.lab.printers["XTSIMP1S"].p["lights_report"][0]["mode"], "on"
        )
        self.assertEqual(
            self.lab.printers["XTSIMH2C"].p["lights_report"][0]["mode"], "off"
        )
        c.sendall(mqtt.packet(0xC0))
        self.assertEqual(mqtt.receive(c), (0xD0, b""))
        c.close()

    def test_reject_unknown_topic(self):
        c, _ = self.connect()
        c.sendall(
            mqtt.packet(
                0x82, b"\x00\x01" + mqtt.mqtt_string("device/REAL/report") + b"\0"
            )
        )
        self.assertEqual(mqtt.receive(c), (0x90, b"\x00\x01\x80"))
        c.close()

    def test_http_command_reply_distinguishes_rejection(self):
        printer = self.lab.printers["XTSIMP1S"]
        for temperature, expected in [(9999, "fail"), (215, "success")]:
            request = urllib.request.Request(
                self.url + "/api/action",
                data=json.dumps(
                    {
                        "serial": printer.serial,
                        "action": "command",
                        "payload": {
                            "print": {
                                "command": "gcode_line",
                                "param": f"M104 S{temperature}",
                            }
                        },
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
            self.assertTrue(result["ok"])
            self.assertEqual(result["commandReply"]["print"]["result"], expected)
            if expected == "fail":
                self.assertTrue(result["commandReply"]["print"]["reason"])
                self.assertEqual(printer.p["nozzle_target_temper"], 0)
            else:
                self.assertEqual(printer.p["nozzle_target_temper"], 215)

    def test_http_discovery_and_cross_origin(self):
        d = json.load(urllib.request.urlopen(self.url + "/api/discovery"))
        self.assertEqual(len(d["devices"]), 2)
        request = urllib.request.Request(
            self.url + "/api/action",
            data=b'{"serial":"XTSIMP1S","action":"reset"}',
            headers={
                "Content-Type": "application/json",
                "Origin": "http://evil.example",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(request)
        self.assertEqual(cm.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.url + "/../lab.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
