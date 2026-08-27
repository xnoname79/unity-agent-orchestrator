// Session Orchestrator dashboard — lightweight, no framework.
// Fetches state from the Control API, refreshes on SSE events.

const $ = (id) => document.getElementById(id);

// Cùng một file index.html chạy hai vai:
//   SHELL  (/)            — header, tab workspace, panel side-by-side. Không có canvas.
//   PANE   (/?pane=1&ws=) — một workspace trong iframe: canvas + inspector + history.
// Nhờ vậy 2 workspace mở song song mà KHÔNG phải bóc mọi state toàn cục của canvas
// (CV, cvTerms, cvNodeEls, currentWS…) thành object per-pane: mỗi pane là một document
// riêng nên các biến đó vốn đã tách sẵn.
const PANE = new URLSearchParams(location.search).has("pane");

// <svg class="ic"><use href="#i-play"/></svg> — sprite nằm cuối index.html.
const ic = (name, cls) => `<svg class="ic ${cls || ""}"><use href="#i-${name}"/></svg>`;

// ── Theme ───────────────────────────────────────────────────────────────────
// 'auto' bám prefers-color-scheme; light/dark ghim cứng. Giá trị ghi ra
// documentElement.dataset.theme — CSS chỉ cần một khối [data-theme="dark"].
const THEMES = ["auto", "light", "dark"];
function themePref() {
  try { return localStorage.getItem("orch-theme") || "auto"; } catch { return "auto"; }
}
function applyTheme(pref) {
  const dark = pref === "dark"
    || (pref === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  const btn = $("theme-btn");
  if (btn) btn.title = `Theme: ${pref} — click to change`;
  // Pane đọc localStorage lúc boot, nhưng đang chạy thì không biết shell vừa đổi → báo xuống.
  for (const f of document.querySelectorAll("iframe.pane"))
    try { f.contentWindow.postMessage({ t: "theme", pref }, location.origin); } catch { /* chưa load */ }
}
function cycleTheme() {
  const next = THEMES[(THEMES.indexOf(themePref()) + 1) % THEMES.length];
  try { localStorage.setItem("orch-theme", next); } catch { /* private mode */ }
  applyTheme(next);
}
window.cycleTheme = cycleTheme;

const SIGNAL_BADGE = { pending: "b-gray", approved: "b-blue", processing: "b-blue",
                       done: "b-green", failed: "b-red", denied: "b-red", blocked: "b-amber" };
const RUN_BADGE = { ok: "b-green", error: "b-red", running: "b-blue" };

const EV_ICON = { system: "⚙️", thinking: "🧠", text: "💬", tool_use: "🔧",
                  tool_result: "📄", result: "✅", error: "⚠️" };

// Effort: 1 thang chung, trần khác nhau theo engine/model. PHẢI khớp EFFORT_LADDER /
// CLAUDE_MAX_EFFORT / CODEX_MAX_EFFORT bên service (check_ui_engine.py đối chiếu) — lệch thì
// UI mời chọn mức mà service sẽ lặng lẽ hạ xuống.
const EFFORT_LADDER = ["low", "medium", "high", "xhigh", "max", "ultra"];
const CLAUDE_MAX_EFFORT = "max";
const CODEX_MAX_EFFORT = { "gpt-5.6-terra": "ultra", "gpt-5.6-luna": "max" };
const CODEX_MAX_EFFORT_DEFAULT = "xhigh";

// Mức cao nhất model này nhận (claude theo CLI, codex theo TỪNG model).
function effortCeiling(model) {
  if (engineOfModel(model) === "claude") return CLAUDE_MAX_EFFORT;
  const m = (model || "").trim().toLowerCase();
  return CODEX_MAX_EFFORT[m.includes(":") ? m.split(":")[1].trim() : ""] || CODEX_MAX_EFFORT_DEFAULT;
}

// Các mức chọn được cho model này ("" = theo mặc định orchestrator).
function effortOptsFor(model) {
  return ["", ...EFFORT_LADDER.slice(0, EFFORT_LADDER.indexOf(effortCeiling(model)) + 1)];
}
let DAILY_STEP = 10;  // số run cộng thêm mỗi lần bấm Allow; đồng bộ từ /health lúc load.
let DEFAULT_EFFORT = "high";  // nhãn cho option "" ở picker effort; đồng bộ từ /health lúc load.
let PAIR_CAP = 4;        // trần ping-pong mỗi cặp agent; đồng bộ từ /health.
let PAIR_WINDOW_MIN = 60;  // cửa sổ đếm, phút; đồng bộ từ /health.

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
  const focus = prompt(`Compact context for '${name}'.\nWhat to keep in focus (leave blank for none):`, "");
  if (focus === null) return;  // huỷ
  try {
    const r = await api(`/api/sessions/${id}/compact`, "POST", { focus: focus.trim() });
    console.log("compact enqueued", r);
    await refreshAll();
  } catch (e) { console.error(e); alert("Compact failed: " + e); }
}
window.compactSession = compactSession;

// Xem compact context MỚI NHẤT của 1 session (metadata + full summary) trong drawer.
async function viewCompact(id, name) {
  openRunId = null;  // rời chế độ xem run-transcript để live-event không chèn nhầm vào đây
  $("dr-title").textContent = `Compact context · ${name}`;
  $("dr-badge").innerHTML = "";
  $("dr-body").innerHTML = `<div class="empty">Reading transcript…</div>`;
  $("drawer").classList.add("open");
  $("drawer-overlay").classList.add("open");
  try {
    const c = await api(`/api/sessions/${id}/compact`);
    // SKILL của role (playbook nhồi mỗi signal) — luôn hiện, không phụ thuộc đã compact hay chưa.
    const skill = c.skill
      ? `<div class="ev text"><div class="k">🧩 SKILL (${c.skill.length.toLocaleString()} chars)</div>
          <div class="s">${esc(c.skill)}</div></div>`
      : `<div class="ev system"><div class="k">🧩 SKILL</div><div class="s">This session has no SKILL yet.</div></div>`;
    if (!c.found) {
      $("dr-badge").innerHTML = badge("never compacted", "b-gray");
      $("dr-body").innerHTML = skill +
        `<div class="empty">${esc(c.reason || "This session has never been compacted.")}</div>`;
      $("dr-body").scrollTop = 0;
      return;
    }
    const b = c.boundary || {};
    $("dr-badge").innerHTML = badge(b.trigger || "compact", b.trigger === "auto" ? "b-amber" : "b-blue");
    const meta = `<div class="ev system">
      <div class="k">⚙️ metadata</div>
      <div class="s">Last compact: <b>${esc(shortDateTime(b.timestamp) || "?")}</b> · trigger <b>${esc(b.trigger || "?")}</b>
        · pre-tokens <b>${b.pre_tokens != null ? b.pre_tokens.toLocaleString() : "?"}</b>
        · ${c.compact_count} compacts total
        · transcript updated ${esc(shortDateTime(c.mtime))}</div></div>`;
    const summary = `<div class="ev text"><div class="k">📄 summary (${c.summary.length.toLocaleString()} chars)</div>
      <div class="s">${esc(c.summary)}</div></div>`;
    $("dr-body").innerHTML = skill + meta + summary;
    $("dr-body").scrollTop = 0;
  } catch (e) {
    $("dr-body").innerHTML = `<div class="empty" style="color:var(--red)">Could not read compact state: ${esc(e)}</div>`;
  }
}
window.viewCompact = viewCompact;

// Editor SKILL của role trong drawer: đọc SKILL hiện tại, sửa, upsert vào
// <cwd>/.claude/skills/<name>/SKILL.md (tạo thư mục nếu chưa có, đè nếu đã có).
async function editSkill(id, name) {
  openRunId = null;
  $("dr-title").textContent = `SKILL · ${name}`;
  $("dr-badge").innerHTML = "";
  $("dr-body").innerHTML = `<div class="empty">Reading SKILL…</div>`;
  $("drawer").classList.add("open");
  $("drawer-overlay").classList.add("open");
  try {
    const r = await api(`/api/sessions/${id}/skill`);
    $("dr-body").innerHTML = `
      <div class="ev system"><div class="k">📘 path</div><div class="s">${esc(r.path)}</div></div>
      <textarea id="skill-ta" class="skill-ta" spellcheck="false"
        placeholder="No SKILL yet — paste SKILL.md content here and press Upsert."></textarea>
      <div class="skill-save">
        <button onclick="saveSkill('${id}')">💾 Upsert SKILL</button>
        <span id="skill-msg" class="hint"></span>
      </div>`;
    $("skill-ta").value = r.skill || "";
  } catch (e) {
    $("dr-body").innerHTML = `<div class="empty" style="color:var(--red)">Could not read SKILL: ${esc(e)}</div>`;
  }
}
window.editSkill = editSkill;

async function saveSkill(id) {
  const content = $("skill-ta").value;
  const msg = $("skill-msg");
  if (!content.trim()) { msg.textContent = "SKILL is empty — nothing written."; return; }
  msg.textContent = "Writing…";
  try {
    const r = await api(`/api/sessions/${id}/skill`, "POST", { content });
    msg.textContent = `✔ Wrote ${r.bytes.toLocaleString()} bytes → ${r.path}`;
  } catch (e) { console.error(e); msg.textContent = "Write failed: " + e; }
}
window.saveSkill = saveSkill;

// Xóa 1 signal đã kết thúc + audit log (runs/run_events) của nó. Có confirm vì phá hủy.
async function deleteSignal(id) {
  if (!confirm(`Delete signal #${id} and its entire audit log? This cannot be undone.`)) return;
  try {
    const r = await api(`/api/signals/${id}`, "DELETE");
    console.log("deleted signal", r);
    if (openRunId != null) closeDrawer();  // drawer có thể đang xem run vừa bị xóa
    await refreshAll();
  } catch (e) { console.error(e); alert("Could not delete signal: " + e); }
}
window.deleteSignal = deleteSignal;

// Đổi model 1 session ngay trên bảng (áp dụng cho các lượt sau).
// Chuyển agent sang workspace khác — để nó signal được với nhóm bên đó (routing resolve theo
// role + workspace). cwd không đổi, file của agent nằm nguyên chỗ cũ.
async function moveWorkspace(id, name, wid, label) {
  if (!wid) return;
  if (!confirm(`Move '${name}' to workspace '${label}'?\n\n`
             + `Its folder and files stay where they are. Past signals and runs stay in the old `
             + `workspace, so its history there does not follow it.`)) { renderInspector(); return; }
  let body = { workspace_id: wid };
  for (;;) {
    try {
      await api(`/api/sessions/${id}/workspace`, "POST", body);
      // Vị trí card cũ chỉ còn là rác trong store của workspace cũ — canvas bên kia không còn card này.
      try {
        const k = "orch-canvas." + currentWS, st = JSON.parse(localStorage.getItem(k)) || {};
        delete st[name]; delete st[body.name || name];
        localStorage.setItem(k, JSON.stringify(st));
      } catch { /* private mode */ }
      await refreshAll();
      return;
    } catch (e) {
      // Trùng tên vai bên đó: hai vai cùng tên trong một workspace là signal đi lúc bản này lúc
      // bản kia, im lặng. Backend chặn — hỏi tên mới rồi chuyển kèm đổi tên trong một nhịp.
      const msg = String(e);
      if (!msg.includes("already has a session named")) {
        alert("Could not move: " + msg); renderInspector(); return;
      }
      const alt = prompt(`Workspace '${label}' already has a role named '${body.name || name}'.\n`
                       + `New name for this agent there:`, (body.name || name) + "-2");
      if (!alt || !alt.trim()) { renderInspector(); return; }
      body = { ...body, name: alt.trim() };
    }
  }
}
window.moveWorkspace = moveWorkspace;

async function setModel(id, model) {
  try { await api(`/api/sessions/${id}/model`, "POST", { model }); await refreshAll(); }
  catch (e) { console.error(e); alert("Could not change model: " + e); }
}
window.setModel = setModel;

// Đổi reasoning effort 1 session ngay trên bảng (áp dụng cho các lượt sau).
async function setEffort(id, effort) {
  try { await api(`/api/sessions/${id}/effort`, "POST", { effort }); await refreshAll(); }
  catch (e) { console.error(e); alert("Could not change effort: " + e); }
}
window.setEffort = setEffort;

// Nới hạn mức run/ngày cho 1 session (Allow +N). Backend tự đưa các signal đang blocked
// của session này về pending để chạy tiếp trong hạn mức mới.
async function allowMore(id, name) {
  try {
    const r = await api(`/api/sessions/${id}/allow`, "POST", {});
    const n = (r.requeued || []).length;
    console.log(`allow ${name}: daily limit = ${r.daily_limit}, re-queued ${n} signal(s)`);
    await refreshAll();
  } catch (e) { console.error(e); alert("Allow failed: " + e); }
}
window.allowMore = allowMore;

// ── Workspaces (multi-tenant) ────────────────────────────────────────────────

// Tạo workspace mới (orchestrator sinh id + mkdir thư mục). Hỏi tên hiển thị.
async function newWorkspace() {
  const name = prompt("New workspace name (display label):", "");
  if (name === null) return;
  try {
    const w = await api("/api/workspaces", "POST", { name: name.trim() });
    await refreshAll();               // grid phải có card mới trước khi mở nó thành tab
    selectWorkspace(w.id);
    alert(`Created workspace '${w.name}'\nid: ${w.id}\nfolder: ${w.root_dir}`);
  } catch (e) { console.error(e); alert("Could not create workspace: " + e); }
}
window.newWorkspace = newWorkspace;

// Render: grid card workspace (shell) + dropdown workspace ở form spawn.
function renderWorkspaces(list) {
  WORKSPACES = list;
  if (PANE) { renderWsBanner(); return; }   // pane không có grid/form spawn
  renderWorkspaceGrid(list);
  renderSpawnPickers();   // form spawn: card picker workspace đồng bộ theo list mới
  renderShellTabs();
  renderPanes();
}

