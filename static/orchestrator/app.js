// Session Orchestrator dashboard — lightweight, no framework.
// Fetches state from the Control API, refreshes on SSE events.

const $ = (id) => document.getElementById(id);

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
    // SKILL của role (playbook nhồi mỗi signal) — luôn hiện, không phụ thuộc đã compact hay chưa.
    const skill = c.skill
      ? `<div class="ev text"><div class="k">🧩 SKILL (${c.skill.length.toLocaleString()} ký tự)</div>
          <div class="s">${esc(c.skill)}</div></div>`
      : `<div class="ev system"><div class="k">🧩 SKILL</div><div class="s">Session chưa có SKILL.</div></div>`;
    if (!c.found) {
      $("dr-badge").innerHTML = badge("chưa compact", "b-gray");
      $("dr-body").innerHTML = skill +
        `<div class="empty">${esc(c.reason || "Session chưa từng compact.")}</div>`;
      $("dr-body").scrollTop = 0;
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
    $("dr-body").innerHTML = skill + meta + summary;
    $("dr-body").scrollTop = 0;
  } catch (e) {
    $("dr-body").innerHTML = `<div class="empty" style="color:var(--red)">Lỗi đọc compact: ${esc(e)}</div>`;
  }
}
window.viewCompact = viewCompact;

// Editor SKILL của role trong drawer: đọc SKILL hiện tại, sửa, upsert vào
// <cwd>/.claude/skills/<name>/SKILL.md (tạo thư mục nếu chưa có, đè nếu đã có).
async function editSkill(id, name) {
  openRunId = null;
  $("dr-title").textContent = `SKILL · ${name}`;
  $("dr-badge").innerHTML = "";
  $("dr-body").innerHTML = `<div class="empty">Đang đọc SKILL…</div>`;
  $("drawer").classList.add("open");
  $("drawer-overlay").classList.add("open");
  try {
    const r = await api(`/api/sessions/${id}/skill`);
    $("dr-body").innerHTML = `
      <div class="ev system"><div class="k">📘 path</div><div class="s">${esc(r.path)}</div></div>
      <textarea id="skill-ta" class="skill-ta" spellcheck="false"
        placeholder="Chưa có SKILL — dán nội dung SKILL.md vào đây rồi bấm Upsert."></textarea>
      <div class="skill-save">
        <button onclick="saveSkill('${id}')">💾 Upsert SKILL</button>
        <span id="skill-msg" class="hint"></span>
      </div>`;
    $("skill-ta").value = r.skill || "";
  } catch (e) {
    $("dr-body").innerHTML = `<div class="empty" style="color:var(--red)">Lỗi đọc SKILL: ${esc(e)}</div>`;
  }
}
window.editSkill = editSkill;

async function saveSkill(id) {
  const content = $("skill-ta").value;
  const msg = $("skill-msg");
  if (!content.trim()) { msg.textContent = "SKILL rỗng — không ghi."; return; }
  msg.textContent = "Đang ghi…";
  try {
    const r = await api(`/api/sessions/${id}/skill`, "POST", { content });
    msg.textContent = `✔ Đã ghi ${r.bytes.toLocaleString()} bytes → ${r.path}`;
  } catch (e) { console.error(e); msg.textContent = "Lỗi ghi: " + e; }
}
window.saveSkill = saveSkill;

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
  renderSpawnPickers();   // form spawn: card picker workspace đồng bộ theo list mới
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
// URL hash #ws=<id> để F5/share link giữ nguyên workspace đang xem.
function selectWorkspace(id) {
  currentWS = id;
  sigShown = runsShown = PAGE;   // đổi workspace → reset phân trang
  location.hash = id ? "ws=" + encodeURIComponent(id) : "";
  refreshAll();
}
window.selectWorkspace = selectWorkspace;

function backToList() {
  currentWS = "";
  location.hash = "";
  refreshAll();
}
window.backToList = backToList;

// Query suffix để scope API theo workspace đang lọc.
function wsQuery() { return currentWS ? "?workspace_id=" + encodeURIComponent(currentWS) : ""; }

// ── Agents canvas (nodeterm-style) ──────────────────────────────────────────
// Mỗi session = 1 card trên canvas pan/zoom; các agent CHUNG cwd (≥2) được bao trong
// 1 group card thư mục. Vị trí card + view (pan/zoom) lưu localStorage theo workspace.

let CV = { k: 1, tx: 40, ty: 40 };  // view transform hiện tại (scale + translate)
let cvWs = null;                    // workspace mà CV đang thuộc về (đổi ws → nạp lại view)
let cvInteracting = false;          // đang pan/kéo card → SSE refresh KHÔNG re-render canvas
let cvPending = null;               // data đến trong lúc kéo → render lại khi thả

const cvStoreKey = () => "orch-canvas." + (currentWS || "default");
function cvLoad() {
  try { return JSON.parse(localStorage.getItem(cvStoreKey())) || {}; } catch { return {}; }
}
function cvSave(patch) {
  const st = { ...cvLoad(), ...patch };
  try { localStorage.setItem(cvStoreKey(), JSON.stringify(st)); } catch { /* full/private mode */ }
}

function applyView() {
  $("world").style.transform = `translate(${CV.tx}px, ${CV.ty}px) scale(${CV.k})`;
  $("cv-zoom").textContent = Math.round(CV.k * 100) + "%";
}

// Zoom quanh tâm khung nhìn (nút +/−) — wheel thì zoom quanh con trỏ (xem cvInit).
function cvZoom(f) {
  const cv = $("canvas"), cx = cv.clientWidth / 2, cy = cv.clientHeight / 2;
  const k2 = Math.min(1.6, Math.max(0.35, CV.k * f));
  CV.tx = cx - (cx - CV.tx) * (k2 / CV.k);
  CV.ty = cy - (cy - CV.ty) * (k2 / CV.k);
  CV.k = k2; applyView(); cvSave({ view: CV });
}
window.cvZoom = cvZoom;

