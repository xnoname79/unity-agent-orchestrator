// Session Orchestrator dashboard — lightweight, no framework.
// Fetches state from the Control API, refreshes on SSE events.

const $ = (id) => document.getElementById(id);

const SESSION_BADGE = { idle: "b-gray", running: "b-blue", paused: "b-amber", stopped: "b-red" };
const SIGNAL_BADGE = { pending: "b-gray", approved: "b-blue", processing: "b-blue",
                       done: "b-green", failed: "b-red", denied: "b-red", blocked: "b-amber" };
const RUN_BADGE = { ok: "b-green", error: "b-red", running: "b-blue" };

const EV_ICON = { system: "⚙️", thinking: "🧠", text: "💬", tool_use: "🔧",
                  tool_result: "📄", result: "✅", error: "⚠️" };

const EFFORT_OPTS = ["", "low", "medium", "high", "max"];  // "" = default (high)
let DAILY_STEP = 10;  // số run cộng thêm mỗi lần bấm Allow; đồng bộ từ /health lúc load.

let openRunId = null;   // run đang mở trong drawer (null = đóng)
let currentWS = "";     // workspace đang lọc ("" = tất cả, admin view)
let WORKSPACES = [];    // cache danh sách workspace (đồng bộ mỗi refreshAll)

const PAGE = 10;        // số record mỗi lần "+"; hiển thị mới nhất trước
let sigShown = PAGE;    // signal queue: số record đang hiển thị (tăng dần khi bấm +)
let runsShown = PAGE;   // audit log: số record đang hiển thị
let sigHasMore = false, runsHasMore = false;  // còn record cũ hơn để bấm + không

// Bấm "+" ở một bảng → hiển thị thêm PAGE record cũ (load-more, nối tiếp).
function showMore(which) {
  if (which === "signals") sigShown += PAGE;
  else if (which === "runs") runsShown += PAGE;
  refreshAll();
}
window.showMore = showMore;

