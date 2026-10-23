// File catalogue and explicit diagnostic preview; no raw traffic polling.
import { $, api, operationFeedback, setText } from "./ui.js";

let catalogue = null,
  busy = false;

function drawFiles() {
  if (!catalogue) return;
  const rows = catalogue.files.filter(
    (file) =>
      (!$("logsCurrent").checked || file.current) &&
      (!$("logsChannel").value || file.channel === $("logsChannel").value),
  );
  $("logFiles").replaceChildren();
  for (const file of rows) {
    const row = document.createElement("tr");
    const description = document.createElement("td");
    const title = document.createElement("strong");
    title.textContent = file.title;
    const purpose = document.createElement("small");
    purpose.textContent = file.description;
    const name = document.createElement("small");
    name.textContent = file.name;
    const session = document.createElement("small");
    session.textContent = `${file.current ? "本次运行" : "历史运行"} · ${new Date(file.started * 1000).toLocaleString()}`;
    description.append(title, purpose, session, name);
    const modified = document.createElement("td");
    modified.textContent = new Date(file.modified * 1000).toLocaleString();
    const size = document.createElement("td");
    size.textContent = (file.bytes / 1024).toFixed(1) + " KiB";
    const actions = document.createElement("td"),
      buttons = document.createElement("div");
    buttons.className = "buttons";
    const view = document.createElement("button");
    view.textContent = "查看";
    view.setAttribute("aria-label", "查看" + file.title + " " + file.name);
    view.onclick = async () => {
      const feedback = operationFeedback(view);
      view.disabled = true;
      try {
        const result = await api(
          "/api/logs/tail?name=" + encodeURIComponent(file.name),
        );
        $("logPreviewTitle").textContent = file.title + " · " + file.name;
        $("logContent").textContent = result.lines
          .map((line) => {
            try {
              return JSON.stringify(JSON.parse(line), null, 2);
            } catch {
              return line;
            }
          })
          .join("\n");
        feedback("日志已读取");
        $("logPreview").open = true;
        $("logPreview").scrollIntoView({ block: "nearest" });
      } catch (error) {
        feedback(error.message, true);
      } finally {
        view.disabled = false;
      }
    };
    const download = document.createElement("a");
    download.textContent = "下载";
    download.className = "button-link";
    download.href = "/api/logs/file?name=" + encodeURIComponent(file.name);
    download.download = file.name;
    download.setAttribute("aria-label", "下载" + file.title + " " + file.name);
    buttons.append(view, download);
    actions.append(buttons);
    row.append(description, modified, size, actions);
    $("logFiles").append(row);
  }
  if (!rows.length) {
    const row = document.createElement("tr"),
      cell = document.createElement("td");
    cell.colSpan = 4;
    cell.textContent = "当前筛选没有日志文件。";
    row.append(cell);
    $("logFiles").append(row);
  }
}

async function refreshLogs() {
  if (busy) return;
  busy = true;
  try {
    catalogue = await api("/api/logs");
    $("logsStatus").className =
      "status-box " + (catalogue.lastError ? "tone-error" : "tone-info");
    setText(
      "logsStatus",
      catalogue.lastError ||
        `数据目录 / ${catalogue.directory} · ${catalogue.files.length} 个文件 · 保留 7 天、总量最多 64 MiB · UTC 会话命名`,
    );
    if (!$("logFiles").contains(document.activeElement)) drawFiles();
  } catch (error) {
    $("logsStatus").className = "status-box tone-error";
    setText("logsStatus", error.message);
  } finally {
    busy = false;
  }
}

$("logsRefresh").onclick = refreshLogs;
$("logsCurrent").onchange = drawFiles;
$("logsChannel").onchange = drawFiles;
refreshLogs();
setInterval(refreshLogs, 10000);