// Fit toàn bộ node vào khung nhìn (padding 40, không phóng quá 100%).
function cvFit() {
  const cv = $("canvas");
  const nodes = [...$("world").children].filter((el) => el.classList.contains("node"));
  if (!nodes.length) { CV = { k: 1, tx: 40, ty: 40 }; applyView(); return; }
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
  for (const el of nodes) {
    const x = parseFloat(el.style.left) || 0, y = parseFloat(el.style.top) || 0;
    x0 = Math.min(x0, x); y0 = Math.min(y0, y);
    x1 = Math.max(x1, x + el.offsetWidth); y1 = Math.max(y1, y + el.offsetHeight);
  }
  const k = Math.min(1, (cv.clientWidth - 80) / (x1 - x0), (cv.clientHeight - 80) / (y1 - y0));
  CV = { k, tx: 40 - x0 * k, ty: 40 - y0 * k };
  applyView(); cvSave({ view: CV });
}
window.cvFit = cvFit;

// ── Terminal nhúng trong card 👑 (xterm.js ↔ /ws/terminal ↔ PTY claude --resume) ──
// Element terminal sống NGOÀI chu trình innerHTML của canvas: tạo 1 lần per session,
// sau mỗi render chỉ re-attach vào .term-slot của card orchestrator → SSE refresh không giết PTY.
let cvTerms = {};  // sid → {sid, host, started, term, fit, ws}
// Khóa terminal orch: run tự động (signal [BÁO CÁO] từ worker) đang/đã chạy trên session
// → PTY cũ hết ngữ cảnh, 2 claude cùng ghi 1 transcript sẽ xung đột. Key theo NAME
// (bền qua xoay session id); chỉ mở lại khi user bấm 🔄.
let termLock = {};  // session name → true

function startTerm(t) {
  t.started = true;
  t.term = new Terminal({ fontSize: 12, cursorBlink: true, scrollback: 5000,
                          theme: { background: "#0c0e12", foreground: "#e6e8eb" } });
  t.fit = new FitAddon.FitAddon();
  t.term.loadAddon(t.fit);
  t.term.open(t.host);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  t.ws = new WebSocket(`${proto}://${location.host}/ws/terminal?session=${encodeURIComponent(t.sid)}`);
  t.ws.binaryType = "arraybuffer";
  t.ws.onopen = () => fitTerm(t);
  t.ws.onmessage = (e) => t.term.write(typeof e.data === "string" ? e.data : new Uint8Array(e.data));
  t.ws.onclose = () => { if (t.term) t.term.write("\r\n\x1b[31m[đã ngắt — bấm 🔄 reload]\x1b[0m\r\n"); };
  t.term.onData((d) => { if (t.ws.readyState === 1) t.ws.send(JSON.stringify({ t: "i", d })); });
}

function fitTerm(t) {
  if (!t.term || !t.fit || !t.host.isConnected) return;
  try { t.fit.fit(); } catch { return; }
  if (t.ws && t.ws.readyState === 1)
    t.ws.send(JSON.stringify({ t: "r", c: t.term.cols, r: t.term.rows }));
}

function destroyTerm(sid) {
  const t = cvTerms[sid];
  if (!t) return;
  if (t.ws) { try { t.ws.close(); } catch { /* đã đóng */ } }
  if (t.term) t.term.dispose();
  t.host.remove();
  delete cvTerms[sid];
}

// Reload session/terminal: hủy PTY cũ, gỡ khóa, refetch data (session id có thể đã xoay
// sau lần resume trước) rồi render lại → attach phiên `claude --resume` mới.
// Nếu run tự động VẪN đang chạy, refreshAll thấy status=running sẽ khóa lại ngay — an toàn.
async function reconnectTerm(sid, name) {
  if (name) delete termLock[name];
  destroyTerm(sid);
  await refreshAll();
}
window.reconnectTerm = reconnectTerm;

// Sau mỗi render: cắm terminal vào slot của các card 👑; hủy terminal của card không còn.
function attachTerms() {
  const seen = new Set();
  for (const slot of $("world").querySelectorAll(".term-slot")) {
    const sid = slot.dataset.sid;
    seen.add(sid);
    let t = cvTerms[sid];
    if (!t) t = cvTerms[sid] = { sid, host: Object.assign(document.createElement("div"),
                                                          { className: "term-host" }),
                                 started: false, term: null, fit: null, ws: null };
    slot.appendChild(t.host);
    // Slot đang khóa: KHÔNG start PTY mới (đợi user bấm 🔄 sau khi run tự động xong).
    requestAnimationFrame(() => {
      if (!t.started) { if (!slot.dataset.lock) startTerm(t); }
      else fitTerm(t);
    });
  }
  for (const sid of Object.keys(cvTerms)) if (!seen.has(sid)) destroyTerm(sid);
}

// Gửi signal nhanh tới 1 agent ngay trên card.
async function sendSignalTo(id, name) {
  const msg = prompt(`Signal tới '${name}':`, "");
  if (!msg || !msg.trim()) return;
  try { await api("/api/signals", "POST", { to_session: id, message: msg.trim() }); await refreshAll(); }
  catch (e) { console.error(e); alert("Lỗi gửi signal: " + e); }
}
window.sendSignalTo = sendSignalTo;

