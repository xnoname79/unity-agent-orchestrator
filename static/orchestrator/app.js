// Session Orchestrator dashboard — lightweight, no framework.
// Fetches state from the Control API, refreshes on SSE events.

const $ = (id) => document.getElementById(id);

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
    currentWS = w.id;                 // nhảy vào workspace vừa tạo
    await refreshAll();
    alert(`Created workspace '${w.name}'\nid: ${w.id}\nfolder: ${w.root_dir}`);
  } catch (e) { console.error(e); alert("Could not create workspace: " + e); }
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
// Khóa terminal: run tự động (signal [REPORT] từ agent khác) đang/đã chạy trên session
// → PTY cũ hết ngữ cảnh, 2 claude cùng ghi 1 transcript sẽ xung đột. Key theo NAME
// (bền qua xoay session id); chỉ mở lại khi user bấm 🔄.
let termLock = {};  // session name → true
// CLI người dùng chọn cho terminal, theo NAME (bền qua xoay session id) — rỗng = theo engine của
// session. Chọn CLI KHÁC engine thì backend mở phiên MỚI (id của claude không resume được bằng
// codex và ngược lại), nên đổi lựa chọn = hủy PTY cũ rồi nối lại.
let termCli = {};   // session name → 'claude' | 'codex'

// Gương của engine_from_model bên service — model là nguồn sự thật duy nhất của engine.
// Lệch với backend là UI hiện sai (vd mời chọn effort 'ultra' mà engine không nhận).
function engineOfModel(model) {
  const m = (model || "").trim().toLowerCase();
  return m === "codex" || m.startsWith("codex:") ? "codex" : "claude";
}

async function setTermCli(sid, name, cli) {
  termCli[name] = cli;
  await reconnectTerm(sid, name);
}
window.setTermCli = setTermCli;

function startTerm(t) {
  t.started = true;
  t.term = new Terminal({ fontSize: 12, cursorBlink: true, scrollback: 5000,
                          theme: { background: "#0c0e12", foreground: "#e6e8eb" } });
  t.fit = new FitAddon.FitAddon();
  t.term.loadAddon(t.fit);
  t.term.open(t.host);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  t.ws = new WebSocket(`${proto}://${location.host}/ws/terminal?session=${encodeURIComponent(t.sid)}`
                       + (t.cli ? `&cli=${encodeURIComponent(t.cli)}` : ""));
  t.ws.binaryType = "arraybuffer";
  t.ws.onopen = () => fitTerm(t);
  t.ws.onmessage = (e) => t.term.write(typeof e.data === "string" ? e.data : new Uint8Array(e.data));
  t.ws.onclose = () => { if (t.term) t.term.write("\r\n\x1b[31m[disconnected — press 🔄 to reload]\x1b[0m\r\n"); };
  t.term.onData((d) => { if (t.ws.readyState === 1) t.ws.send(JSON.stringify({ t: "i", d })); });
}

// Gửi thẳng 1 chuỗi phím vào PTY qua WS, KHÔNG đi qua bàn phím/xterm. Dùng cho phím hay bị
// nuốt trước khi tới trang: Esc bị bộ gõ tiếng Việt (Unikey/ibus) ăn để đóng bảng gợi ý, bị
// trình duyệt ăn khi đang fullscreen, hoặc terminal vừa mất focus sau một lần SSE re-render.
// Phím thường (chữ, Enter) không dính vì bộ gõ nhả chúng ra; Esc thì không.
function termSend(sid, seq) {
  const t = cvTerms[sid];
  if (!t || !t.ws || t.ws.readyState !== 1) return;
  t.ws.send(JSON.stringify({ t: "i", d: seq }));
  if (t.term) t.term.focus();   // trả focus để gõ tiếp ngay
}
window.termSend = termSend;

function termEsc(sid) { termSend(sid, "\x1b"); }
window.termEsc = termEsc;

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
// ── Card VS Code (code serve-web trong iframe) ──────────────────────────────
// CHỈ 1 card tại một thời điểm — server chỉ giữ 1 tiến trình, mở cho session khác là giết cái cũ.
// iframe sống NGOÀI chu trình innerHTML của canvas (giống terminal): mỗi render chỉ re-attach vào
// .vscode-slot, nếu không thì mỗi lần SSE re-render là VS Code tải lại từ đầu.
let vscodeState = { open: false };   // {open, session, name, cwd, port, token, ready}
let cvVscode = null;                 // {el: <iframe>, key} — key đổi thì mới dựng iframe mới

