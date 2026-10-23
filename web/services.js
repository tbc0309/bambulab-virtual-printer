import { $, api, operationFeedback, setText, highlightActivity } from "./ui.js";
const cameraLabels = {
  frame: "已发送帧",
  connected: "已连接",
  disconnected: "已断开",
  closed: "连接异常",
  unauthorized: "认证失败",
  challenge: "认证协商",
  request: "协议请求",
  "intentional-drop": "模拟断流",
  configured: "场景已更新",
  "settings-reset": "配置已回退",
};

let cameraInitialized = false;
let cameraConnections;
async function refreshCamera(initial = false) {
  try {
    const c = await api("/api/camera");
    const connections = `${c.clients}:${c.counters.connected || 0}:${c.counters.disconnected || 0}`;
    if (cameraConnections !== undefined && cameraConnections !== connections)
      highlightActivity($("cameraStatus"));
    cameraConnections = connections;
    $("cameraLogs").textContent =
      c.events
        .slice(-10)
        .map(
          (e) =>
            `${new Date(e.time * 1000).toLocaleTimeString()} ${cameraLabels[e.event] || e.event} ${e.transport || ""} ${e.method || ""}`,
        )
        .join("\n") || "暂无摄像头事件";
    $("cameraStatus").className =
      ($("cameraStatus").classList.contains("client-activity")
        ? "client-activity "
        : "") +
      "status-box " +
      (c.ports.rtsps || c.ports.jpeg ? "tone-success" : "tone-info");
    setText(
      "cameraStatus",
      `RTSPS ${c.ports.rtsps || "未启用"} · JPEG ${c.ports.jpeg || "未启用"} · ${c.clients} 连接`,
    );
    $("cameraCounters").replaceChildren(
      ...["frame", "unauthorized", "intentional-drop", "closed"].map((key) => {
        const card = document.createElement("div"),
          value = document.createElement("b");
        card.append(document.createTextNode(cameraLabels[key]));
        value.textContent = c.counters[key] || 0;
        card.append(value);
        return card;
      }),
    );
    if (initial || !cameraInitialized) {
      $("cameraMode").value = c.options.mode;
      $("cameraAuth").value = c.options.auth;
      $("cameraFps").value = c.options.fps;
      $("cameraDelay").value = c.options.delaySeconds;
      $("cameraDrop").value = c.options.dropAfter;
      $("cameraFragment").value = c.options.fragmentBytes;
      $("cameraStap").checked = c.options.aggregateParameterSets;
      for (const [id, ext, key] of [
        ["cameraH264", ".h264", "h264Source"],
        ["cameraJpeg", ".jpg", "jpegSource"],
      ]) {
        $(id).replaceChildren(
          ...c.sources[ext].map((s) => {
            const option = new Option(s, s);
            if (s !== "fixture") option.setAttribute("data-i18n-skip", "");
            return option;
          }),
        );
        $(id).value = c.options[key];
      }
    }
    cameraInitialized = true;
    cameraFields();
  } catch (e) {
    $("cameraStatus").className = "status-box tone-error";
    setText("cameraStatus", e.message);
  }
}
$("cameraApply").onclick = async () => {
  if ($("cameraApply").disabled) return;
  for (const input of document.querySelectorAll("#cameraPanel input")) {
    if (!input.disabled && !input.reportValidity()) return;
  }
  const feedback = operationFeedback($("cameraApply"));
  $("cameraApply").disabled = true;
  try {
    await api("/api/camera", {
      mode: $("cameraMode").value,
      auth: $("cameraAuth").value,
      fps: Number($("cameraFps").value),
      delaySeconds: Number($("cameraDelay").value),
      dropAfter: Number($("cameraDrop").value),
      fragmentBytes: Number($("cameraFragment").value),
      aggregateParameterSets: $("cameraStap").checked,
      h264Source: $("cameraH264").value,
      jpegSource: $("cameraJpeg").value,
    });
    feedback("摄像头配置已应用，客户端将重新连接");
    await refreshCamera();
  } catch (e) {
    feedback(e.message, true);
  } finally {
    $("cameraApply").disabled = false;
  }
};
function cameraFields() {
  const mode = $("cameraMode").value;
  $("cameraDelay").disabled = mode !== "delayed";
  $("cameraDrop").disabled = mode !== "drop";
  setText(
    "cameraHint",
    ["oversize", "truncated-header", "truncated-frame"].includes(mode)
      ? "此异常仅适用于 JPEG；RTSPS 视频仍按正常场景传输。"
      : "认证方式、RTP 时间戳与 STAP-A 仅适用于 RTSPS。首帧延迟和断流帧数仅用于对应场景。",
  );
}
$("cameraMode").onchange = cameraFields;
refreshCamera(true);
setInterval(() => refreshCamera(), 2000);