// 1 card agent. needsYou = có signal chờ duyệt tới nó; isOrch = được chọn làm orchestrator của cwd.
function agentCard(s, needsYou, isOrch) {
  const id = encodeURIComponent(s.id);
  const tools = JSON.parse(s.allowed_tools || "[]") || [];
  const ctrl = (s.status === "paused" || s.status === "stopped")
    ? `<button onclick="act('/api/sessions/${id}/resume')" title="Resume">▶</button>`
    : `<button onclick="act('/api/sessions/${id}/pause')" title="Pause">⏸</button>`;
  const allow = s.daily_blocked
    ? `<button class="warn" onclick="allowMore('${id}','${esc(s.name)}')">Allow +${DAILY_STEP}</button>` : "";
  const today = s.daily_limit
    ? `<span class="${s.daily_blocked ? "day-hit" : "day-ok"}" title="run hôm nay / hạn mức">${s.used_today}/${s.daily_limit}</span>`
    : "";
  const effortSel = `<select class="mini" onchange="setEffort('${id}', this.value)">` +
    EFFORT_OPTS.map((e) => `<option value="${e}"${e === (s.effort || "") ? " selected" : ""}>${e || "effort"}</option>`).join("") +
    `</select>`;
  const head = `<div class="node-head">
      <span class="status-dot dot-${esc(s.status)}"></span>
      ${isOrch ? `<span title="Orchestrator của project này">👑</span>` : ""}
      <b title="${esc(s.name)}">${esc(s.name)}</b>
      ${needsYou ? `<span class="needs-badge">NEEDS YOU</span>` : ""}
      <span class="spacer"></span>
      <span class="sid" title="${esc(s.id)}">${esc(s.id)}</span>
    </div>`;
  const ctxBtn = `<button class="secondary" onclick="viewCompact('${id}','${esc(s.name)}')" title="Xem context/SKILL">📄</button>`;
  const unregBtn = `<button class="danger" onclick="if(confirm('Gỡ session ${esc(s.name)}?'))act('/api/sessions/${id}/unregister')" title="Unregister">🗑</button>`;
  const cls = `st-${esc(s.status)}${needsYou ? " needs-you" : ""}`;

  // Card 👑: terminal thật nhúng thẳng trong card, action buttons xếp dọc left bar.
  if (isOrch) {
    // Run tự động (báo cáo worker → run mới) đang chạy HOẶC đang xếp hàng → khóa chat +
    // ngắt PTY cũ ngay (2 claude cùng ghi 1 session = xung đột transcript). Tính cả signal
    // queued để 🔄 trong khe hở giữa 2 run liên tiếp không mở PTY xung đột. Signal pending
    // chờ approval KHÔNG tính (không tự chạy — đừng khóa oan). Khóa giữ tới khi bấm 🔄.
    const queued = (cvLast.signals || []).some((sg) =>
      (sg.to_session === s.id || sg.to_session === s.name) &&
      (sg.status === "processing" || sg.status === "approved" ||
       (sg.status === "pending" && !sg.requires_approval)));
    const busy = s.status === "running" || queued;
    if (busy) termLock[s.name] = true;
    const locked = !!termLock[s.name];
    if (locked) {
      const t = cvTerms[s.id];
      if (t && t.ws && t.ws.readyState <= 1) { try { t.ws.close(); } catch { /* đã đóng */ } }
    }
    const lock = busy
      ? `<div class="term-lock">⏳ Orch đang chạy run tự động (xử lý báo cáo từ agent)…<br>
           Click để xem run · xong sẽ mở lại chat bằng nút 🔄</div>`
      : locked
        ? `<div class="term-lock">✅ Run tự động đã xong — bấm 🔄 để nạp ngữ cảnh mới và chat tiếp.<br>
           Click để xem run vừa chạy.</div>`
        : "";
    return `<div class="agent-card orch-term is-orch ${cls}" data-sid="${esc(s.id)}">
      ${head}
      <div class="orch-body">
        <div class="orch-side">
          ${ctrl}${allow}
          <button onclick="reconnectTerm('${esc(s.id)}','${esc(s.name)}')" title="Reload session/terminal (chạy lại claude --resume)">🔄</button>
          ${ctxBtn}${unregBtn}
        </div>
        <div class="term-slot" data-sid="${esc(s.id)}"${locked ? ` data-lock="1"` : ""}>${lock}</div>
      </div>
    </div>`;
  }

  return `<div class="agent-card ${cls}" data-sid="${esc(s.id)}">
    ${head}
    <div class="agent-body">
      <div class="rw"><input class="mini model-in grow" list="model-list" value="${esc(s.model || "")}"
        placeholder="model: auto" onchange="setModel('${id}', this.value.trim())">${effortSel}</div>
      <div class="rw"><span title="${esc(tools.join(", ") || "full quyền")}">🔧 ${tools.length ? tools.length + " tools" : "all tools"}</span>
        <span class="spacer"></span>${today}</div>
    </div>
    <div class="agent-actions">
      ${ctrl}${allow}
      ${ctxBtn}<button class="secondary" onclick="editSkill('${id}','${esc(s.name)}')"
        title="Update SKILL của role (upsert vào .claude/skills trong cwd project)">📘</button>${unregBtn}
    </div>
  </div>`;
}

// ── Zone (cwd) + orchestrator + chat ────────────────────────────────────────
let cvGroups = [];    // [{cwd, els:[nodeEl]}] — rebuild mỗi render; drag group đọc từ đây
let cvNodeEls = {};   // session_id → node element (để vẽ edge)
let cvEdges = [];     // [{from, to, cls}] resolve từ signal list
let cvLast = { sessions: [], signals: [] };  // data mới nhất (setOrch re-render không cần fetch)

const EDGE_COLORS = { wait: "#f0a020", run: "#4c8dff" };  // done/failed không vẽ mũi tên
const EDGE_DEFS = "<defs>" + Object.entries(EDGE_COLORS).map(([k, c]) =>
  `<marker id="ah-${k}" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
     <path d="M0,0L8,4L0,8z" fill="${c}"/></marker>`).join("") + "</defs>";

// Danh sách session Claude Code CLI (transcript ~/.claude/projects) theo cwd.
// undefined = chưa tải, null = đang tải, [] / [...] = kết quả. Nguồn CHỌN orchestrator —
// khác agent DB (agent do orchestrator spawn, đã là card trên canvas).
let cvClaude = {};

async function loadClaudeSessions(gi, force) {
  const g = cvGroups[gi];
  if (!g || (!force && cvClaude[g.cwd] !== undefined)) return;
  cvClaude[g.cwd] = null;
  try { cvClaude[g.cwd] = await api("/api/claude-sessions?cwd=" + encodeURIComponent(g.cwd)); }
  catch (e) { console.error(e); cvClaude[g.cwd] = []; }
  if (!cvInteracting) renderCanvas(cvLast.sessions, cvLast.signals);
}
window.loadClaudeSessions = loadClaudeSessions;

// Chọn 1 session Claude CLI làm orchestrator chính của project (cwd). Session này nằm ngoài DB
// → auto-register vào orchestrator (engine claude, giữ nguyên id) để nhận signal/chat được.
// Lựa chọn lưu localStorage theo workspace.
async function setOrch(gi, sid) {
  const g = cvGroups[gi];
  if (!g) return;
  const orch = cvLoad().orch || {};
  if (sid) {
    if (!(cvLast.sessions || []).some((s) => s.id === sid)) {
      const base = g.cwd.replace(/\/+$/, "").split("/").pop() || "project";
      try {
        await api("/api/sessions", "POST",
                  { id: sid, name: "orch-" + base, cwd: g.cwd, workspace_id: currentWS,
                    seed_director_skill: true });  // chưa có SKILL → seed playbook director vào cwd
      } catch (e) { console.error(e); alert("Lỗi đăng ký session làm orchestrator: " + e); return; }
    }
    orch[g.cwd] = sid;
  } else delete orch[g.cwd];
  cvSave({ orch });
  await refreshAll();
}
window.setOrch = setOrch;

