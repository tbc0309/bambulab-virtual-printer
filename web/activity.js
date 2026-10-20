// Map incoming client requests to controls without changing simulator state.
export function commandActivity(payload) {
  const c = payload.print || payload.system || {};
  const ids = [],
    values = {};
  const mark = (...names) => ids.push(...names);
  const value = (name, v) => {
    if (v !== undefined) {
      values[name] = String(v);
      mark(name);
    }
  };
  switch (c.command) {
    case "ledctrl":
      mark("clientLight");
      break;
    case "print_speed":
      mark("clientSpeed");
      break;
    case "pause":
      mark("clientPause");
      break;
    case "resume":
      mark("clientResume");
      break;
    case "stop":
      mark("clientStop");
      break;
    case "set_nozzle_temp":
      mark("setNozzle");
      value("nozzle", c.extruder_index);
      value("targetTemp", c.target_temp);
      break;
    case "select_extruder":
      mark("switchNozzle");
      value("nozzle", c.extruder_index);
      break;
    case "set_ctt":
      mark("heatChamber");
      break;
    case "set_extrusion_length":
      mark(Number(c.length) < 0 ? "retract" : "extrude");
      value("nozzle", c.extruder_index);
      break;
    case "ams_get_rfid":
    case "ams_change_filament":
    case "ams_filament_drying":
      value("source", c.ams_id);
      if (Number(c.slot_id) !== 255) value("slot", c.slot_id);
      value("nozzle", c.extruder_id);
      if (c.command === "ams_get_rfid") mark("reread");
      else if (c.command === "ams_change_filament")
        mark(
          Number(c.target) === 255 && Number(c.slot_id) === 255
            ? "unload"
            : "load",
        );
      else {
        mark(Number(c.mode) === 0 ? "dryStop" : "dryStart");
        if (Number(c.mode)) {
          value("dryTemp", c.temp);
          value("dryHours", c.duration);
        }
      }
      break;
    case "clean_print_error":
    case "ams_control":
      mark("sendCommand");
      break;
    case "holder_nozzle_refresh":
    case "nozzle_info_confirm":
    case "nozzle_holder_ctrl":
      mark("rack");
      break;
    case "gcode_line":
      for (const line of String(c.param || "")
        .split(/\r?\n/)
        .map((line) => line.trim())) {
        let m;
        if ((m = line.match(/^M(?:104|109)\s+S(-?\d+)/))) {
          mark("setNozzle");
          value("targetTemp", m[1]);
        }
        if (/^M(?:140|190)\s/.test(line)) mark("heatBed");
        if ((m = line.match(/^M106\s+P([123])\s+S(\d+)/))) {
          mark("setFan");
          value("fan", m[1]);
          value("fanSpeed", m[2]);
        }
        if (/^G28\b/.test(line)) mark("homeAxes");
        if (/^G0?[01]\b/.test(line)) {
          m = line.match(/\bE(-?\d+(?:\.\d+)?)/);
          mark(
            m ? (Number(m[1]) < 0 ? "retract" : "extrude") : "motionActivity",
          );
        }
      }
      break;
  }
  return { command: c.command, ids: [...new Set(ids)], values };
}

export function activityTracker() {
  const seen = new Map();
  return (printers, selected) => {
    const fresh = [];
    for (const p of printers) {
      const events = p.activity || p.events;
      const previous = seen.get(p.serial);
      const keys = new Set(events.map((e) => `${e.time}:${e.id}`));
      if (previous && p.serial === selected)
        for (const e of events)
          if (
            !previous.has(`${e.time}:${e.id}`) &&
            e.direction === "RX" &&
            e.note === "MQTT"
          )
            fresh.push(e);
      seen.set(p.serial, keys);
    }
    for (const serial of seen.keys())
      if (!printers.some((p) => p.serial === serial)) seen.delete(serial);
    return fresh;
  };
}