function vscodeUrl(st) {
  // Server bind 0.0.0.0 nên không tự biết hostname client gọi được → ghép ở đây.
  return `http://${location.hostname}:${st.port}/?tkn=${encodeURIComponent(st.token)}`
       + `&folder=${encodeURIComponent(st.cwd)}`;
}

async function openVscode(sid, name) {
  if (vscodeState.open && vscodeState.session !== sid
      && !confirm(`VS Code is already open for '${vscodeState.name}'.\n\nOpening it for '${name}' will SHUT THAT ONE DOWN (the process exits). Continue?`))
    return;
  try {
    vscodeState = await api("/api/vscode/open", "POST", { session: sid });
    await refreshAll();
  } catch (e) { alert("Could not open VS Code: " + e); }
}
window.openVscode = openVscode;

async function closeVscode() {
  if (!confirm("Close VS Code? The serve-web process will exit.")) return;
  try { await api("/api/vscode/close", "POST"); vscodeState = { open: false }; await refreshAll(); }
  catch (e) { alert("Could not close VS Code: " + e); }
}
window.closeVscode = closeVscode;

function reloadVscode() {
  // Lần chạy đầu VS Code còn tải server → iframe có thể rỗng. Nút này nạp lại chính iframe đó.
  if (cvVscode) cvVscode.el.src = cvVscode.el.src;
}
window.reloadVscode = reloadVscode;

function vscodeCardHtml(st) {
  return `<div class="agent-card vscode-card">
    <div class="node-head vscode-head">
      <span>💻 VS Code</span><b>${esc(st.name || "")}</b>
      <span class="cwd" title="${esc(st.cwd || "")}">${esc(st.cwd || "")}</span>
      <span class="spacer"></span>
      <button class="secondary" onclick="reloadVscode()" title="Reload the iframe — useful while the server is still starting">🔄</button>
      <button class="danger" onclick="closeVscode()" title="Close VS Code (exits the process)">✕</button>
    </div>
    <div class="vscode-slot"></div>
  </div>`;
}

function attachVscode() {
  const slot = $("world").querySelector(".vscode-slot");
  if (!slot) { cvVscode = null; return; }   // card không còn → bỏ iframe (server đã giết proc)
  const key = `${vscodeState.session}|${vscodeState.token}`;
  if (!cvVscode || cvVscode.key !== key) {
    const el = document.createElement("iframe");
    el.className = "vscode-frame";
    el.src = vscodeUrl(vscodeState);
    cvVscode = { el, key };
  }
  slot.appendChild(cvVscode.el);   // re-attach, KHÔNG đặt lại src → không reload
}