// ── MCP của project (claude mcp list/add tại cwd) ───────────────────────────
let mcpCache = {};  // cwd → {out} | null (đang tải) | undefined (chưa tải)
let mcpOpen = {};   // cwd → panel đang mở
let mcpDraft = {};  // cwd → text đang gõ trong input add (sống qua re-render SSE)

async function loadMcp(gi, force) {
  const g = cvGroups[gi];
  if (!g || (!force && mcpCache[g.cwd] !== undefined)) return;
  mcpCache[g.cwd] = null;
  if (!cvInteracting) renderCanvas(cvLast.sessions, cvLast.signals);
  try { mcpCache[g.cwd] = await api("/api/mcp?cwd=" + encodeURIComponent(g.cwd)); }
  catch (e) { console.error(e); mcpCache[g.cwd] = { out: "lỗi: " + e }; }
  if (!cvInteracting) renderCanvas(cvLast.sessions, cvLast.signals);
}
window.loadMcp = loadMcp;

function toggleMcp(gi) {
  const g = cvGroups[gi];
  if (!g) return;
  mcpOpen[g.cwd] = !mcpOpen[g.cwd];
  if (mcpOpen[g.cwd]) loadMcp(gi);
  renderCanvas(cvLast.sessions, cvLast.signals);
}
window.toggleMcp = toggleMcp;

function mcpDraftSet(gi, v) { const g = cvGroups[gi]; if (g) mcpDraft[g.cwd] = v; }
window.mcpDraftSet = mcpDraftSet;

// Add MCP: chạy `claude mcp add <args>` tại cwd, rồi reload list — output add hiển thị trên đầu panel.
async function mcpAdd(gi) {
  const g = cvGroups[gi];
  if (!g) return;
  const args = (mcpDraft[g.cwd] || "").trim();
  if (!args) return;
  mcpCache[g.cwd] = null;
  renderCanvas(cvLast.sessions, cvLast.signals);
  try {
    const r = await api("/api/mcp", "POST", { cwd: g.cwd, args });
    mcpDraft[g.cwd] = "";
    const l = await api("/api/mcp?cwd=" + encodeURIComponent(g.cwd));
    mcpCache[g.cwd] = { out: "$ claude mcp add " + args + "\n" + (r.out || "").trim() + "\n\n" + (l.out || "") };
  } catch (e) { console.error(e); mcpCache[g.cwd] = { out: "lỗi add: " + e }; }
  renderCanvas(cvLast.sessions, cvLast.signals);
}
window.mcpAdd = mcpAdd;

function mcpPanelHtml(gi, cwd) {
  if (!mcpOpen[cwd]) return "";
  const m = mcpCache[cwd];
  const body = (m === undefined || m === null) ? "đang tải…" : (m.out || "").trim() || "(chưa có MCP nào)";
  return `<div class="zone-mcp">
    <div class="mcp-head"><b>🔌 MCP của project</b><span class="spacer"></span>
      <button class="secondary" onclick="loadMcp(${gi}, true)" title="Tải lại danh sách MCP">🔄</button>
      <button class="secondary" onclick="toggleMcp(${gi})" title="Đóng panel">✕</button></div>
    <pre class="mcp-list">${esc(body)}</pre>
    <div class="mcp-add">
      <span class="mcp-pre">claude mcp add</span>
      <input class="mini" placeholder="vd: unity npx -y unity-mcp" value="${esc(mcpDraft[cwd] || "")}"
        oninput="mcpDraftSet(${gi}, this.value)" onkeydown="if(event.key==='Enter')mcpAdd(${gi})">
      <button onclick="mcpAdd(${gi})" title="Chạy claude mcp add tại cwd project">➕</button>
    </div>
  </div>`;
}

// Options cho dropdown orchestrator: session Claude CLI của cwd (trừ những id đã là agent DB —
// transcript của agent spawn nằm cùng thư mục; giữ lại id đang được chọn để select không mất giá trị).
function orchOptions(cwd, agentList, orchId) {
  let opts = `<option value="">— chọn orchestrator —</option>`;
  const claude = cvClaude[cwd];
  if (claude === undefined || claude === null)
    return opts + `<option disabled>đang tải sessions…</option>`;
  const agentIds = new Set(agentList.map((s) => s.id));
  const items = claude.filter((c) => !agentIds.has(c.id) || c.id === orchId);
  if (orchId && !items.some((c) => c.id === orchId))
    opts += `<option value="${esc(orchId)}" selected>👑 ${esc(orchId.slice(0, 8))}…</option>`;
  if (!items.length && !orchId)
    opts += `<option disabled>không có session claude nào trong cwd này</option>`;
  return opts + items.map((c) =>
    `<option value="${esc(c.id)}"${c.id === orchId ? " selected" : ""} title="${esc(c.id)} · ${esc(c.mtime)}">` +
    `${esc((c.title || "").slice(0, 48) || c.id.slice(0, 8))} · ${esc(c.id.slice(0, 8))}</option>`).join("");
}

function zoneHtml(gi, cwd, list, orchId) {
  const base = cwd.replace(/\/+$/, "").split("/").pop() || cwd;
  return `<div class="node group-zone" data-nid="g:${esc(cwd)}" data-gi="${gi}">
    <div class="zone-head">
      <div class="zone-title">📁 <b>${esc(base)}</b><span class="g-count">${list.length} agents</span>
        <span class="g-path" title="${esc(cwd)}">${esc(cwd)}</span></div>
      <div class="zone-ctl">
        <select class="mini" onchange="setOrch(${gi}, this.value)"
          title="Session Claude Code của project (từ ~/.claude/projects) làm orchestrator chính">${orchOptions(cwd, list, orchId)}</select>
        <button class="secondary" onclick="loadClaudeSessions(${gi}, true)" title="Tải lại danh sách session">🔄</button>
        <button class="secondary" onclick="toggleMcp(${gi})" title="MCP của project (claude mcp list/add)">🔌 MCP</button>
      </div>
      ${mcpPanelHtml(gi, cwd)}
    </div>
  </div>`;
}

