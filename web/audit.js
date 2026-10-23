import { escape as e, reportClientError } from "./ui.js";
fetch("/api/catalog")
  .then((r) => r.json())
  .then((c) => {
    document.querySelector("#source").textContent =
      "BambuStudio " +
      c.studioCommit.slice(0, 12) +
      " · ha-bambulab " +
      c.haCommit.slice(0, 12);
    document.querySelector("#models").innerHTML = Object.entries(c.models)
      .map(
        ([code, m]) =>
          `<article class="panel"><div class="cover"><img src="assets/printer_${code}.png" alt="${e(m.name)}"><div><h2>${e(m.name)}</h2><code>${code}</code><p>${m.nozzles} 喷嘴 · ${m.newProtocol ? "新协议" : "传统协议"}</p></div></div><div class="chips"><span>${m.nozzleRange.join("–")}°C 喷嘴</span><span>热床 ≤${m.bedMax}°C</span><span>${m.support_chamber_temp_display ? "腔体温度显示" : "腔体温度不显示"}</span><span>${m.support_chamber_temp_edit ? "腔体温度加热 " + m.chamberRange.join("–") + "°C" : "无腔体温度加热"}</span><span>${m.amsFamily === "lite" ? "Lite 系列" : "标准 AMS 系列"}</span>${m.rack ? "<span>H2C 热端仓</span>" : ""}</div><p class="muted">已模拟：状态、温度、灯、风扇、进度、错误、进退料；按能力提供喷嘴切换 / 烘干 / 热端仓。可在管理页启用合成 RTSPS / JPEG 摄像流；文件传输及完整运动学未模拟。</p><details><summary>完整上游能力字段 / 固件版本</summary><pre tabindex="0">${e(JSON.stringify({ versions: m.firmwareVersions, capabilities: m.upstreamFlags }, null, 2))}</pre></details></article>`,
      )
      .join("");
    document.querySelector("#amsMatrix").innerHTML = Object.values(c.ams)
      .map(
        (m) =>
          `<article class="unit"><h3>${e(m.name)}</h3><p>${m.slots} 槽 · RFID ${m.rfid ? "支持" : "不支持"} · 烘干 ${m.dry ? m.temperature.join("–") + "°C" : "不支持"}</p><div class="spools">${Array.from({ length: m.slots }, (_, i) => `<div class="spool" style="--spool:${["#ed544b", "#42addf", "#f5cd4a", "#68c799"][i % 4]}"><b>${i + 1}</b>PLA</div>`).join("")}</div></article>`,
      )
      .join("");
  })
  .catch((error) => {
    document.querySelector("#source").className = "status-box tone-error";
    document.querySelector("#source").textContent =
      "能力目录加载失败，请刷新重试。";
    reportClientError(error.message);
  });