function badge(text, cls, tip) {
  // tip (tùy chọn): lý do hiển thị khi hover (vd lý do signal bị blocked/failed).
  const t = tip ? ` has-tip" title="${esc(tip)}` : "";
  return `<span class="badge ${cls || "b-gray"}${t}">${text}</span>`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function shortTime(iso) {
  if (!iso) return "";
  return String(iso).slice(11, 19);
}

// Ngày + giờ (YYYY-MM-DD HH:MM:SS) — dùng ở compact drawer, nơi cần biết compact xảy ra hôm nào.
function shortDateTime(iso) {
  if (!iso) return "";
  const s = String(iso);
  return (s.slice(0, 10) + " " + s.slice(11, 19)).trim();
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

// Nén context 1 session: hỏi focus (tùy chọn), enqueue /compact qua endpoint.
async function compactSession(id, name) {
  const focus = prompt(`Compact context cho '${name}'.\nFocus cần giữ lại (bỏ trống nếu không):`, "");
  if (focus === null) return;  // huỷ
  try {
    const r = await api(`/api/sessions/${id}/compact`, "POST", { focus: focus.trim() });
    console.log("compact enqueued", r);
    await refreshAll();
  } catch (e) { console.error(e); alert("Lỗi compact: " + e); }
}
window.compactSession = compactSession;

// Xem compact context MỚI NHẤT của 1 session (metadata + full summary) trong drawer.
async function viewCompact(id, name) {
  openRunId = null;  // rời chế độ xem run-transcript để live-event không chèn nhầm vào đây
  $("dr-title").textContent = `Compact context · ${name}`;
  $("dr-badge").innerHTML = "";
  $("dr-body").innerHTML = `<div class="empty">Đang đọc transcript…</div>`;
  $("drawer").classList.add("open");
  $("drawer-overlay").classList.add("open");
  try {
    const c = await api(`/api/sessions/${id}/compact`);
    if (!c.found) {
      $("dr-badge").innerHTML = badge("chưa compact", "b-gray");
      $("dr-body").innerHTML = `<div class="empty">${esc(c.reason || "Session chưa từng compact.")}</div>`;
      return;
    }
    const b = c.boundary || {};
    $("dr-badge").innerHTML = badge(b.trigger || "compact", b.trigger === "auto" ? "b-amber" : "b-blue");
    const meta = `<div class="ev system">
      <div class="k">⚙️ metadata</div>
      <div class="s">Compact gần nhất: <b>${esc(shortDateTime(b.timestamp) || "?")}</b> · trigger <b>${esc(b.trigger || "?")}</b>
        · pre-tokens <b>${b.pre_tokens != null ? b.pre_tokens.toLocaleString() : "?"}</b>
        · tổng ${c.compact_count} lần compact
        · cập nhật transcript ${esc(shortDateTime(c.mtime))}</div></div>`;
    const summary = `<div class="ev text"><div class="k">📄 summary (${c.summary.length.toLocaleString()} ký tự)</div>
      <div class="s">${esc(c.summary)}</div></div>`;
    $("dr-body").innerHTML = meta + summary;
    $("dr-body").scrollTop = 0;
  } catch (e) {
    $("dr-body").innerHTML = `<div class="empty" style="color:var(--red)">Lỗi đọc compact: ${esc(e)}</div>`;
  }
}
window.viewCompact = viewCompact;

// Xóa 1 signal đã kết thúc + audit log (runs/run_events) của nó. Có confirm vì phá hủy.
async function deleteSignal(id) {
  if (!confirm(`Xóa signal #${id} và toàn bộ audit log của nó? Không thể hoàn tác.`)) return;
  try {
    const r = await api(`/api/signals/${id}`, "DELETE");
    console.log("deleted signal", r);
    if (openRunId != null) closeDrawer();  // drawer có thể đang xem run vừa bị xóa
    await refreshAll();
  } catch (e) { console.error(e); alert("Lỗi xóa signal: " + e); }
}
window.deleteSignal = deleteSignal;

// Đổi model 1 session ngay trên bảng (áp dụng cho các lượt sau).
async function setModel(id, model) {
  try { await api(`/api/sessions/${id}/model`, "POST", { model }); await refreshAll(); }
  catch (e) { console.error(e); alert("Lỗi đổi model: " + e); }
}
window.setModel = setModel;

// Đổi reasoning effort 1 session ngay trên bảng (áp dụng cho các lượt sau).
async function setEffort(id, effort) {
  try { await api(`/api/sessions/${id}/effort`, "POST", { effort }); await refreshAll(); }
  catch (e) { console.error(e); alert("Lỗi đổi effort: " + e); }
}
window.setEffort = setEffort;

// Nới hạn mức run/ngày cho 1 session (Allow +N). Backend tự đưa các signal đang blocked
// của session này về pending để chạy tiếp trong hạn mức mới.
async function allowMore(id, name) {
  try {
    const r = await api(`/api/sessions/${id}/allow`, "POST", {});
    const n = (r.requeued || []).length;
    console.log(`allow ${name}: hạn mức ngày = ${r.daily_limit}, re-queue ${n} signal`);
    await refreshAll();
  } catch (e) { console.error(e); alert("Lỗi Allow: " + e); }
}
window.allowMore = allowMore;

// ── Workspaces (multi-tenant) ────────────────────────────────────────────────

// Tạo workspace mới (orchestrator sinh id + mkdir thư mục). Hỏi tên hiển thị.
async function newWorkspace() {
  const name = prompt("Tên workspace mới (nhãn hiển thị):", "");
  if (name === null) return;
  try {
    const w = await api("/api/workspaces", "POST", { name: name.trim() });
    currentWS = w.id;                 // nhảy vào workspace vừa tạo
    await refreshAll();
    alert(`Đã tạo workspace '${w.name}'\nid: ${w.id}\nthư mục: ${w.root_dir}`);
  } catch (e) { console.error(e); alert("Lỗi tạo workspace: " + e); }
}
window.newWorkspace = newWorkspace;

// Render: grid card workspace (master view) + dropdown workspace ở form spawn.
function renderWorkspaces(list) {
  WORKSPACES = list;
  renderWorkspaceGrid(list);
  // form spawn: dropdown workspace (giữ 'default' để vẫn spawn được vào default).
  const opt = (w) => `<option value="${esc(w.id)}">${esc(w.name || w.id)}${w.status !== "active" ? " (" + w.status + ")" : ""}</option>`;
  const spws = $("sp-ws");
  if (spws) {
    spws.innerHTML = `<option value="">default</option>` +
      list.filter((w) => w.id !== "default").map(opt).join("");
    // trong detail view, ghim dropdown vào workspace đang xem (spawn vào đúng nơi).
    if (currentWS && currentWS !== "default") spws.value = currentWS;
  }
  renderWsBanner();
}

// Grid card: mỗi workspace 1 card, click vào detail view.
function renderWorkspaceGrid(list) {
  const grid = $("ws-grid");
  $("ws-grid-empty").hidden = list.length > 0;
  grid.innerHTML = list.map((w) => {
    const st = badge(w.status, w.status === "active" ? "b-green" : "b-amber");
    return `<div class="ws-card" onclick="selectWorkspace('${esc(w.id)}')">
      <h3>${esc(w.name || w.id)}</h3>
      <div class="ws-id">${esc(w.id)}</div>
      <div class="ws-meta"><span class="ws-count">${w.sessions}</span> session · ${st}</div>
      ${w.root_dir ? `<div class="ws-root" title="${esc(w.root_dir)}">${esc(w.root_dir)}</div>` : ""}
    </div>`;
  }).join("");
}

// Detail view: tiêu đề + nút suspend/activate của workspace đang xem.
function renderWsBanner() {
  const w = WORKSPACES.find((x) => x.id === currentWS);
  if (!currentWS || !w) { $("ws-banner-actions").innerHTML = ""; return; }
  $("ws-detail-title").innerHTML =
    `${esc(w.name || w.id)} ` +
    `${badge(w.status, w.status === "active" ? "b-green" : "b-amber")}`;
  const suspend = w.status === "active"
    ? `<button class="warn" onclick="act('/api/workspaces/${encodeURIComponent(w.id)}/suspend')">Suspend</button>`
    : `<button onclick="act('/api/workspaces/${encodeURIComponent(w.id)}/activate')">Activate</button>`;
  $("ws-banner-actions").innerHTML = w.id === "default" ? "" : suspend;
}

// Master-detail navigation: chọn workspace → detail view; back → list view.
function selectWorkspace(id) {
  currentWS = id;
  sigShown = runsShown = PAGE;   // đổi workspace → reset phân trang
  refreshAll();
}
window.selectWorkspace = selectWorkspace;

function backToList() {
  currentWS = "";
  refreshAll();
}
window.backToList = backToList;

// Query suffix để scope API theo workspace đang lọc.
function wsQuery() { return currentWS ? "?workspace_id=" + encodeURIComponent(currentWS) : ""; }

// ── Renderers ──────────────────────────────────────────────────────────────

function renderSessions(list) {
  const tb = $("sessions");
  $("sessions-empty").hidden = list.length > 0;
  tb.innerHTML = list.map((s) => {
    const id = encodeURIComponent(s.id);
    const tools = (JSON.parse(s.allowed_tools || "[]") || []).join(", ") || "—";
    // paused HOẶC stopped → cho Resume (đưa về idle để nhận signal lại); còn lại → Pause.
    // (Nút Stop đã ẩn nên phải để stopped resume được từ đây, tránh session kẹt trạng thái.)
    const ctrl = (s.status === "paused" || s.status === "stopped")
      ? `<button onclick="act('/api/sessions/${id}/resume')">Resume</button>`
      : `<button onclick="act('/api/sessions/${id}/pause')">Pause</button>`;
    // Nút Stop đã ẩn: run thường chạy hết rồi mới ngừng; cần dừng khẩn thì dùng Kill-switch tổng.
    const compact = `<button onclick="compactSession('${id}','${esc(s.name)}')">🗜 Compact</button>`;
    const viewCompact = `<button class="secondary" onclick="viewCompact('${id}','${esc(s.name)}')">📄 Context</button>`;
    // Cap theo ngày: hiện "đã dùng/hạn mức"; khi chạm trần thì thêm nút Allow +N để nới hôm nay.
    const allow = s.daily_blocked
      ? `<button class="warn" onclick="allowMore('${id}','${esc(s.name)}')">Allow +${DAILY_STEP}</button>`
      : "";
    const unreg = `<button class="danger" onclick="if(confirm('Gỡ session ${esc(s.name)}?'))act('/api/sessions/${id}/unregister')">Unregister</button>`;
    const cur = s.model || "";
    const modelSel = `<input class="mini model-in" list="model-list" value="${esc(cur)}" placeholder="auto"
      onchange="setModel('${id}', this.value.trim())">`;
    const curEff = s.effort || "";
    const effortSel = `<select class="mini" onchange="setEffort('${id}', this.value)">` +
      EFFORT_OPTS.map((e) => `<option value="${e}"${e === curEff ? " selected" : ""}>${e || "default"}</option>`).join("") +
      `</select>`;
    // Ô "hôm nay": số run đã dùng / hạn mức ngày. 0 hạn mức = cap ngày đang tắt → "—".
    const today = s.daily_limit
      ? `<span class="${s.daily_blocked ? "day-hit" : "day-ok"}" title="Số run đã chạy hôm nay / hạn mức ngày">${s.used_today}/${s.daily_limit}</span>`
      : "—";
    // cwd: có thể dài (thư mục workspace) → cắt bớt hiển thị, hover xem đầy đủ.
    const cwd = s.cwd
      ? `<code class="cwd" title="${esc(s.cwd)}">${esc(s.cwd)}</code>`
      : `<span class="cwd-none">—</span>`;
    return `<tr>
      <td>${esc(s.name)}</td>
      <td><code>${esc(s.id)}</code></td>
      <td>${cwd}</td>
      <td>${badge(s.status, SESSION_BADGE[s.status])}</td>
      <td>${modelSel}</td>
      <td>${effortSel}</td>
      <td class="day-cell">${today}</td>
      <td class="tools">${esc(tools)}</td>
      <td><div class="actions">${ctrl}${compact}${viewCompact}${allow}${unreg}</div></td>
    </tr>`;
  }).join("");
}

// ── Tool picker (checklist từ MCP servers của cwd) ───────────────────────────

function toolCheck(val, cls) {
  return `<label class="tool-item ${cls || ""}"><input type="checkbox" value="${esc(val)}"> ${esc(val)}</label>`;
}

function renderTools(data) {
  let html = `<div class="tool-group"><b>Built-in</b>${(data.builtin || []).map((t) => toolCheck(t)).join("")}</div>`;
  for (const [srv, info] of Object.entries(data.mcp || {})) {
    html += `<div class="tool-group"><b>MCP: ${esc(srv)}</b>`;
    html += toolCheck(info.wildcard, "wild");
    html += (info.tools || []).map((t) => toolCheck(t)).join("");
    html += `</div>`;
  }
  return html;
}

async function loadTools(prefix) {
  const cwd = $(prefix + "-cwd").value.trim();
  const box = $(prefix + "-tools");
  box.innerHTML = `<div class="tool-group">Đang tải…</div>`;
  try {
    const data = await api("/api/available-tools?cwd=" + encodeURIComponent(cwd));
    box.innerHTML = renderTools(data);
  } catch (e) {
    box.innerHTML = `<div class="tool-group" style="color:var(--red)">Lỗi tải tools: ${esc(e)}</div>`;
  }
}
window.loadTools = loadTools;

async function loadTemplates() {
  const sel = $("sp-template");
  if (!sel) return;
  try {
    const list = await api("/api/skills/templates");
    sel.innerHTML = `<option value="">— chọn template —</option>` +
      list.map(t => `<option value="${esc(t.name)}" title="${esc(t.description)}">${esc(t.name)}</option>`).join("");
  } catch (e) { /* để dropdown trống nếu lỗi */ }
}

function collectTools(prefix) {
  return [...$(prefix + "-tools").querySelectorAll("input:checked")].map((i) => i.value);
}

// ── Form handlers ────────────────────────────────────────────────────────────

function showMsg(id, text, ok) {
  const el = $(id);
  el.textContent = text;
  el.className = "form-msg " + (ok ? "ok" : "err");
}

async function spawnAgent() {
  const name = $("sp-template").value;
  if (!name) return showMsg("sp-msg", "Cần chọn vai/template", false);
  showMsg("sp-msg", "Đang spawn…", true);
  try {
    const r = await api("/api/sessions/spawn", "POST", {
      name, cwd: $("sp-cwd").value.trim(),
      workspace_id: $("sp-ws").value,       // "" = default; ≠ default thì cwd tự ghim
      model: $("sp-model").value,
      effort: $("sp-effort").value,
      allowed_tools: collectTools("sp"),
      init_prompt: $("sp-init").value.trim(),
    });
    showMsg("sp-msg", `Đã spawn '${r.name}' (${r.id})`, true);
    $("sp-init").value = "";
    $("sp-tools").innerHTML = "";
    refreshAll();
  } catch (e) { showMsg("sp-msg", "Lỗi: " + e, false); }
}

window.spawnAgent = spawnAgent;

// Hàng "load more" ở cuối bảng: nút + hiển thị thêm PAGE record cũ. Ẩn khi đã hết.
function moreRow(which, cols, hasMore, shown) {
  if (!hasMore) {
    // Chỉ hiện dòng "đã hết" khi đang xem nhiều hơn 1 trang (đỡ rối khi ít record).
    if (shown <= PAGE) return "";
    return `<tr class="more-row"><td colspan="${cols}"><span class="more-done">— hết —</span></td></tr>`;
  }
  return `<tr class="more-row"><td colspan="${cols}">
    <button class="more-btn" onclick="showMore('${which}')">+ ${PAGE} cũ hơn</button>
    <span class="more-count">đang xem ${shown}</span></td></tr>`;
}

function renderSignals(list) {
  const tb = $("signals");
  $("signals-empty").hidden = list.length > 0;
  const RERUNNABLE = ["failed", "denied", "blocked"];
  tb.innerHTML = list.map((s) => {
    const needsApproval = s.requires_approval && s.status === "pending";
    let actions = "";
    if (needsApproval) {
      actions = `<button onclick="act('/api/signals/${s.id}/approve')">Approve</button>
         <button class="danger" onclick="act('/api/signals/${s.id}/deny')">Deny</button>`;
    } else {
      if (RERUNNABLE.includes(s.status))
        actions += `<button onclick="act('/api/signals/${s.id}/rerun')">↻ Re-run</button>`;
      if (s.status === "failed")
        actions += `<button class="danger" onclick="deleteSignal(${s.id})">🗑 Delete</button>`;
    }
    return `<tr>
      <td>${s.id}</td>
      <td><code>${esc(s.from_session || "—")} → ${esc(s.to_session)}</code></td>
      <td class="msg" title="${esc(s.message)}">${esc(s.message)}</td>
      <td>${s.requires_approval ? badge("required", "b-amber") : "—"}</td>
      <td>${badge(s.status, SIGNAL_BADGE[s.status], s.reason)}</td>
      <td><div class="actions">${actions}</div></td>
    </tr>`;
  }).join("") + moreRow("signals", 6, sigHasMore, sigShown);
}

function renderRuns(list) {
  const tb = $("runs");
  $("runs-empty").hidden = list.length > 0;
  tb.innerHTML = list.map((r) => {
    const live = r.status === "running" ? " live" : "";
    return `<tr class="run-row${live}" onclick="openRun(${r.id})">
      <td>${r.id}</td>
      <td><code>${esc(r.session_id)}</code></td>
      <td>${r.signal_id ?? "—"}</td>
      <td>${badge(r.status, RUN_BADGE[r.status])}</td>
      <td>${r.tokens || 0}</td>
      <td><code>${shortTime(r.ended_at || r.started_at)}</code></td>
    </tr>`;
  }).join("") + moreRow("runs", 6, runsHasMore, runsShown);
}

// ── Transcript drawer ────────────────────────────────────────────────────────

function evRow(e) {
  const kind = e.kind || "text";
  const icon = EV_ICON[kind] || "•";
  return `<div class="ev ${esc(kind)}">
    <span class="t">${shortTime(e.ts)}</span>
    <div class="k">${icon} ${esc(kind)}</div>
    <div class="s">${esc(e.summary)}</div>
  </div>`;
}

function scrollDrawerBottom() {
  const b = $("dr-body");
  b.scrollTop = b.scrollHeight;
}

async function openRun(runId) {
  openRunId = runId;
  $("dr-title").textContent = "Run #" + runId;
  $("dr-badge").innerHTML = "";
  $("dr-body").innerHTML = `<div class="empty">Đang tải transcript…</div>`;
  $("drawer").classList.add("open");
  $("drawer-overlay").classList.add("open");
  try {
    const events = await api("/api/runs/" + runId + "/events");
    $("dr-body").innerHTML = events.length
      ? events.map(evRow).join("")
      : `<div class="empty">Chưa có bước nào (run có thể đang khởi động).</div>`;
    scrollDrawerBottom();
  } catch (e) {
    $("dr-body").innerHTML = `<div class="empty" style="color:var(--red)">Lỗi tải: ${esc(e)}</div>`;
  }
}
window.openRun = openRun;

function closeDrawer() {
  openRunId = null;
  $("drawer").classList.remove("open");
  $("drawer-overlay").classList.remove("open");
}
window.closeDrawer = closeDrawer;

// Append 1 event live nếu drawer đang mở đúng run đó.
function appendLiveEvent(ev) {
  if (openRunId == null || ev.run_id !== openRunId) return;
  const empty = $("dr-body").querySelector(".empty");
  if (empty) $("dr-body").innerHTML = "";
  $("dr-body").insertAdjacentHTML("beforeend",
    evRow({ kind: ev.kind, summary: ev.summary, ts: ev.ts }));
  scrollDrawerBottom();
}

// ── Data ───────────────────────────────────────────────────────────────────

// Ghép query workspace filter + phân trang (luôn offset=0, lấy từ đầu đến `shown` record —
// nhờ vậy SSE refresh giữ nguyên số đang xem, không nhảy trang).
function pagedQuery(shown) {
  const ws = currentWS ? "workspace_id=" + encodeURIComponent(currentWS) + "&" : "";
  return `?${ws}limit=${shown}&offset=0`;
}

async function refreshAll() {
  try {
    const [workspaces, health] = await Promise.all([api("/api/workspaces"), api("/health")]);
    if (health.daily_allow_step) DAILY_STEP = health.daily_allow_step;
    $("dry").hidden = !health.dry_run;
    // Workspace đang xem bị xóa/không còn → về màn list.
    if (currentWS && !workspaces.some((w) => w.id === currentWS)) currentWS = "";
    renderWorkspaces(workspaces);

    const inDetail = !!currentWS;
    $("ws-list-view").hidden = inDetail;
    $("ws-detail-view").hidden = !inDetail;
    if (!inDetail) return;   // màn list chỉ cần workspaces, khỏi fetch sessions/signals/runs

    const q = wsQuery();
    const [sessions, signals, runs] = await Promise.all([
      api("/api/sessions" + q),
      api("/api/signals" + pagedQuery(sigShown)),
      api("/api/runs" + pagedQuery(runsShown)),
    ]);
    renderSessions(sessions);
    sigHasMore = signals.has_more; renderSignals(signals.items);
    runsHasMore = runs.has_more; renderRuns(runs.items);
  } catch (e) { console.error(e); }
}

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
  es.onmessage = (m) => {
    let ev = null;
    try { ev = JSON.parse(m.data); } catch { /* keepalive */ }
    if (ev && ev.type === "run_event") appendLiveEvent(ev);  // live vào drawer, không cần refetch
    scheduleRefresh();  // tables (debounced)
  };
  es.onerror = () => {
    $("conn").className = "pill dead"; $("conn").textContent = "reconnecting…";
    // EventSource auto-reconnects; refresh once connection likely back
  };
}

refreshAll();
loadTemplates();
connectSSE();