// Zone tự bo quanh member: bbox các node member + header. Gọi sau mỗi lần đặt/kéo node.
function layoutZones() {
  for (const z of $("world").querySelectorAll(".group-zone")) {
    const g = cvGroups[+z.dataset.gi];
    if (!g || !g.els.length) continue;
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    for (const el of g.els) {
      const x = parseFloat(el.style.left) || 0, y = parseFloat(el.style.top) || 0;
      x0 = Math.min(x0, x); y0 = Math.min(y0, y);
      x1 = Math.max(x1, x + el.offsetWidth); y1 = Math.max(y1, y + el.offsetHeight);
    }
    const headH = (z.querySelector(".zone-head") || {}).offsetHeight || 74;
    z.style.left = (x0 - 18) + "px";
    z.style.top = (y0 - headH - 14) + "px";
    z.style.width = Math.max(x1 - x0 + 36, 400) + "px";
    z.style.height = (y1 - y0 + headH + 32) + "px";
  }
}

// Điểm trên biên rect r theo hướng tới (tx,ty) — mũi tên chạm mép card thay vì chui vào giữa.
function rectBorderPoint(r, tx, ty) {
  const cx = r.x + r.w / 2, cy = r.y + r.h / 2;
  const dx = tx - cx, dy = ty - cy;
  if (!dx && !dy) return { x: cx, y: cy };
  const t = Math.min((r.w / 2) / Math.abs(dx || 1e-9), (r.h / 2) / Math.abs(dy || 1e-9));
  return { x: cx + dx * t, y: cy + dy * t };
}

// Vẽ lại toàn bộ mũi tên signal theo vị trí node hiện tại (gọi cả trong lúc kéo).
function redrawEdges() {
  const svg = $("edges");
  if (!svg) return;
  const rect = (el) => ({ x: parseFloat(el.style.left) || 0, y: parseFloat(el.style.top) || 0,
                          w: el.offsetWidth, h: el.offsetHeight });
  let out = "";
  for (const e of cvEdges) {
    const a = cvNodeEls[e.from], b = cvNodeEls[e.to];
    if (!a || !b) continue;
    const ra = rect(a), rb = rect(b);
    const p1 = rectBorderPoint(ra, rb.x + rb.w / 2, rb.y + rb.h / 2);
    const p2 = rectBorderPoint(rb, ra.x + ra.w / 2, ra.y + ra.h / 2);
    out += `<line x1="${p1.x}" y1="${p1.y}" x2="${p2.x}" y2="${p2.y}" class="edge edge-${e.cls}" marker-end="url(#ah-${e.cls})"/>`;
  }
  svg.innerHTML = EDGE_DEFS + out;
}

function renderCanvas(sessions, signals) {
  cvLast = { sessions, signals };
  if (cvInteracting) { cvPending = cvLast; return; }  // đừng phá thao tác kéo
  const world = $("world");
  $("cv-empty").hidden = sessions.length > 0;

  // Signal pending chờ duyệt → card đích sáng "NEEDS YOU" (kiểu nodeterm).
  const needs = new Set((signals || [])
    .filter((s) => s.requires_approval && s.status === "pending")
    .map((s) => s.to_session));

  const st = cvLoad();
  const pos = st.pos || {};
  const orch = st.orch || {};

  // Gom theo cwd: ≥2 agent chung cwd → zone bo quanh; member vẫn là node TỰ DO trên canvas.
  const byCwd = new Map();
  for (const s of sessions) {
    const k = s.cwd || "";
    if (!byCwd.has(k)) byCwd.set(k, []);
    byCwd.get(k).push(s);
  }
  cvGroups = [];
  let zonesHtml = "", nodesHtml = "";
  const nodeMeta = [];  // {sid, cwd, grouped, gi} theo thứ tự render
  for (const [cwd, list] of byCwd) {
    const grouped = !!cwd && list.length >= 2;
    let gi = -1;
    if (grouped) {
      gi = cvGroups.length;
      cvGroups.push({ cwd, els: [] });
      zonesHtml += zoneHtml(gi, cwd, list, orch[cwd]);
    }
    for (const s of list) {
      nodesHtml += `<div class="node" data-nid="s:${esc(s.id)}">${agentCard(s, needs.has(s.id), orch[cwd] === s.id)}</div>`;
      nodeMeta.push({ sid: s.id, cwd, grouped, gi });
    }
  }
  // Thứ tự vẽ: zone (dưới) → edges (giữa) → agent card (trên).
  world.innerHTML = zonesHtml + `<svg id="edges" class="edges"></svg>` + nodesHtml;

  // Đặt vị trí agent: có lưu → dùng lại; mới → xếp cụm theo cwd (seed từ pos group cũ nếu có).
  cvNodeEls = {};
  const agentEls = world.querySelectorAll(".node:not(.group-zone)");
  let cx = 40, cy = 40, rowH = 0;
  const gcur = {};  // cwd → con trỏ xếp lưới 3 cột cho member mới
  nodeMeta.forEach((m, i) => {
    const el = agentEls[i], nid = "s:" + m.sid;
    cvNodeEls[m.sid] = el;
    if (m.grouped) cvGroups[m.gi].els.push(el);
    if (!pos[nid]) {
      if (m.grouped) {
        let gc = gcur[m.cwd];
        if (!gc) {
          const old = pos["g:" + m.cwd];  // migrate: vị trí group-card kiểu cũ làm gốc cụm
          if (old) gc = { x0: old.x + 20, y0: old.y + 90, i: 0 };
          else {
            if (cx + 940 > 1360 && cx > 40) { cx = 40; cy += rowH + 80; rowH = 0; }
            gc = { x0: cx, y0: cy + 80, i: 0 };
            cx += 980; rowH = Math.max(rowH, 480);
          }
          gcur[m.cwd] = gc;
        }
        pos[nid] = { x: gc.x0 + (gc.i % 3) * 300, y: gc.y0 + Math.floor(gc.i / 3) * 200 };
        gc.i++;
      } else {
        if (cx + el.offsetWidth > 1360 && cx > 40) { cx = 40; cy += rowH + 40; rowH = 0; }
        pos[nid] = { x: cx, y: cy };
        cx += el.offsetWidth + 40;
        rowH = Math.max(rowH, el.offsetHeight);
      }
    }
    el.style.left = pos[nid].x + "px";
    el.style.top = pos[nid].y + "px";
  });
  cvSave({ pos });
  layoutZones();
  // Nạp danh sách session Claude CLI cho các cwd chưa có cache (async — về thì re-render).
  cvGroups.forEach((g, gi) => { if (cvClaude[g.cwd] === undefined) loadClaudeSessions(gi); });

  // Mũi tên signal: CHỈ vẽ task ĐANG hoạt động (chạy/chờ) — done/failed ẩn, xem ở History.
  // Resolve from/to về session id (nhận cả id lẫn name), dedup theo cặp (chạy > chờ).
  const byId = {}, byName = {};
  for (const s of sessions) { byId[s.id] = s.id; byName[s.name] = s.id; }
  const pairBest = new Map();
  for (const sg of signals || []) {
    const from = byId[sg.from_session] || byName[sg.from_session];
    const to = byId[sg.to_session] || byName[sg.to_session];
    if (!from || !to || from === to) continue;
    const cls = sg.status === "processing" ? "run"
      : (sg.status === "pending" || sg.status === "approved") ? "wait" : null;
    if (!cls) continue;
    const key = from + "→" + to;
    if (!pairBest.has(key) || cls === "run")
      pairBest.set(key, { from, to, cls });
  }
  cvEdges = [...pairBest.values()];
  redrawEdges();
  attachTerms();  // cắm terminal bền vào card 👑 (sau khi node đã vào DOM)

  // Đổi workspace (hoặc lần đầu) → nạp view đã lưu, chưa có thì fit.
  if (cvWs !== currentWS) {
    cvWs = currentWS;
    if (st.view) { CV = st.view; applyView(); } else cvFit();
  } else applyView();
}

