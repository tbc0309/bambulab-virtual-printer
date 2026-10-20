"""Virtual printer/AMS state, capability validation and simulated controls."""

import collections
import copy
import json
import math
import pathlib
import re
import time

ROOT = pathlib.Path(__file__).resolve().parent
CATALOG = json.loads((ROOT / "catalog.json").read_text("utf-8"))
KINDS = {"ams": (1, 4), "lite": (2, 4), "pro": (3, 4), "ht": (4, 1), "mixed": (5, 4)}
COLORS = ["ED544BFF", "42ADDFFF", "F5CD4AFF", "68C799FF"]


def integer(value, low, high):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"Expected integer {low}..{high}")
    return value


def merge(target, patch):
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            merge(target[k], v)
        else:
            target[k] = copy.deepcopy(v)


class Printer:
    def __init__(self, spec):
        self.serial = spec.get("serial", "")
        if not re.fullmatch(r"XTSIM[A-Za-z0-9_-]{1,24}", self.serial):
            raise ValueError(
                "Serial must begin XTSIM and contain ASCII letters/numbers/_/-"
            )
        self.model = spec.get("model", "C12")
        if self.model not in CATALOG["models"]:
            raise ValueError("Unknown model")
        self.name = spec.get("name", self.serial)
        if not isinstance(self.name, str) or not re.fullmatch(
            r"[ -~]{1,63}", self.name
        ):
            raise ValueError("Name must be 1..63 ASCII characters")
        self.cap = CATALOG["models"][self.model]
        self.units = copy.deepcopy(spec.get("units", []))
        if not isinstance(self.units, list) or len(self.units) > 13:
            raise ValueError("At most 13 protocol-addressable units")
        ids = set()
        for u in self.units:
            if not isinstance(u, dict) or u.get("kind") not in KINDS:
                raise ValueError("Unknown AMS kind")
            raw = integer(u.get("id"), 0, 135)
            kind = u["kind"]
            valid = (
                128 <= raw <= 135
                if kind == "ht"
                else raw == 16 if kind == "mixed" else 0 <= raw <= 3
            )
            if not valid or raw in ids:
                raise ValueError("Duplicate/invalid AMS address")
            ids.add(raw)
            integer(u.get("bound", 0), 0, self.cap["nozzles"] - 1)
        self.external = integer(spec.get("external", 0), 0, self.cap["nozzles"])
        self.events = collections.deque(maxlen=200)
        self.event_id = 0
        self.activity = collections.deque(maxlen=40)
        self.commands = collections.Counter()
        self.failures = 0
        self.tx = 0
        self.rx = 0
        self.rx_web = 0
        self.last_rx = 0
        self.auto_report = True
        self.fault = {
            "error": 0,
            "noack": False,
            "drop": False,
            "delay": 0,
            "noVersion": False,
        }
        self.warnings = []
        lite = any(u["kind"] in ("lite", "mixed") for u in self.units)
        if lite != (self.cap["amsFamily"] == "lite") and self.units:
            self.warnings.append(
                "非标准 AMS 组合：仅用于协议边界测试，未声明硬件兼容。"
            )
        if lite and len(self.units) > 1:
            self.warnings.append(
                "多 Lite / Lite 与其他 AMS 混合：协议压力场景，未声明硬件兼容。"
            )
        if any(u["kind"] == "mixed" for u in self.units):
            self.warnings.append("Lite Mixed 为协议边界场景，型号/固件支持需实机确认。")
        self.p = self.initial()
        self.dry_seconds = {}

    def initial(self):
        units, mask, slots = [], 0, 0
        for u in self.units:
            raw, kind = u["id"], u["kind"]
            n, count = KINDS[kind]
            mask |= 1 << (12 if kind == "mixed" else raw if raw < 4 else raw - 124)
            slots |= (
                (15 << 24)
                if kind == "mixed"
                else (15 << (raw * 4)) if raw < 4 else (1 << (16 + raw - 128))
            )
            units.append(
                {
                    "id": str(raw),
                    "info": format(n | (u.get("bound", 0) << 8), "x"),
                    "temp": "26.5",
                    "humidity": "2",
                    "humidity_raw": "23",
                    "dry_time": 0,
                    "dry_setting": {"dry_temperature": 55, "dry_duration": 4},
                    "tray": [self.tray(i) for i in range(count)],
                }
            )
        p = {
            "command": "push_status",
            "printer_type": self.model,
            "gcode_state": "IDLE",
            "nozzle_temper": 26,
            "nozzle_target_temper": 0,
            "bed_temper": 25,
            "bed_target_temper": 0,
            "wifi_signal": "-45dBm",
            "spd_lvl": 2,
            "fan_gear": 0,
            "home_flag": 0,
            "mc_percent": 0,
            "mc_remaining_time": 0,
            "layer_num": 0,
            "total_layer_num": 200,
            "subtask_name": "Virtual print job",
            "print_error": 0,
            "hms": [],
            "ams_status": 0,
            "hw_switch_state": 0,
            "virtual_motion": {
                "absolute": True,
                "extruderRelative": False,
                "position": {"X": 0, "Y": 0, "Z": 0, "E": 0},
                "homed": False,
            },
            "lights_report": [{"node": "chamber_light", "mode": "off"}],
            "ams": {
                "ams_exist_bits": format(mask, "x"),
                "tray_exist_bits": format(slots, "x"),
                "tray_now": "255",
                "tray_tar": "255",
                "tray_reading_bits": "0",
                "ams": units,
            },
        }
        for key in (
            "support_chamber_temp_display",
            "support_chamber_temp_edit",
            "support_aux_fan",
            "support_chamber_fan",
        ):
            p[key] = self.cap[key]
        if self.cap["support_chamber_temp_edit"]:
            p["support_chamber_temp_edit_range"] = self.cap["chamberRange"]
            p["ctt"] = 0
        # Preserve the real P1S unavailable sentinel for parser regression tests.
        p["chamber_temper"] = 5 if self.model in ("C11", "C12") else 28
        if self.cap["newProtocol"]:
            p["device"] = {
                "extruder": {
                    "state": self.cap["nozzles"],
                    "info": [
                        {
                            "id": i,
                            "temp": 26,
                            "snow": 65535,
                            "star": 65535,
                            "info": 10,
                            "stat": 0,
                        }
                        for i in range(self.cap["nozzles"])
                    ],
                }
            }
            if self.cap["support_chamber_temp_display"]:
                p["device"]["ctc"] = {"info": {"temp": 28}}
            if self.cap["rack"]:
                p["device"]["holder"] = {"stat": 0, "pos": 1, "info": 1}
                p["device"]["nozzle"] = {
                    "state": 0,
                    "info": [
                        {
                            "id": 16 + i,
                            "diameter": 0.4 if i % 2 == 0 else 0.6,
                            "type": "hardened_steel",
                        }
                        for i in range(6)
                    ],
                }
        if self.external:
            if self.cap["newProtocol"]:
                p["vir_slot"] = [
                    dict(self.tray(i), id=str(255 - i)) for i in range(self.external)
                ]
            else:
                p["vt_tray"] = dict(self.tray(0), id="254")
        return p

    @staticmethod
    def tray(i):
        return {
            "id": str(i),
            "tray_type": "PETG" if i % 2 else "PLA",
            "tray_color": COLORS[i % 4],
            "remain": [9, 18, 72, 100][i % 4],
            "nozzle_temp_min": 200,
            "nozzle_temp_max": 220,
        }

    def event(self, direction, payload, note=""):
        self.event_id += 1
        self.events.append(
            {
                "time": time.time(),
                "id": self.event_id,
                "direction": direction,
                "payload": copy.deepcopy(payload),
                "note": note,
            }
        )
        if direction == "RX":
            self.activity.append(self.events[-1])
        if getattr(self, "log", None):
            self.log.record(
                "mqtt",
                direction,
                {"serial": self.serial, "payload": payload, "note": note},
                level=(
                    "warning"
                    if direction == "CHECK"
                    and any(
                        isinstance(value, dict) and value.get("result") == "fail"
                        for value in payload.values()
                    )
                    else "info"
                ),
            )

    def status(self):
        value = copy.deepcopy(self.p)
        if self.fault["noVersion"]:
            value.pop("printer_type", None)
        return {"print": value}

    def command(self, data, source="MQTT"):
        self.rx += 1
        self.rx_web += source == "WEB"
        self.last_rx = time.time()
        group = next(
            (
                g
                for g in ("print", "system", "info", "pushing")
                if isinstance(data.get(g), dict)
            ),
            None,
        )
        c = data.get(group, {})
        action = c.get("command", "")
        if not isinstance(action, str) or len(action) > 64:
            raise ValueError("Invalid command name")
        self.commands[action] += 1
        if len(self.commands) > 64:
            del self.commands[next(iter(self.commands))]
        self.event("RX", data, source)
        if group == "info" and action == "get_version":
            modules = (
                []
                if self.fault["noVersion"]
                else [
                    {
                        "name": "ota",
                        "product_name": self.model,
                        "sw_ver": "99.00.00.00",
                        "sn": self.serial,
                    }
                ]
            )
            return {
                "info": {
                    "command": action,
                    "sequence_id": c.get("sequence_id", ""),
                    "module": modules,
                }
            }
        if (
            group == "pushing" and action in ("pushall", "start")
        ) or action == "push_status":
            return self.status()
        err, reason = self.fault["error"], ""
        if not err:
            try:
                self.apply(action, c)
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                err, reason = 9001, str(exc)
        if err:
            self.failures += 1
        reply = {
            group
            or "print": {
                "command": action,
                "sequence_id": c.get("sequence_id", ""),
                "result": "fail" if err else "success",
                "err_code": err,
            }
        }
        if reason:
            reply[group or "print"]["reason"] = "SIM: " + reason
        self.event(
            "CHECK",
            reply,
            reason or ("Injected rejection" if err else "Validated and applied"),
        )
        return reply

    def apply(self, action, c):
        p, cap = self.p, self.cap
        busy = p["gcode_state"] in ("RUNNING", "PREPARE", "PAUSE")
        if (
            action
            in (
                "ams_change_filament",
                "select_extruder",
                "set_extrusion_length",
                "nozzle_holder_ctrl",
            )
            and busy
        ):
            raise ValueError("Motion blocked while printing/preparing/paused")
        if action == "set_nozzle_temp":
            i = integer(c["extruder_index"], 0, cap["nozzles"] - 1)
            t = integer(c["target_temp"], 0, cap["nozzleRange"][1])
            if not cap["newProtocol"]:
                raise ValueError("Legacy printer expects G-code temperature command")
            n = p["device"]["extruder"]["info"][i]
            n["temp"] = (t << 16) | (n["temp"] & 65535)
        elif action == "select_extruder":
            i = integer(c["extruder_index"], 0, cap["nozzles"] - 1)
            if cap["nozzles"] < 2:
                raise ValueError("No tool switching on single nozzle")
            p["device"]["extruder"]["state"] = cap["nozzles"] | (i << 4)
        elif action == "set_extrusion_length":
            i = integer(c["extruder_index"], 0, cap["nozzles"] - 1)
            integer(c["length"], -100, 100)
            if (p["device"]["extruder"]["info"][i]["temp"] & 65535) < 170:
                raise ValueError("Nozzle below 170C")
        elif action == "set_ctt":
            if not cap["support_chamber_temp_edit"]:
                raise ValueError("Chamber has no active heater")
            t = integer(c["ctt_val"], *cap["chamberRange"])
            p["ctt"] = t
            if "ctc" in p.get("device", {}):
                p["device"]["ctc"]["info"]["temp"] = (t << 16) | (
                    p["device"]["ctc"]["info"]["temp"] & 65535
                )
        elif action in ("ams_get_rfid", "ams_change_filament", "ams_filament_drying"):
            raw = integer(c["ams_id"], 0, 255)
            u = next((u for u in p["ams"]["ams"] if int(u["id"]) == raw), None)
            ext = raw in (
                [255 - i for i in range(self.external)]
                if cap["newProtocol"]
                else [254] if self.external else []
            )
            if u is None and not ext:
                raise ValueError("AMS/external source does not exist")
            if action == "ams_filament_drying":
                kind = int(u["info"], 16) & 15 if u else 0
                if kind not in (3, 4):
                    raise ValueError("This AMS cannot dry filament")
                mode = integer(c["mode"], 0, 1)
                if mode and busy:
                    raise ValueError("Dry start blocked during active print")
                if mode:
                    integer(c["temp"], 45, 85 if kind == 4 else 65)
                    integer(c["duration"], 1, 24)
                u["info"] = format(
                    (int(u["info"], 16) & ~240) | (32 if mode else 0), "x"
                )
                u["dry_time"] = c["duration"] * 60 if mode else 0
                self.dry_seconds[raw] = c["duration"] * 3600 if mode else 0
                u["dry_setting"] = {
                    "dry_temperature": c.get("temp", 0),
                    "dry_duration": c.get("duration", 0),
                }
            else:
                slot = integer(c["slot_id"], 0, 255)
                unloading = (
                    action == "ams_change_filament"
                    and c.get("target") == 255
                    and slot == 255
                )
                if not unloading and (slot >= (len(u["tray"]) if u else 1)):
                    raise ValueError("Slot outside source")
                if action == "ams_get_rfid":
                    if not u or (int(u["info"], 16) & 15) == 2:
                        raise ValueError("RFID unavailable for external/AMS Lite")
                    p["ams"]["tray_reading_bits"] = "0"
                else:
                    i = integer(c.get("extruder_id", 0), 0, cap["nozzles"] - 1)
                    if (
                        u
                        and cap["nozzles"] > 1
                        and ((int(u["info"], 16) >> 8) & 15) != i
                    ):
                        raise ValueError("AMS is bound to another extruder")
                    target = integer(c["target"], 0, 255)
                    expected = (
                        255
                        if unloading
                        else raw if ext or raw >= 128 else raw * 4 + slot
                    )
                    if target != expected:
                        raise ValueError("Target does not match physical AMS/slot")
                    p["ams"]["tray_now"] = p["ams"]["tray_tar"] = str(target)
                    if cap["newProtocol"]:
                        p["device"]["extruder"]["info"][i]["snow"] = (
                            65535 if unloading else (raw << 8) | slot
                        )
        elif action in (
            "holder_nozzle_refresh",
            "nozzle_info_confirm",
            "nozzle_holder_ctrl",
        ):
            if not cap["rack"]:
                raise ValueError("No H2C hotend rack")
            if action == "nozzle_holder_ctrl":
                integer(c["action"], 0, 2)
                p["device"]["holder"]["pos"] = c["action"]
            else:
                if c.get("id") not in (*range(16, 22), 255):
                    raise ValueError("Unknown rack position")
        elif action == "ledctrl":
            if c.get("led_mode") not in ("on", "off"):
                raise ValueError("Unsupported light mode")
            p["lights_report"][0]["mode"] = c["led_mode"]
        elif action == "print_speed":
            p["spd_lvl"] = integer(int(c["param"]), 1, 4)
        elif action in ("pause", "resume", "stop"):
            if action == "pause" and p["gcode_state"] != "RUNNING":
                raise ValueError("Not printing")
            if action == "resume" and p["gcode_state"] != "PAUSE":
                raise ValueError("Not paused")
            p["gcode_state"] = {"pause": "PAUSE", "resume": "RUNNING", "stop": "IDLE"}[
                action
            ]
        elif action == "clean_print_error":
            p["print_error"] = 0
        elif action == "gcode_line":
            self.gcode(c.get("param", ""))
        elif action == "ams_control":
            if c.get("param") not in ("resume", "reset", "pause", "done"):
                raise ValueError("Unknown AMS action")
            p["ams_status"] = 0
        else:
            raise ValueError("Command not implemented; no false success")

    def gcode(self, text):
        # Validate the entire batch on a copy before committing any side effect.
        p = copy.deepcopy(self.p)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            motion = p["virtual_motion"]
            if line in ("M82", "M83"):
                motion["extruderRelative"] = line == "M83"
                continue
            m = re.fullmatch(r"M(104|109|140|190)\s+S(-?\d+)(?:\s.*)?", line)
            if m:
                bed = m[1] in ("140", "190")
                t = integer(
                    int(m[2]),
                    0,
                    self.cap["bedMax"] if bed else self.cap["nozzleRange"][1],
                )
                p["bed_target_temper" if bed else "nozzle_target_temper"] = t
                continue
            m = re.fullmatch(r"M106\s+P([123])\s+S(\d+)", line)
            if m:
                i, v = int(m[1]) - 1, integer(int(m[2]), 0, 255)
                if (
                    i == 1
                    and not self.cap["support_aux_fan"]
                    or i == 2
                    and not self.cap["support_chamber_fan"]
                ):
                    raise ValueError("Fan unavailable")
                p["fan_gear"] = (p["fan_gear"] & ~(255 << (8 * i))) | (v << (8 * i))
                continue
            if re.match(r"^(G0?[01]|G28|G90|G91|M211|M1002)\b", line):
                if p["gcode_state"] in ("RUNNING", "PREPARE", "PAUSE"):
                    raise ValueError("Motion blocked")
                if re.match(r"^G0?[01]\b", line):
                    axes = re.findall(r"\b([XYZE])(-?\d+(?:\.\d+)?)", line)
                    if (
                        any(axis == "E" for axis, _ in axes)
                        and p["nozzle_temper"] < 170
                    ):
                        raise ValueError("Nozzle below 170C")
                    for axis, value in axes:
                        number = float(value)
                        if not -10000 <= number <= 10000:
                            raise ValueError("Virtual move outside test bounds")
                        relative = (
                            motion["extruderRelative"]
                            if axis == "E"
                            else not motion["absolute"]
                        )
                        motion["position"][axis] = (
                            motion["position"][axis] + number if relative else number
                        )
                elif line == "G90":
                    motion["absolute"] = True
                elif line == "G91":
                    motion["absolute"] = False
                elif line.startswith("G28"):
                    for axis in "XYZ":
                        motion["position"][axis] = 0
                    motion["homed"] = True
                # Positions are logical counters, not a physical motion/limit model.
                continue
            raise ValueError("G-code not modeled: " + line[:60])
        self.p = p

    def tick(self, elapsed):
        p = self.p

        def approach(a, t):
            target = max(25, t)
            return round(a + max(-elapsed * 4, min(elapsed * 4, target - a)), 1)

        for key in ("nozzle", "bed"):
            p[key + "_temper"] = approach(p[key + "_temper"], p[key + "_target_temper"])
        for n in p.get("device", {}).get("extruder", {}).get("info", []):
            target = n["temp"] >> 16
            n["temp"] = (target << 16) | int(approach(n["temp"] & 65535, target))
        extruder = p.get("device", {}).get("extruder")
        if extruder:
            active = (extruder["state"] >> 4) & 15
            if active < len(extruder["info"]):
                packed = extruder["info"][active]["temp"]
                p["nozzle_temper"] = packed & 65535
                p["nozzle_target_temper"] = packed >> 16
        ctc = p.get("device", {}).get("ctc", {}).get("info")
        if ctc:
            target = ctc["temp"] >> 16
            actual = int(approach(ctc["temp"] & 65535, target))
            ctc["temp"] = (target << 16) | actual
            p["chamber_temper"] = actual
        for u in p.get("ams", {}).get("ams", []):
            raw = int(u["id"])
            left = self.dry_seconds.get(raw, 0)
            if left > 0:
                left = max(0, left - elapsed)
                self.dry_seconds[raw] = left
                u["dry_time"] = math.ceil(left / 60)
                u["temp"] = str(
                    approach(float(u["temp"]), u["dry_setting"]["dry_temperature"])
                )
                if not left:
                    u["info"] = format(int(u["info"], 16) & ~240, "x")
        if p["gcode_state"] == "RUNNING":
            p["mc_percent"] = min(100, p["mc_percent"] + elapsed / 12)
            layers = p.get("total_layer_num")
            if isinstance(layers, int) and not isinstance(layers, bool) and layers >= 0:
                p["layer_num"] = min(
                    layers, max(0, int(p["mc_percent"] * layers / 100))
                )
            p["mc_remaining_time"] = int((100 - p["mc_percent"]) / 5)
            if p["mc_percent"] >= 100:
                p["gcode_state"] = "FINISH"
