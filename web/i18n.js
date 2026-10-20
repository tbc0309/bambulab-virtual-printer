// Display-only localization. Never rewrites values, protocol JSON or user labels.
import { messages, templates, errors } from "./locales.js";
const storageKey = "bambulab-virtual-printer.language";
let language = "zh-CN";
try {
  const saved = localStorage.getItem(storageKey);
  if (["zh-CN", "en"].includes(saved)) language = saved;
} catch {
  /* Storage may be disabled. */
}
const textSources = new WeakMap(),
  attributeSources = new WeakMap();
const normalize = (value) => String(value).replace(/\s+/g, " ").trim();
const quote = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const vocabulary = Object.keys(messages)
  .filter((key) => /[\u3400-\u9fff]/u.test(key))
  .sort((a, b) => b.length - a.length);
const terms = new RegExp(vocabulary.map(quote).join("|"), "gu");
const dynamic = templates.map(([zh, en]) => ({
  zh,
  en,
  regex: new RegExp(
    "^" + quote(zh).replace(/\\\{\d+\\\}/g, "(.+?)") + "$",
    "u",
  ),
}));
const errorPairs = Object.entries(errors);
export function getLanguage() {
  return language;
}
export function translate(value) {
  const text = normalize(value);
  if (!text) return value;
  if (text === "AMS Lite") return "AMS lite";
  if (text === "fixture")
    return language === "en" ? "Built-in sample" : "内置素材";
  if (text === "Unknown") return language === "en" ? "Unknown" : "未知";
  const socketCode = /(?:WinError|Errno) (10048|10013|10049|98|13)/.exec(text);
  if (socketCode) {
    const code = socketCode[1],
      occupied = ["10048", "98"].includes(code),
      denied = ["10013", "13"].includes(code);
    return language === "en"
      ? `${occupied ? "Address already in use" : denied ? "Permission denied" : "Bind address unavailable"} (${code}); check the service port and permissions.`
      : `${occupied ? "端口已被占用" : denied ? "权限不足" : "监听地址不可用"}（${code}），请检查端口和权限。`;
  }
  const startup =
    /^(mqtt|rtsps|jpeg) could not start: (.+)\. Check port usage and permissions\.$/.exec(
      text,
    );
  if (startup && language === "zh-CN")
    return `${startup[1].toUpperCase()} 启动失败：${translate(startup[2])}。请检查端口占用和权限。`;
  if (language === "zh-CN") {
    if (errors[text]) return errors[text];
    const option =
      /^(fps|delaySeconds|dropAfter|fragmentBytes) must be an integer (\d+)\.\.(\d+)$/.exec(
        text,
      );
    if (option)
      return `${{ fps: "帧率", delaySeconds: "首帧延迟", dropAfter: "断流前帧数", fragmentBytes: "TCP 分片字节" }[option[1]]}应为 ${option[2]}–${option[3]} 范围内的整数`;
    const range = /^Expected integer (\d+)\.\.(\d+)$/.exec(text);
    if (range) return `请输入 ${range[1]}–${range[2]} 范围内的整数`;
    return value;
  }
  if (messages[text]) return messages[text];
  const error = errorPairs.find(([, zh]) => zh === text);
  if (error) return error[0];
  for (const entry of dynamic) {
    const match = entry.regex.exec(text);
    if (match)
      return entry.en.replace(/\{(\d+)\}/g, (_, n) =>
        translate(match[Number(n) + 1]),
      );
  }
  return text
    .replace(terms, (key) => messages[key])
    .replace(/：/g, ": ")
    .replace(/；/g, "; ")
    .replace(/（/g, " (")
    .replace(/）/g, ")");
}
const excluded = "[data-i18n-skip], pre, code, textarea, script, style";
function ignored(element) {
  return !element || !!element.closest(excluded);
}
function localizeText(node, force = false) {
  if (ignored(node.parentElement)) return;
  const prior = textSources.get(node);
  if (!force && prior?.rendered === node.data) return;
  const source = prior?.rendered === node.data ? prior.source : node.data;
  const rendered = translate(source);
  textSources.set(node, { source, rendered });
  if (node.data !== rendered) node.data = rendered;
}
export function sourceText(element) {
  if (!element) return "";
  return [...element.childNodes]
    .map((node) =>
      node.nodeType === Node.TEXT_NODE
        ? (textSources.get(node)?.source ?? node.data)
        : sourceText(node),
    )
    .join("");
}
export function setLocalizedText(element, value) {
  const source = String(value),
    rendered = String(translate(source));
  if (element.textContent !== rendered) element.textContent = rendered;
  if (element.firstChild?.nodeType === Node.TEXT_NODE)
    textSources.set(element.firstChild, { source, rendered });
}
function localizeAttributes(element, force = false) {
  if (!element || element.closest("[data-i18n-skip], pre, code, script, style"))
    return;
  let records = attributeSources.get(element);
  if (!records) {
    records = new Map();
    attributeSources.set(element, records);
  }
  for (const name of ["aria-label", "title", "placeholder", "alt"]) {
    if (
      !element.hasAttribute(name) ||
      element.getAttribute("data-i18n-skip-attrs")?.split(" ").includes(name)
    )
      continue;
    const value = element.getAttribute(name),
      prior = records.get(name);
    if (!force && prior?.rendered === value) continue;
    const source = prior?.rendered === value ? prior.source : value,
      rendered = translate(source);
    records.set(name, { source, rendered });
    if (value !== rendered) element.setAttribute(name, rendered);
  }
}
function localize(root, force = false) {
  if (root.nodeType === Node.TEXT_NODE) {
    localizeText(root, force);
    return;
  }
  if (
    root.nodeType !== Node.ELEMENT_NODE &&
    root.nodeType !== Node.DOCUMENT_NODE
  )
    return;
  if (root.nodeType === Node.ELEMENT_NODE) {
    localizeAttributes(root, force);
    if (ignored(root)) return;
  }
  const walker = document.createTreeWalker(
    root,
    NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
    {
      acceptNode: (node) => {
        if (node.nodeType === Node.ELEMENT_NODE && ignored(node)) {
          localizeAttributes(node, force);
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    },
  );
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (node.nodeType === Node.TEXT_NODE) localizeText(node, force);
    else localizeAttributes(node, force);
  }
}
export function setLanguage(value) {
  if (!["zh-CN", "en"].includes(value)) return;
  language = value;
  try {
    localStorage.setItem(storageKey, value);
  } catch {
    /* Keep an in-memory choice if storage is unavailable. */
  }
  document.documentElement.lang = value;
  const picker = document.getElementById("languagePicker");
  if (picker) picker.value = value;
  localize(document.documentElement, true);
  window.dispatchEvent(
    new CustomEvent("ui-language-change", { detail: value }),
  );
}
function start() {
  document.documentElement.lang = language;
  const label = document.createElement("label");
  label.className = "language-picker";
  const text = document.createElement("span");
  text.textContent = "界面语言";
  const picker = document.createElement("select");
  picker.id = "languagePicker";
  for (const [value, title] of [
    ["zh-CN", "简体中文"],
    ["en", "English"],
  ]) {
    const option = new Option(title, value);
    option.setAttribute("data-i18n-skip", "");
    picker.add(option);
  }
  picker.value = language;
  picker.onchange = () => setLanguage(picker.value);
  label.append(text, picker);
  document.querySelector("header")?.append(label);
  localize(document.documentElement, true);
  const pending = new Set();
  let queued = false;
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === "childList")
        for (const node of record.addedNodes) pending.add(node);
      else pending.add(record.target);
    }
    if (!queued) {
      queued = true;
      queueMicrotask(() => {
        queued = false;
        const nodes = [...pending];
        pending.clear();
        for (const node of nodes) if (node.isConnected) localize(node);
      });
    }
  });
  observer.observe(document.documentElement, {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeFilter: ["aria-label", "title", "placeholder", "alt"],
  });
}
if (document.readyState === "loading")
  document.addEventListener("DOMContentLoaded", start, { once: true });
else start();