// Pan (kéo nền) / zoom (wheel, quanh con trỏ) / kéo card (header) / kéo zone-head (cả cụm).
// Gắn 1 lần lúc load.
function cvInit() {
  const cv = $("canvas");
  let drag = null;  // {mode:'pan'|'node'|'group', ...}
  cv.addEventListener("pointerdown", (e) => {
    if (e.target.closest("button, select, input, textarea, option")) return;
    if (e.target.closest(".zone-mcp, .cv-overlay")) return;  // panel MCP / overlay toolbar-hint: không pan/kéo
    const head = e.target.closest(".node-head, .zone-head");
    const node = head && head.closest(".node");
    if (node && node.classList.contains("group-zone")) {
      // Kéo header 📁 → di chuyển cả cụm member (zone tự bo theo).
      const g = cvGroups[+node.dataset.gi] || { els: [] };
      drag = { mode: "group", sx: e.clientX, sy: e.clientY,
               parts: g.els.map((el) => ({ el, ox: parseFloat(el.style.left) || 0,
                                           oy: parseFloat(el.style.top) || 0 })) };
    } else if (node) {
      drag = { mode: "node", el: node, nid: node.dataset.nid, sx: e.clientX, sy: e.clientY,
               ox: parseFloat(node.style.left) || 0, oy: parseFloat(node.style.top) || 0 };
    } else if (!e.target.closest(".node")) {
      drag = { mode: "pan", sx: e.clientX, sy: e.clientY, ox: CV.tx, oy: CV.ty };
    } else return;
    cvInteracting = true;
    cv.classList.add("grabbing");
    cv.setPointerCapture(e.pointerId);
  });
  cv.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;  // phân biệt click vs kéo
    if (drag.mode === "pan") { CV.tx = drag.ox + dx; CV.ty = drag.oy + dy; applyView(); return; }
    if (drag.mode === "node") {
      drag.el.style.left = (drag.ox + dx / CV.k) + "px";
      drag.el.style.top = (drag.oy + dy / CV.k) + "px";
    } else {
      for (const p of drag.parts) {
        p.el.style.left = (p.ox + dx / CV.k) + "px";
        p.el.style.top = (p.oy + dy / CV.k) + "px";
      }
    }
    layoutZones(); redrawEdges();  // zone bo theo + mũi tên bám node ngay khi kéo
  });
  const up = () => {
    if (!drag) return;
    if (drag.mode === "node" || drag.mode === "group") {
      const pos = cvLoad().pos || {};
      const save = (el) => {
        pos[el.dataset.nid] = { x: parseFloat(el.style.left) || 0, y: parseFloat(el.style.top) || 0 };
      };
      if (drag.mode === "node") save(drag.el); else drag.parts.forEach((p) => save(p.el));
      cvSave({ pos });
    } else cvSave({ view: CV });
    // Click (không kéo) vào header card agent (kể cả card 👑) → mở drawer run mới nhất.
    if (drag.mode === "node" && !drag.moved) {
      const card = drag.el.querySelector(".agent-card");
      if (card) openSessionRun(card.dataset.sid);
    }
    drag = null; cvInteracting = false;
    cv.classList.remove("grabbing");
    if (cvPending) { const p = cvPending; cvPending = null; renderCanvas(p.sessions, p.signals); }
  };
  cv.addEventListener("pointerup", up);
  cv.addEventListener("pointercancel", up);
  // Click vào THÂN card (ngoài header — header đi đường pointerup ở trên) → drawer run.
  // Card 👑: terminal (.term-slot) miễn trừ, nhưng overlay khóa (.term-lock) thì mở drawer
  // để xem run tự động đang chạy.
  cv.addEventListener("click", (e) => {
    const lock = e.target.closest(".term-lock");
    if (lock) { openSessionRun(lock.closest(".agent-card").dataset.sid); return; }
    if (e.target.closest("button, select, input, textarea, option, .term-slot, .zone-mcp, .node-head, .zone-head")) return;
    const card = e.target.closest(".agent-card");
    if (card) openSessionRun(card.dataset.sid);
  });
  cv.addEventListener("wheel", (e) => {
    if (e.target.closest(".term-slot, .zone-mcp, .cv-overlay")) return;  // wheel trong terminal/panel MCP/overlay = scroll, không zoom
    e.preventDefault();  // wheel = zoom quanh con trỏ (không scroll trang)
    const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    const k2 = Math.min(1.6, Math.max(0.35, CV.k * Math.exp(-e.deltaY * 0.0012)));
    CV.tx = mx - (mx - CV.tx) * (k2 / CV.k);
    CV.ty = my - (my - CV.ty) * (k2 / CV.k);
    CV.k = k2; applyView();
    clearTimeout(cvInit._t); cvInit._t = setTimeout(() => cvSave({ view: CV }), 300);
  }, { passive: false });
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

