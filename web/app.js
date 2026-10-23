import { commandActivity, activityTracker } from "./activity.js";
import { sourceText, translate } from "./i18n.js";
import {
  $,
  escape,
  api,
  notify,
  setText,
  operationFeedback,
  clearOperationFeedback,
  downloadJSON,
  reportClientError,
  highlightActivity,
} from "./ui.js";

let catalog,
  state,
  selected = "",
  draft = [],
  editing = false,
  refreshing = false;
const incoming = activityTracker();
let clientActivityTimer;
function showClientActivity(events) {
  for (const event of events) {
    const activity = commandActivity(event.payload);
    // Never overwrite a form while the operator is editing it.
    const editingControls =
      $("workspace").contains(document.activeElement) &&
      document.activeElement.matches("input,select,textarea");
    if (!editingControls) {
      for (const id of [
        "nozzle",
        "source",
        "slot",
        "targetTemp",
        "fan",
        "fanSpeed",
        "dryTemp",
        "dryHours",
      ]) {
        const element = $(id),
          value = activity.values[id];
        if (value === undefined || !element) continue;
        if (
          element.tagName === "SELECT" &&
          ![...element.options].some((o) => o.value === value)
        )
          continue;
        element.value = value;
        if (id === "source") sourceChanged();
        if (id === "slot") loadFilament();
      }
    }
    for (const id of activity.ids) {
      const element = $(id);
      if (!element) continue;
      highlightActivity(element);
    }
    if (activity.ids.length) {
      setText("clientActivity", "控制屏操作：" + activity.command);
      clearTimeout(clientActivityTimer);
      clientActivityTimer = setTimeout(
        () => setText("clientActivity", ""),
        8000,
      );
    }
  }
}
function current() {
  return state?.printers.find((p) => p.serial === selected);
}
async function act(action, payload) {
  const feedback = operationFeedback();
  try {
    const response = await api("/api/action", {
      serial: selected,
      action,
      payload,
    });
    const rejected = Object.values(response.commandReply || {}).find(
      (result) => result?.result === "fail",
    );
    if (rejected)
      feedback(
        rejected.reason?.replace(/^SIM: /, "") ||
          `模拟器拒绝命令，错误码 ${rejected.err_code}`,
        true,
      );
    else feedback("已发送虚拟操作");
    await refresh();
  } catch (e) {
    feedback(e.message, true);
  }
}
function cmd(command, values = {}, group = "print") {
  return act("command", {
    [group]: {
      command,
      sequence_id: String(Date.now() % 1000000000),
      ...values,
    },
  });
}
function patch(p) {
  return act("patch", p);
}
function label(u) {
  const raw = u.id;
  return u.kind === "ht"
    ? "HT-" + (raw - 127)
    : u.kind === "lite" || u.kind === "mixed"
      ? "L-" + (raw === 16 ? 1 : raw + 1)
      : (u.kind === "pro" ? "2-" : "1-") + String.fromCharCode(65 + raw);
}
function fillModels() {
  for (const [code, c] of Object.entries(catalog.models))
    $("model").add(new Option(c.name + " · " + code, code));
}
function drawDraft() {
  $("unitDraft").replaceChildren();
  if (!draft.length) {
    const empty = document.createElement("p");
    empty.className = "status-box tone-info";
    empty.textContent =
      "尚未添加 AMS 单元；可选择类型后添加，也可仅使用外置耗材。";
    $("unitDraft").append(empty);
  }
  draft.forEach((u, i) => {
    const row = document.createElement("div");
    row.className = "unitDraft";
    const title = document.createElement("span");
    title.textContent = `${label(u)} · ${catalog.ams[u.kind].name} · ID ${u.id}`;
    row.append(title);
    const side = document.createElement("div"),
      bind = document.createElement("select");
    for (let n = 0; n < catalog.models[$("model").value].nozzles; n++)
      bind.add(new Option("绑定 T" + n, n));
    side.className = "inline-controls";
    bind.value = u.bound || 0;
    bind.setAttribute("aria-label", label(u) + " 绑定喷嘴");
    bind.onchange = () => (u.bound = Number(bind.value));
    side.append(bind);
    const b = document.createElement("button");
    b.textContent = "移除";
    b.onclick = () => {
      draft.splice(i, 1);
      drawDraft();
    };
    side.append(b);
    row.append(side);
    $("unitDraft").append(row);
  });
}
$("model").onchange = () => {
  draft.forEach((u) => (u.bound = 0));
  $("external").value = 0;
  drawDraft();
};
$("create").onclick = () => {
  editing = false;
  $("serial").disabled = false;
  $("serial").value = "XTSIM" + String(Date.now()).slice(-6);
  $("name").value = "Virtual printer";
  $("model").value = "C12";
  $("external").value = 0;
  draft = [];
  $("builder").hidden = false;
  drawDraft();
};
$("closeBuilder").onclick = () => ($("builder").hidden = true);
$("presets").onclick = async () => {
  const feedback = operationFeedback($("presets"));
  try {
    for (const [model, cap] of Object.entries(catalog.models)) {
      const serial = "XTSIM" + model.replaceAll("-", "");
      if (state.printers.some((p) => p.serial === serial)) continue;
      await api("/api/printer", {
        serial,
        model,
        name: cap.name + " sample",
        units: [
          {
            id: 0,
            kind:
              cap.amsFamily === "lite"
                ? "lite"
                : cap.newProtocol
                  ? "pro"
                  : "ams",
          },
        ],
        external: 0,
      });
    }
    feedback("已生成全部机型样例");
    await refresh();
  } catch (e) {
    feedback(e.message, true);
  }
};
$("maximum").onclick = async () => {
  const feedback = operationFeedback($("maximum"));
  try {
    await api("/api/printer", {
      serial: "XTSIMMAXIMUM",
      model: "O1D",
      name: "Maximum AMS stress",
      units: [
        ...Array.from({ length: 4 }, (_, id) => ({
          id,
          kind: "pro",
          bound: id % 2,
        })),
        ...Array.from({ length: 8 }, (_, i) => ({
          id: 128 + i,
          kind: "ht",
          bound: i % 2,
        })),
        { id: 16, kind: "mixed" },
      ],
      external: 2,
    });
    selected = "XTSIMMAXIMUM";
    await refresh();
    drawWorkspace(true);
    feedback("最大 AMS 压力组合已生成");
  } catch (e) {
    feedback(e.message, true);
  }
};
$("addUnit").onclick = () => {
  const kind = $("unitKind").value;
  const ids =
    kind === "ht"
      ? [128, 129, 130, 131, 132, 133, 134, 135]
      : kind === "mixed"
        ? [16]
        : [0, 1, 2, 3];
  const id = ids.find((n) => !draft.some((u) => u.id === n));
  if (id === undefined)
    return operationFeedback($("addUnit"))(
      "该类型协议地址已用完，请选择其它类型或移除已有单元",
      true,
    );
  draft.push({ id, kind, bound: 0 });
  drawDraft();
};
$("saveCombination").onclick = async () => {
  const feedback = operationFeedback($("saveCombination"));
  const listFeedback = operationFeedback($("printersPanel"));
  try {
    await api("/api/printer", {
      serial: $("serial").value,
      model: $("model").value,
      name: $("name").value,
      units: draft,
      external: Number($("external").value),
    });
    selected = $("serial").value;
    $("builder").hidden = true;
    listFeedback("组合已保存");
    await refresh();
  } catch (e) {
    feedback(e.message, true);
  }
};
$("edit").onclick = () => {
  const p = current();
  editing = true;
  $("serial").value = p.serial;
  $("serial").disabled = true;
  $("model").value = p.model;
  $("name").value = p.name;
  $("external").value = p.external;
  draft = structuredClone(p.units);
  $("builder").hidden = false;
  drawDraft();
  $("builder").scrollIntoView({ behavior: "smooth" });
};
$("delete").onclick = async () => {
  if (!confirm(translate("删除此虚拟组合？"))) return;
  const feedback = operationFeedback($("delete"));
  const listFeedback = operationFeedback($("printersPanel"));
  try {
    await api("/api/delete", { serial: selected });
    selected = "";
    listFeedback("组合已删除");
    await refresh();
  } catch (e) {
    feedback(e.message, true);
  }
};
$("reset").onclick = () => act("reset");
$("publish").onclick = () => act("publish");
function button(root, text, click, enabled = true) {
  const b = document.createElement("button");
  b.textContent = text;
  b.onclick = click;
  b.disabled = !enabled;
  root.append(b);
  return b;
}
function drawCards() {
  const root = $("combinations");
  const focused = root.contains(document.activeElement)
    ? document.activeElement.dataset.serial
    : null;
  root.replaceChildren();
  if (!state.printers.length) {
    const empty = document.createElement("p");
    empty.className = "status-box tone-info";
    empty.textContent = "暂无打印机组合。点击“新建组合”或生成样例后开始测试。";
    root.append(empty);
  }
  for (const p of state.printers) {
    const card = document.createElement("article");
    card.className = "combination" + (selected === p.serial ? " active" : "");
    card.tabIndex = 0;
    card.dataset.serial = p.serial;
    card.setAttribute("aria-pressed", String(selected === p.serial));
    card.setAttribute("role", "button");
    card.setAttribute("aria-label", p.name);
    card.setAttribute("data-i18n-skip-attrs", "aria-label");
    card.innerHTML = `<div class="cover"><img src="assets/printer_${escape(p.model)}.png" alt="${escape(p.cap.name)}"><div><h3 data-i18n-skip>${escape(p.name)}</h3><p>${escape(p.cap.name)} · <code>${escape(p.model)}</code></p><code>${escape(p.serial)}</code><p><span class="badge ${p.mqtt.connected ? "online" : "tone-warning"}">${p.mqtt.connected ? "MQTT 已连接" : "等待控制屏"}</span></p></div></div><div class="chips">${p.units.map((u) => `<span>${label(u)}</span>`).join("")}${p.external ? `<span>外置 × ${p.external}</span>` : ""}</div><div class="muted">${escape(p.state.gcode_state)} · RX ${p.mqtt.rx} / TX ${p.mqtt.tx} · 拒绝 ${p.mqtt.failures}</div><div class="muted">${p.fault.drop ? "分析：停止推送" : p.fault.noack ? "分析：命令无回复" : p.fault.error ? "分析：注入错误 " + p.fault.error : p.mqtt.failures ? "分析：有命令被拒绝，请查看记录" : p.mqtt.connected ? "分析：MQTT 通道正常" : "分析：等待控制屏连接"}</div>${p.warnings.map((x) => `<div class="warning">${escape(x)}</div>`).join("")}`;
    card.onclick = () => {
      selected = p.serial;
      drawCards();
      drawWorkspace(true);
      $("workspace").scrollIntoView({ behavior: "smooth", block: "start" });
    };
    card.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        card.click();
      }
    };
    root.append(card);
    if (focused === p.serial) card.focus({ preventScroll: true });
  }
}
function drawWorkspace(rebuild = false) {
  const p = current();
  $("workspace").hidden = !p;
  if (!p) return;
  if (rebuild) {
    clearOperationFeedback($("workspace"));
    clearTimeout(clientActivityTimer);
    setText("clientActivity", "");
    for (const element of $("workspace").querySelectorAll(".client-activity"))
      element.classList.remove("client-activity");
  }
  $("selectedTitle").textContent = p.name + " · " + p.cap.name;
  const s = p.state,
    n = s.device?.extruder?.info || [];
  $("live").textContent =
    `${s.gcode_state} · ${Math.round(s.mc_percent)}% · 喷嘴 ${n.length ? n.map((x) => `${x.temp & 65535}/${x.temp >>> 16}°C`).join(" | ") : s.nozzle_temper + "/" + s.nozzle_target_temper + "°C"} · 热床 ${s.bed_temper}/${s.bed_target_temper}°C`;
  $("capabilities").innerHTML = [
    p.cap.nozzles + " 个加热喷嘴",
    p.cap.rack ? "6 位热端仓" : "无热端仓",
    p.cap.support_chamber_temp_edit ? "主动腔体温度" : "无腔体温度加热",
    p.cap.support_aux_fan ? "辅助风扇" : "无辅助风扇",
  ]
    .map((x) => `<span>${x}</span>`)
    .join("");
  $("mqttStats").innerHTML = [
    [
      "订阅连接",
      p.mqtt.connected,
      p.mqtt.connected ? "tone-success" : "tone-warning",
    ],
    ["接收命令", p.mqtt.rx],
    ["状态发送", p.mqtt.tx],
    [
      "拒绝 / 无效",
      p.mqtt.failures,
      p.mqtt.failures ? "tone-error" : "tone-info",
    ],
  ]
    .map(
      ([a, b, tone = "tone-info"]) =>
        `<div class="${tone}">${a}<b>${b}</b></div>`,
    )
    .join("");
  const analysis = [...p.warnings];
  const motion = s.virtual_motion;
  if (motion)
    analysis.push(
      "虚拟位置：" +
        Object.entries(motion.position)
          .map(([k, v]) => k + " " + Number(v).toFixed(1))
          .join(" · "),
    );
  if (!p.mqtt.connected)
    analysis.push("控制屏未订阅：核对电脑 IP、8883 端口、防火墙及虚拟序列号。");
  if (p.fault.error) analysis.push("已注入拒绝错误码 " + p.fault.error);
  if (p.fault.noack) analysis.push("正在模拟命令回复超时。");
  if (p.model === "C12")
    analysis.push("P1S 腔体温度上报 5 为不可用占位；固件应隐藏。");
  if (p.cap.nozzles === 2)
    analysis.push("双喷嘴按 AMS 绑定 T0/T1 校验；热端仓不等于独立加热喷嘴。");
  $("analysis").innerHTML =
    analysis
      .map(
        (t) =>
          `<div class="status-box ${t.includes("错误码") ? "tone-error" : t.includes("未订阅") || t.includes("超时") ? "tone-warning" : "tone-info"}">${escape(t)}</div>`,
      )
      .join("") +
    `<div class="muted">最近 RX：${p.mqtt.lastRx ? new Date(p.mqtt.lastRx * 1000).toLocaleTimeString() : "暂无"} · 已覆盖 ${Object.keys(p.mqtt.commands).length} 种命令</div>`;
  $("ams").innerHTML =
    (s.ams?.ams || [])
      .map((u) => {
        const spec = p.units.find((x) => String(x.id) === u.id);
        return `<div class="unit"><div class="unitHead"><b>${spec ? label(spec) : escape(u.id)} · ${spec ? escape(catalog.ams[spec.kind].name) : "Unknown"}</b><small>${escape(u.temp)}°C · 湿度 ${escape(u.humidity_raw)} · ${u.dry_time ? "烘干 " + u.dry_time + " min" : "待机"}</small></div><div class="spools">${(u.tray || []).map((t) => `<div class="spool" style="--spool:#${/^[0-9a-f]{8}$/i.test(t.tray_color) ? t.tray_color.slice(0, 6) : "999999"}"><b data-i18n-skip>${escape(t.tray_type)}</b>${escape(t.remain)}%</div>`).join("")}</div></div>`;
      })
      .join("") +
    (p.external
      ? `<div class="unit">外置耗材 × ${p.external}（独立固定区）</div>`
      : "");
  if (!$("ams").textContent.trim())
    $("ams").innerHTML =
      '<p class="status-box tone-info">此组合未配置 AMS 或外置耗材。</p>';
  if (
    !$("pauseLog").checked &&
    (rebuild || !$("events").contains(document.activeElement))
  ) {
    const opened = new Set(
      rebuild
        ? []
        : [...$("events").querySelectorAll("details[open]")].map(
            (node) => node.dataset.event,
          ),
    );
    $("events").innerHTML =
      [...p.events]
        .reverse()
        .map(
          (e) =>
            `<details data-event="${escape(e.time + "-" + e.direction)}" ${opened.has(e.time + "-" + e.direction) ? "open" : ""}><summary><b>${escape(e.direction)}</b> ${new Date(e.time * 1000).toLocaleTimeString()} · ${escape(e.payload?.print?.command || e.payload?.system?.command || e.payload?.info?.command || e.payload?.pushing?.command || "状态")} ${escape(e.note)}</summary><pre tabindex="0">${escape(JSON.stringify(e.payload, null, 2))}</pre></details>`,
        )
        .join("") ||
      '<p class="status-box tone-info">暂无收发记录。连接控制屏或发送虚拟命令后可在此查看。</p>';
  }
  $("autoReport").textContent = p.autoReport ? "暂停自动上报" : "恢复自动上报";
  $("live").className =
    "status-box " +
    (s.gcode_state === "FAILED"
      ? "tone-error"
      : s.gcode_state === "RUNNING"
        ? "tone-success"
        : "tone-info");
  for (const b of $("stateActions").children)
    b.setAttribute("aria-pressed", String(b.textContent === s.gcode_state));
  if (!rebuild) return;
  $("nozzle").replaceChildren();
  for (let i = 0; i < p.cap.nozzles; i++)
    $("nozzle").add(new Option("T" + i, i));
  $("source").replaceChildren();
  for (const u of p.units) $("source").add(new Option(label(u), u.id));
  for (let i = 0; i < p.external; i++)
    $("source").add(
      new Option("外置 T" + i, p.cap.newProtocol ? 255 - i : 254),
    );
  sourceChanged();
  const root = $("actions"),
    states = $("stateActions");
  root.replaceChildren();
  states.replaceChildren();
  for (const status of [
    "IDLE",
    "PREPARE",
    "RUNNING",
    "PAUSE",
    "FINISH",
    "FAILED",
  ])
    button(states, status, () => patch({ gcode_state: status }));
  button(root, "灯开 / 关", () =>
    cmd(
      "ledctrl",
      {
        led_mode:
          current().state.lights_report?.[0]?.mode === "on" ? "off" : "on",
        led_node: "chamber_light",
      },
      "system",
    ),
  ).id = "clientLight";
  button(root, "静音模式", () => cmd("print_speed", { param: "1" })).id =
    "clientSpeed";
  button(root, "暂停", () => cmd("pause")).id = "clientPause";
  button(root, "继续", () => cmd("resume")).id = "clientResume";
  button(root, "停止", () => cmd("stop")).id = "clientStop";
  for (const b of states.children)
    b.setAttribute("aria-pressed", String(b.textContent === s.gcode_state));
  $("heatChamber").disabled = !p.cap.support_chamber_temp_edit;
  $("switchNozzle").disabled = p.cap.nozzles < 2;
  $("rack").disabled = !p.cap.rack;
  for (const k of ["noack", "drop", "noVersion"]) $(k).checked = p.fault[k];
  $("errorCode").value = p.fault.error;
  $("delay").value = p.fault.delay;
}
function sourceChanged() {
  const p = current();
  if (!p) return;
  const u = p.units.find((u) => u.id === Number($("source").value));
  $("slot").replaceChildren();
  for (let i = 0; i < (u ? catalog.ams[u.kind].slots : 1); i++)
    $("slot").add(new Option("槽 " + (i + 1), i));
  for (const id of ["dryStart", "dryStop"])
    $(id).disabled = !u || !catalog.ams[u.kind].dry;
  $("reread").disabled = !u || !catalog.ams[u.kind].rfid;
  loadFilament();
}
$("source").onchange = sourceChanged;
$("slot").onchange = loadFilament;
function selectedFilament() {
  const p = current();
  if (!p) return null;
  const raw = Number($("source").value),
    slot = Number($("slot").value);
  const unit = p.state.ams?.ams?.find((u) => Number(u.id) === raw);
  return (
    unit?.tray[slot] ||
    p.state.vir_slot?.find((u) => Number(u.id) === raw) ||
    (raw === 254 ? p.state.vt_tray : null)
  );
}
function loadFilament() {
  clearOperationFeedback($("source").closest(".subcard"));
  const tray = selectedFilament();
  const target = tray
    ? `${sourceText($("source").selectedOptions[0])} · ${sourceText($("slot").selectedOptions[0])}`
    : "未配置可编辑耗材";
  $("filamentTarget").textContent = "当前操作目标：" + target;
  $("filamentEditorTitle").textContent = "编辑耗材属性 · " + target;
  for (const id of [
    "filamentColor",
    "filamentRemain",
    "filamentType",
    "updateFilament",
  ])
    $(id).disabled = !tray;
  if (!tray) return;
  const colour = String(tray.tray_color || "999999FF");
  $("filamentColor").value = /^[0-9a-f]{8}$/i.test(colour)
    ? "#" + colour.slice(0, 6)
    : "#999999";
  $("filamentRemain").value = tray.remain ?? 0;
  $("filamentType").value = tray.tray_type || "";
}
$("autoReport").onclick = () => act("auto_report", !current().autoReport);
$("updateFilament").onclick = () => {
  const p = current(),
    raw = Number($("source").value),
    slot = Number($("slot").value),
    copy = structuredClone(p.state),
    unit = copy.ams.ams.find((u) => Number(u.id) === raw);
  const tray =
    unit?.tray[slot] ||
    (copy.vir_slot || []).find((u) => Number(u.id) === raw) ||
    (raw === 254 ? copy.vt_tray : null);
  const remain = Number($("filamentRemain").value);
  if (!tray || !Number.isInteger(remain) || remain < 0 || remain > 100)
    return operationFeedback($("updateFilament"))(
      "请选择有效槽位，剩余量应为 0–100 的整数",
      true,
    );
  Object.assign(tray, {
    tray_type: $("filamentType").value,
    tray_color: $("filamentColor").value.slice(1).toUpperCase() + "FF",
    remain,
  });
  return patch(
    unit
      ? { ams: copy.ams }
      : copy.vir_slot
        ? { vir_slot: copy.vir_slot }
        : { vt_tray: copy.vt_tray },
  );
};
$("setNozzle").onclick = () => {
  const p = current(),
    i = Number($("nozzle").value),
    t = Number($("targetTemp").value);
  return p.cap.newProtocol
    ? cmd("set_nozzle_temp", { extruder_index: i, target_temp: t })
    : cmd("gcode_line", { param: "M104 S" + t });
};
$("switchNozzle").onclick = () =>
  cmd("select_extruder", { extruder_index: Number($("nozzle").value) });