// Grid card: mỗi workspace 1 card, click = mở thành tab.
function renderWorkspaceGrid(list) {
  const grid = $("ws-grid");
  $("ws-grid-empty").hidden = list.length > 0;
  grid.innerHTML = list.map((w) => {
    const st = badge(w.status, w.status === "active" ? "b-green" : "b-amber");
    const open = OPEN.includes(w.id);
    return `<div class="ws-card${open ? " open" : ""}" onclick="selectWorkspace('${esc(w.id)}')">
      ${open ? `<span class="open-tag">OPEN</span>` : ""}
      <h3>${esc(w.name || w.id)}</h3>
      <div class="ws-id">${esc(w.id)}</div>
      <div class="ws-meta"><span class="ws-count">${w.sessions}</span> session · ${st}</div>
      ${w.root_dir ? `<div class="ws-root" title="${esc(w.root_dir)}">${esc(w.root_dir)}</div>` : ""}
    </div>`;
  }).join("");
}

// ── Shell: tab workspace + pane side-by-side ────────────────────────────────
// Tối đa 2 workspace mở cùng lúc. Mỗi cái là một <iframe> (?pane=1) — xem chú thích
// ở đầu file. iframe KHÔNG BAO GIỜ được di chuyển trong DOM: trình duyệt reload nó,
// terminal bên trong chết theo. Nên OPEN chỉ push cuối / remove, và thứ tự DOM luôn
// khớp OPEN; đầy chỗ thì đóng tab hiện hành TRƯỚC rồi mới push.
const MAX_PANES = 2;
let OPEN = [];        // workspace id đang mở
let ACTIVE = -1;      // tab đang xem; -1 = màn Home
let SPLIT = false;    // hiện cả 2 pane cạnh nhau
let SPLIT_PCT = 50;   // bề rộng pane trái, %

function shellLoad() {
  try { return JSON.parse(localStorage.getItem("orch-shell")) || {}; } catch { return {}; }
}
function shellSave() {
  try {
    localStorage.setItem("orch-shell",
      JSON.stringify({ open: OPEN, active: ACTIVE, split: SPLIT, pct: SPLIT_PCT }));
  } catch { /* private mode */ }
  // Deep-link: F5 hoặc chia sẻ URL giữ nguyên tab đang mở.
  const q = OPEN.length ? "?ws=" + OPEN.map(encodeURIComponent).join(",") + (SPLIT ? "&split=1" : "") : "";
  history.replaceState(null, "", location.pathname + q);
}

function renderShellTabs() {
  const nav = $("ws-tabs");
  if (!nav) return;
  const label = (id) => {
    const w = WORKSPACES.find((x) => x.id === id);
    return w ? (w.name || w.id) : id;
  };
  const split = SPLIT && OPEN.length === MAX_PANES;
  nav.innerHTML = OPEN.map((id, i) =>
    `<button class="ws-tab${!split && i === ACTIVE ? " active" : ""}${split ? " split-on" : ""}"
       onclick="focusWs(${i})" title="${esc(label(id))}">
       <span class="lbl">${esc(label(id))}</span>
       <span class="x" title="Close" onclick="event.stopPropagation();closeWs('${esc(id)}')">${ic("x", "sm")}</span>
     </button>`).join("")
    + (OPEN.length < MAX_PANES
        ? `<button class="tab-add" onclick="goHome()" title="Open another workspace">${ic("plus", "sm")}</button>`
        : "");
  const sb = $("split-btn");
  sb.disabled = OPEN.length < MAX_PANES;
  sb.classList.toggle("on", split);
}

// iframe của 1 workspace. Tạo một lần rồi giữ nguyên — đặt lại src (hoặc move node)
// là reload cả pane.
function paneFrame(id) {
  const box = $("panes");
  let f = [...box.querySelectorAll("iframe.pane")].find((x) => x.dataset.ws === id);
  if (!f) {
    f = document.createElement("iframe");
    f.className = "pane";
    f.dataset.ws = id;
    f.title = id;
    // ?nosse truyền xuống pane: EventSource giữ kết nối mãi nên trình duyệt headless
    // không bao giờ bắn 'load' — không có cờ này thì không test được shell tự động.
    f.src = "/?pane=1&ws=" + encodeURIComponent(id)
          + (location.search.includes("nosse") ? "&nosse=1" : "");
    box.appendChild(f);
  }
  return f;
}

function renderPanes() {
  const box = $("panes");
  if (!box) return;
  if (ACTIVE >= OPEN.length) ACTIVE = OPEN.length - 1;
  const home = ACTIVE < 0 || !OPEN.length;
  $("ws-list-view").hidden = !home;
  box.hidden = home;
  // Workspace đã đóng → bỏ iframe (terminal bên trong bị huỷ, đúng ý người bấm ✕).
  for (const f of [...box.querySelectorAll("iframe.pane")])
    if (!OPEN.includes(f.dataset.ws)) f.remove();
  if (home) return;

  const split = SPLIT && OPEN.length === MAX_PANES;
  const frames = OPEN.map(paneFrame);   // append-only: thứ tự DOM khớp OPEN
  frames.forEach((f, i) => {
    const show = split || i === ACTIVE;
    f.hidden = !show;
    f.style.flex = split && i === 0 ? `0 0 ${SPLIT_PCT}%` : "1 1 0";
    // display:none → xterm đo được 0×0. Bảo pane fit lại khi nó vừa hiện ra.
    if (show) try { f.contentWindow.postMessage({ t: "show" }, location.origin); } catch { /* chưa load */ }
  });

  let div = $("pane-div");
  if (split) {
    if (!div) {
      div = document.createElement("div");
      div.id = "pane-div";
      div.className = "pane-split";
      div.title = "Drag to resize";
    }
    box.insertBefore(div, frames[1]);   // chỉ divider bị move, iframe đứng yên
  } else if (div) div.remove();
}

// Bấm tab = xem MỘT workspace đó. Đang split mà bấm tab thì thoát split — nếu không,
// tab trông bấm được nhưng bấm xong không có gì đổi, người dùng tưởng hỏng.
function focusWs(i) { ACTIVE = i; SPLIT = false; shellSave(); renderShellTabs(); renderPanes(); }
window.focusWs = focusWs;

// Mở workspace thành tab. Đã mở → nhảy tới nó. Đầy chỗ → đóng tab hiện hành trước
// (giữ thứ tự DOM khớp OPEN, xem chú thích khối này).
function selectWorkspace(id) {
  const i = OPEN.indexOf(id);
  if (i >= 0) return focusWs(i);
  if (OPEN.length >= MAX_PANES) {
    const victim = OPEN[Math.max(0, ACTIVE)];
    OPEN = OPEN.filter((x) => x !== victim);
    const box = $("panes");
    for (const f of [...box.querySelectorAll("iframe.pane")])
      if (f.dataset.ws === victim) f.remove();
  }
  OPEN.push(id);
  ACTIVE = OPEN.length - 1;
  shellSave(); renderWorkspaceGrid(WORKSPACES); renderShellTabs(); renderPanes();
}
window.selectWorkspace = selectWorkspace;

function closeWs(id) {
  OPEN = OPEN.filter((x) => x !== id);
  if (!OPEN.length) { ACTIVE = -1; SPLIT = false; }
  else if (ACTIVE >= OPEN.length) ACTIVE = OPEN.length - 1;
  shellSave(); renderWorkspaceGrid(WORKSPACES); renderShellTabs(); renderPanes();
}
window.closeWs = closeWs;

function goHome() {
  if (PANE) return;         // trong pane, nút brand không tồn tại
  ACTIVE = -1;
  shellSave(); renderShellTabs(); renderPanes();
  refreshAll();
}
window.goHome = goHome;

function toggleSplit() {
  if (OPEN.length < MAX_PANES) return;
  SPLIT = !SPLIT;
  shellSave(); renderShellTabs(); renderPanes();
}
window.toggleSplit = toggleSplit;

// Kéo divider giữa 2 pane. pointer capture để con trỏ đi vào iframe vẫn không mất
// sự kiện; thêm .dragging tắt pointer-events của iframe cho chắc.
function initSplitDrag() {
  const box = $("panes");
  if (!box) return;
  let start = null;
  box.addEventListener("pointerdown", (e) => {
    const div = e.target.closest(".pane-split");
    if (!div) return;
    start = { x: e.clientX, pct: SPLIT_PCT, w: box.clientWidth };
    div.classList.add("dragging");
    box.classList.add("dragging");
    div.setPointerCapture(e.pointerId);
  });
  box.addEventListener("pointermove", (e) => {
    if (!start) return;
    const pct = start.pct + ((e.clientX - start.x) / start.w) * 100;
    SPLIT_PCT = Math.min(80, Math.max(20, pct));
    const f = box.querySelector("iframe.pane");
    if (f) f.style.flex = `0 0 ${SPLIT_PCT}%`;
  });
  const end = () => {
    if (!start) return;
    start = null;
    box.classList.remove("dragging");
    const d = $("pane-div"); if (d) d.classList.remove("dragging");
    shellSave();
    // Pane vừa đổi bề rộng → xterm phải fit lại, iframe không tự báo cho ai cả.
    for (const f of box.querySelectorAll("iframe.pane"))
      try { f.contentWindow.postMessage({ t: "show" }, location.origin); } catch { /* chưa load */ }
  };
  box.addEventListener("pointerup", end);
  box.addEventListener("pointercancel", end);
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
  applyPin();       // node ghim tự bù transform vừa đặt → đứng yên trên màn hình
  redrawMinimap();
}

// ── Ghim một cửa sổ vào mép trái ────────────────────────────────────────────
// Node ghim vẫn nằm TRONG #world. Bứng nó sang cha khác (một lớp overlay không transform) là
// cách hiển nhiên, nhưng node ghim phải giữ nguyên cha để không mất trạng thái đang gõ dở.
// Và position:fixed bên trong #world vô dụng: ancestor có transform là containing block của
// mọi con fixed. Nên cách duy nhất vừa giữ nguyên cha vừa đứng yên là TỰ BÙ world:
//   left/top   = (PAD − CV.t) / CV.k   → sau khi world dịch+phóng thì rơi đúng góc trên trái
//   scale(1/k) → nội dung về 1:1, không teo/phình theo mức zoom (nếu chỉ chia w/h cho k thì
//                khung đúng cỡ nhưng terminal đổi số cột và chữ VS Code bé lại)
let pinnedNid = null;
const PIN_PAD = 10;
let pinSize = "";   // "w×h" của lần áp gần nhất — chỉ fit lại terminal khi số này đổi
let pinResizing = false;   // đang kéo bề ngang → hoãn fit xterm tới lúc thả

function pinnedEl() {
  if (!pinnedNid) return null;
  return $("world").querySelector(`.node[data-nid="${CSS.escape(pinnedNid)}"]`);
}

// Bề ngang của card ghim: đã kéo thì dùng số đã lưu, chưa thì 55% canvas (trần 760). Luôn kẹp
// lại theo canvas hiện tại — cửa sổ thu nhỏ mà giữ nguyên 900px là panel tràn ra ngoài.
function pinWidth(nid, cv) {
  const saved = ((cvLoad().pos || {})[nid] || {}).pw;
  const max = Math.max(PIN_W_MIN, cv.clientWidth - PIN_PAD * 2);
  if (saved) return Math.round(Math.min(max, Math.max(PIN_W_MIN, saved)));
  return Math.round(Math.min(760, max, Math.max(PIN_W_MIN, cv.clientWidth * 0.55)));
}

function applyPin() {
  const cv = $("canvas"), el = pinnedEl();
  if (!el || !cv) return;
  // Bề ngang do người dùng kéo, lưu ở pos[nid].pw — KHÁC khoá với w/h của .rz. Chung khoá thì
  // kích thước lúc ghim đè lên kích thước lúc thả tự do, và bỏ ghim ra là card sai cỡ.
  const w = pinWidth(el.dataset.nid, cv);
  const h = Math.max(160, cv.clientHeight - PIN_PAD * 2);
  el.classList.add("pinned", "sized");
  el.style.left = ((PIN_PAD - CV.tx) / CV.k) + "px";
  el.style.top = ((PIN_PAD - CV.ty) / CV.k) + "px";
  el.style.transform = `scale(${1 / CV.k})`;
  el.style.width = w + "px";
  el.style.height = h + "px";
  // Pan/zoom KHÔNG đổi cỡ trên màn hình, nên đừng fit xterm mỗi khung hình khi đang kéo.
  const size = w + "x" + h;
  if (size !== pinSize) {
    pinSize = size;
    if (!pinResizing) requestAnimationFrame(() => refitNode(el));
  }
}

function pinWindow(nid) {
  if (pinnedNid === nid) return unpinWindow();
  unpinWindow(true);          // trả cái đang ghim về chỗ cũ; store để pin mới ghi đè
  pinnedNid = nid;
  cvSave({ pin: nid });
  applyView();
  layoutZones(); redrawEdges(); renderWinBar();
}
window.pinWindow = pinWindow;

// Bỏ ghim: trả node về đúng vị trí/kích thước đã lưu trong pos[nid] (applyPin chỉ ghi style
// nội tuyến, không đụng store — nên không có gì phải khôi phục ngoài việc áp lại pos).
function unpinWindow(keep) {
  const el = pinnedEl();
  pinnedNid = null;
  pinSize = "";
  if (!keep) cvSave({ pin: null });
  if (el) {
    el.classList.remove("pinned");
    el.style.transform = "";
    const p = (cvLoad().pos || {})[el.dataset.nid] || {};
    el.style.left = (p.x || 0) + "px";
    el.style.top = (p.y || 0) + "px";
    applySize(el, p);
    refitNode(el);
  }
  layoutZones(); redrawEdges(); renderWinBar();
}