// ── Spawn form: picker dạng card (workspace / template / model) + duyệt thư mục ──

const MODELS = [
  { id: "", name: "Auto", desc: "Orchestrator / CLI tự chọn model mặc định" },
  { id: "opus", name: "Opus · alias", desc: "Luôn trỏ bản Opus mới nhất" },
  { id: "sonnet", name: "Sonnet · alias", desc: "Cân bằng chất lượng / tốc độ / giá" },
  { id: "haiku", name: "Haiku · alias", desc: "Nhanh và rẻ — việc nhẹ, lặp nhiều" },
  { id: "claude-fable-5", name: "Fable 5", desc: "Mạnh nhất (Claude 5) — reasoning + agentic dài hơi; đắt hơn Opus" },
  { id: "claude-opus-4-8", name: "Opus 4.8", desc: "Opus mới nhất — agentic tự chủ dài hơi, mặc định tốt nhất" },
  { id: "claude-sonnet-5", name: "Sonnet 5", desc: "Gần chất lượng Opus cho code/agentic, giá Sonnet" },
  { id: "claude-haiku-4-5", name: "Haiku 4.5", desc: "Nhanh nhất, rẻ nhất — task đơn giản" },
  { id: "__custom", name: "Tùy chỉnh…", desc: "Tự nhập model id / alias khác (bản cũ: opus-4-7/4-6, sonnet-4-6…)" },
];
let SP_TEMPLATES = [];                          // cache /api/skills/templates
let spSel = { ws: "", template: "", model: "" };  // lựa chọn hiện tại của form spawn

function pickCard(group, val, inner, title) {
  return `<div class="pick-card${spSel[group] === val ? " sel" : ""}"` +
    `${title ? ` title="${esc(title)}"` : ""} onclick="spPick('${group}','${esc(val)}')">${inner}</div>`;
}

function renderSpawnPickers() {
  const wsBox = $("sp-ws-cards");
  if (!wsBox) return;
  const wsItems = [{ id: "", name: "default", note: "workspace chung — cwd tự chọn bên dưới" }]
    .concat(WORKSPACES.filter((w) => w.id !== "default").map((w) => ({
      id: w.id, name: w.name || w.id,
      note: w.id + (w.status !== "active" ? " · " + w.status : ""),
    })));
  wsBox.innerHTML = wsItems.map((w) =>
    pickCard("ws", w.id, `<b>${esc(w.name)}</b><div class="pd">${esc(w.note)}</div>`)).join("");
  $("sp-template-cards").innerHTML = SP_TEMPLATES.length
    ? SP_TEMPLATES.map((t) => pickCard("template", t.name,
        `<b>${esc(t.name)}</b><div class="pd">${esc(t.description || "")}</div>`, t.description)).join("")
    : `<div class="hint">Chưa có template (.claude/skills của repo orchestrator).</div>`;
  $("sp-model-cards").innerHTML = MODELS.map((m) => pickCard("model", m.id,
    `<b>${esc(m.name)}</b>` +
    (m.id && m.id !== "__custom" ? `<code>${esc(m.id)}</code>` : "") +
    `<div class="pd">${esc(m.desc)}</div>`)).join("");
  $("sp-model-custom").hidden = spSel.model !== "__custom";
}

function spPick(group, val) {
  spSel[group] = val;
  renderSpawnPickers();
}
window.spPick = spPick;

// Duyệt thư mục server-side (/api/fs) cho Working dir. Path đi qua data-attribute +
// listener ủy quyền (không nhét vào inline onclick — path có thể chứa ký tự phá attr).
async function browseDir(start) {
  const box = $("sp-dir");
  box.hidden = false;
  box.innerHTML = `<div class="dir-crumb">Đang tải…</div>`;
  try {
    const d = await api("/api/fs" + (start ? "?path=" + encodeURIComponent(start) : ""));
    box.dataset.path = d.path;
    const item = (p, label) => `<div class="dir-item" data-path="${esc(p)}">${label}</div>`;
    box.innerHTML =
      `<div class="dir-crumb">📂 ${esc(d.path)}</div>
       <div class="dir-list">
         ${d.parent ? item(d.parent, "⬆ ..") : ""}
         ${d.dirs.map((n) => item(d.path.endsWith("/") ? d.path + n : d.path + "/" + n,
                                  "📁 " + esc(n))).join("") || `<div class="hint">(không có thư mục con)</div>`}
       </div>
       <div class="dir-actions">
         <button type="button" onclick="pickDir()">✔ Chọn thư mục này</button>
         <button type="button" class="secondary" onclick="closeDirBrowse()">Đóng</button>
       </div>`;
  } catch (e) {
    if (start) return browseDir("");   // path gõ tay sai → fallback về $HOME
    box.innerHTML = `<div class="dir-crumb" style="color:var(--red)">Lỗi: ${esc(e)}</div>`;
  }
}
function pickDir() { $("sp-cwd").value = $("sp-dir").dataset.path || ""; closeDirBrowse(); }
function closeDirBrowse() { $("sp-dir").hidden = true; }
window.browseDir = browseDir; window.pickDir = pickDir; window.closeDirBrowse = closeDirBrowse;