$("heatBed").onclick = () => cmd("gcode_line", { param: "M140 S60" });
$("heatChamber").onclick = () => cmd("set_ctt", { ctt_val: 55 });
$("rack").onclick = () => cmd("holder_nozzle_refresh", { id: 255 });
$("setFan").onclick = () =>
  cmd("gcode_line", {
    param: `M106 P${$("fan").value} S${$("fanSpeed").value}`,
  });
$("homeAxes").onclick = () => cmd("gcode_line", { param: "G28" });
for (const [id, length] of [
  ["extrude", 10],
  ["retract", -10],
])
  $(id).onclick = () =>
    current().cap.newProtocol
      ? cmd("set_extrusion_length", {
          extruder_index: Number($("nozzle").value),
          length,
        })
      : cmd("gcode_line", { param: "G91\nG1 E" + length + " F300\nG90" });
$("reread").onclick = () =>
  cmd("ams_get_rfid", {
    ams_id: Number($("source").value),
    slot_id: Number($("slot").value),
  });
function filament(unload) {
  const raw = Number($("source").value),
    slot = Number($("slot").value);
  return cmd("ams_change_filament", {
    ams_id: raw,
    slot_id: unload ? 255 : slot,
    target: unload ? 255 : raw >= 128 ? raw : raw * 4 + slot,
    extruder_id: Number($("nozzle").value),
    curr_temp: 210,
    tar_temp: 210,
  });
}
$("load").onclick = () => filament(false);
$("unload").onclick = () => filament(true);
function dry(stop) {
  return cmd("ams_filament_drying", {
    ams_id: Number($("source").value),
    mode: stop ? 0 : 1,
    temp: stop ? 0 : Number($("dryTemp").value),
    duration: stop ? 0 : Number($("dryHours").value),
  });
}
$("dryStart").onclick = () => dry(false);
$("dryStop").onclick = () => dry(true);
$("fault").onclick = () =>
  act("fault", {
    error: Number($("errorCode").value),
    delay: Number($("delay").value),
    noack: $("noack").checked,
    drop: $("drop").checked,
    noVersion: $("noVersion").checked,
  });