function attachTerms() {
  const seen = new Set();
  for (const slot of $("world").querySelectorAll(".term-slot")) {
    const sid = slot.dataset.sid;
    seen.add(sid);
    let t = cvTerms[sid];
    if (!t) t = cvTerms[sid] = { sid, host: Object.assign(document.createElement("div"),
                                                          { className: "term-host" }),
                                 started: false, term: null, fit: null, ws: null };
    t.cli = slot.dataset.cli || "";   // đọc TRƯỚC startTerm (rAF chạy sau vòng render này)
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
  const msg = prompt(`Signal to '${name}':`, "");
  if (!msg || !msg.trim()) return;
  try { await api("/api/signals", "POST", { to_session: id, message: msg.trim() }); await refreshAll(); }
  catch (e) { console.error(e); alert("Could not send signal: " + e); }
}
window.sendSignalTo = sendSignalTo;

// Dropdown "Action" trên card: mọi lựa chọn đều phá hủy nên luôn qua confirm.
// Trả select về "Chọn…" NGAY (trước cả confirm) — nếu để kẹt ở lựa chọn cũ thì lần sau chọn lại
// đúng mục đó sẽ không phát onchange, người dùng bấm mà không thấy gì xảy ra.
async function runAction(id, name, sel) {
  const action = sel.value;
  sel.value = "";
  if (action === "unregister") {
    if (confirm(`Remove session '${name}' from the orchestrator?\n\nRuns, signals and audit records are kept.`))
      await act(`/api/sessions/${id}/unregister`);
  }
}
window.runAction = runAction;

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

// 1 card agent. needsYou = có signal chờ duyệt tới nó; isOrch = s.is_orch (backend, toggle 💻
// — card này mở terminal nhúng, 1 terminal mỗi project; không còn ý nghĩa gì với routing signal).
function agentCard(s, needsYou, isOrch) {
  const id = encodeURIComponent(s.id);
  const tools = JSON.parse(s.allowed_tools || "[]") || [];
  const ctrl = (s.status === "paused" || s.status === "stopped")
    ? `<button onclick="act('/api/sessions/${id}/resume')" title="Resume">▶</button>`
    : `<button onclick="act('/api/sessions/${id}/pause')" title="Pause">⏸</button>`;
  const allow = s.daily_blocked
    ? `<button class="warn" onclick="allowMore('${id}','${esc(s.name)}')">Allow +${DAILY_STEP}</button>` : "";
  const today = s.daily_limit
    ? `<span class="${s.daily_blocked ? "day-hit" : "day-ok"}" title="runs today / daily limit">${s.used_today}/${s.daily_limit}</span>`
    : "";
  // Mức hiện ra bám theo model của CHÍNH session này — gpt-5.5 không có 'max', đừng mời chọn.
  const effortSel = `<select class="mini" onchange="setEffort('${id}', this.value)">` +
    effortOptsFor(s.model).map((e) => `<option value="${e}"${e === (s.effort || "") ? " selected" : ""}>${e || "effort"}</option>`).join("") +
    `</select>`;
  const head = `<div class="node-head">
      <span class="status-dot dot-${esc(s.status)}"></span>
      ${isOrch ? `<span title="This session owns the project terminal">👑</span>` : ""}
      <b title="${esc(s.name)}">${esc(s.name)}</b>
      ${needsYou ? `<span class="needs-badge">NEEDS YOU</span>` : ""}
      <span class="spacer"></span>
      <span class="sid" title="${esc(s.id)}">${esc(s.id)}</span>
    </div>`;
  const engine = engineOfModel(s.model);
  // Nút biểu tượng = thao tác MỞ một thứ gì đó (terminal / thư mục project) → giữ icon.
  // Nút chữ = thao tác trên dữ liệu session → gom theo nhãn Context / Action ở dưới.
  const vsBtn = (s.cwd || "").trim()
    ? `<button class="secondary" onclick="openVscode('${id}','${esc(s.name)}')"
        title="Open this session's project folder in VS Code (one card only — opening another closes this one)">📁</button>`
    : "";
  const killBtn = s.status === "running"
    ? `<button class="danger" onclick="if(confirm('Kill the running job on ${esc(s.name)}? The run is marked failed and is not retried.'))act('/api/sessions/${id}/kill')" title="Kill the running job (stops a runaway)">🛑</button>`
    : "";
  const ctxGroup = (extra = "") => `<div class="act-group"><span class="act-label">Context</span>
    <button class="secondary act-txt" onclick="viewCompact('${id}','${esc(s.name)}')"
      title="View this session's current context / SKILL">Xem</button>${extra}</div>`;
  // Action đều là thao tác PHÁ HỦY → giấu sau dropdown, không để bấm nhầm khi rê chuột trên card.
  // KHÔNG có "Xóa vĩnh viễn": transcript do CLI (claude/codex) giữ, orchestrator không xóa được
  // — bày ra là mời user chọn thứ chắc chắn lỗi. Gỡ session thì dùng Unregister.
  // Nhãn NGẮN: select đóng chỉ hiện 1 dòng và không ellipsis được text option — nhãn dài sẽ
  // đẩy rộng cả cột nút, ăn chỗ terminal. Giải thích đầy đủ để ở title (hover).
  const actOpts = [["unregister", "Unregister", "Remove the session from the orchestrator — runs, signals and audit are kept"]];
  const actGroup = `<div class="act-group"><span class="act-label">Action</span>
    <select class="mini act-sel" title="Destructive actions — pick one, then confirm"
      onchange="runAction('${id}','${esc(s.name)}', this)">
      <option value="">Choose…</option>
      ${actOpts.map(([v, label, hint]) =>
        `<option value="${v}" title="${esc(hint)}">${esc(label)}</option>`).join("")}
    </select></div>`;
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
          ${ctrl}${killBtn}${allow}${vsBtn}
          ${cliSel}
          <button onclick="reconnectTerm('${esc(s.id)}','${esc(s.name)}')" title="Reload the session/terminal (restarts the selected CLI)">🔄</button>
          <button class="secondary" onclick="termEsc('${esc(s.id)}')"
                  title="Send an Esc keypress to the terminal — for when the keyboard Esc is swallowed by an input method or the browser. Same as Ctrl+[">⎋</button>
          <button class="secondary" onclick="if(confirm('Close the terminal on ${esc(s.name)}? The session goes back to headless.'))toggleOrch('${id}',0)"
            title="Close the terminal — the session goes back to a headless worker">💻</button>
          ${ctxGroup()}${actGroup}
        </div>
        <div class="term-slot" data-sid="${esc(s.id)}" data-cli="${cli}"${locked ? ` data-lock="1"` : ""}>${lock}</div>
      </div>
    </div>`;
  }

  return `<div class="agent-card ${cls}" data-sid="${esc(s.id)}">
    ${head}
    <div class="agent-body">
      <div class="rw"><input class="mini model-in grow" list="model-list" value="${esc(s.model || "")}"
        placeholder="model: auto" onchange="setModel('${id}', this.value.trim())">${effortSel}</div>
      <div class="rw"><span title="${esc(tools.join(", ") || "every tool allowed")}">🔧 ${tools.length ? tools.length + " tools" : "all tools"}</span>
        <span class="spacer"></span>${today}</div>
    </div>
    <div class="agent-actions">
      <div class="act-row">
        ${ctrl}${killBtn}${allow}
        <button class="secondary" onclick="toggleOrch('${id}',1)"
          title="Open a terminal for this session (one per project — closes any other terminal in the same cwd)">💻</button>
        ${vsBtn}
      </div>
      ${ctxGroup(`<button class="secondary act-txt" onclick="editSkill('${id}','${esc(s.name)}')"
        title="Edit this role's SKILL (upserts into .claude/skills in the project cwd)">Update</button>`)}
      ${actGroup}
    </div>
  </div>`;
}

// ── Zone (cwd) + orchestrator + chat ────────────────────────────────────────
let cvGroups = [];    // [{cwd, els:[nodeEl]}] — rebuild mỗi render; drag group đọc từ đây
let cvNodeEls = {};   // session_id → node element (để vẽ edge)
let cvEdges = [];     // [{from, to, cls}] resolve từ signal list
let cvLast = { sessions: [], signals: [] };  // data mới nhất (re-render cục bộ không cần fetch)

const EDGE_COLORS = { wait: "#f0a020", run: "#4c8dff" };  // done/failed không vẽ mũi tên
const EDGE_DEFS = "<defs>" + Object.entries(EDGE_COLORS).map(([k, c]) =>
  `<marker id="ah-${k}" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
     <path d="M0,0L8,4L0,8z" fill="${c}"/></marker>`).join("") + "</defs>";

// Toggle vai orchestrator cho 1 session DB (nguồn sự thật: cột is_orch backend — không còn
// localStorage). Bật: backend tự đóng terminal của session khác cùng cwd (1 terminal/project).
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
      <div class="zone-title">📁 <b>${esc(base)}</b><span class="g-count">${list.length} agents</span>
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
const RZ = `<div class="rz" title="Drag to resize the card (double-click to reset)"></div>`;
const RZ_MIN = { w: 220, h: 130 };

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
  const t = nid.startsWith("s:") && cvTerms[nid.slice(2)];
  if (t) fitTerm(t);
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
  // Thứ tự vẽ: zone (dưới) → edges (giữa) → agent card (trên).
  // Card VS Code: 1 node độc lập (không thuộc session nào) — kéo thả/lưu vị trí như node khác.
  if (vscodeState.open)
    nodesHtml += `<div class="node" data-nid="vscode" data-rz="1">${vscodeCardHtml(vscodeState)}${RZ}</div>`;
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
    applySize(el, pos[nid]);
  });
  const vsEl = world.querySelector('.node[data-nid="vscode"]');
  if (vsEl) {
    if (!pos.vscode) pos.vscode = { x: 40, y: 40 };
    vsEl.style.left = pos.vscode.x + "px";
    vsEl.style.top = pos.vscode.y + "px";
    applySize(vsEl, pos.vscode);
  }
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
  attachTerms();  // cắm terminal bền vào card 👑 (sau khi node đã vào DOM)
  attachVscode(); // iframe VS Code cũng phải bền qua re-render, không thì reload liên tục
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
    if (drag.mode === "node" || drag.mode === "group" || drag.mode === "resize") {
      const pos = cvLoad().pos || {};
      const save = (el) => saveNodeGeom(el, pos);
      if (drag.mode === "group") drag.parts.forEach((p) => save(p.el)); else save(drag.el);
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
  // Click vào THÂN card (ngoài header — header đi đường pointerup ở trên) → drawer run.
  // Card 👑: terminal (.term-slot) miễn trừ, nhưng overlay khóa (.term-lock) thì mở drawer
  // để xem run tự động đang chạy.
  cv.addEventListener("click", (e) => {
    const lock = e.target.closest(".term-lock");
    if (lock) { openSessionRun(lock.closest(".agent-card").dataset.sid); return; }
    if (e.target.closest("button, select, input, textarea, option, .term-slot, .vscode-slot, .node-head, .zone-head")) return;
    const card = e.target.closest(".agent-card");
    if (card) openSessionRun(card.dataset.sid);
  });
  cv.addEventListener("wheel", (e) => {
    if (e.target.closest(".term-slot, .vscode-slot, .cv-overlay")) return;  // wheel trong terminal/VS Code/overlay = scroll, không zoom
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
  box.innerHTML = `<div class="tool-group">Loading…</div>`;
  try {
    const data = await api("/api/available-tools?cwd=" + encodeURIComponent(cwd));
    box.innerHTML = renderTools(data);
  } catch (e) {
    box.innerHTML = `<div class="tool-group" style="color:var(--red)">Could not load tools: ${esc(e)}</div>`;
  }
}
window.loadTools = loadTools;

// ── Spawn form: picker dạng card (workspace / template / model) + duyệt thư mục ──

// Model chia theo engine — mỗi tab 1 engine.
// Tiền tố 'codex:' = engine Codex CLI, chạy bằng tài khoản ChatGPT đã `codex login` (không tốn
// API credits). BẮT BUỘC có tiền tố: 'gpt-5.6-terra' trơn cũng là tên model API hợp lệ, không
// có tiền tố thì không phân biệt được ý người dùng.
const MODEL_TABS = [
  { engine: "claude", label: "Claude", note: "claude CLI · API credits Anthropic", models: [
    { id: "", name: "Auto", desc: "Let the CLI pick its own default model" },
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
  syncToolPicker();
}

// Model đang chọn ở form (ô custom tính cả text đang gõ) → dùng cho effort + hiện/ẩn tools.
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

// codex KHÔNG có allowlist tool (chỉ shell + apply_patch, luôn bật) → allowed_tools bị bỏ qua.
// Hiện picker ở đó là mời user tick thứ không có tác dụng.
function syncToolPicker() {
  const codex = engineOfModel(spModel()) === "codex";
  $("sp-tools-wrap").hidden = codex;
  $("sp-tools-codex").hidden = !codex;
  if (codex) $("sp-tools").innerHTML = "";
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
function onModelCustomInput() { renderSpawnEffort(); syncToolPicker(); }
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
  // Tên vai và template là HAI thứ khác nhau: template chỉ là playbook NGUỒN (nhiều agent dùng
  // chung một template được), tên vai là danh tính để signal — phải unique trong workspace.
  const name = slugRole($("sp-role").value);
  const cwd = $("sp-cwd").value.trim();
  const model = spModel();
  const effort = $("sp-effort").value;
  // Mọi field phải có giá trị: agent thiếu cấu hình chỉ lộ ra ở run đầu tiên, lúc đó sửa đã tốn
  // một session. Chặn ở đây rẻ hơn nhiều. (Workspace luôn có card 'default' được chọn sẵn;
  // allowed tools để trống là CÓ nghĩa — bỏ cờ --allowedTools = CLI cho phép mọi tool.)
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
      // codex bỏ qua allowed_tools → gửi [] cho khớp sự thật, đừng lưu vào DB thứ không có hiệu lực.
      allowed_tools: engineOfModel(spModel()) === "codex" ? [] : collectTools("sp"),
      template: spSel.template,
    });
    showMsg("sp-msg", `Spawned '${r.name}' (${r.id})`, true);
    $("sp-role").value = "";
    spRoleSlug();
    $("sp-tools").innerHTML = "";
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
    if (health.default_effort) DEFAULT_EFFORT = health.default_effort;
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
    const [sessions, signals, runs, vsc] = await Promise.all([
      api("/api/sessions" + q),
      api("/api/signals" + pagedQuery(sigShown)),
      api("/api/runs" + pagedQuery(runsShown)),
      api("/api/vscode").catch(() => ({ open: false })),
    ]);
    vscodeState = vsc;
    renderCanvas(sessions, signals.items);
    fillSignalForm(sessions);
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
  if (!it) return;
  // Kết quả tìm: click là CHỌN luôn (đã là thư mục đích). Duyệt cây: click là đi vào.
  if (it.dataset.pick) { $("sp-cwd").value = it.dataset.path; closeDirBrowse(); }
  else browseDir(it.dataset.path);
});
try { switchTab(localStorage.getItem("orch-tab") || "agents"); } catch { /* tab mặc định */ }
refreshAll();
loadTemplates();
if (!location.search.includes("nosse")) connectSSE();  // ?nosse: tắt SSE khi debug/test headless
