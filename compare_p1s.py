"""Read-only P1S LAN/Cloud comparison. Never exports raw topics or credentials."""

import argparse
import json
import pathlib
import socket
import ssl
import time
from model import Printer, merge
from mqtt import mqtt_string, packet, receive, read_string
from diagnostics import LogStore
from model import ROOT


def snapshot(host, user, password, serial, cloud):
    context = (
        ssl.create_default_context() if cloud else ssl._create_unverified_context()
    )
    state, replies = {}, []
    with context.wrap_socket(
        socket.create_connection((host, 8883), timeout=6), server_hostname=host
    ) as c:
        c.settimeout(3)
        c.sendall(
            packet(
                0x10,
                mqtt_string("MQTT")
                + b"\x04\xc2\x00\x1e"
                + mqtt_string("XTouchReadOnlyAudit")
                + mqtt_string(user)
                + mqtt_string(password),
            )
        )
        if receive(c) != (0x20, b"\0\0"):
            raise ValueError("MQTT authentication rejected")
        c.sendall(
            packet(0x82, b"\0\1" + mqtt_string("device/" + serial + "/report") + b"\0")
        )
        for group, command in [("info", "get_version"), ("pushing", "pushall")]:
            data = {group: {"command": command, "sequence_id": "98007123"}}
            c.sendall(
                packet(
                    0x30,
                    mqtt_string("device/" + serial + "/request")
                    + json.dumps(data).encode(),
                )
            )
        until = time.monotonic() + 16
        while time.monotonic() < until:
            try:
                kind, b = receive(c)
            except socket.timeout:
                if state:
                    break
                continue
            if kind >> 4 != 3:
                continue
            _, offset = read_string(b, 0)
            if (kind >> 1) & 3:
                offset += 2
            data = json.loads(b[offset:])
            if isinstance(data.get("print"), dict):
                merge(state, data["print"])
            for group in ("info", "pushing"):
                if isinstance(data.get(group), dict):
                    replies.append(data[group].get("command", ""))
            if state and "nozzle_temper" in state and "ams" in state:
                break
        c.sendall(packet(0xE0))
    if not state:
        raise ValueError("No print status within 16 seconds")
    keys = [
        "chamber_temper",
        "nozzle_temper",
        "nozzle_target_temper",
        "bed_temper",
        "bed_target_temper",
        "fan_gear",
        "gcode_state",
        "spd_lvl",
        "print_error",
    ]
    return {
        "received": True,
        "fields": {k: state[k] for k in keys if k in state},
        "fieldNames": sorted(state.keys()),
        "amsUnits": len(state.get("ams", {}).get("ams", [])),
        "amsKinds": [
            int(str(u.get("info", "1")), 16) & 15
            for u in state.get("ams", {}).get("ams", [])
        ],
        "externalPresent": bool(
            state.get("vt_tray", {}).get("tray_type") or state.get("vir_slot")
        ),
        "queryReplies": replies,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        required=True,
        type=pathlib.Path,
        help="Private xTouch configuration backup; never commit this file",
    )
    parser.add_argument(
        "--output", type=pathlib.Path, help="Optional extra summary snapshot"
    )
    parser.add_argument("--data-dir", type=pathlib.Path, default=ROOT / "data")
    args = parser.parse_args()
    logs = LogStore(args.data_dir)
    cfg = json.loads(args.config.read_text("utf-8"))
    result = {
        "mode": "read-only; only info/get_version and pushing/pushall",
        "transports": {},
    }
    serials = {
        p["serialNumber"]
        for p in cfg["printers"]
        if p.get("printerModel") in ("C12", "P1S", "Bambu Lab P1S")
    }
    for mode in ("lan", "cloud"):
        p = next(
            (
                p
                for p in cfg["printers"]
                if p.get("mode", "lan") == mode and p["serialNumber"] in serials
            ),
            None,
        )
        if not p:
            continue
        try:
            if mode == "lan":
                value = snapshot(
                    p["host"], "bblp", p["accessCode"], p["serialNumber"], False
                )
            else:
                a = next(a for a in cfg["cloudAccounts"] if a["id"] == p["accountId"])
                value = snapshot(
                    (
                        "cn.mqtt.bambulab.com"
                        if a.get("region", "cn") == "cn"
                        else "us.mqtt.bambulab.com"
                    ),
                    a["userId"],
                    a["token"],
                    p["serialNumber"],
                    True,
                )
            result["transports"][mode] = value
        except Exception as e:
            result["transports"][mode] = {
                "received": False,
                "errorType": type(e).__name__,
            }
    virtual = Printer({"serial": "XTSIMBASELINE", "model": "C12"}).p
    result["simulator"] = {
        "chamberSentinel": virtual["chamber_temper"],
        "chamberDisplay": virtual["support_chamber_temp_display"],
        "chamberHeater": virtual["support_chamber_temp_edit"],
        "fields": sorted(virtual),
    }
    logs.record("comparison", "read-only-result", result)
    logs.catalogue()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
        )
    print(
        json.dumps(
            {
                "transports": {
                    k: v.get("received", False) for k, v in result["transports"].items()
                },
                "outputWritten": bool(args.output),
                "logFile": f"{logs.session}--comparison.jsonl",
            }
        )
    )


if __name__ == "__main__":
    main()