async function loadTemplates() {
  if (!$("sp-template-cards")) return;
  try {
    SP_TEMPLATES = await api("/api/skills/templates");
  } catch (e) { SP_TEMPLATES = []; }
  renderSpawnPickers();
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
  const name = spSel.template;
  if (!name) return showMsg("sp-msg", "Cần chọn vai/template", false);
  showMsg("sp-msg", "Đang spawn…", true);
  try {
    const r = await api("/api/sessions/spawn", "POST", {
      name, cwd: $("sp-cwd").value.trim(),
      workspace_id: spSel.ws,               // "" = default; ≠ default thì cwd tự ghim
      model: spSel.model === "__custom" ? $("sp-model").value.trim() : spSel.model,
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
  const n = e.n || 1;
  // Card tool_use gộp luôn kết quả (tool_result kế tiếp) bên dưới; chưa có = "…" (pend).
  let tr = "";
  if (kind === "tool_use") {
    const r = e.result_ev;
    const cls = r ? ((r.summary || "").startsWith("⚠") ? " err" : "") : " pend";
    tr = `<div class="tr${cls}">${r ? esc(r.summary) : "…"}</div>`;
  }
  return `<div class="ev ${esc(kind)}" data-kind="${esc(kind)}" data-sum="${esc(e.summary)}" data-n="${n}">
    <span class="ev-ic">${icon}</span>
    <div class="ev-main">
      <div class="ev-meta">
        <span class="k">${kind === "tool_use" ? "tool" : esc(kind)}</span>
        <span class="rep" ${n > 1 ? "" : "hidden"}>×${n}</span>
        <span class="t">${shortTime(e.ts)}</span>
      </div>
      <div class="s">${esc(e.summary)}</div>${tr}
    </div>
  </div>`;
}

// Ghép tool_result vào tool_use đứng ngay trước nó → 1 card gọi + kết quả.
function pairTools(evs) {
  const out = [];
  for (const e of evs) {
    const last = out[out.length - 1];
    if (e.kind === "tool_result" && last && last.kind === "tool_use" && !last.result_ev) {
      last.result_ev = e;
      continue;
    }
    out.push(e);
  }
  return out;
}

// Gộp các event LIÊN TIẾP trùng kind+summary (vd system lặp) thành 1 row ×N.
function coalesceEvents(events) {
  const out = [];
  for (const e of events) {
    const last = out[out.length - 1];
    if (last && last.kind === e.kind && last.summary === e.summary) { last.n++; last.ts = e.ts; }
    else out.push({ ...e, n: 1 });
  }
  return out;
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
      ? pairTools(coalesceEvents(events)).map(evRow).join("")
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

// Click card agent trên canvas → drawer run MỚI NHẤT của session đó
// (đang chạy thì openRun đặt openRunId → SSE append messages live).
async function openSessionRun(sid) {
  const s = (cvLast.sessions || []).find((x) => x.id === sid);
  openRunId = null;
  $("dr-title").textContent = `Run · ${s ? s.name : sid}`;
  $("dr-badge").innerHTML = "";
  $("dr-body").innerHTML = `<div class="empty">Đang tìm run…</div>`;
  $("drawer").classList.add("open");
  $("drawer-overlay").classList.add("open");
  try {
    const runs = await api(`/api/sessions/${encodeURIComponent(sid)}/runs`);
    if (!runs.length) {
      $("dr-body").innerHTML = `<div class="empty">Session chưa có run nào.</div>`;
      return;
    }
    await openRun(runs[0].id);
    $("dr-title").textContent = `Run #${runs[0].id} · ${s ? s.name : ""}`;
    $("dr-badge").innerHTML = badge(runs[0].status, RUN_BADGE[runs[0].status]);
  } catch (e) {
    $("dr-body").innerHTML = `<div class="empty" style="color:var(--red)">Lỗi: ${esc(e)}</div>`;
  }
}
window.openSessionRun = openSessionRun;

// ── Tabs: Agents (canvas) / History (signal queue + audit log) ──────────────
function switchTab(name) {
  $("tab-agents").hidden = name !== "agents";
  $("tab-history").hidden = name !== "history";
  $("tab-btn-agents").classList.toggle("active", name === "agents");
  $("tab-btn-history").classList.toggle("active", name === "history");
  try { localStorage.setItem("orch-tab", name); } catch { /* private mode */ }
  // Quay lại tab agents: xterm cần fit lại (lúc ẩn display:none đo được 0×0).
  if (name === "agents") requestAnimationFrame(() => Object.values(cvTerms).forEach(fitTerm));
}
window.switchTab = switchTab;

// Append 1 event live nếu drawer đang mở đúng run đó.
// Trùng kind+summary với row cuối → bump ×N thay vì thêm row (chống spam system lặp).
function appendLiveEvent(ev) {
  if (openRunId == null || ev.run_id !== openRunId) return;
  const body = $("dr-body");
  const empty = body.querySelector(".empty");
  if (empty) body.innerHTML = "";
  const last = body.lastElementChild;
  // tool_result → điền vào card tool_use đang chờ (".tr.pend") thay vì thêm card mới.
  if (ev.kind === "tool_result" && last && last.dataset.kind === "tool_use") {
    const tr = last.querySelector(".tr.pend");
    if (tr) {
      tr.textContent = ev.summary;
      tr.classList.remove("pend");
      if ((ev.summary || "").startsWith("⚠")) tr.classList.add("err");
      last.querySelector(".t").textContent = shortTime(ev.ts);
      scrollDrawerBottom();
      return;
    }
  }
  if (last && last.dataset.kind === ev.kind && last.dataset.sum === ev.summary) {
    const n = (+last.dataset.n || 1) + 1;
    last.dataset.n = n;
    const rep = last.querySelector(".rep");
    rep.hidden = false; rep.textContent = "×" + n;
    last.querySelector(".t").textContent = shortTime(ev.ts);
  } else {
    body.insertAdjacentHTML("beforeend", evRow({ kind: ev.kind, summary: ev.summary, ts: ev.ts }));
  }
  scrollDrawerBottom();
}

// Row thinking/tool dài bị clamp — click để mở/thu (bỏ qua khi đang bôi đen copy).
$("dr-body").addEventListener("click", (e) => {
  if (getSelection().toString()) return;
  const row = e.target.closest(".ev");
  if (row) row.classList.toggle("open");
});

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
    $("hdr-ws").hidden = !inDetail;   // breadcrumb + tabs trong header chỉ hiện ở detail view
    if (!inDetail) return;   // màn list chỉ cần workspaces, khỏi fetch sessions/signals/runs

    const q = wsQuery();
    const [sessions, signals, runs] = await Promise.all([
      api("/api/sessions" + q),
      api("/api/signals" + pagedQuery(sigShown)),
      api("/api/runs" + pagedQuery(runsShown)),
    ]);
    renderCanvas(sessions, signals.items);
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

// Deep-link: mở lại đúng workspace từ URL hash (#ws=<id>).
if (location.hash.startsWith("#ws=")) currentWS = decodeURIComponent(location.hash.slice(4));
cvInit();
// Duyệt thư mục: click folder trong panel → đi sâu vào (path nằm ở data-path, không inline).
$("sp-dir").addEventListener("click", (e) => {
  const it = e.target.closest(".dir-item");
  if (it) browseDir(it.dataset.path);
});
try { switchTab(localStorage.getItem("orch-tab") || "agents"); } catch { /* tab mặc định */ }
refreshAll();
loadTemplates();
if (!location.search.includes("nosse")) connectSSE();  // ?nosse: tắt SSE khi debug/test headless