const serviceBusy = new Set();
const serviceInfo = new Map();
const titles = {
  mqtt: "MQTT 打印机",
  rtsps: "RTSPS 视频",
  jpeg: "JPEG 摄像头",
};
const descriptions = {
  mqtt: "TLS MQTT · 状态、控制回复与 AMS 联调",
  rtsps: "TLS / H.264 · 视频、认证与断流测试",
  jpeg: "TLS / JPEG · 图像传输与异常边界测试",
};
function createServiceCard(name, info) {
  const row = document.createElement("article");
  row.id = "service-" + name;
  row.className = "service-card";
  row.innerHTML = `<div class="heading"><h3>${titles[name]}</h3><span class="badge" id="status-${name}" role="status"></span></div><p class="muted">${descriptions[name]}</p><label>监听端口<input id="port-${name}" type="number" min="1" max="65535" required aria-label="${titles[name]} 监听端口"></label><p class="service-detail" id="detail-${name}"></p><button id="toggle-${name}"></button>`;
  $("serviceControls").append(row);
  const input = $("port-" + name),
    button = $("toggle-" + name);
  input.value = info.port;
  button.onclick = async () => {
    if (serviceBusy.has(name)) return;
    const current = serviceInfo.get(name);
    if (!current || (!current.enabled && !input.reportValidity())) return;
    const feedback = operationFeedback(button);
    serviceBusy.add(name);
    renderService(name, current);
    try {
      await api("/api/services", {
        service: name,
        enabled: !current.enabled,
        port: Number(input.value),
      });
      feedback(
        titles[name] + (current.enabled ? "已关闭，连接已断开" : "已开启"),
      );
    } catch (e) {
      feedback(e.message, true);
    } finally {
      serviceBusy.delete(name);
      await refreshServices();
      await refreshCamera();
    }
  };
}
function renderService(name, info) {
  serviceInfo.set(name, info);
  const busy = serviceBusy.has(name),
    state = busy ? "pending" : info.state;
  const labels = {
    running: "运行中",
    stopped: "未开启",
    error: "启动失败",
    pending: "切换中…",
    unknown: "状态未知",
  };
  const row = $("service-" + name),
    input = $("port-" + name),
    button = $("toggle-" + name);
  row.dataset.state = state;
  row.setAttribute("aria-busy", String(busy));
  setText("status-" + name, labels[state] || labels.unknown);
  setText(
    "detail-" + name,
    info.error ||
      (info.enabled
        ? "正在监听；关闭服务会断开现有连接。"
        : "未监听；开启前可修改端口。"),
  );
  if (info.enabled) input.value = info.port;
  input.disabled = info.enabled || busy || state === "unknown";
  button.disabled = busy || state === "unknown";
  button.dataset.enabled = String(info.enabled);
  button.setAttribute("aria-pressed", String(info.enabled));
  button.textContent = busy
    ? "请稍候…"
    : info.enabled
      ? "关闭服务"
      : "开启服务";
  button.setAttribute("aria-label", titles[name] + " " + button.textContent);
}
let servicesRefreshing = false;
async function refreshServices() {
  if (servicesRefreshing) return;
  servicesRefreshing = true;
  try {
    const s = await api("/api/services");
    setText("serviceStatus", `监听地址 ${s.bind}`);
    $("environment").textContent =
      `Python ${s.environment.python} · ${s.environment.tls} · cryptography ${s.environment.cryptography} · 内置媒体${s.environment.mediaReady ? "就绪" : "缺失"}`;
    for (const [name, info] of Object.entries(s.services)) {
      if (!$("service-" + name)) createServiceCard(name, info);
      renderService(name, info);
    }
    setText(
      "runtimeWarnings",
      [
        s.logStorage.lastError ||
          (s.logStorage.failedRecords
            ? `日志写入已恢复，累计失败 ${s.logStorage.failedRecords} 次`
            : ""),
        ...s.warnings,
      ]
        .filter(Boolean)
        .join(" · "),
    );
    $("serviceLogs").textContent =
      s.events
        .slice(-10)
        .map(
          (e) =>
            `${new Date(e.time * 1000).toLocaleTimeString()} ${e.service} ${e.action}: ${e.message}`,
        )
        .join("\n") || "暂无服务事件";
  } catch (e) {
    setText("serviceStatus", "无法获取服务状态：" + e.message);
    for (const [name, info] of serviceInfo)
      renderService(name, {
        ...info,
        state: "unknown",
        error: "管理连接中断，请等待恢复。",
      });
  } finally {
    servicesRefreshing = false;
  }
}
refreshServices();
setInterval(refreshServices, 2000);