for (const [id, action] of [
  ["patch", "patch"],
  ["delta", "delta"],
  ["sendCommand", "command"],
])
  $(id).onclick = () => {
    try {
      act(action, JSON.parse($("payload").value));
    } catch (e) {
      operationFeedback($(id))("JSON 格式无效：" + e.message, true);
    }
  };
const templates = {
  faults: {
    print: {
      print_error: 0x05004007,
      hms: [{ attr: 0x03000100, code: 0x00010001 }],
    },
  },
  clear: { print: { print_error: 0, hms: [] } },
  sentinel: { print: { chamber_temper: 5 } },
  invalid: { print: { nozzle_temper: 65535, chamber_temper: "NaN" } },
  external: { print: { vir_slot: [], vt_tray: {} } },
  busy: { print: { device: { extruder: { state: 2 | (1 << 19) } } } },
};
$("template").onclick = () =>
  ($("payload").value = JSON.stringify(
    templates[$("templates").value],
    null,
    2,
  ));
$("export").onclick = () =>
  downloadJSON(
    current(),
    `snapshot-${new Date().toISOString().replace(/[:.]/g, "-")}-${selected}.json`,
  );
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  let connected = false;
  try {
    state = await api("/api/state");
    connected = true;
    if (!selected && state.printers.length) selected = state.printers[0].serial;
    $("health").className = "status-box tone-success";
    setText(
      "health",
      `模拟器在线 · ${state.clients} 个 MQTT 连接 · ${Math.floor(state.uptime / 60)} min`,
    );
    $("port").textContent = state.mqttPort;
    const rebuild = $("workspace").hidden;
    drawCards();
    drawWorkspace(rebuild);
    showClientActivity(incoming(state.printers, selected));
  } catch (e) {
    $("health").className = "status-box tone-error";
    setText(
      "health",
      connected ? "服务在线，状态数据异常" : "模拟器离线，请检查服务是否运行",
    );
    if (connected) reportClientError(e);
    // Connectivity belongs to the persistent health region, not operation results.
  } finally {
    refreshing = false;
  }
}
api("/api/catalog")
  .then((c) => {
    catalog = c;
    fillModels();
    return refresh();
  })
  .catch((e) => notify(e.message, true));
setInterval(refresh, 2000);
