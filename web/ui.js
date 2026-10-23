import { setLocalizedText } from "./i18n.js";
// Shared DOM, API and download helpers for dashboard modules.
export const $ = (id) => document.getElementById(id);
export const escape = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
export async function api(path, body) {
  const r = await fetch(
    path,
    body
      ? {
          method: "POST",
          signal: AbortSignal.timeout(10000),
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : { signal: AbortSignal.timeout(10000) },
  );
  const d = await r.json();
  if (!r.ok) throw Error(d.error || r.status);
  return d;
}
export function setText(id, text) {
  const element = $(id);
  setLocalizedText(element, text);
}
export function reportClientError(error, source = location.pathname, line = 0) {
  // Error metadata only: never collect DOM, storage, forms or request bodies.
  fetch("/api/logs/client", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    signal: AbortSignal.timeout(3000),
    body: JSON.stringify({
      message: String(error).slice(0, 2048),
      source,
      line,
    }),
  }).catch(() => {});
}
window.addEventListener("error", (event) =>
  reportClientError(event.message, event.filename, event.lineno || 0),
);
window.addEventListener("unhandledrejection", (event) =>
  reportClientError(event.reason),
);
// Keep each card's latest operation result; background status uses separate regions.
const feedbackVersions = new WeakMap();
// Capture the originating card before an asynchronous operation changes focus.
export function operationFeedback(origin = document.activeElement) {
  const card = origin?.closest(
    ".subcard:not(details), .service-card, .panel, .section-stack",
  );
  const version = ((card && feedbackVersions.get(card)) || 0) + 1;
  if (card) feedbackVersions.set(card, version);
  return (message, error = false) => {
    if (!card || feedbackVersions.get(card) === version)
      notify(message, error, card, origin);
  };
}
export function clearOperationFeedback(root) {
  for (const card of [
    root,
    ...root.querySelectorAll(".subcard, .service-card, .panel, .section-stack"),
  ]) {
    feedbackVersions.set(card, (feedbackVersions.get(card) || 0) + 1);
  }
  root.querySelectorAll(".card-feedback").forEach((node) => {
    clearTimeout(feedbackTimers.get(node)?.timer);
    node.remove();
  });
}
export const feedbackTimeouts = Object.freeze({ success: 10000, error: 20000 });
const feedbackTimers = new WeakMap();
function showFeedback(target, message, error, origin) {
  const old = feedbackTimers.get(target);
  if (old?.timer) clearTimeout(old.timer);
  let text = target.querySelector(".feedback-text");
  if (!text) {
    target.replaceChildren();
    text = document.createElement("span");
    text.className = "feedback-text";
    const close = document.createElement("button");
    close.type = "button";
    close.className = "feedback-dismiss";
    close.textContent = "×";
    close.setAttribute("aria-label", "关闭提示");
    close.onclick = (event) => {
      event.preventDefault();
      event.stopPropagation();
      const state = feedbackTimers.get(target);
      if (state?.timer) clearTimeout(state.timer);
      if (target.contains(document.activeElement) && state?.origin?.isConnected)
        state.origin.focus({ preventScroll: true });
      if (target.id === "message") target.replaceChildren();
      else target.remove();
    };
    target.append(text, close);
    target.tabIndex = 0;
    if (!old) {
      target.addEventListener("pointerenter", () =>
        pauseFeedback(target, "pointer"),
      );
      target.addEventListener("pointerleave", () =>
        resumeFeedback(target, "pointer"),
      );
      target.addEventListener("focusin", () => pauseFeedback(target, "focus"));
      target.addEventListener("focusout", (event) => {
        if (!target.contains(event.relatedTarget))
          resumeFeedback(target, "focus");
      });
    }
  }
  setLocalizedText(text, message);
  const holds = new Set();
  if (target.matches(":hover")) holds.add("pointer");
  if (target.contains(document.activeElement)) holds.add("focus");
  feedbackTimers.set(target, {
    remaining: error ? feedbackTimeouts.error : feedbackTimeouts.success,
    holds,
    origin,
  });
  scheduleFeedback(target);
}
function scheduleFeedback(target) {
  const state = feedbackTimers.get(target);
  if (!state || state.holds.size || !target.isConnected) return;
  clearTimeout(state.timer);
  state.started = Date.now();
  state.timer = setTimeout(() => {
    if (target.id === "message") target.replaceChildren();
    else target.remove();
  }, state.remaining);
}
function pauseFeedback(target, reason) {
  const state = feedbackTimers.get(target);
  if (!state) return;
  if (!state.holds.size) {
    clearTimeout(state.timer);
    state.remaining = Math.max(
      0,
      state.remaining - (Date.now() - state.started),
    );
  }
  state.holds.add(reason);
}
function resumeFeedback(target, reason) {
  const state = feedbackTimers.get(target);
  if (!state) return;
  state.holds.delete(reason);
  if (!state.holds.size) scheduleFeedback(target);
}
export function notify(s, error = false, card = null, origin = null) {
  let target = $("message");
  if (card?.isConnected && !card.closest("[hidden]")) {
    let heading = card.querySelector(":scope > .heading");
    if (!heading) {
      const title = card.querySelector(":scope > h3, :scope > h2");
      if (title) {
        heading = document.createElement("div");
        heading.className = "heading";
        title.before(heading);
        heading.append(title);
      }
    }
    if (heading) {
      target = heading.querySelector(".card-feedback");
      if (!target) {
        target = document.createElement("span");
        target.setAttribute("role", "status");
        target.setAttribute("aria-atomic", "true");
        heading.append(target);
      }
      target.className =
        "card-feedback " + (error ? "tone-error" : "tone-success");
    }
  }
  showFeedback(target, s, error, origin);
  if (target.id === "message")
    target.className =
      "status-box notification " + (error ? "tone-error" : "tone-success");
}
export function downloadJSON(value, name) {
  const blob = new Blob([JSON.stringify(value, null, 2)], {
      type: "application/json",
    }),
    a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

const activityTimers = new WeakMap();
export function highlightActivity(element) {
  if (!element) return;
  clearTimeout(activityTimers.get(element));
  element.classList.remove("client-activity");
  void element.offsetWidth;
  element.classList.add("client-activity");
  activityTimers.set(
    element,
    setTimeout(() => element.classList.remove("client-activity"), 8000),
  );
}
