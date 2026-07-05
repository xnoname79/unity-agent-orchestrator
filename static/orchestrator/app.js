// Session Orchestrator dashboard — lightweight, no framework.
// Fetches state from the Control API, refreshes on SSE events.

const $ = (id) => document.getElementById(id);

const SESSION_BADGE = { idle: "b-gray", running: "b-blue", paused: "b-amber", stopped: "b-red" };
const SIGNAL_BADGE = { pending: "b-gray", approved: "b-blue", processing: "b-blue",
                       done: "b-green", failed: "b-red", denied: "b-red", blocked: "b-amber" };
const RUN_BADGE = { ok: "b-green", error: "b-red" };

let killOn = false;

function badge(text, cls) {
  return `<span class="badge ${cls || "b-gray"}">${text}</span>`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function shortTime(iso) {
  if (!iso) return "";
  return String(iso).slice(11, 19);
}

async function api(path, method = "GET", body) {
  const opt = { method, headers: {} };
  if (body) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  return r.ok ? r.json().catch(() => ({})) : Promise.reject(await r.text());
}

async function act(path, method = "POST") {
  try { await api(path, method); await refreshAll(); }
  catch (e) { console.error(e); }
}
window.act = act;

// ── Renderers ──────────────────────────────────────────────────────────────

function renderSessions(list) {
  const tb = $("sessions");
  $("sessions-empty").hidden = list.length > 0;
  tb.innerHTML = list.map((s) => {
    const tools = (JSON.parse(s.allowed_tools || "[]") || []).join(", ") || "—";
    const ctrl = s.status === "paused"
      ? `<button onclick="act('/api/sessions/${encodeURIComponent(s.id)}/resume')">Resume</button>`
      : `<button onclick="act('/api/sessions/${encodeURIComponent(s.id)}/pause')">Pause</button>`;
    const stop = s.status === "stopped" ? ""
      : `<button class="danger" onclick="act('/api/sessions/${encodeURIComponent(s.id)}/stop')">Stop</button>`;
    return `<tr>
      <td>${esc(s.name)}</td>
      <td><code>${esc(s.id)}</code></td>
      <td>${badge(s.status, SESSION_BADGE[s.status])}</td>
      <td class="tools">${esc(tools)}</td>
      <td><div class="actions">${ctrl}${stop}</div></td>
    </tr>`;
  }).join("");
}

function renderSignals(list) {
  const tb = $("signals");
  $("signals-empty").hidden = list.length > 0;
  tb.innerHTML = list.map((s) => {
    const needsApproval = s.requires_approval && s.status === "pending";
    const actions = needsApproval
      ? `<button onclick="act('/api/signals/${s.id}/approve')">Approve</button>
         <button class="danger" onclick="act('/api/signals/${s.id}/deny')">Deny</button>`
      : "";
    return `<tr>
      <td>${s.id}</td>
      <td><code>${esc(s.from_session || "—")} → ${esc(s.to_session)}</code></td>
      <td class="msg" title="${esc(s.message)}">${esc(s.message)}</td>
      <td>${s.requires_approval ? badge("required", "b-amber") : "—"}</td>
      <td>${badge(s.status, SIGNAL_BADGE[s.status])}</td>
      <td><div class="actions">${actions}</div></td>
    </tr>`;
  }).join("");
}

function renderRuns(list) {
  const tb = $("runs");
  $("runs-empty").hidden = list.length > 0;
  tb.innerHTML = list.map((r) => `<tr>
      <td>${r.id}</td>
      <td><code>${esc(r.session_id)}</code></td>
      <td>${r.signal_id ?? "—"}</td>
      <td>${badge(r.status, RUN_BADGE[r.status])}</td>
      <td>${r.tokens || 0}</td>
      <td><code>${shortTime(r.ended_at || r.started_at)}</code></td>
    </tr>`).join("");
}

// ── Data ───────────────────────────────────────────────────────────────────

async function refreshAll() {
  try {
    const [sessions, signals, runs, health] = await Promise.all([
      api("/api/sessions"), api("/api/signals"), api("/api/runs"), api("/health"),
    ]);
    renderSessions(sessions);
    renderSignals(signals);
    renderRuns(runs);
    $("dry").hidden = !health.dry_run;
    setKill(health.kill_switch);
  } catch (e) { console.error(e); }
}

function setKill(on) {
  killOn = on;
  const btn = $("killBtn");
  btn.textContent = on ? "RESUME ALL" : "STOP ALL";
  btn.classList.toggle("on", on);
}

$("killBtn").onclick = () => act(killOn ? "/api/resume-all" : "/api/stop-all");

// ── Live updates (SSE) ───────────────────────────────────────────────────────

let debounce;
function scheduleRefresh() {
  clearTimeout(debounce);
  debounce = setTimeout(refreshAll, 150);
}

function connectSSE() {
  const es = new EventSource("/api/events");
  es.addEventListener("ready", () => $("conn").className = "pill live", $("conn").textContent = "live");
  es.onopen = () => { $("conn").className = "pill live"; $("conn").textContent = "live"; };
  es.onmessage = () => scheduleRefresh();
  es.onerror = () => {
    $("conn").className = "pill dead"; $("conn").textContent = "reconnecting…";
    // EventSource auto-reconnects; refresh once connection likely back
  };
}

refreshAll();
connectSSE();