// Mọi "cửa sổ" của workspace này: card 👑 (terminal nhúng) + card VS Code đang mở.
// Card agent thường không có gì để ghim — nội dung là vài dòng meta.
function winList() {
  const sessions = cvLast.sessions || [];
  const out = sessions.filter((s) => s.is_orch).map((s) => ({
    nid: "s:" + s.id, kind: "Terminal", icon: ic("terminal", "sm"),
    name: s.name, sub: s.cwd || "" }));
  for (const c of editorCards)
    if (sessions.some((s) => s.id === c.session))
      out.push({ nid: "editor:" + c.session, kind: "nvim", icon: ic("edit", "sm"),
                 name: c.name || c.session, sub: c.cwd || "" });
  return out;
}

// Thanh cửa sổ: mọi card ghim được của workspace nằm ngang ngay dưới topbar, bấm phát là
// ghim/bỏ ghim. Tab History không có canvas để ghim vào → ẩn thanh. Workspace chưa có cửa sổ nào
// thì VẪN hiện, kèm câu chỉ đường: ẩn sạch thì người dùng tưởng tính năng biến mất (nút Windows
// cũ luôn nằm đó, dù đếm 0).
function renderWinBar() {
  const bar = $("win-bar"), chips = $("win-chips");
  if (!bar || !chips) return;
  const list = winList();
  bar.hidden = $("tab-agents").hidden;
  if (!list.length) {
    chips.innerHTML = `<span class="win-sep"></span><span class="win-empty">No windows yet — turn on
      the terminal for an agent (${ic("terminal", "sm")} in its inspector) or open an editor, then
      pin it here.</span>`;
    return;
  }
  chips.innerHTML = `<span class="win-sep"></span>` + list.map((w) =>
    `<button class="win-chip${w.nid === pinnedNid ? " on" : ""}"
      onclick="pinWindow('${esc(w.nid)}')"
      title="${esc(w.kind)}${w.sub ? " · " + esc(w.sub) : ""} — pin it to the left edge at full
        height (it stays put while you pan and zoom); click again to send it back">
      ${w.icon}<span>${esc(w.name)}</span></button>`).join("");
}
window.renderWinBar = renderWinBar;

// ── Tìm một card rồi đưa nó vào giữa khung nhìn ──────────────────────────────
// Workspace đông card thì cuộn canvas đi tìm bằng mắt là việc tệ nhất; ô tìm là <input list=>
// nên trình duyệt lo phần lọc theo chữ gõ, không cần dropdown tự chế.
// Danh sách nạp lúc FOCUS chứ không mỗi lần render: SSE refresh chạy liên tục, dựng lại
// <option> giữa lúc dropdown đang mở là nó tự đóng dưới tay người dùng.
let findMap = {};   // nhãn hiện trong ô tìm → nid của node

function fillFindList() {
  const dl = $("cv-find-list");
  if (!dl) return;
  const sessions = cvLast.sessions || [];
  const rows = sessions.map((s) => ({ nid: "s:" + s.id, name: s.name,
    kind: s.is_orch ? "terminal" : "agent", sub: s.cwd || "" }));
  // Card editor dùng CHUNG tên với session của nó → thêm hậu tố, không thì hai dòng trùng nhãn
  // và findMap chỉ giữ được một cái.
  for (const c of editorCards)
    if (sessions.some((s) => s.id === c.session))
      rows.push({ nid: "editor:" + c.session, name: (c.name || c.session) + " · editor",
                  kind: "editor", sub: c.cwd || "" });
  findMap = {};
  dl.innerHTML = rows.map((r) => {
    findMap[r.name] = r.nid;
    return `<option value="${esc(r.name)}" label="${esc(r.kind + (r.sub ? " · " + r.sub : ""))}">`;
  }).join("");
}
window.fillFindList = fillFindList;

// Chỉ nhảy khi chữ trong ô KHỚP HẲN một dòng của danh sách — gõ dở dang thì không đi đâu cả.
function findCard(el) {
  const nid = findMap[el.value];
  if (nid) { el.blur(); focusNode(nid); }
}
window.findCard = findCard;

// Đưa node vào giữa khung nhìn + chọn nó. Giữ nguyên mức zoom: người dùng đặt zoom nào là cố ý,
// tự phóng to giúp chỉ làm họ mất chỗ.
function focusNode(nid) {
  const el = $("world").querySelector(`.node[data-nid="${nid}"]`);
  if (!el) return;
  if (nid.startsWith("s:")) selectNode(nid.slice(2));
  // Node ghim đứng sẵn ở mép trái màn hình rồi; left/top của nó là kết quả bù transform nên
  // lấy làm tâm sẽ ném khung nhìn đi đâu không biết.
  if (nid !== pinnedNid) {
    const cv = $("canvas");
    const x = parseFloat(el.style.left) || 0, y = parseFloat(el.style.top) || 0;
    CV.tx = cv.clientWidth / 2 - (x + el.offsetWidth / 2) * CV.k;
    CV.ty = cv.clientHeight / 2 - (y + el.offsetHeight / 2) * CV.k;
    applyView(); cvSave({ view: CV });
  }
  // Gỡ .found ở MỌI node trước: animation kết thúc ở outline trong suốt nên nhìn thì không thấy
  // gì, nhưng để nó bám lại thì "card vừa tìm ra" không còn là một node duy nhất nữa.
  // remove + đọc offsetWidth = ép reflow, không thì tìm lại đúng card đó sẽ không nháy nữa.
  for (const n of $("world").querySelectorAll(".node.found")) n.classList.remove("found");
  void el.offsetWidth;
  el.classList.add("found");
}
window.focusNode = focusNode;

// ── Minimap ─────────────────────────────────────────────────────────────────
// Toàn cảnh canvas + ô khung nhìn. Click/kéo trong map = dời khung nhìn tới đó.
// Vẽ lại từ applyView (pan/zoom) và redrawEdges (node đổi vị trí/số lượng) — hai chỗ đó
// phủ hết mọi trường hợp hình học đổi, khỏi phải nhớ rải lời gọi khắp nơi.
const MM = { w: 180, h: 120, pad: 6 };

// Node được tính vào hình học của canvas. Node ĐANG GHIM bị loại: left/top của nó là kết quả
// bù transform, tức là đổi theo từng cú pan — để nó vào bbox thì minimap, cvFit và zone đều
// chạy theo khung nhìn thay vì theo nội dung.
function cvGeomNodes() {
  return [...$("world").children].filter(
    (el) => el.classList.contains("node") && !el.classList.contains("pinned") && !el.hidden);
}

// Khung bao gồm cả node LẪN khung nhìn: chỉ lấy node thì pan ra vùng trống là ô xanh
// trôi ra ngoài map, người dùng mất luôn thứ duy nhất chỉ họ đang ở đâu.
function mmBox() {
  const cv = $("canvas"), world = $("world");
  if (!cv || !world) return null;
  const nodes = cvGeomNodes();
  if (!nodes.length) return null;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  const add = (x, y, w, h) => {
    x0 = Math.min(x0, x); y0 = Math.min(y0, y);
    x1 = Math.max(x1, x + w); y1 = Math.max(y1, y + h);
  };
  for (const el of nodes)
    add(parseFloat(el.style.left) || 0, parseFloat(el.style.top) || 0, el.offsetWidth, el.offsetHeight);
  const view = { x: -CV.tx / CV.k, y: -CV.ty / CV.k,
                 w: cv.clientWidth / CV.k, h: cv.clientHeight / CV.k };
  // Mọi node đã nằm trong khung nhìn → bản đồ chỉ còn là một ô xanh to, không nói thêm gì.
  // Ẩn luôn thay vì thêm nút bật/tắt: nó tự xuất hiện đúng lúc có ích.
  if (x0 >= view.x && y0 >= view.y && x1 <= view.x + view.w && y1 <= view.y + view.h) return null;
  add(view.x, view.y, view.w, view.h);
  const k = Math.min((MM.w - MM.pad * 2) / (x1 - x0 || 1),
                     (MM.h - MM.pad * 2) / (y1 - y0 || 1));
  return { x0, y0, k, nodes, view };
}

function redrawMinimap() {
  const mm = $("minimap");
  if (!mm) return;
  const b = mmBox();
  // toggleAttribute, KHÔNG phải `.hidden =`: hidden là thuộc tính của HTMLElement, SVGElement
  // không có nó. Gán `.hidden` chỉ tạo expando trên object JS, attribute trong DOM đứng yên
  // và minimap ẩn vĩnh viễn — đọc lại `.hidden` vẫn thấy false nên nhìn như đang chạy tốt.
  mm.toggleAttribute("hidden", !b);
  if (!b) return;
  const X = (x) => (MM.pad + (x - b.x0) * b.k).toFixed(1);
  const Y = (y) => (MM.pad + (y - b.y0) * b.k).toFixed(1);
  const S = (v) => Math.max(2, v * b.k).toFixed(1);   // sàn 2px: card nhỏ quá thì mất hút
  let out = "";
  for (const el of b.nodes) {
    const zone = el.classList.contains("group-zone");
    const card = el.querySelector(".agent-card");
    const st = card ? ([...card.classList].find((c) => c.startsWith("st-")) || "") : "";
    const sel = el.classList.contains("sel") ? " mm-sel" : "";
    out += `<rect class="${zone ? "mm-zone" : "mm-r " + st + sel}" rx="${zone ? 3 : 1.5}"
      x="${X(parseFloat(el.style.left) || 0)}" y="${Y(parseFloat(el.style.top) || 0)}"
      width="${S(el.offsetWidth)}" height="${S(el.offsetHeight)}"/>`;
  }
  out += `<rect class="mm-view" rx="2" x="${X(b.view.x)}" y="${Y(b.view.y)}"
    width="${S(b.view.w)}" height="${S(b.view.h)}"/>`;
  mm.innerHTML = out;
}

