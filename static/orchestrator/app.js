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
    const id = encodeURIComponent(s.id);
    const tools = (JSON.parse(s.allowed_tools || "[]") || []).join(", ") || "—";
    const ctrl = s.status === "paused"
      ? `<button onclick="act('/api/sessions/${id}/resume')">Resume</button>`
      : `<button onclick="act('/api/sessions/${id}/pause')">Pause</button>`;
    const stop = s.status === "stopped" ? ""
      : `<button class="danger" onclick="act('/api/sessions/${id}/stop')">Stop</button>`;
    const unreg = `<button class="danger" onclick="if(confirm('Gỡ session ${esc(s.name)}?'))act('/api/sessions/${id}/unregister')">Unregister</button>`;
    return `<tr>
      <td>${esc(s.name)}</td>
      <td><code>${esc(s.id)}</code></td>
      <td>${badge(s.status, SESSION_BADGE[s.status])}</td>
      <td class="tools">${esc(tools)}</td>
      <td><div class="actions">${ctrl}${stop}${unreg}</div></td>
    </tr>`;
  }).join("");
  // populate the "to role" dropdown, preserve selection
  const sel = $("sg-role");
  const cur = sel.value;
  sel.innerHTML = list.map((s) => `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join("");
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
}

// ── Form handlers ────────────────────────────────────────────────────────────

function parseTools(str) {
  return (str || "").split(",").map((t) => t.trim()).filter(Boolean);
}
function showMsg(id, text, ok) {
  const el = $(id);
  el.textContent = text;
  el.className = "form-msg " + (ok ? "ok" : "err");
}

async function spawnAgent() {
  const name = $("sp-name").value.trim();
  if (!name) return showMsg("sp-msg", "Cần role/name", false);
  showMsg("sp-msg", "Đang spawn…", true);
  try {
    const r = await api("/api/sessions/spawn", "POST", {
      name, cwd: $("sp-cwd").value.trim(),
      allowed_tools: parseTools($("sp-tools").value),
      init_prompt: $("sp-init").value.trim(),
    });
    showMsg("sp-msg", `Đã spawn '${r.name}' (${r.id})`, true);
    $("sp-name").value = $("sp-init").value = "";
    refreshAll();
  } catch (e) { showMsg("sp-msg", "Lỗi: " + e, false); }
}

async function registerAgent() {
  const id = $("rg-id").value.trim(), name = $("rg-name").value.trim();
  if (!id || !name) return showMsg("rg-msg", "Cần session ID và name", false);
  try {
    await api("/api/sessions", "POST", {
      id, name, cwd: $("rg-cwd").value.trim(),
      allowed_tools: parseTools($("rg-tools").value),
    });
    showMsg("rg-msg", `Đã register '${name}'`, true);
    $("rg-id").value = $("rg-name").value = "";
    refreshAll();
  } catch (e) { showMsg("rg-msg", "Lỗi: " + e, false); }
}

async function sendSignal() {
  const to_role = $("sg-role").value, message = $("sg-msg").value.trim();
  if (!to_role || !message) return showMsg("sg-result", "Cần role và message", false);
  try {
    const r = await api("/api/signals", "POST", {
      to_role, message, from_role: "human",
      requires_approval: $("sg-approval").checked ? 1 : 0,
      dry_run: $("sg-dry").checked ? 1 : 0,
    });
    showMsg("sg-result", `Đã gửi signal #${r.id} → ${to_role}`, true);
    $("sg-msg").value = "";
    refreshAll();
  } catch (e) { showMsg("sg-result", "Lỗi: " + e, false); }
}
window.spawnAgent = spawnAgent; window.registerAgent = registerAgent; window.sendSignal = sendSignal;

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
