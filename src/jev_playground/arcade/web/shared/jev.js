// Shared client for every arcade site: one ask(), a speed badge, and the x-ray drawer.

export const SITE = location.pathname.split("/")[1] || "hub";

const session = { calls: 0, questions: 0, ms: 0, cost: 0, tokens: 0, last: null, history: [] };
const listeners = new Set();

export function onCall(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export async function ask(state, questions, { signal } = {}) {
  const started = performance.now();
  const response = await fetch("/api/jev", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ site: SITE, state, questions }),
    signal,
  });
  const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  const wall = Math.round(performance.now() - started);
  if (!response.ok) {
    const error = new Error(data.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  data.wall_ms = wall;
  session.calls += 1;
  session.questions += data.questions;
  session.ms += data.elapsed_ms;
  session.cost += Number(data.usage.cost || 0);
  session.tokens += data.usage.input_tokens;
  session.last = { request: { model: data.model, state, questions }, response: data };
  session.history.unshift({ ms: data.elapsed_ms, q: data.questions, at: Date.now() });
  session.history.length = Math.min(session.history.length, 40);
  listeners.forEach((fn) => fn(data));
  renderXray();
  return data;
}

// A Noul answer, a Choice answer, and a Score answer read the same way here.
export const yes = (answer) => answer.noul;
export const pick = (answer) => answer.choice;
export const prob = (answer, key) => answer.probabilities[key] ?? 0;
export const ranked = (answer) =>
  Object.entries(answer.probabilities).sort((a, b) => b[1] - a[1]);

export function money(value) {
  if (!value) return "$0";
  if (value < 0.01) return `$${value.toFixed(6)}`;
  return `$${value.toFixed(4)}`;
}

export function badge(result) {
  return `<span class="jx-badge" title="Jev round trip / questions in this call / OpenRouter cost">
    <b>${Math.round(result.elapsed_ms)} ms</b> · ${result.questions} q · ${money(result.usage.cost)}</span>`;
}

export function bars(answer, { legend, limit, highlight } = {}) {
  const names = legend || answer.legend || {};
  let rows;
  if (answer.type === "noul") {
    rows = [["yes", answer.noul], ["no", 1 - answer.noul]];
  } else {
    rows = Object.entries(answer.probabilities);
    if (answer.type === "choice") rows.sort((a, b) => b[1] - a[1]);
  }
  if (limit) rows = rows.slice(0, limit);
  const top = answer.type === "choice" ? answer.choice : answer.type === "score" ? String(Math.round(answer.score)) : answer.noul >= 0.5 ? "yes" : "no";
  return `<div class="jx-bars">${rows
    .map(([key, value]) => {
      const pct = Math.round(value * 1000) / 10;
      const on = highlight ? highlight === key : key === top;
      return `<div class="jx-row${on ? " on" : ""}"><span class="jx-name">${escapeHtml(names[key] || key)}</span><span class="jx-track"><span style="width:${Math.max(0.5, pct)}%"></span></span><span class="jx-pct">${pct.toFixed(pct < 10 ? 1 : 0)}%</span></div>`;
    })
    .join("")}</div>`;
}

export function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// Drop stale answers when the user keeps typing: only the newest call may render.
export function latest(fn) {
  let generation = 0;
  return async (...args) => {
    const mine = ++generation;
    const result = await fn(...args);
    if (mine !== generation) return undefined;
    return result;
  };
}

export function shuffle(items) {
  const copy = [...items];
  for (let i = copy.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [copy[i], copy[j]] = [copy[j], copy[i]];
  }
  return copy;
}

let drawer;

function renderXray() {
  if (!drawer) return;
  drawer.querySelector(".jx-count").textContent = session.calls
    ? `${session.calls} calls · ${session.questions} questions · avg ${Math.round(session.ms / session.calls)} ms · ${money(session.cost)}`
    : "No calls yet. Loading a page never calls Jev.";
  const spark = drawer.querySelector(".jx-spark");
  const max = Math.max(1500, ...session.history.map((h) => h.ms));
  spark.innerHTML = session.history
    .slice()
    .reverse()
    .map((h) => `<span title="${Math.round(h.ms)} ms · ${h.q} q" style="height:${Math.max(4, (h.ms / max) * 100)}%"></span>`)
    .join("");
  if (session.last) {
    drawer.querySelector(".jx-req").textContent = JSON.stringify(session.last.request, null, 2).slice(0, 20000);
    const { answers, ...rest } = session.last.response;
    drawer.querySelector(".jx-res").textContent = JSON.stringify({ ...rest, answers }, null, 2).slice(0, 20000);
  }
}

export function chrome({ title, kind, accent } = {}) {
  if (accent) document.documentElement.style.setProperty("--jx-accent", accent);
  const bar = document.createElement("div");
  bar.className = "jx-top";
  bar.innerHTML = `<a href="/" class="jx-home" title="Back to the arcade">◂ jev arcade</a>
    <span class="jx-title">${title ? escapeHtml(title) : ""}${kind ? ` <i>${kind}</i>` : ""}</span>
    <button type="button" class="jx-open" aria-expanded="false">x-ray</button>`;
  document.body.prepend(bar);
  drawer = document.createElement("aside");
  drawer.className = "jx-drawer";
  drawer.hidden = true;
  drawer.innerHTML = `<header><strong>Under the hood</strong><button type="button" class="jx-close" aria-label="Close">×</button></header>
    <p class="jx-count"></p>
    <div class="jx-spark" aria-label="Latency of recent calls"></div>
    <p class="jx-note">Everything below is the literal traffic to <code>typesafe/jev-1.13</code> through OpenRouter's Decisions API. Jev never writes text. It returns typed answers with probabilities, and this page does the rest in JavaScript.</p>
    <h4>Last request</h4><pre class="jx-req">—</pre>
    <h4>Last response</h4><pre class="jx-res">—</pre>`;
  document.body.append(drawer);
  const toggle = (open) => {
    drawer.hidden = !open;
    bar.querySelector(".jx-open").setAttribute("aria-expanded", String(open));
  };
  bar.querySelector(".jx-open").addEventListener("click", () => toggle(drawer.hidden));
  drawer.querySelector(".jx-close").addEventListener("click", () => toggle(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") toggle(false);
  });
  renderXray();
}