// Kéo trong minimap: GIỮ NGUYÊN phép chiếu bắt được lúc pointerdown. Tính lại mỗi lần
// move thì khung bao đổi theo chính khung nhìn vừa dời — con trỏ và bản đồ đá nhau.
function mmInit() {
  const mm = $("minimap");
  if (!mm) return;
  let drag = null;
  const goto_ = (e) => {
    const cv = $("canvas"), r = drag.rect, b = drag.b;
    const wx = b.x0 + (e.clientX - r.left - MM.pad) / b.k;
    const wy = b.y0 + (e.clientY - r.top - MM.pad) / b.k;
    CV.tx = cv.clientWidth / 2 - wx * CV.k;
    CV.ty = cv.clientHeight / 2 - wy * CV.k;
    applyView();
  };
  mm.addEventListener("pointerdown", (e) => {
    // DỪNG nổi bọt. Canvas có sẵn chốt `e.target.closest('.cv-overlay')` để không pan khi bấm vào
    // overlay, nhưng chốt đó ĐỌC HỤT ở đây: goto_ bên dưới gọi applyView → redrawMinimap →
    // mm.innerHTML = … , tức là cái <rect> đang là e.target bị GỠ khỏi cây trước khi canvas kịp
    // xét. Node rời cây thì closest() trả null → canvas tưởng bấm vào nền và mở luôn một cú pan
    // thứ hai chạy song song. Đã đo: canvas pointerdown nhìn thấy "rect overlay=false".
    e.stopPropagation();
    const b = mmBox();
    if (!b) return;
    drag = { b, rect: mm.getBoundingClientRect() };
    mm.setPointerCapture(e.pointerId);
    goto_(e);
  });
  const up = () => { if (drag) { drag = null; cvSave({ view: CV }); } };
  mm.addEventListener("pointermove", (e) => {
    if (!drag) return;
    if (!e.buttons) return up();   // không còn giữ nút = kéo đã kết thúc ở đâu đó, tự gỡ
    goto_(e);
  });
  // up/cancel bắt ở WINDOW chứ không ở minimap. Kéo cho mọi node lọt hết vào khung nhìn thì
  // redrawMinimap ẩn luôn minimap NGAY GIỮA cú kéo → element ẩn mất pointer capture, pointerup
  // rơi vào canvas, `drag` không ai xoá. Lần sau minimap hiện lại là chỉ rê chuột qua đã kéo
  // khung nhìn đi mà chưa bấm nút nào.
  addEventListener("pointerup", up);
  addEventListener("pointercancel", up);
  mm.addEventListener("lostpointercapture", up);
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
  const nodes = cvGeomNodes();
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
// CLI người dùng chọn cho terminal, theo NAME (bền qua xoay session id) — rỗng = theo engine của
// session. Chọn CLI KHÁC engine thì backend mở phiên MỚI (id của claude không resume được bằng
// codex và ngược lại), nên đổi lựa chọn = hủy PTY cũ rồi nối lại.
let termCli = {};   // session name → 'claude' | 'codex'

// Gương của engine_from_model bên service — model là nguồn sự thật duy nhất của engine.
// Lệch với backend là UI hiện sai nút (vd mời xóa vĩnh viễn một session Claude rồi ăn 400).
function engineOfModel(model) {
  const m = (model || "").trim().toLowerCase();
  return m === "codex" || m.startsWith("codex:") ? "codex" : "claude";
}

async function setTermCli(sid, name, cli) {
  termCli[name] = cli;
  await reconnectTerm(sid, name);
}
window.setTermCli = setTermCli;

// Ghim phiên cũ để chat tiếp. State nằm ở DB (cột resume_id) chứ KHÔNG ở client: run headless
// (signal chạy nền) phải mở cùng transcript với terminal, mà nó thì không đọc được biến của tab.
async function setTermSid(sid, name, chosen) {
  try { await api(`/api/sessions/${encodeURIComponent(sid)}/resume-id`, "POST", { resume_id: chosen }); }
  catch (e) {
    let msg = String(e);
    try { const j = JSON.parse(e); if (j && j.error) msg = j.error; } catch (_) {}
    alert("Could not pin that session: " + msg);
  }
  await reconnectTerm(sid, name);
}
window.setTermSid = setTermSid;

// Nạp danh sách phiên của cwd LÚC MỞ select (không phải mỗi lần render): quét transcript trên đĩa
// là việc của filesystem, không nên chạy theo mỗi lần SSE refresh.
async function fillTermSessions(el, sid, cli) {
  if (el.dataset.filled) return;
  el.dataset.filled = "1";
  let list = [];
  try {
    const r = await api(`/api/cli-sessions?session=${encodeURIComponent(sid)}&cli=${encodeURIComponent(cli)}`);
    list = r.sessions || [];
  } catch (e) { console.error(e); el.dataset.filled = ""; return; }
  const cur = el.value;
  el.innerHTML = `<option value="">▸ this session</option>` + list.map((x) =>
    `<option value="${esc(x.id)}" title="${esc(x.preview || x.id)}">${esc(x.ts.slice(5))} · `
    + `${esc(x.preview ? x.preview.slice(0, 40) : x.id.slice(0, 8))}</option>`).join("");
  el.value = cur;
  if (el.value !== cur) el.value = "";   // phiên cũ không còn trong danh sách
}
window.fillTermSessions = fillTermSessions;

function startTerm(t) {
  t.started = true;
  // Nền terminal đọc từ token --term-bg để đổi theme không để lại một ô lệch màu.
  const bg = getComputedStyle(document.documentElement).getPropertyValue("--term-bg").trim();
  t.term = new Terminal({ fontSize: 12, cursorBlink: true, scrollback: 5000,
                          theme: { background: bg || "#0c0e12", foreground: "#e6e8eb" } });
  t.fit = new FitAddon.FitAddon();
  t.term.loadAddon(t.fit);
  t.term.open(t.host);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const path = t.wsPath
    || `/ws/terminal?session=${encodeURIComponent(t.sid)}`
       + (t.cli ? `&cli=${encodeURIComponent(t.cli)}` : "");
  t.ws = new WebSocket(`${proto}://${location.host}${path}`);
  t.ws.binaryType = "arraybuffer";
  t.ws.onopen = () => fitTerm(t);
  t.ws.onmessage = (e) => t.term.write(typeof e.data === "string" ? e.data : new Uint8Array(e.data));
  t.ws.onclose = () => { if (t.term) t.term.write("\r\n\x1b[31m[disconnected — press 🔄 to reload]\x1b[0m\r\n"); };
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
// ── Card editor (nvim trong PTY, y hệt terminal) ────────────────────────────
// Không iframe, không cổng, không tải server: nvim là TUI nên card này chỉ là một .term-slot
// nữa, nối vào /ws/editor. Nhờ vậy nó đi qua ĐÚNG đường render của card agent — không còn phải
// giữ node ngoài chu trình innerHTML như hồi iframe VS Code (bứng iframe sang cha mới là tải lại).
let editorCards = [];   // [{open, session, name, cwd, persistent}] từ /api/editor

async function openEditor(sid) {
  try {
    await api("/api/editor/open", "POST", { session: sid });
    await refreshAll();
  } catch (e) { alert("Could not open the editor: " + e); }
}
window.openEditor = openEditor;

async function closeEditor(sid, name) {
  if (!confirm(`Close the editor for '${name}'?\n\nIts nvim session is killed — unsaved buffers `
               + `are lost (nvim leaves a swap file to :recover from).`))
    return;
  try { await api("/api/editor/close", "POST", { session: sid }); await refreshAll(); }
  catch (e) { alert("Could not close the editor: " + e); }
}
window.closeEditor = closeEditor;

// Đổi tab = gửi một lệnh ex vào nvim ở SERVER (:DiffviewOpen / :DiffviewClose). Không đụng gì
// tới xterm: nó vẫn attach vào đúng phiên, nvim tự vẽ lại. Không có state tab nào ở client phải
// giữ đồng bộ — nút chỉ sáng lên cho biết vừa bấm gì.
async function editorFocus(sid, win, el) {
  try { await api("/api/editor/focus", "POST", { session: sid, window: win }); }
  catch (e) { console.error(e); return; }
  const bar = el && el.parentElement;
  if (bar) for (const b of bar.querySelectorAll("button")) b.classList.toggle("on", b === el);
}
window.editorFocus = editorFocus;

function editorCardHtml(st) {
  const sid = esc(st.session);
  // key riêng ("ed:") vì một session có thể mở CẢ terminal agent lẫn editor — trùng key là hai
  // card dùng chung một xterm.
  const tip = st.persistent
    ? "Runs in tmux — closing the tab only detaches, the buffer survives"
    : "No tmux on this machine — closing the tab ends the nvim session";
  // Một tab = một cửa sổ tmux. Chỉ hiện khi CÓ tmux và có nhiều hơn một cửa sổ — máy không có
  // tmux thì card chỉ là một PTY nvim, không chuyển đi đâu được.
  const wins = st.windows || ["nvim"];
  const tabs = wins.length < 2 ? "" : `<span class="ed-tabs">` + wins.map((w, i) =>
    `<button class="${i === 0 ? "on" : ""}" onclick="editorFocus('${sid}','${esc(w)}',this)"
      title="${w === "git" ? "diffview.nvim — side-by-side diff of the working tree, staged, or any revision" : "back to editing (closes the diff view)"}"
      >${esc(w)}</button>`).join("") + `</span>`;
  return `<div class="agent-card editor-card">
    <div class="node-head editor-head" title="${esc(tip)}">
      <span class="ed-ic">${ic("edit", "sm")}</span>${tabs}<b>${esc(st.name || '')}</b>
      <span class="cwd" title="${esc(st.cwd || '')}">${esc(st.cwd || '')}</span>
      <span class="spacer"></span>
      <button class="icon-btn danger" onclick="closeEditor('${sid}','${esc(st.name || '')}')"
        title="Close the editor (kills its nvim session)">${ic("x", "sm")}</button>
    </div>
    <div class="term-slot" data-sid="${sid}" data-key="ed:${sid}"
      data-ws="/ws/editor?session=${encodeURIComponent(st.session)}"></div>
  </div>`;
}

function attachTerms() {
  const seen = new Set();
  for (const slot of $("world").querySelectorAll(".term-slot")) {
    // key ≠ sid: một session mở được cả terminal agent lẫn card editor, hai xterm riêng.
    const key = slot.dataset.key || slot.dataset.sid;
    const sid = slot.dataset.sid;
    seen.add(key);
    let t = cvTerms[key];
    if (!t) t = cvTerms[key] = { sid, key, host: Object.assign(document.createElement("div"),
                                                               { className: "term-host" }),
                                 started: false, term: null, fit: null, ws: null };
    t.cli = slot.dataset.cli || "";   // đọc TRƯỚC startTerm (rAF chạy sau vòng render này)
    t.wsPath = slot.dataset.ws || "";
    slot.appendChild(t.host);
    // Slot đang khóa: KHÔNG start PTY mới (đợi user bấm 🔄 sau khi run tự động xong).
    requestAnimationFrame(() => {
      if (!t.started) { if (!slot.dataset.lock) startTerm(t); }
      else fitTerm(t);
    });
  }
  for (const key of Object.keys(cvTerms)) if (!seen.has(key)) destroyTerm(key);
}

// Gửi signal nhanh tới 1 agent ngay trên card.
async function sendSignalTo(id, name) {
  const msg = prompt(`Signal to '${name}':`, "");
  if (!msg || !msg.trim()) return;
  try { await api("/api/signals", "POST", { to_session: id, message: msg.trim() }); await refreshAll(); }
  catch (e) { console.error(e); alert("Could not send signal: " + e); }
}
window.sendSignalTo = sendSignalTo;

// Form gửi signal thủ công (tab History) — giữ từ dashboard cũ: chọn role, bật
// requires_approval / dry_run (sendSignalTo trên card chỉ gửi nhanh, không có 2 flag này).
function fillSignalForm(sessions) {
  const sel = $("sg-to");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = sessions.map((s) =>
    `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join("");
  if (cur && sessions.some((s) => s.name === cur)) sel.value = cur;
}

async function sendSignal() {
  const to = $("sg-to").value;
  const msg = $("sg-msg").value.trim();
  if (!to || !msg) { alert("Pick a target role and type a message."); return; }
  try {
    await api("/api/signals", "POST", {
      to_role: to, message: msg, from_role: "human",
      workspace_id: currentWS || undefined,
      requires_approval: $("sg-approval").checked ? 1 : 0,
      dry_run: $("sg-dry").checked ? 1 : 0,
    });
    $("sg-msg").value = "";
    await refreshAll();
  } catch (e) { console.error(e); alert("Could not send signal: " + e); }
}
window.sendSignal = sendSignal;

// 1 card agent. needsYou = có signal chờ duyệt tới nó; isOrch = s.is_orch (backend, toggle 💻)
// — card mở terminal nhúng + kéo giãn được. KHÔNG ảnh hưởng routing signal hay SKILL của vai.
function agentCard(s, needsYou, isOrch) {
  const id = encodeURIComponent(s.id);
  const tools = JSON.parse(s.allowed_tools || "[]") || [];
  const today = s.daily_limit
    ? `<span class="${s.daily_blocked ? "day-hit" : "day-ok"} num" title="runs today / daily limit">${s.used_today}/${s.daily_limit}</span>`
    : "";
  const head = `<div class="node-head">
      <span class="status-dot dot-${esc(s.status)}"></span>
      ${isOrch ? `<span class="crown" title="This session owns the project terminal">${ic("crown", "sm")}</span>` : ""}
      <b title="${esc(s.name)}">${esc(s.name)}</b>
      ${needsYou ? `<span class="needs-badge">NEEDS YOU</span>` : ""}
      <span class="spacer"></span>
    </div>`;
  const engine = engineOfModel(s.model);
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
    // Chọn CLI cho terminal. CLI trùng engine của session = resume đúng phiên đó; CLI khác =
    // phiên MỚI trong cùng cwd (nhãn nói rõ để không tưởng mất ngữ cảnh vì lỗi).
    const eng = engineOfModel(s.model);
    const cli = termCli[s.name] || eng;
    const cliSel = `<select class="mini cli-sel" title="Which CLI runs in the terminal (session engine: ${eng}) — ✦ = a different engine, opens a NEW session in the same cwd"
      onchange="setTermCli('${esc(s.id)}','${esc(s.name)}', this.value)">
      ${["claude", "codex"].map((c) => `<option value="${c}"${c === cli ? " selected" : ""}
        >${c === "claude" ? "🅒" : "🅞"} ${c}${c === eng ? "" : " ✦"}</option>`).join("")}
    </select>`;
    // Ghim PHIÊN để chat tiếp: mọi transcript của CLI này trong cwd của session, mới nhất trên
    // cùng. Danh sách chỉ nạp khi mở select (xem fillTermSessions). Ghim ăn cho cả run headless.
    const psid = s.resume_id || "";
    const sidSel = `<select class="mini cli-sel" title="Which past ${cli} session this card resumes — terminal AND background runs. Lists every ${cli} session recorded in this folder"
      onmousedown="fillTermSessions(this,'${esc(s.id)}','${cli}')"
      onchange="setTermSid('${esc(s.id)}','${esc(s.name)}', this.value)">
      <option value="${esc(psid)}">${psid ? "▸ " + esc(psid.slice(0, 8)) : "▸ this session"}</option>
    </select>`;
    const lock = busy
      ? `<div class="term-lock">⏳ An automatic run is in progress (handling a report from another agent)…<br>
           Click to watch it · when it finishes, press 🔄 to chat again</div>`
      : locked
        ? `<div class="term-lock">✅ The automatic run finished — press 🔄 to load the new context and keep chatting.<br>
           Click to review that run.</div>`
        : "";
    return `<div class="agent-card orch-term is-orch ${cls}" data-sid="${esc(s.id)}">
      ${head}
      <div class="orch-body">
        <div class="orch-side">
          ${quickBtns(s, id, true)}
          ${cliSel}
          ${sidSel}
          <button class="icon-btn" onclick="reconnectTerm('${esc(s.id)}','${esc(s.name)}')"
            title="Reload the session/terminal (restarts the selected CLI)">${ic("refresh", "sm")}</button>
        </div>
        <div class="term-slot" data-sid="${esc(s.id)}" data-key="${esc(s.id)}" data-cli="${cli}"${locked ? ` data-lock="1"` : ""}>${lock}</div>
      </div>
    </div>`;
  }

  return `<div class="agent-card ${cls}" data-sid="${esc(s.id)}">
    ${head}
    <div class="agent-body">
      <div class="mrow">
        <span class="eng-chip eng-${esc(engine)}">${esc(engine)}</span>
        <span class="mono" title="${esc(s.model || "auto")}">${esc(s.model || "auto")}</span>
        <span class="spacer"></span><span>${esc(s.effort || DEFAULT_EFFORT)}</span>
      </div>
      <div class="mrow">
        <span>${esc(s.status)}</span>
        <span title="${esc(tools.join(", ") || "every tool allowed")}">·
          ${tools.length ? tools.length + " tools" : "all tools"}</span>
        <span class="spacer"></span>${today}
      </div>
    </div>
    ${quickBtns(s, id)}
  </div>`;
}

// Nút quick trên card: 4 thao tác hay dùng nhất, chỉ hiện khi hover hoặc card đang chọn.
// Phần còn lại (model, effort, SKILL, context, xoá…) ở inspector — nhồi hết vào card thì
// 5 agent = 45 control cùng lúc trên canvas, mắt không biết nhìn đâu.
// side=true: cột dọc trong card 👑, không cần khung nổi.
function quickBtns(s, id, side) {
  const b = [];
  // class GHÉP một lần ở đây: nhét thêm attribute class thứ hai vào chuỗi HTML thì parser
  // lấy cái đầu và bỏ im cái sau — nút danger sẽ mất màu đỏ mà không ai thấy sai ở đâu.
  const btn = (extra, title, onclick, inner) =>
    `<button class="${side ? "icon-btn " : ""}${extra}" title="${title}" onclick="${onclick}">${inner}</button>`;

  b.push(s.status === "paused" || s.status === "stopped"
    ? btn("", "Resume", `act('/api/sessions/${id}/resume')`, ic("play", "sm"))
    : btn("", "Pause", `act('/api/sessions/${id}/pause')`, ic("pause", "sm")));
  if (s.status === "running")
    b.push(btn("danger", "Kill the running job (stops a runaway)",
      `if(confirm('Kill the running job on ${esc(s.name)}? The run is marked failed and is not retried.'))act('/api/sessions/${id}/kill')`,
      ic("stop", "sm")));
  if (s.daily_blocked)
    b.push(btn("warn", `Daily run limit hit — allow ${DAILY_STEP} more`,
      `allowMore('${id}','${esc(s.name)}')`, "+" + DAILY_STEP));
  b.push(s.is_orch
    ? btn("", "Close the terminal — the session goes back to a headless worker",
        `if(confirm('Close the terminal on ${esc(s.name)}? The session goes back to headless.'))toggleOrch('${id}',0)`,
        ic("x", "sm"))
    : btn("", "Open a terminal for this session (one per project — closes any other terminal in the same cwd)",
        `toggleOrch('${id}',1)`, ic("terminal", "sm")));
  if ((s.cwd || "").trim())
    b.push(btn("", "Open this session's project folder in nvim — as many editors as you like",
      `openEditor('${id}')`, ic("edit", "sm")));
  return side ? b.join("") : `<div class="quick">${b.join("")}</div>`;
}

// ── Inspector: mọi thứ về agent đang chọn ───────────────────────────────────
// Chọn card = mở panel bên phải. Trước đây các control này nằm hết trên card; canvas
// đông agent là thành một bức tường nút.
let selSid = null;

function selectNode(sid) {
  selSid = sid || null;
  renderInspector();
  markSelection();
}
window.selectNode = selectNode;

// renderCanvas dựng lại innerHTML mỗi lần refresh → class .sel bay mất, phải gắn lại.
function markSelection() {
  for (const el of $("world").querySelectorAll(".node"))
    el.classList.toggle("sel", !!selSid && el.dataset.nid === "s:" + selSid);
  redrawMinimap();   // minimap tô sáng node đang chọn — .sel vừa đổi thì nó phải theo
}

// Ngân sách ping-pong của vai này với từng peer (backend gửi kèm trong /api/sessions).
// CHỈ hiện cặp đã trao đổi trong chu kỳ hiện tại — cặp 0 lượt là nhiễu, mỗi agent sẽ ra
// N-1 dòng vô nghĩa. Không có nút reset: bộ đếm tự mở lại, phần hint nói rõ bằng cách nào.
function pairBudget(s) {
  const pairs = s.pairs || [];
  if (!pairs.length) return "";
  const rows = pairs.map((p) => {
    const cls = p.n >= PAIR_CAP ? " hit" : p.n >= PAIR_CAP - 1 ? " warn" : "";
    return `<div class="insp-pair">
      <span class="peer" title="${esc(p.peer)}">↔ ${esc(p.peer)}</span>
      <span class="num${cls}">${p.n}/${PAIR_CAP}</span>
    </div>`;
  }).join("");
  const spent = pairs.some((p) => p.n >= PAIR_CAP);
  return `
    <div class="insp-sec">
      <h4>Signal budget</h4>
      ${rows}
      <div class="hint">${spent
        ? "Out of turns — that agent has been told to report back to you instead of signalling. "
        : ""}Resets when you give either side new work: a signal from the History tab, or
        typing in its terminal. Also after ${windowLabel()} of silence.</div>
    </div>`;
}

// Đọc từ /health thay vì ghi cứng "an hour": đổi ORCH_PAIR_SIGNAL_WINDOW_MIN mà hint vẫn nói
// 60 phút là dạy người dùng một điều sai.
function windowLabel() {
  const m = Math.round(PAIR_WINDOW_MIN);
  if (m < 60) return `${m} minutes`;
  const h = m / 60;
  return h === 1 ? "an hour" : `${Number.isInteger(h) ? h : h.toFixed(1)} hours`;
}

// Mục "Workspace" trong inspector: chuyển agent sang nhóm khác để nó signal được với nhóm đó.
function wsMove(s) {
  const others = WORKSPACES.filter((w) => w.id !== (s.workspace_id || "default") && w.status === "active");
  if (!others.length) return "";
  const id = encodeURIComponent(s.id);
  return `<div class="insp-sec">
    <h4>Workspace</h4>
    <select onchange="moveWorkspace('${id}','${esc(s.name)}', this.value, this.options[this.selectedIndex].text)"
      title="Move this agent so it can signal the agents in another workspace">
      <option value="">${esc(wsLabel(s.workspace_id))} — move to…</option>
      ${others.map((w) => `<option value="${esc(w.id)}">${esc(w.name || w.id)}</option>`).join("")}
    </select>
  </div>`;
}

const wsLabel = (id) => (WORKSPACES.find((w) => w.id === id) || {}).name || id || "default";

function renderInspector() {
  const box = $("inspector");
  if (!box) return;
  const s = (cvLast.sessions || []).find((x) => x.id === selSid);
  if (!s) { box.hidden = true; selSid = null; return; }   // session vừa bị xoá/unregister
  box.hidden = false;
  $("insp-dot").className = "status-dot dot-" + esc(s.status);
  $("insp-name").textContent = s.name;
  $("insp-name").title = s.name;

  const id = encodeURIComponent(s.id);
  const engine = engineOfModel(s.model);
  const tools = JSON.parse(s.allowed_tools || "[]") || [];
  // Mức hiện ra bám theo model của CHÍNH session này — gpt-5.5 không có 'max', đừng mời chọn.
  const effortSel = `<select onchange="setEffort('${id}', this.value)">`
    + effortOptsFor(s.model).map((e) =>
        `<option value="${e}"${e === (s.effort || "") ? " selected" : ""}>${e || "default (" + DEFAULT_EFFORT + ")"}</option>`).join("")
    + `</select>`;
  const limit = s.daily_limit
    ? `<span class="${s.daily_blocked ? "day-hit" : "day-ok"} num">${s.used_today}/${s.daily_limit}</span>`
      + (s.daily_blocked
          ? ` <button class="warn" onclick="allowMore('${id}','${esc(s.name)}')">Allow +${DAILY_STEP}</button>` : "")
    : `<span>no limit</span>`;

  $("insp-body").innerHTML = `
    <div class="insp-sec">
      <div class="insp-row">
        <span class="eng-chip eng-${esc(engine)}">${esc(engine)}</span>
        ${badge(s.status, { running: "b-blue", paused: "b-amber", stopped: "b-red" }[s.status] || "b-gray")}
        ${s.is_orch ? badge("terminal", "b-amber") : ""}
      </div>
      <div class="insp-kv"><span>Session</span><span class="mono" title="${esc(s.id)}">${esc(s.id)}</span></div>
      <div class="insp-kv"><span>Folder</span><span class="mono" title="${esc(s.cwd || "")}">${esc(s.cwd || "—")}</span></div>
      <div class="insp-kv"><span>Tools</span><span title="${esc(tools.join(", ") || "every tool allowed")}">${tools.length ? tools.length + " allowed" : "all tools"}</span></div>
      <div class="insp-kv"><span>Today</span>${limit}</div>
    </div>
${pairBudget(s)}

    <div class="insp-sec">
      <h4>Model</h4>
      <input list="model-list" value="${esc(s.model || "")}" placeholder="auto — the CLI picks"
        onchange="setModel('${id}', this.value.trim())">
      ${effortSel}
    </div>

    ${wsMove(s)}

    <div class="insp-sec">
      <h4>Work</h4>
      <div class="insp-row">
        <button class="secondary" onclick="openSessionRun('${esc(s.id)}')">${ic("doc", "sm")} Latest run</button>
        <button class="secondary" onclick="sendSignalTo('${esc(s.id)}','${esc(s.name)}')">${ic("send", "sm")} Signal</button>
      </div>
    </div>

    <div class="insp-sec">
      <h4>Context</h4>
      <div class="insp-row">
        <button class="secondary" onclick="viewCompact('${id}','${esc(s.name)}')"
          title="View this session's current context / SKILL">${ic("doc", "sm")} View</button>
        <button class="secondary" onclick="editSkill('${id}','${esc(s.name)}')"
          title="Edit this role's SKILL (upserts into .claude/skills in the project cwd)">${ic("book", "sm")} SKILL</button>
      </div>
      <button class="secondary" onclick="compactSession('${id}','${esc(s.name)}')"
        title="Summarise the transcript so the role stops drifting on long jobs">${ic("compress", "sm")} Compact context</button>
    </div>

    <div class="insp-sec insp-danger">
      <h4>Danger zone</h4>
      <button class="danger" onclick="if(confirm('Remove session ${esc(s.name)} from the orchestrator?\\n\\nRuns, signals and audit records are kept.'))act('/api/sessions/${id}/unregister')">
        ${ic("back", "sm")} Unregister</button>
      <div class="hint">There is no permanent delete: the transcript belongs to the CLI that
        owns the session, so the orchestrator cannot wipe it. Unregister drops the session here
        and keeps its runs, signals and audit records.</div>
    </div>`;
}

// ── Zone (cwd) + orchestrator + chat ────────────────────────────────────────
let cvGroups = [];    // [{cwd, els:[nodeEl]}] — rebuild mỗi render; drag group đọc từ đây
let cvNodeEls = {};   // session_id → node element (để vẽ edge)
let cvEdges = [];     // [{from, to, cls}] resolve từ signal list
let cvLast = { sessions: [], signals: [] };  // data mới nhất (re-render cục bộ không cần fetch)

// Chỉ dùng cho <marker> đầu mũi tên. Đọc token thay vì hex cứng: dây vẽ bằng CSS
// (.edge-wait/.edge-run) nên hardcode ở đây là dark mode có dây một màu, đầu mũi tên màu khác.
const EDGE_COLORS = { wait: "var(--edge-wait)", run: "var(--edge-run)" };  // done/failed không vẽ mũi tên
const EDGE_DEFS = "<defs>" + Object.entries(EDGE_COLORS).map(([k, c]) =>
  `<marker id="ah-${k}" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
     <path d="M0,0L8,4L0,8z" fill="${c}"/></marker>`).join("") + "</defs>";

// Bật/tắt terminal nhúng cho 1 session DB (nguồn sự thật: cột is_orch backend — không còn
// localStorage). Bật: backend tự tắt terminal của session khác cùng cwd (1 terminal/project).
async function toggleOrch(id, on) {
  try { await api("/api/sessions/" + id + "/orch", "POST", { on: !!on }); }
  catch (e) { console.error(e); alert("Could not toggle the terminal: " + e); return; }
  await refreshAll();
}
window.toggleOrch = toggleOrch;

function zoneHtml(gi, cwd, list) {
  const base = cwd.replace(/\/+$/, "").split("/").pop() || cwd;
  return `<div class="node group-zone" data-nid="g:${esc(cwd)}" data-gi="${gi}">
    <div class="zone-head">
      <div class="zone-title">${ic("folder", "sm")} <b>${esc(base)}</b><span class="g-count">${list.length} agents</span>
        <span class="g-path" title="${esc(cwd)}">${esc(cwd)}</span></div>
    </div>
  </div>`;
}

// ── Kéo giãn card trên canvas ────────────────────────────────────────────────
// Kích thước lưu chung chỗ với vị trí (pos[nid] = {x, y, w, h}) → cùng workspace, cùng
// localStorage, không thêm store thứ hai phải đồng bộ. Không có w/h = dùng size mặc định của CSS.
// CHỈ card có "cửa sổ" bên trong mới kéo giãn được: card 👑 (terminal) và card VS Code (thư mục).
// Card headless không có gì để giãn — nội dung là vài dòng meta, kéo to chỉ ra khoảng trống.
// Guard theo data-rz thay vì chỉ ẩn tay nắm: session từng là 👑 rồi bị demote vẫn còn w/h trong
// store, không chặn thì card headless bị kéo giãn theo size cũ.
// Hai tay nắm, KHÔNG bao giờ dùng cùng lúc: .rz cho card tự do trên canvas, .rzw cho card đang
// ghim (CSS chỉ hiện đúng một cái). Ghim thì chiều cao đã bằng canvas, chỉ còn bề ngang để chỉnh.
const RZ = `<div class="rz" title="Drag to resize the card (double-click to reset)"></div>`
  + `<div class="rzw" title="Drag to set the pinned width (double-click to reset)"></div>`;
const RZ_MIN = { w: 220, h: 130 };
const PIN_W_MIN = 280;

function applySize(el, p) {
  if (p && p.w && p.h && el.dataset.rz) {
    el.style.width = p.w + "px";
    el.style.height = p.h + "px";
    el.classList.add("sized");
  } else {
    el.style.width = el.style.height = "";
    el.classList.remove("sized");
  }
}

// Ghi vị trí + kích thước của 1 node vào store. MERGE chứ không ghi đè cả entry: kéo vị trí
// không được xoá w/h, kéo giãn không được xoá x/y (cùng dùng chung pos[nid]).
function saveNodeGeom(el, pos) {
  const p = pos[el.dataset.nid] = { ...(pos[el.dataset.nid] || {}) };
  p.x = parseFloat(el.style.left) || 0;
  p.y = parseFloat(el.style.top) || 0;
  // Node không kéo giãn được (card headless, zone): chỉ ghi vị trí. Xoá w/h ở đây thì size của
  // card 👑 sẽ mất mỗi lần user kéo nó lúc đang headless — bật 👑 lại là về mặc định, không hiểu vì sao.
  if (!el.dataset.rz) return p;
  if (el.classList.contains("sized")) {
    p.w = Math.round(parseFloat(el.style.width) || 0);
    p.h = Math.round(parseFloat(el.style.height) || 0);
  } else { delete p.w; delete p.h; }
  return p;
}

// Terminal xterm KHÔNG tự biết khung đổi: phải fit lại, nếu không chữ giữ nguyên cols/rows cũ.
function refitNode(el) {
  const nid = el.dataset.nid || "";
  // "s:<sid>" = card agent (key = sid) · "editor:<sid>" = card editor (key = "ed:"+sid)
  const key = nid.startsWith("s:") ? nid.slice(2)
    : nid.startsWith("editor:") ? "ed:" + nid.slice(7) : "";
  const t = key && cvTerms[key];
  if (t) fitTerm(t);
}

// Zone tự bo quanh member: bbox các node member + header. Gọi sau mỗi lần đặt/kéo node.
function layoutZones() {
  for (const z of $("world").querySelectorAll(".group-zone")) {
    const g = cvGroups[+z.dataset.gi];
    if (!g || !g.els.length) continue;
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    let n = 0;
    for (const el of g.els) {
      if (el.classList.contains("pinned")) continue;   // xem cvGeomNodes
      n++;
      const x = parseFloat(el.style.left) || 0, y = parseFloat(el.style.top) || 0;
      x0 = Math.min(x0, x); y0 = Math.min(y0, y);
      x1 = Math.max(x1, x + el.offsetWidth); y1 = Math.max(y1, y + el.offsetHeight);
    }
    // Cả cụm chỉ còn mỗi cái đang ghim → không còn gì để bo.
    z.hidden = !n;
    if (!n) continue;
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
    // Mũi tên tới node ghim sẽ quét ngang màn hình theo từng cú pan — bỏ, đằng nào node ghim
    // cũng luôn nhìn thấy được.
    if (a.classList.contains("pinned") || b.classList.contains("pinned")) continue;
    const ra = rect(a), rb = rect(b);
    const p1 = rectBorderPoint(ra, rb.x + rb.w / 2, rb.y + rb.h / 2);
    const p2 = rectBorderPoint(rb, ra.x + ra.w / 2, ra.y + ra.h / 2);
    out += `<line x1="${p1.x}" y1="${p1.y}" x2="${p2.x}" y2="${p2.y}" class="edge edge-${e.cls}" marker-end="url(#ah-${e.cls})"/>`;
  }
  svg.innerHTML = EDGE_DEFS + out;
  redrawMinimap();   // gọi ở đây = mọi lần node đổi vị trí/kích thước/số lượng đều bắt được
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
  // Ghim lưu theo workspace, cùng chỗ với pos/view → đổi pane hay F5 vẫn giữ nguyên cửa sổ ghim.
  pinnedNid = st.pin || null;

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
      zonesHtml += zoneHtml(gi, cwd, list);
    }
    for (const s of list) {
      // Chỉ card 👑 (có terminal) mới gắn tay nắm + cờ data-rz; headless giữ size cố định của CSS.
      const rz = s.is_orch ? ` data-rz="1"` : "";
      nodesHtml += `<div class="node" data-nid="s:${esc(s.id)}"${rz}>`
                 + agentCard(s, needs.has(s.id), !!s.is_orch) + (s.is_orch ? RZ : "") + `</div>`;
      nodeMeta.push({ sid: s.id, cwd, grouped, gi });
    }
  }
  // innerHTML rebuild sẽ detach t.host → textarea của xterm bị BLUR (mất focus giữa lúc gõ).
  // Nhớ terminal nào đang giữ focus để trả lại sau khi attach (SSE re-render rất thường xuyên
  // khi worker đang chạy — không nhớ là user gõ vài phím lại văng focus một lần).
  const focusSid = Object.keys(cvTerms).find((sid) =>
    cvTerms[sid].host.contains(document.activeElement)) || null;
  // Thứ tự vẽ: zone (dưới) → edges (giữa) → agent card (trên) → card editor (sau cùng).
  // CHỈ dựng card của workspace NÀY: /api/editor trả mọi card đang mở của cả orchestrator, không
  // có khái niệm workspace. Không lọc thì mở editor ở workspace này lại hiện card ở cả pane bên
  // kia — tức là lộ cây mã nguồn sang tenant khác. `sessions` đã scope theo workspace của pane.
  const myEditors = editorCards.filter((c) => sessions.some((s) => s.id === c.session));
  // Card editor đi qua innerHTML như mọi card khác: ruột nó là .term-slot, mà host của xterm là
  // một <div> sống ngoài chu trình render và được attachTerms cắm lại sau — chuyển <div> sang cha
  // mới không mất gì. (Bản VS Code cũ phải giữ node ngoài innerHTML vì iframe đổi cha là tải lại.)
  for (const c of myEditors)
    nodesHtml += `<div class="node" data-nid="editor:${esc(c.session)}" data-rz="1">`
               + editorCardHtml(c) + RZ + `</div>`;
  world.replaceChildren();
  world.insertAdjacentHTML("afterbegin",
    zonesHtml + `<svg id="edges" class="edges"></svg>` + nodesHtml);

  // Đặt vị trí agent: có lưu → dùng lại; mới → xếp cụm theo cwd (seed từ pos group cũ nếu có).
  cvNodeEls = {};
  const agentEls = world.querySelectorAll('.node[data-nid^="s:"]');
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
            cx += 980; rowH = Math.max(rowH, 360);
          }
          gcur[m.cwd] = gc;
        }
        // Bước dòng 130 bám chiều cao card hiện tại (~76px + thở). Card cũ cao ~200px nên
        // con số cũ là 200 — giữ nguyên thì mỗi cụm mới thủng một khoảng trống to.
        pos[nid] = { x: gc.x0 + (gc.i % 3) * 300, y: gc.y0 + Math.floor(gc.i / 3) * 130 };
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
    applySize(el, pos[nid]);
  });
  // Card editor: mỗi cái một nid riêng nên nhớ được vị trí/kích thước riêng. Card mới xếp
  // chéo xuống để không chồng khít lên cái đang mở.
  world.querySelectorAll('.node[data-nid^="editor:"]').forEach((vsEl, i) => {
    const nid = vsEl.dataset.nid;
    if (!pos[nid]) pos[nid] = { x: 40 + i * 40, y: 40 + i * 40 };
    vsEl.style.left = pos[nid].x + "px";
    vsEl.style.top = pos[nid].y + "px";
    applySize(vsEl, pos[nid]);
  });
  cvSave({ pos });
  layoutZones();

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
  renderWinBar();      // danh sách cửa sổ đổi theo session 👑 và card editor
  renderInspector();   // dữ liệu vừa đổi → panel bên phải phải theo
  markSelection();     // innerHTML rebuild xoá .sel, gắn lại
  attachTerms();  // cắm terminal bền vào card 👑 (sau khi node đã vào DOM)
  // Trả focus cho terminal đang gõ dở (rAF: sau khi attachTerms đã cắm host vào slot mới).
  if (focusSid && cvTerms[focusSid])
    requestAnimationFrame(() => { const t = cvTerms[focusSid]; if (t && t.term) t.term.focus(); });

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
    if (e.target.closest(".cv-overlay")) return;  // overlay toolbar-hint: không pan/kéo
    // Tay nắm bề ngang của card GHIM. Phải xét trước chốt "node ghim thì bỏ qua" ngay bên dưới,
    // nếu không nó bị chặn và không bao giờ kéo được.
    const rzw = e.target.closest(".rzw");
    if (rzw) {
      const n = rzw.closest(".node");
      drag = { mode: "pinw", el: n, nid: n.dataset.nid, sx: e.clientX, ow: n.offsetWidth };
      pinResizing = true;
      cvInteracting = true;
      cv.classList.add("grabbing");
      cv.setPointerCapture(e.pointerId);
      return;
    }
    // Tay nắm resize phải xét TRƯỚC .node-head: nó nằm ngoài header nên nhánh dưới sẽ coi là
    // click nền rồi return, không kéo giãn được.
    const rz = e.target.closest(".rz");
    if (rz) {
      const n = rz.closest(".node");
      drag = { mode: "resize", el: n, sx: e.clientX, sy: e.clientY,
               ow: n.offsetWidth, oh: n.offsetHeight };
      cvInteracting = true;
      cv.classList.add("grabbing");
      cv.setPointerCapture(e.pointerId);
      return;
    }
    const head = e.target.closest(".node-head, .zone-head");
    const node = head && head.closest(".node");
    // Node ghim: hình học do applyPin tính, kéo nó chỉ làm hai bên ghi đè lẫn nhau.
    if (node && node.classList.contains("pinned")) return;
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
    // Không còn giữ nút nào mà `drag` vẫn còn = cú pointerup đã lạc mất (nhả ngoài cửa sổ, mất
    // pointer capture…). Không chốt chỗ này thì chỉ rê chuột qua canvas là khung nhìn tự chạy —
    // đúng triệu chứng "hover là tự dời vùng hiển thị".
    if (!e.buttons) return up();
    const dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;  // phân biệt click vs kéo
    if (drag.mode === "pan") { CV.tx = drag.ox + dx; CV.ty = drag.oy + dy; applyView(); return; }
    if (drag.mode === "pinw") {
      // Card ghim đã bù scale nên nó 1:1 với màn hình — dx dùng thẳng, KHÔNG chia CV.k như .rz.
      const pos = cvLoad().pos || {};
      const nid = drag.el.dataset.nid;
      pos[nid] = { ...(pos[nid] || {}), pw: Math.max(PIN_W_MIN, drag.ow + dx) };
      cvSave({ pos });
      applyPin();
      return;
    }
    if (drag.mode === "resize") {
      // chia CV.k: chuột đi 1px màn hình = 1/k px trong toạ độ world (canvas đang zoom)
      applySize(drag.el, { w: Math.max(RZ_MIN.w, drag.ow + dx / CV.k),
                           h: Math.max(RZ_MIN.h, drag.oh + dy / CV.k) });
      refitNode(drag.el);
      layoutZones(); redrawEdges();
      return;
    }
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
    if (drag.mode === "pinw") {
      pinResizing = false;
      // Bấm đúp lên tay nắm = bỏ bề ngang tuỳ chỉnh, về mặc định. KHÔNG dùng event `dblclick`:
      // nhánh này gọi cv.setPointerCapture(), và trong lúc capture mọi event chuột bị RETARGET
      // về canvas — `e.target.closest('.rzw')` trong handler dblclick đọc ra null, nên nhánh đó
      // không bao giờ chạy. Đã đo: bấm đúp giữ nguyên 980px thay vì về 760px.
      const now = performance.now();
      if (!drag.moved && cvInit._pinwAt && now - cvInit._pinwAt < 400) {
        const pos = cvLoad().pos || {};
        if (pos[drag.nid]) delete pos[drag.nid].pw;
        cvSave({ pos });
        applyPin();
      }
      cvInit._pinwAt = now;
      refitNode(drag.el);   // hoãn suốt lúc kéo, fit đúng một lần ở đây
      drag = null; cvInteracting = false;
      cv.classList.remove("grabbing");
      if (cvPending) { const p = cvPending; cvPending = null; renderCanvas(p.sessions, p.signals); }
      return;
    }
    if (drag.mode === "node" || drag.mode === "group" || drag.mode === "resize") {
      const pos = cvLoad().pos || {};
      const save = (el) => saveNodeGeom(el, pos);
      if (drag.mode === "group") drag.parts.forEach((p) => save(p.el)); else save(drag.el);
      cvSave({ pos });
    } else cvSave({ view: CV });
    // Click (không kéo) vào header card agent → CHỌN card (inspector mở bên phải).
    // Trước đây click mở thẳng drawer transcript; giờ transcript là một nút trong inspector.
    if (drag.mode === "node" && !drag.moved) {
      const card = drag.el.querySelector(".agent-card");
      if (card && card.dataset.sid) selectNode(card.dataset.sid);
    }
    // Click nền (pan mà không kéo) → bỏ chọn.
    if (drag.mode === "pan" && !drag.moved) selectNode(null);
    drag = null; cvInteracting = false;
    cv.classList.remove("grabbing");
    if (cvPending) { const p = cvPending; cvPending = null; renderCanvas(p.sessions, p.signals); }
  };
  cv.addEventListener("pointerup", up);
  cv.addEventListener("pointercancel", up);
  // Double-click tay nắm → bỏ size tùy chỉnh, về mặc định của CSS.
  cv.addEventListener("dblclick", (e) => {
    const rz = e.target.closest(".rz");
    if (!rz) return;
    const el = rz.closest(".node");
    applySize(el, null);
    const pos = cvLoad().pos || {};
    if (pos[el.dataset.nid]) { delete pos[el.dataset.nid].w; delete pos[el.dataset.nid].h; }
    cvSave({ pos });
    refitNode(el); layoutZones(); redrawEdges();
  });
  // Click vào THÂN card (ngoài header — header đi đường pointerup ở trên) → chọn card.
  // Card 👑: terminal (.term-slot) miễn trừ, nhưng overlay khóa (.term-lock) thì mở thẳng
  // drawer để xem run tự động đang chạy — đó là thứ người dùng đang chờ.
  cv.addEventListener("click", (e) => {
    const lock = e.target.closest(".term-lock");
    if (lock) { openSessionRun(lock.closest(".agent-card").dataset.sid); return; }
    if (e.target.closest("button, select, input, textarea, option, .term-slot, .node-head, .zone-head")) return;
    const card = e.target.closest(".agent-card");
    if (card && card.dataset.sid) selectNode(card.dataset.sid);
  });
  // Chiều cao node ghim đo từ canvas, mà canvas co giãn theo cửa sổ trình duyệt (và theo thanh
  // kéo giữa 2 pane) → không nghe resize thì panel ghim giữ nguyên chiều cao cũ.
  addEventListener("resize", () => { if (pinnedNid) applyPin(); });
  cv.addEventListener("wheel", (e) => {
    if (e.target.closest(".term-slot, .cv-overlay")) return;  // wheel trong terminal/editor/overlay = scroll, không zoom
    e.preventDefault();  // wheel = zoom quanh con trỏ (không scroll trang)
    const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    const k2 = Math.min(1.6, Math.max(0.35, CV.k * Math.exp(-e.deltaY * 0.0012)));
    CV.tx = mx - (mx - CV.tx) * (k2 / CV.k);
    CV.ty = my - (my - CV.ty) * (k2 / CV.k);
    CV.k = k2; applyView();
    clearTimeout(cvInit._t); cvInit._t = setTimeout(() => cvSave({ view: CV }), 300);
  }, { passive: false });
}

// ── Spawn form: picker dạng card (workspace / template / model) + duyệt thư mục ──

// Model chia theo engine — mỗi tab 1 engine.
// Tiền tố 'codex:' = engine Codex CLI, chạy bằng tài khoản ChatGPT đã `codex login` (không tốn
// API credits). BẮT BUỘC có tiền tố: 'gpt-5.6-terra' trơn cũng là tên model API hợp lệ, không
// có tiền tố thì không phân biệt được ý người dùng.
const MODEL_TABS = [
  // KHÔNG có card "Auto" ở tab Claude: id của nó phải là "" (bỏ cờ --model), mà form đọc model
  // rỗng là CHƯA CHỌN → chọn Auto xong bấm Spawn ăn "Pick a model", không đường nào ra.
  // Tab Codex vẫn có Auto vì id của nó là "codex" — một giá trị thật, không lẫn với rỗng.
  { engine: "claude", label: "Claude", note: "claude CLI · API credits Anthropic", models: [
    { id: "opus", name: "Opus · alias", desc: "Always points at the latest Opus" },
    { id: "sonnet", name: "Sonnet · alias", desc: "Balanced on quality, speed and price" },
    { id: "haiku", name: "Haiku · alias", desc: "Fast and cheap — light, repetitive work" },
    { id: "claude-fable-5", name: "Fable 5", desc: "Most capable (Claude 5) — deep reasoning and long agentic runs; pricier than Opus" },
    { id: "claude-opus-4-8", name: "Opus 4.8", desc: "Latest Opus — long autonomous agentic work, the best default" },
    { id: "claude-sonnet-5", name: "Sonnet 5", desc: "Close to Opus on code and agentic work, at Sonnet pricing" },
    { id: "claude-haiku-4-5", name: "Haiku 4.5", desc: "Fastest and cheapest — simple tasks" },
  ] },
  { engine: "codex", label: "Codex", note: "codex CLI · runs on your ChatGPT plan, spends no API credits", models: [
    { id: "codex", name: "Auto", desc: "Whatever the CLI picks from ~/.codex/config.toml" },
    { id: "codex:gpt-5.6-terra", name: "Terra 5.6", desc: "Balanced for everyday coding (Codex default) · effort up to ultra" },
    { id: "codex:gpt-5.6-luna", name: "Luna 5.6", desc: "Roomier limits than Terra — repetitive, long-running work · effort up to max" },
    { id: "codex:gpt-5.5", name: "GPT-5.5", desc: "Previous generation, stable · effort up to xhigh" },
    { id: "codex:gpt-5.4-mini", name: "5.4 mini", desc: "Light and fast — simple tasks, easy on your limits · effort up to xhigh" },
  ] },
];
const MODEL_CUSTOM = { id: "__custom", name: "Custom…",
  desc: "Type another model id / alias (claude: opus-4-7, opus-4-6…; codex: 'codex:<slug>')" };
let SP_TEMPLATES = [];                          // cache /api/skills/templates
let spSel = { ws: "", template: "", model: "" };  // lựa chọn hiện tại của form spawn
let spTab = MODEL_TABS[0].engine;                 // tab engine đang mở ở picker Model

// Format tên role thành slug kiểu folder: bỏ dấu tiếng Việt, chữ thường, [a-z0-9-].
function slugRole(s) {
  return s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/đ/gi, "d")
    .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

function spRoleSlug() {
  const slug = slugRole($("sp-role").value);
  $("sp-role-hint").innerHTML = slug
    ? `Session/skill: <code>${esc(slug)}</code> → <code>&lt;cwd&gt;/.claude/skills/${esc(slug)}/SKILL.md</code>`
    : `Formatted like a folder name: lowercase, accents stripped, spaces become "-".`;
}
window.spRoleSlug = spRoleSlug;

function pickCard(group, val, inner, title) {
  return `<div class="pick-card${spSel[group] === val ? " sel" : ""}"` +
    `${title ? ` title="${esc(title)}"` : ""} onclick="spPick('${group}','${esc(val)}')">${inner}</div>`;
}

function renderSpawnPickers() {
  const wsBox = $("sp-ws-cards");
  if (!wsBox) return;
  const wsItems = [{ id: "", name: "default", note: "shared workspace — pick the cwd below" }]
    .concat(WORKSPACES.filter((w) => w.id !== "default").map((w) => ({
      id: w.id, name: w.name || w.id,
      note: w.id + (w.status !== "active" ? " · " + w.status : ""),
    })));
  wsBox.innerHTML = wsItems.map((w) =>
    pickCard("ws", w.id, `<b>${esc(w.name)}</b><div class="pd">${esc(w.note)}</div>`)).join("");
  $("sp-template-cards").innerHTML = SP_TEMPLATES.length
    ? SP_TEMPLATES.map((t) => pickCard("template", t.name,
        `<b>${esc(t.name)}</b><div class="pd">${esc(t.description || "")}</div>`, t.description)).join("")
    // Init prompt gõ tay đã bỏ → hết template là hết đường spawn. Nói thẳng thay vì để form câm.
    : `<div class="hint">No templates found in <code>.claude/skills/</code> next to the program —
       add a <code>&lt;name&gt;/SKILL.md</code> folder there and reload this page.</div>`;
  const tab = MODEL_TABS.find((t) => t.engine === spTab) || MODEL_TABS[0];
  $("sp-model-tabs").innerHTML = MODEL_TABS.map((t) =>
    `<button type="button" class="tab-btn${t.engine === tab.engine ? " sel" : ""}" ` +
    `title="${esc(t.note)}" onclick="spTabPick('${t.engine}')">${esc(t.label)}</button>`).join("");
  $("sp-model-tabs-note").textContent = tab.note;
  $("sp-model-cards").innerHTML = tab.models.concat(MODEL_CUSTOM).map((m) => pickCard("model", m.id,
    `<b>${esc(m.name)}</b>` +
    (m.id && m.id !== "__custom" ? `<code>${esc(m.id)}</code>` : "") +
    `<div class="pd">${esc(m.desc)}</div>`)).join("");
  $("sp-model-custom").hidden = spSel.model !== "__custom";
  renderSpawnEffort();
}

// Model đang chọn ở form (ô custom tính cả text đang gõ) → dùng cho effort.
function spModel() {
  return spSel.model === "__custom" ? $("sp-model").value.trim() : spSel.model;
}

function renderSpawnEffort() {
  const sel = $("sp-effort"), cur = sel.value;
  const opts = effortOptsFor(spModel());
  sel.innerHTML = opts.map((e) =>
    `<option value="${e}">${e || `— select — (server default: ${esc(DEFAULT_EFFORT)})`}</option>`).join("");
  sel.value = opts.includes(cur) ? cur : "";   // mức cũ vượt trần model mới → về default
}

function spTabPick(engine) {
  spTab = engine;
  const tab = MODEL_TABS.find((t) => t.engine === engine);
  // Model đang chọn không thuộc tab vừa mở → về card đầu của tab (đỡ cảnh tab Codex mà đang chọn Opus).
  if (spSel.model !== "__custom" && !tab.models.some((m) => m.id === spSel.model))
    spSel.model = tab.models[0].id;
  renderSpawnPickers();
}
window.spTabPick = spTabPick;

// Gõ model tùy chỉnh: chỉ cập nhật 2 thứ phụ thuộc model, KHÔNG render lại cả picker
// (re-render mỗi ký tự là phí, và ô đang gõ nằm ngoài vùng render nên cũng chẳng cần).
function onModelCustomInput() { renderSpawnEffort(); }
window.onModelCustomInput = onModelCustomInput;

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
  box.innerHTML = `<div class="dir-crumb">Loading…</div>`;
  try {
    const d = await api("/api/fs" + (start ? "?path=" + encodeURIComponent(start) : ""));
    box.dataset.path = d.path;
    const item = (p, label) => `<div class="dir-item" data-path="${esc(p)}">${label}</div>`;
    box.innerHTML =
      `<div class="dir-crumb">📂 ${esc(d.path)}</div>
       <div class="dir-list">
         ${d.parent ? item(d.parent, "⬆ ..") : ""}
         ${d.dirs.map((n) => item(d.path.endsWith("/") ? d.path + n : d.path + "/" + n,
                                  "📁 " + esc(n))).join("") || `<div class="hint">(no subfolders)</div>`}
       </div>
       <div class="dir-actions">
         <button type="button" onclick="pickDir()">✔ Use this folder</button>
         <button type="button" class="secondary" onclick="closeDirBrowse()">Close</button>
       </div>`;
  } catch (e) {
    if (start) return browseDir("");   // path gõ tay sai → fallback về $HOME
    box.innerHTML = `<div class="dir-crumb" style="color:var(--red)">Error: ${esc(e)}</div>`;
  }
}
function pickDir() { $("sp-cwd").value = $("sp-dir").dataset.path || ""; closeDirBrowse(); }
function closeDirBrowse() { $("sp-dir").hidden = true; }
window.browseDir = browseDir; window.pickDir = pickDir; window.closeDirBrowse = closeDirBrowse;

// Gõ TÊN thư mục (không phải path) → tìm dưới $HOME. Nhận diện "đang gõ tên" = không có '/':
// có '/' nghĩa là user đang gõ path thật, để yên cho họ gõ (và nút 📁 duyệt như cũ).
async function searchDir(q) {
  const box = $("sp-dir");
  box.hidden = false;
  box.dataset.path = "";
  try {
    const d = await api("/api/fs?q=" + encodeURIComponent(q));
    const hits = d.matches || [];
    box.innerHTML = `<div class="dir-crumb">🔎 "${esc(q)}" — ${hits.length} folder(s)</div>
      <div class="dir-list">${hits.map((p) => `<div class="dir-item" data-path="${esc(p)}" data-pick="1">📁 ${esc(p)}</div>`).join("")
        || `<div class="hint">No folder name contains "${esc(q)}" (searched 4 levels under $HOME)</div>`}</div>
      <div class="dir-actions"><button type="button" class="secondary" onclick="closeDirBrowse()">Close</button></div>`;
  } catch (e) {
    box.innerHTML = `<div class="dir-crumb" style="color:var(--red)">Search failed: ${esc(e)}</div>`;
  }
}

let cwdTimer = null;
function onCwdInput(v) {
  clearTimeout(cwdTimer);
  const q = v.trim();
  if (q.includes("/") || q.length < 2) return closeDirBrowse();  // path gõ tay → không tìm
  cwdTimer = setTimeout(() => searchDir(q), 250);   // gõ xong mới bắn, đỡ 1 request/ký tự
}
window.onCwdInput = onCwdInput;

async function loadTemplates() {
  if (!$("sp-template-cards")) return;
  try {
    SP_TEMPLATES = await api("/api/skills/templates");
  } catch (e) { SP_TEMPLATES = []; }
  renderSpawnPickers();
}

// ── Form handlers ────────────────────────────────────────────────────────────

function showMsg(id, text, ok) {
  const el = $(id);
  el.textContent = text;
  el.className = "form-msg " + (ok ? "ok" : "err");
}

// ── MCP servers ──────────────────────────────────────────────────────────────
// Đăng ký ở scope user nên nó áp cho MỌI session claude, không thuộc workspace nào — vì thế
// mở từ topbar chứ không từ canvas.
// Token đi MỘT CHIỀU: gõ vào ô rồi POST đi. Server không bao giờ trả token về (chỉ 4 ký tự
// cuối), nên KHÔNG có chỗ nào điền ngược lại vào ô input — cố tình như vậy.
const MCP_STATE = {
  connected: "b-green", rejected: "b-red", unreachable: "b-amber",
  error: "b-red", unsupported: "b-gray",
};
let mcpStatus = {};   // name → {state, tools, detail} của lần kiểm gần nhất

function mcpRows(list) {
  if (!list.length) return `<div class="hint">Nothing registered yet.</div>`;
  return list.map((s) => {
    const st = mcpStatus[s.name];
    const label = !st ? (s.checkable ? "not checked" : s.type || "unknown")
      : st.state === "connected" ? `${st.tools} tools` : st.state;
    const meta = [s.type, s.url || s.command, s.token_hint && "token " + s.token_hint]
      .filter(Boolean).join(" · ");
    // Tên do người dùng đặt và có thể đã nằm sẵn trong file — KHÔNG nhét vào inline onclick,
    // đi qua data-attribute + listener uỷ quyền như chỗ duyệt thư mục.
    return `<div class="mcp-row">
      <span class="nm">${esc(s.name)}</span>
      ${badge(label, st ? MCP_STATE[st.state] : "b-gray")}
      <span class="meta" title="${esc(meta)}">${esc(meta)}</span>
      <div class="spacer"></div>
      ${s.checkable ? `<button class="secondary" data-mcp-check="${esc(s.name)}">Check</button>` : ""}
      <button class="danger" data-mcp-rm="${esc(s.name)}">Remove</button>
    </div>`;
  }).join("");
}

async function mcpLoad(check) {
  const list = await api("/api/mcp");
  $("mcp-list").innerHTML = mcpRows(list);
  if (!check) return list;
  // Kiểm SONG SONG khi mở modal. Không kiểm lúc boot: mỗi server tắt là một timeout, và người
  // dùng có thể đăng ký cả chục cái — trả giá đó cho mọi lần tải trang là vô lý.
  await Promise.all(list.filter((s) => s.checkable).map(async (s) => {
    try { mcpStatus[s.name] = await api("/api/mcp/check", "POST", { name: s.name }); }
    catch { mcpStatus[s.name] = { state: "error", tools: 0 }; }
  }));
  $("mcp-list").innerHTML = mcpRows(list);
  return list;
}

function mcpOpen() {
  $("mcp-modal").hidden = false;
  $("mcp-msg").textContent = "";
  $("mcp-list").innerHTML = `<div class="hint">Loading…</div>`;
  mcpLoad(true).catch((e) => showMsg("mcp-msg", "Error: " + e, false));
}
window.mcpOpen = mcpOpen;

function mcpClose() {
  $("mcp-modal").hidden = true;
  $("mcp-token").value = "";   // đừng để token gõ dở nằm lại trong DOM sau khi đóng
}
window.mcpClose = mcpClose;

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("mcp-modal").hidden) mcpClose();
});

// Nút trong danh sách: tên đi qua data-attribute, không qua inline onclick.
$("mcp-list").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-mcp-check], button[data-mcp-rm]");
  if (!btn) return;
  const name = btn.dataset.mcpCheck || btn.dataset.mcpRm;
  try {
    if (btn.dataset.mcpCheck) {
      showMsg("mcp-msg", `Checking ${name}…`, true);
      const r = await api("/api/mcp/check", "POST", { name });
      mcpStatus[name] = r;
      await mcpLoad(false);
      showMsg("mcp-msg", r.state === "connected" ? `${name}: ${r.tools} tools`
        : `${name}: ${r.detail || r.state}`, r.state === "connected");
    } else {
      if (!confirm(`Remove '${name}' from Claude Code?\n\nAgents already running keep it until they restart.`)) return;
      await api("/api/mcp/disconnect", "POST", { name });
      delete mcpStatus[name];
      await mcpLoad(false);
      showMsg("mcp-msg", `Removed ${name}.`, true);
    }
  } catch (e) { showMsg("mcp-msg", mcpErr(e), false); }
});

function mcpErr(e) {
  try { return JSON.parse(e).error || String(e); } catch { return String(e); }
}

function mcpForm() {
  return { name: $("mcp-name").value.trim(), url: $("mcp-url").value.trim(),
           token: $("mcp-token").value };
}

// Thử mà KHÔNG lưu — xem server có sống và token có đúng không trước khi ghi vào cấu hình.
async function mcpTest() {
  const f = mcpForm();
  if (!f.url) return showMsg("mcp-msg", "URL is required.", false);
  showMsg("mcp-msg", "Testing…", true);
  try {
    const r = await api("/api/mcp/check", "POST", { url: f.url, token: f.token });
    showMsg("mcp-msg", r.state === "connected" ? `Reachable — ${r.tools} tools. Not saved yet.`
      : (r.detail || r.state), r.state === "connected");
  } catch (e) { showMsg("mcp-msg", mcpErr(e), false); }
}
window.mcpTest = mcpTest;

async function mcpAdd() {
  const f = mcpForm();
  if (!f.name || !f.url) return showMsg("mcp-msg", "Name and URL are required.", false);
  showMsg("mcp-msg", "Connecting…", true);
  try {
    const r = await api("/api/mcp/connect", "POST", f);
    mcpStatus[r.name] = r;
    $("mcp-name").value = ""; $("mcp-url").value = "";
    $("mcp-token").value = "";   // xong việc là bỏ khỏi DOM, đừng để nằm lại trong form
    await mcpLoad(false);
    showMsg("mcp-msg", `Added ${r.name} — ${r.tools} tools. New agents pick it up automatically.`, true);
  } catch (e) { showMsg("mcp-msg", mcpErr(e), false); }
}
window.mcpAdd = mcpAdd;

async function spawnAgent() {
  // Tên vai và template là HAI thứ khác nhau: template chỉ là playbook NGUỒN (nhiều agent dùng
  // chung một template được), tên vai là danh tính để signal.
  const name = slugRole($("sp-role").value);
  const cwd = $("sp-cwd").value.trim();
  const model = spModel();
  const effort = $("sp-effort").value;
  // Mọi field phải có giá trị: agent thiếu cấu hình chỉ lộ ra ở run đầu tiên, lúc đó sửa đã tốn
  // một session. Chặn ở đây rẻ hơn nhiều. (Workspace luôn có card 'default' được chọn sẵn.)
  const missing = !name ? "Role name is required"
    : !spSel.template ? "Pick a playbook template"
    : !cwd ? "Working dir is required"
    : !model ? (spSel.model === "__custom" ? "Type a custom model id" : "Pick a model")
    : !effort ? "Pick a reasoning effort level" : "";
  if (missing) return showMsg("sp-msg", missing, false);
  showMsg("sp-msg", "Spawning…", true);
  try {
    const r = await api("/api/sessions/spawn", "POST", {
      name, cwd,
      workspace_id: spSel.ws,               // "" = default; ≠ default thì cwd tự ghim
      model,
      effort,
      // allowed_tools KHÔNG gửi: backend mặc định [] → bỏ cờ --allowedTools → CLI cho phép mọi
      // tool. Đó là mặc định cho cả claude lẫn codex. Muốn siết thì gọi thẳng API.
      template: spSel.template,
    });
    showMsg("sp-msg", `Spawned '${r.name}' (${r.id})`, true);
    $("sp-role").value = "";
    spRoleSlug();
    refreshAll();
  } catch (e) { showMsg("sp-msg", "Error: " + e, false); }
}

window.spawnAgent = spawnAgent;

// Hàng "load more" ở cuối bảng: nút + hiển thị thêm PAGE record cũ. Ẩn khi đã hết.
function moreRow(which, cols, hasMore, shown) {
  if (!hasMore) {
    // Chỉ hiện dòng "đã hết" khi đang xem nhiều hơn 1 trang (đỡ rối khi ít record).
    if (shown <= PAGE) return "";
    return `<tr class="more-row"><td colspan="${cols}"><span class="more-done">— end —</span></td></tr>`;
  }
  return `<tr class="more-row"><td colspan="${cols}">
    <button class="more-btn" onclick="showMore('${which}')">+ ${PAGE} older</button>
    <span class="more-count">showing ${shown}</span></td></tr>`;
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

const EV_LABEL = { history_user: "user (history)", history_ai: "ai (history)" };

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
        <span class="k">${kind === "tool_use" ? "tool" : esc(EV_LABEL[kind] || kind)}</span>
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
  $("dr-body").innerHTML = `<div class="empty">Loading transcript…</div>`;
  $("drawer").classList.add("open");
  $("drawer-overlay").classList.add("open");
  try {
    const steps = await api("/api/runs/" + runId + "/events");
    $("dr-body").innerHTML = steps.length
      ? pairTools(coalesceEvents(steps)).map(evRow).join("")
      : `<div class="empty">No steps yet — the run may still be starting.</div>`;
    scrollDrawerBottom();
  } catch (e) {
    $("dr-body").innerHTML = `<div class="empty" style="color:var(--red)">Load failed: ${esc(e)}</div>`;
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
  $("dr-body").innerHTML = `<div class="empty">Looking for a run…</div>`;
  $("drawer").classList.add("open");
  $("drawer-overlay").classList.add("open");
  try {
    const runs = await api(`/api/sessions/${encodeURIComponent(sid)}/runs`);
    if (!runs.length) {
      $("dr-body").innerHTML = `<div class="empty">This session has no runs yet.</div>`;
      return;
    }
    await openRun(runs[0].id);
    $("dr-title").textContent = `Run #${runs[0].id} · ${s ? s.name : ""}`;
    $("dr-badge").innerHTML = badge(runs[0].status, RUN_BADGE[runs[0].status]);
  } catch (e) {
    $("dr-body").innerHTML = `<div class="empty" style="color:var(--red)">Error: ${esc(e)}</div>`;
  }
}
window.openSessionRun = openSessionRun;

// ── Tabs: Agents (canvas) / History (signal queue + audit log) ──────────────
function switchTab(name) {
  $("tab-agents").hidden = name !== "agents";
  $("tab-history").hidden = name !== "history";
  $("tab-btn-agents").classList.toggle("active", name === "agents");
  $("tab-btn-history").classList.toggle("active", name === "history");
  // Key theo workspace: hai pane mở cùng lúc dùng chung một key thì đổi tab ở pane này
  // lật luôn pane kia.
  try { localStorage.setItem("orch-tab." + currentWS, name); } catch { /* private mode */ }
  // Quay lại tab agents: xterm cần fit lại (lúc ẩn display:none đo được 0×0).
  renderWinBar();
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

// Shell không mở SSE (xem chú thích connectSSE) → pill trạng thái bám kết quả fetch.
function setConn(ok) {
  const el = $("conn");
  if (!el) return;
  el.className = "pill " + (ok ? "live" : "dead");
  el.textContent = ok ? "live" : "offline";
}

async function refreshAll() {
  try {
    const [workspaces, health] = await Promise.all([api("/api/workspaces"), api("/health")]);
    if (health.daily_allow_step) DAILY_STEP = health.daily_allow_step;
    if (health.default_effort) DEFAULT_EFFORT = health.default_effort;
    if (health.pair_signal_cap) PAIR_CAP = health.pair_signal_cap;
    if (health.pair_signal_window_min) PAIR_WINDOW_MIN = health.pair_signal_window_min;
    $("dry").hidden = !health.dry_run;

    if (!PANE) {
      // Workspace bị xoá ở nơi khác → đóng tab của nó, đừng để iframe trỏ vào hư không.
      const live = new Set(workspaces.map((w) => w.id));
      if (OPEN.some((id) => !live.has(id))) {
        OPEN = OPEN.filter((id) => live.has(id));
        if (!OPEN.length) { ACTIVE = -1; SPLIT = false; }
        shellSave();
      }
      renderWorkspaces(workspaces);
      setConn(true);
      return;   // shell không đụng tới sessions/signals/runs — pane lo phần đó
    }

    // Workspace của pane này biến mất → nhờ shell đóng tab (pane không tự đóng được).
    if (!workspaces.some((w) => w.id === currentWS)) {
      try { parent.postMessage({ t: "gone", ws: currentWS }, location.origin); } catch { /* không có shell */ }
      return;
    }
    renderWorkspaces(workspaces);
    $("ws-detail-view").hidden = false;
    $("hdr-ws").hidden = false;

    const q = wsQuery();
    const [sessions, signals, runs, vsc] = await Promise.all([
      api("/api/sessions" + q),
      api("/api/signals" + pagedQuery(sigShown)),
      api("/api/runs" + pagedQuery(runsShown)),
      api("/api/editor").catch(() => []),
    ]);
    editorCards = Array.isArray(vsc) ? vsc : [];
    renderCanvas(sessions, signals.items);
    fillSignalForm(sessions);
    sigHasMore = signals.has_more; renderSignals(signals.items);
    runsHasMore = runs.has_more; renderRuns(runs.items);
  } catch (e) { console.error(e); if (!PANE) setConn(false); }
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

// ── Boot ────────────────────────────────────────────────────────────────────
// Hai vai, hai đường khởi động. Xem chú thích PANE ở đầu file.
const qs = new URLSearchParams(location.search);

if (PANE) {
  // class .pane đã được gắn vào <html> từ script trong <head> (trước paint).
  currentWS = qs.get("ws") || "";
  $("ws-detail-view").hidden = false;
  $("hdr-ws").hidden = false;
  cvInit();
  mmInit();
  try { switchTab(localStorage.getItem("orch-tab." + currentWS) || "agents"); }
  catch { /* tab mặc định */ }
  // Shell nói chuyện xuống: 'show' = pane vừa hiện/đổi bề rộng (xterm phải fit lại,
  // display:none đo được 0×0); 'theme' = user vừa đổi theme ở shell.
  window.addEventListener("message", (e) => {
    if (e.origin !== location.origin) return;
    const m = e.data || {};
    if (m.t === "show") requestAnimationFrame(() => Object.values(cvTerms).forEach(fitTerm));
    else if (m.t === "theme") applyTheme(m.pref);
  });
  refreshAll();
  // SSE CHỈ ở pane. HTTP/1.1 giới hạn 6 kết nối mỗi origin và EventSource giữ kết nối
  // vĩnh viễn — shell mở thêm một cái nữa là 2 pane + shell = 3, phần còn lại cho fetch
  // mỏng đi thấy rõ. Shell poll thay thế (nó chỉ cần đếm session ở màn Home).
  if (!location.search.includes("nosse")) connectSSE();
} else {
  applyTheme(themePref());
  const st = shellLoad();
  const fromUrl = (qs.get("ws") || "").split(",").filter(Boolean);
  OPEN = (fromUrl.length ? fromUrl : (st.open || [])).slice(0, MAX_PANES);
  SPLIT = qs.has("split") ? qs.get("split") === "1" : !!st.split;
  SPLIT_PCT = st.pct || 50;
  ACTIVE = !OPEN.length ? -1
    : fromUrl.length ? 0
    : Math.min(st.active == null ? 0 : st.active, OPEN.length - 1);
  initSplitDrag();
  // Pane báo lên: workspace của nó không còn → đóng tab.
  window.addEventListener("message", (e) => {
    if (e.origin !== location.origin) return;
    if ((e.data || {}).t === "gone") closeWs(e.data.ws);
  });
  // Duyệt thư mục: click folder trong panel → đi sâu vào (path ở data-path, không inline).
  $("sp-dir").addEventListener("click", (ev) => {
    const it = ev.target.closest(".dir-item");
    if (!it) return;
    // Kết quả tìm: click là CHỌN luôn (đã là thư mục đích). Duyệt cây: click là đi vào.
    if (it.dataset.pick) { $("sp-cwd").value = it.dataset.path; closeDirBrowse(); }
    else browseDir(it.dataset.path);
  });
  refreshAll();
  loadTemplates();
  // Màn Home hiện số session mỗi workspace — poll nhẹ, không cần SSE (xem trên).
  setInterval(() => { if (ACTIVE < 0) refreshAll(); }, 5000);
}
