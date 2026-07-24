"""
Session Orchestrator — Phase A (core engine, no UI yet)

Điều phối headless Claude sessions: agent phát signal → orchestrator poll →
inject message vào session target qua `claude -p --resume` → ghi audit log.

Phase A gồm: SQLite store (sessions/signals/runs), session registry, signal
poller, executor có per-session lock + tool allowlist, audit log.

An toàn (nền từ Phase A):
  - Tool allowlist per session (--allowedTools)
  - Per-session lock: chỉ 1 prompt in-flight mỗi session (chống trộn transcript)
  - Max concurrent sessions (semaphore)
  - requires_approval: signal nhạy cảm chờ approve (UI ở Phase C), không auto-run
  - Audit log: mọi injection ghi vào bảng runs
  - ORCH_DRY_RUN=1: chạy thử pipeline mà KHÔNG gọi claude thật

Env:
  ORCH_DB              tên DB (default "orchestrator") → ~/.session_orch_db/<name>.db
  ORCH_DRY_RUN         "1" = không gọi claude thật, trả stub (default "0")
  ORCH_POLL_INTERVAL   giây giữa các lần poll (default 5)
  ORCH_MAX_CONCURRENT  số session chạy song song tối đa (default 3)
  ORCH_STREAM          "1" = stream transcript (thinking/tool_use/text) real-time (default 1)
  ORCH_STREAM_PARTIAL  "1" = thêm --include-partial-messages, text chảy từng token (default 0)
  ORCH_EVENT_TRUNC     số ký tự tối đa mỗi payload event (default 2000)
  ORCH_DEFAULT_EFFORT  reasoning effort mặc định mọi session (default "high"; low|medium|high|max)
  ORCH_DEFAULT_PERMISSION_MODE  permission mode fallback khi session không set (default "bypassPermissions")
  CLAUDE_BIN           đường dẫn claude CLI (default "claude")

Usage:
  python3 session_orchestrator.py init            # tạo DB
  python3 session_orchestrator.py once            # poll & xử lý 1 lần
  python3 session_orchestrator.py loop            # chạy daemon poll
  python3 session_orchestrator.py list-sessions
  python3 session_orchestrator.py list-signals
  python3 session_orchestrator.py list-runs
"""

import argparse
import asyncio
import json
import os
import re
import secrets
import shlex
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import httpx

DB_DIR = Path.home() / ".session_orch_db"
DB_NAME = os.environ.get("ORCH_DB", "orchestrator")
# Multi-tenant: mỗi workspace là 1 thư mục riêng dưới root này; cwd của mọi session
# trong workspace bị GHIM vào <root>/<workspace_id> để cô lập file/memory/transcript.
WORKSPACES_ROOT = Path(os.environ.get("ORCH_WORKSPACES_ROOT", str(Path.home() / ".session_orch_workspaces")))
# workspace_id gán cho dữ liệu single-tenant cũ khi migrate + fallback khi request không kèm ws.
DEFAULT_WORKSPACE = "default"
DRY_RUN = os.environ.get("ORCH_DRY_RUN", "0") == "1"
POLL_INTERVAL = int(os.environ.get("ORCH_POLL_INTERVAL", "5"))
MAX_CONCURRENT = int(os.environ.get("ORCH_MAX_CONCURRENT", "3"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
ORCH_HOST = os.environ.get("ORCH_HOST", "0.0.0.0")
ORCH_PORT = int(os.environ.get("ORCH_PORT", "8992"))
# Service-to-service auth. Để TRỐNG = tắt (localhost/dev như cũ). Set = mọi /api/* yêu cầu
# header 'X-API-Key' (hoặc query ?api_key= cho SSE) khớp. Web app backend giữ key này.
ORCH_API_KEY = os.environ.get("ORCH_API_KEY", "")
# Phase D — safety caps (0 = tắt/không giới hạn)
MAX_RUNS_PER_SESSION = int(os.environ.get("ORCH_MAX_RUNS_PER_SESSION", "0"))
# Trần số run/NGÀY cho mỗi session (reset mỗi ngày). Đạt trần → signal bị blocked, chờ người
# bấm "Allow +N" trên dashboard để nới thêm hạn mức cho riêng ngày hôm nay. 0 = tắt (unlimited).
# TẠM để 0 = unlimited (bỏ cap ngày). Set ENV ORCH_MAX_RUNS_PER_DAY nếu muốn bật lại cap.
MAX_RUNS_PER_DAY = int(os.environ.get("ORCH_MAX_RUNS_PER_DAY", "0"))
# Mỗi lần bấm "Allow" thì nới thêm bao nhiêu run cho ngày hôm nay.
DAILY_ALLOW_STEP = int(os.environ.get("ORCH_DAILY_ALLOW_STEP", "10"))
SESSION_TOKEN_BUDGET = int(os.environ.get("ORCH_SESSION_TOKEN_BUDGET", "0"))
MAX_RETRIES = int(os.environ.get("ORCH_MAX_RETRIES", "0"))
RETRY_BACKOFF = float(os.environ.get("ORCH_RETRY_BACKOFF", "2"))
# Streaming — hiển thị chi tiết (thinking/tool_use/text) của headless agent theo thời gian thực.
STREAM = os.environ.get("ORCH_STREAM", "1") == "1"          # 1 = dùng --output-format stream-json
STREAM_PARTIAL = os.environ.get("ORCH_STREAM_PARTIAL", "0") == "1"  # 1 = thêm --include-partial-messages (token-level)
EVENT_TRUNC = int(os.environ.get("ORCH_EVENT_TRUNC", "2000"))  # cắt payload event để tránh phình DB/lộ dữ liệu
# Buffer đọc stdout/stderr của subprocess. asyncio mặc định 64KB → 1 dòng NDJSON lớn
# (vd tool_result đọc file dài / output Bash đồ sộ) sẽ ném "Separator is not found,
# and chunk exceed the limit". Nâng lên để chứa trọn dòng dài. (default 16MB)
STREAM_LIMIT = int(os.environ.get("ORCH_STREAM_LIMIT", str(16 * 1024 * 1024)))
# Reasoning effort mặc định cho mọi session (session có thể override). "" = không truyền (claude dùng 'high').
# Lưu ý: các mức phải khớp với `claude --effort` của CLI đang cài (hiện chỉ: low|medium|high|max).
EFFORT_LEVELS = ("low", "medium", "high", "max")
DEFAULT_EFFORT = os.environ.get("ORCH_DEFAULT_EFFORT", "high")  # high mặc định
# Permission mode mặc định khi session KHÔNG set. CLI 2.1.200 đổi default 'default'→'Manual':
# headless -p ở Manual sẽ CHẶN tool chờ user duyệt → agent kẹt (không ai ở terminal). Orchestrator
# đã có lớp approval riêng qua signal (requires_approval) nên bypass an toàn. Set '' để tắt fallback này.
DEFAULT_PERMISSION_MODE = os.environ.get("ORCH_DEFAULT_PERMISSION_MODE", "bypassPermissions")


# ─── Store (SQLite) ───────────────────────────────────────────────────────────


def _db_path() -> str:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    return str(DB_DIR / f"{DB_NAME}.db")


def _conn():
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = _conn()
    conn.executescript("""
        -- Multi-tenant: mỗi workspace là 1 không gian cô lập (1 thư mục riêng). Mọi session/
        -- signal/run đều thuộc đúng 1 workspace; role chỉ unique trong phạm vi workspace.
        CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY,               -- ws_<random>, orchestrator sinh
            name TEXT NOT NULL DEFAULT '',      -- nhãn hiển thị
            root_dir TEXT NOT NULL,             -- WORKSPACES_ROOT/<id> — cwd ghim cho mọi session
            kill_switch INTEGER NOT NULL DEFAULT 0,   -- dừng riêng workspace này
            max_runs_per_day INTEGER,           -- NULL = dùng MAX_RUNS_PER_DAY global
            status TEXT NOT NULL DEFAULT 'active',    -- active | suspended
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,              -- claude session_id
            workspace_id TEXT NOT NULL DEFAULT 'default',
            name TEXT NOT NULL,               -- role/label (unique trong workspace)
            project TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'idle',   -- idle | running | paused | stopped
            allowed_tools TEXT NOT NULL DEFAULT '[]',
            permission_mode TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',         -- '' = auto (claude tự chọn); vd 'opus'/'sonnet'/'haiku'
            effort TEXT NOT NULL DEFAULT '',         -- '' = dùng ORCH_DEFAULT_EFFORT; low|medium|high|xhigh|max
            engine TEXT NOT NULL DEFAULT 'claude',   -- engine chạy session (luôn 'claude')
            created_at TEXT NOT NULL,
            last_active TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL DEFAULT 'default',
            from_session TEXT NOT NULL DEFAULT '',
            to_session TEXT NOT NULL,          -- target session_id
            message TEXT NOT NULL,
            requires_approval INTEGER NOT NULL DEFAULT 0,
            dry_run INTEGER NOT NULL DEFAULT 0,      -- 1 = preview, không gọi claude thật
            status TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|processing|done|failed|denied|blocked
            reason TEXT NOT NULL DEFAULT '',          -- lý do khi blocked/failed/denied (hiển thị hover trên dashboard)
            created_at TEXT NOT NULL,
            delivered_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL DEFAULT 'default',
            session_id TEXT NOT NULL,
            signal_id INTEGER,
            prompt TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,              -- running | ok | error
            tokens INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL DEFAULT ''
        );
        -- Streaming transcript: mỗi bước (thinking/text/tool_use/tool_result) của 1 run là 1 dòng.
        CREATE TABLE IF NOT EXISTS run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            signal_id INTEGER,
            seq INTEGER NOT NULL,              -- thứ tự trong run
            kind TEXT NOT NULL,                -- system|thinking|text|tool_use|tool_result|result|error
            summary TEXT NOT NULL DEFAULT '',  -- dòng ngắn để hiển thị
            payload TEXT NOT NULL DEFAULT '{}',-- chi tiết (đã cắt bớt)
            ts TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, seq);
        -- Hạn mức run/ngày được người dùng nới thêm ("Allow +N") cho từng session, theo ngày.
        -- extra = tổng số run được cộng thêm cho session đó trong ngày `day` (YYYY-MM-DD).
        CREATE TABLE IF NOT EXISTS daily_allowance (
            session_id TEXT NOT NULL,
            day TEXT NOT NULL,                 -- YYYY-MM-DD (local)
            extra INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (session_id, day)
        );
    """)
    # migrate: thêm cột dry_run cho signals nếu DB cũ chưa có
    cols = [r[1] for r in conn.execute("PRAGMA table_info(signals)").fetchall()]
    if "dry_run" not in cols:
        conn.execute("ALTER TABLE signals ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0")
    if "reason" not in cols:
        conn.execute("ALTER TABLE signals ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
    if "workspace_id" not in cols:
        conn.execute(f"ALTER TABLE signals ADD COLUMN workspace_id TEXT NOT NULL DEFAULT '{DEFAULT_WORKSPACE}'")
    # migrate: thêm cột model cho sessions nếu DB cũ chưa có
    scols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
    if "model" not in scols:
        conn.execute("ALTER TABLE sessions ADD COLUMN model TEXT NOT NULL DEFAULT ''")
    if "effort" not in scols:
        conn.execute("ALTER TABLE sessions ADD COLUMN effort TEXT NOT NULL DEFAULT ''")
    if "workspace_id" not in scols:
        conn.execute(f"ALTER TABLE sessions ADD COLUMN workspace_id TEXT NOT NULL DEFAULT '{DEFAULT_WORKSPACE}'")
    if "engine" not in scols:
        conn.execute("ALTER TABLE sessions ADD COLUMN engine TEXT NOT NULL DEFAULT 'claude'")
    # migrate: thêm workspace_id cho runs nếu DB cũ chưa có
    rcols = [r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
    if "workspace_id" not in rcols:
        conn.execute(f"ALTER TABLE runs ADD COLUMN workspace_id TEXT NOT NULL DEFAULT '{DEFAULT_WORKSPACE}'")
    # Đảm bảo workspace 'default' luôn tồn tại — nơi trú của mọi dữ liệu single-tenant cũ.
    conn.execute(
        "INSERT OR IGNORE INTO workspaces (id, name, root_dir, status, created_at) VALUES (?, ?, ?, 'active', ?)",
        (DEFAULT_WORKSPACE, "Default", "", _now()),
    )
    # Index (không UNIQUE) trên name để lookup-or-create theo tên nhanh. Không ép unique vì
    # DB cũ có thể đã có tên trùng; lookup luôn lấy bản cũ nhất (ORDER BY created_at) cho ổn định.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_workspaces_name ON workspaces(name)")
    conn.commit()
    conn.close()


def _ensure_db():
    if not os.path.exists(_db_path()):
        init_db()


def _now():
    return datetime.now().isoformat()


# workspaces (multi-tenant)

def create_workspace(name="", max_runs_per_day=None):
    """Tạo 1 workspace mới: sinh id ws_<random>, mkdir thư mục riêng, insert DB.
    Trả dict workspace (kèm root_dir đã tạo). cwd của mọi session trong ws bị ghim vào đây."""
    _ensure_db()
    wid = "ws_" + secrets.token_hex(8)
    root = WORKSPACES_ROOT / wid
    root.mkdir(parents=True, exist_ok=True)
    conn = _conn()
    conn.execute(
        "INSERT INTO workspaces (id, name, root_dir, max_runs_per_day, status, created_at) "
        "VALUES (?, ?, ?, ?, 'active', ?)",
        (wid, name or wid, str(root), max_runs_per_day, _now()),
    )
    conn.commit()
    conn.close()
    return get_workspace(wid)


def get_workspace(workspace_id):
    _ensure_db()
    conn = _conn()
    row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_workspace_by_name(name):
    """Tìm workspace theo NAME (nhãn FE gán, vd email/tenant-key). Lấy bản cũ nhất nếu tình cờ
    có nhiều bản trùng tên (DB cũ) để kết quả ổn định. None nếu chưa có. Name rỗng → None."""
    if not name:
        return None
    _ensure_db()
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM workspaces WHERE name = ? ORDER BY created_at LIMIT 1", (name,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def lookup_or_create_workspace(name, max_runs_per_day=None):
    """Idempotent theo NAME: đã có tên đó thì trả workspace cũ, chưa có thì tạo mới. FE chỉ cần
    gửi tên (vd user id/email) là nhận lại 1 workspace ổn định — gọi bao nhiêu lần cũng 1 kết quả.
    Trả (workspace_dict, created_bool). Name rỗng thì bắt buộc tạo mới (không gộp các bản vô danh)."""
    existing = get_workspace_by_name(name) if name else None
    if existing:
        return existing, False
    return create_workspace(name, max_runs_per_day), True


def list_workspaces():
    _ensure_db()
    conn = _conn()
    rows = conn.execute("SELECT * FROM workspaces ORDER BY created_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_workspace_status(workspace_id, status):
    """status: active | suspended. Suspended → không spawn/không nhận signal mới (dữ liệu giữ nguyên)."""
    conn = _conn()
    conn.execute("UPDATE workspaces SET status = ? WHERE id = ?", (status, workspace_id))
    conn.commit()
    conn.close()


def workspace_root(workspace_id):
    """Thư mục ghim của 1 workspace (đảm bảo tồn tại). None nếu workspace không có / thiếu root_dir."""
    ws = get_workspace(workspace_id)
    if not ws or not ws.get("root_dir"):
        return None
    root = Path(ws["root_dir"])
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


# sessions

def register_session(session_id, name, project="", cwd="", allowed_tools=None, permission_mode="",
                     model="", effort="", workspace_id=DEFAULT_WORKSPACE, engine="claude"):
    _ensure_db()
    conn = _conn()
    conn.execute(
        "INSERT INTO sessions (id, workspace_id, name, project, cwd, status, allowed_tools, permission_mode, model, effort, engine, created_at, last_active) "
        "VALUES (?, ?, ?, ?, ?, 'idle', ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET workspace_id=excluded.workspace_id, name=excluded.name, project=excluded.project, cwd=excluded.cwd, "
        "allowed_tools=excluded.allowed_tools, permission_mode=excluded.permission_mode, model=excluded.model, "
        "effort=excluded.effort, engine=excluded.engine",
        (session_id, workspace_id, name, project, cwd, json.dumps(allowed_tools or []), permission_mode, model, effort, engine or "claude", _now(), _now()),
    )
    conn.commit()
    conn.close()
    return session_id


def get_session(session_id):
    _ensure_db()
    conn = _conn()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_sessions():
    _ensure_db()
    conn = _conn()
    rows = conn.execute("SELECT * FROM sessions ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_session_status(session_id, status):
    conn = _conn()
    conn.execute("UPDATE sessions SET status = ?, last_active = ? WHERE id = ?", (status, _now(), session_id))
    conn.commit()
    conn.close()


def set_session_model(session_id, model):
    conn = _conn()
    conn.execute("UPDATE sessions SET model = ?, last_active = ? WHERE id = ?", (model, _now(), session_id))
    conn.commit()
    conn.close()


def set_session_effort(session_id, effort):
    conn = _conn()
    conn.execute("UPDATE sessions SET effort = ?, last_active = ? WHERE id = ?", (effort, _now(), session_id))
    conn.commit()
    conn.close()


def get_session_by_name(name, workspace_id=None):
    """Tìm session theo role/name. workspace_id != None → chỉ tìm TRONG workspace đó
    (đa tenant: role chỉ unique trong 1 workspace). None → tìm toàn cục (tương thích cũ)."""
    _ensure_db()
    conn = _conn()
    if workspace_id is not None:
        row = conn.execute(
            "SELECT * FROM sessions WHERE name = ? AND workspace_id = ? ORDER BY last_active DESC LIMIT 1",
            (name, workspace_id)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM sessions WHERE name = ? ORDER BY last_active DESC LIMIT 1", (name,)).fetchone()
    conn.close()
    return dict(row) if row else None


def resolve_session_id(ref, workspace_id=None):
    """ref = session_id (exact match) HOẶC role/name → trả session_id, None nếu không thấy.

    workspace_id != None: chỉ resolve trong phạm vi workspace đó (chống signal đi nhầm tenant
    khi hai workspace trùng role). Nếu ref là session_id thì cũng phải thuộc đúng workspace."""
    if not ref:
        return None
    s = get_session(ref)
    if s:
        if workspace_id is not None and s.get("workspace_id") != workspace_id:
            return None
        return ref
    s = get_session_by_name(ref, workspace_id)
    return s["id"] if s else None


# signals

def _coerce_message(message):
    """Chuẩn hoá message về TEXT để lưu cột signals.message (SQLite không bind dict/list).
    FE thường gửi message JSON có cấu trúc ({goal,inputs,...} id=8, hay {kind:'approval_result',...})
    → serialize thành JSON string (agent tự parse ngữ cảnh — đúng 'message JSON tự do' id=10).
    String đi qua nguyên vẹn; None → ''; số/bool → str."""
    if message is None:
        return ""
    if isinstance(message, str):
        return message
    if isinstance(message, (dict, list)):
        return json.dumps(message, ensure_ascii=False)
    return str(message)


def _extract_ticket(message):
    """Bóc field 'ticket' từ message enqueue (id=14/Q1) để đóng đúng signal ask_user_choice đang
    chờ. message có thể là dict (FE gửi JSON) hoặc JSON string. Trả '' nếu không có ticket / không
    phải object. Không ném lỗi — message tự do, hỏng thì coi như không có ticket."""
    obj = message
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except (json.JSONDecodeError, ValueError):
            return ""
    if isinstance(obj, dict):
        t = obj.get("ticket")
        return t if isinstance(t, str) else ""
    return ""


def enqueue_signal(to_session, message, from_session="", requires_approval=0, dry_run=0,
                   workspace_id=DEFAULT_WORKSPACE):
    _ensure_db()
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO signals (workspace_id, from_session, to_session, message, requires_approval, dry_run, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
        (workspace_id, from_session, to_session, _coerce_message(message),
         int(requires_approval), int(dry_run), _now()),
    )
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    return sid


def eligible_signals():
    """Signal sẵn sàng inject: pending & không cần approval, HOẶC đã được approved."""
    _ensure_db()
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM signals WHERE (status = 'pending' AND requires_approval = 0) "
        "OR status = 'approved' ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_signal_status(signal_id, status, reason=""):
    """Cập nhật status của signal. reason: lý do khi blocked/failed/denied (để hover trên
    dashboard); truyền chuỗi rỗng ở các status khác để xóa reason cũ (vd khi re-run)."""
    conn = _conn()
    delivered = _now() if status in ("done", "failed") else ""
    conn.execute("UPDATE signals SET status = ?, reason = ?, delivered_at = ? WHERE id = ?",
                 (status, reason, delivered, signal_id))
    conn.commit()
    conn.close()


def close_ask_user_choice_by_ticket(ticket, workspace_id=None):
    """Đóng signal auto-signal ask_user_choice (spec id=14/Q1) khi user đã trả lời: tìm signal
    PENDING có message JSON {tool:'ask_user_choice', ticket:<khớp>} → set thẳng 'done'.

    QUAN TRỌNG (an toàn): set 'done' CHỨ KHÔNG 'approved' — 'approved' sẽ bị eligible_signals()
    nhặt lại và inject message (chính câu hỏi) trở lại agent. 'done' là trạng thái kết thúc, poller
    bỏ qua. Và CHỈ khớp signal có tool=='ask_user_choice' (auto-signal UI) → KHÔNG bao giờ đụng
    signal điều khiển khác. Lọc theo workspace nếu có
    (chống đóng nhầm tenant). Trả list signal id đã đóng (thường 0 hoặc 1)."""
    if not ticket:
        return []
    _ensure_db()
    conn = _conn()
    where = "status = 'pending' AND requires_approval = 1"
    params = []
    if workspace_id is not None:
        where += " AND workspace_id = ?"
        params.append(workspace_id)
    rows = conn.execute(f"SELECT id, message FROM signals WHERE {where} ORDER BY id", params).fetchall()
    conn.close()
    closed = []
    for r in rows:
        try:
            msg = json.loads(r["message"] or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("tool") == "ask_user_choice" and msg.get("ticket") == ticket:
            set_signal_status(r["id"], "done")
            closed.append(r["id"])
    return closed


def list_signals(limit=50, offset=0, workspace_id=None):
    """Signal mới nhất trước (id DESC). offset để phân trang; workspace_id != None để lọc
    theo tenant. Lấy limit+1 để biết còn record cũ hơn không (has_more) mà không cần COUNT."""
    _ensure_db()
    conn = _conn()
    where = "WHERE workspace_id = ? " if workspace_id is not None else ""
    params = ([workspace_id] if workspace_id is not None else []) + [limit + 1, offset]
    rows = conn.execute(
        f"SELECT * FROM signals {where}ORDER BY id DESC LIMIT ? OFFSET ?", params).fetchall()
    conn.close()
    items = [dict(r) for r in rows]
    has_more = len(items) > limit
    return items[:limit], has_more


def delete_signal(signal_id):
    """Xóa 1 signal + toàn bộ audit log liên quan (runs + run_events của nó).
    Trả dict đếm số bản ghi đã xóa. Thứ tự: run_events → runs → signal."""
    _ensure_db()
    conn = _conn()
    run_ids = [r[0] for r in conn.execute("SELECT id FROM runs WHERE signal_id = ?", (signal_id,)).fetchall()]
    n_events = 0
    if run_ids:
        q = ",".join("?" * len(run_ids))
        n_events = conn.execute(f"DELETE FROM run_events WHERE run_id IN ({q})", run_ids).rowcount
    n_runs = conn.execute("DELETE FROM runs WHERE signal_id = ?", (signal_id,)).rowcount
    n_sig = conn.execute("DELETE FROM signals WHERE id = ?", (signal_id,)).rowcount
    conn.commit()
    conn.close()
    return {"signals": n_sig, "runs": n_runs, "run_events": n_events}


# runs (audit)

def record_run(session_id, signal_id, prompt, result_json, status, tokens, started_at, ended_at,
               workspace_id=DEFAULT_WORKSPACE):
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO runs (workspace_id, session_id, signal_id, prompt, result_json, status, tokens, started_at, ended_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (workspace_id, session_id, signal_id, prompt, json.dumps(result_json, ensure_ascii=False), status, tokens, started_at, ended_at),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def start_run(session_id, signal_id, prompt, started_at, workspace_id=DEFAULT_WORKSPACE):
    """Mở 1 run ở trạng thái 'running' TRƯỚC khi chạy — để stream event vào ngay lúc chạy."""
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO runs (workspace_id, session_id, signal_id, prompt, result_json, status, tokens, started_at) "
        "VALUES (?, ?, ?, ?, '{}', 'running', 0, ?)",
        (workspace_id, session_id, signal_id, prompt, started_at),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def finish_run(run_id, result_json, status, tokens, ended_at):
    """Chốt 1 run đã mở bằng start_run."""
    conn = _conn()
    conn.execute(
        "UPDATE runs SET result_json = ?, status = ?, tokens = ?, ended_at = ? WHERE id = ?",
        (json.dumps(result_json, ensure_ascii=False), status, tokens, ended_at, run_id),
    )
    conn.commit()
    conn.close()


def record_run_event(run_id, session_id, signal_id, seq, kind, summary, payload):
    """Ghi 1 bước transcript của run (thinking/text/tool_use/tool_result/...)."""
    conn = _conn()
    conn.execute(
        "INSERT INTO run_events (run_id, session_id, signal_id, seq, kind, summary, payload, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, session_id, signal_id, seq, kind, summary, json.dumps(payload, ensure_ascii=False), _now()),
    )
    conn.commit()
    conn.close()


def list_run_events(run_id):
    _ensure_db()
    conn = _conn()
    rows = conn.execute("SELECT * FROM run_events WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_runs(limit=50, offset=0, workspace_id=None):
    """Run mới nhất trước (id DESC). offset để phân trang; workspace_id != None để lọc theo
    tenant. Lấy limit+1 để biết còn record cũ hơn không (has_more)."""
    _ensure_db()
    conn = _conn()
    where = "WHERE workspace_id = ? " if workspace_id is not None else ""
    params = ([workspace_id] if workspace_id is not None else []) + [limit + 1, offset]
    rows = conn.execute(
        f"SELECT * FROM runs {where}ORDER BY id DESC LIMIT ? OFFSET ?", params).fetchall()
    conn.close()
    items = [dict(r) for r in rows]
    has_more = len(items) > limit
    return items[:limit], has_more


def session_stats(session_id):
    """Số run + tổng token đã dùng của 1 session (để check cap/budget)."""
    _ensure_db()
    conn = _conn()
    row = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(tokens),0) t FROM runs WHERE session_id = ?", (session_id,)
    ).fetchone()
    conn.close()
    return {"runs": row["c"], "tokens": row["t"]}


def _today():
    """Ngày local dạng YYYY-MM-DD — khớp prefix của started_at (ISO local)."""
    return datetime.now().date().isoformat()


def runs_today(session_id, day=None):
    """Số run của session trong 1 ngày (mặc định hôm nay). Đếm theo prefix started_at."""
    _ensure_db()
    day = day or _today()
    conn = _conn()
    row = conn.execute(
        "SELECT COUNT(*) c FROM runs WHERE session_id = ? AND started_at LIKE ?",
        (session_id, day + "%"),
    ).fetchone()
    conn.close()
    return row["c"]


def daily_extra(session_id, day=None):
    """Số run được người dùng nới thêm ('Allow +N') cho session trong ngày. 0 nếu chưa nới."""
    _ensure_db()
    day = day or _today()
    conn = _conn()
    row = conn.execute(
        "SELECT extra FROM daily_allowance WHERE session_id = ? AND day = ?", (session_id, day)
    ).fetchone()
    conn.close()
    return row["extra"] if row else 0


def _daily_base_for_session(session_id):
    """Cap run/ngày BASE áp cho 1 session = override của workspace nó thuộc (nếu có),
    ngược lại dùng MAX_RUNS_PER_DAY global. 0 = tắt cap ngày."""
    s = get_session(session_id)
    if s:
        ws = get_workspace(s.get("workspace_id") or DEFAULT_WORKSPACE)
        if ws and ws.get("max_runs_per_day") is not None:
            return int(ws["max_runs_per_day"])
    return MAX_RUNS_PER_DAY


def grant_daily_allowance(session_id, step=None, day=None):
    """Nới thêm `step` run cho session trong ngày hôm nay. Trả về hạn mức mới (base + extra)."""
    _ensure_db()
    step = DAILY_ALLOW_STEP if step is None else step
    day = day or _today()
    conn = _conn()
    conn.execute(
        "INSERT INTO daily_allowance (session_id, day, extra) VALUES (?, ?, ?) "
        "ON CONFLICT(session_id, day) DO UPDATE SET extra = extra + excluded.extra",
        (session_id, day, step),
    )
    conn.commit()
    row = conn.execute(
        "SELECT extra FROM daily_allowance WHERE session_id = ? AND day = ?", (session_id, day)
    ).fetchone()
    conn.close()
    return _daily_base_for_session(session_id) + (row["extra"] if row else 0)


def daily_stats(session_id, day=None):
    """Trạng thái cap-theo-ngày của 1 session: đã dùng / hạn mức / còn lại / có bị chặn không.
    Base cap lấy theo workspace của session (override) rồi mới cộng phần 'Allow +N'."""
    day = day or _today()
    base = _daily_base_for_session(session_id)
    used = runs_today(session_id, day)
    limit = base + daily_extra(session_id, day) if base else 0
    return {
        "used_today": used,
        "daily_limit": limit,                       # 0 = tắt cap ngày
        "daily_remaining": max(0, limit - used) if limit else None,
        "daily_blocked": bool(limit) and used >= limit,
    }


def cap_exceeded(session_id):
    """Trả (True, reason) nếu session vượt cap run (trọn đời), cap run/ngày, hoặc budget token.
    Cap run/ngày dùng base theo workspace của session (override được)."""
    st = session_stats(session_id)
    if MAX_RUNS_PER_SESSION and st["runs"] >= MAX_RUNS_PER_SESSION:
        return True, f"đạt trần {MAX_RUNS_PER_SESSION} runs"
    base = _daily_base_for_session(session_id)
    if base:
        used = runs_today(session_id)
        limit = base + daily_extra(session_id)
        if used >= limit:
            return True, f"đạt trần {limit} runs hôm nay (bấm Allow +{DAILY_ALLOW_STEP} để chạy tiếp)"
    if SESSION_TOKEN_BUDGET and st["tokens"] >= SESSION_TOKEN_BUDGET:
        return True, f"đạt budget {SESSION_TOKEN_BUDGET} tokens"
    return False, ""


# ─── Executor ─────────────────────────────────────────────────────────────────


def _trunc(s, n=None):
    n = EVENT_TRUNC if n is None else n
    s = str(s or "")
    return s if len(s) <= n else s[:n] + f"… (+{len(s) - n} ký tự)"


# [[RESULT]] marker ĐÃ GỠ (spec id=14/G1+G2): FE chốt pure signal-driven, không parse text agent
# để dựng UI. Kết quả/nháp đi qua tool push_draft_to_ui; tiến độ qua notify_progress; duyệt qua
# signal. Không còn regex bóc marker → event 'result'.


def _stringify_tool_result(content):
    """tool_result.content có thể là str hoặc list block {type:text,text}."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text") or b.get("content") or json.dumps(b, ensure_ascii=False))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False) if content is not None else ""


def _content_blocks(ev):
    """Lấy list content block (dict) từ 1 event, chịu lỗi mọi biến thể:
    message có thể thiếu / là str; content có thể là str (→ 1 block text) / list lẫn non-dict."""
    msg = ev.get("message")
    if isinstance(msg, str):
        return [{"type": "text", "text": msg}]
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _iter_display_events(ev):
    """Chuyển 1 event NDJSON của claude thành list (kind, summary, payload) để hiển thị.

    1 message assistant có thể có nhiều content block → tách thành nhiều event con
    (thinking / text / tool_use) cho timeline mượt. Chịu lỗi mọi biến thể payload.
    """
    if not isinstance(ev, dict):
        return [("text", _trunc(str(ev), 500), {"raw": _trunc(str(ev))})]
    t = ev.get("type")
    out = []
    if t == "system":
        sub = ev.get("subtype") or "system"
        tools = ev.get("tools") or []
        if sub == "init":
            model = ev.get("model") or "?"
            out.append(("system", f"session bắt đầu · model={model} · {len(tools)} tools",
                        {"subtype": sub, "tools": tools[:60]}))
        elif sub.startswith("hook_") or sub == "thinking_tokens":
            pass  # hook_* / thinking_tokens: nhiễu tiến trình bắn liên tục — bỏ khỏi audit log.
        else:
            # subtype khác (compact_boundary...) giữ lại — cần biết agent compact lúc nào.
            out.append(("system", f"system · {sub}", {"subtype": sub, "raw": _trunc(json.dumps(ev, ensure_ascii=False))}))
    elif t == "assistant":
        for b in _content_blocks(ev):
            bt = b.get("type")
            if bt == "text":
                tx = (b.get("text") or "").strip()
                if tx:
                    # summary cắt (audit); payload['text']=FULL → publish gắn 'result' full (id=70).
                    out.append(("text", _trunc(tx, 500), {"text": tx}))
            elif bt == "thinking":
                th = (b.get("thinking") or "").strip()
                if th:
                    out.append(("thinking", _trunc(th, 500), {"thinking": _trunc(th)}))
            elif bt == "tool_use":
                inp = _trunc(json.dumps(b.get("input", {}), ensure_ascii=False), 300)
                out.append(("tool_use", f"{b.get('name', '?')}({inp})",
                            {"name": b.get("name"), "input": b.get("input")}))
    elif t == "user":
        for b in _content_blocks(ev):
            if b.get("type") == "tool_result":
                txt = _stringify_tool_result(b.get("content"))
                is_err = bool(b.get("is_error"))
                out.append(("tool_result", ("⚠ " if is_err else "") + _trunc(txt, 400),
                            {"result": _trunc(txt), "is_error": is_err}))
    elif t == "result":
        usage = ev.get("usage") or {}
        out.append(("result", f"xong · {ev.get('subtype', '')} · {ev.get('num_turns', '?')} turns",
                    {"cost_usd": ev.get("total_cost_usd"), "duration_ms": ev.get("duration_ms"),
                     "output_tokens": usage.get("output_tokens")}))
    return out


async def _run_claude(session, prompt, on_event=None, dry_run=False):
    """Chạy `claude -p --resume <id>` với allowlist. Trả dict kết quả.

    on_event(kind, summary, payload): async callback được gọi cho mỗi bước khi STREAM=1
    (thinking/text/tool_use/tool_result/...). Dùng để ghi run_events + đẩy SSE live.
    Dry-run (ORCH_DRY_RUN=1 hoặc dry_run per-signal): trả stub, không gọi claude.
    """
    session_id = session["id"]
    if DRY_RUN or dry_run:
        if on_event:
            await on_event("text", f"[dry-run] would inject: {_trunc(prompt, 300)}", {"dry_run": True})
        return {
            "ok": True,
            "result": f"[dry-run] would inject to {session['name']}: {prompt}",
            "session_id": session_id,
            "tokens": 0,
            "raw": {"dry_run": True},
        }

    allowed = json.loads(session.get("allowed_tools") or "[]")
    stream = STREAM and on_event is not None
    fmt = "stream-json" if stream else "json"
    # Prompt truyền qua STDIN (không phải argv) để tránh lỗi khi prompt bắt đầu bằng
    # dấu '-' (vd YAML frontmatter '---') hoặc chứa ký tự đặc biệt/multiline.
    cmd = [CLAUDE_BIN, "-p", "--resume", session_id, "--output-format", fmt]
    if stream:
        cmd.append("--verbose")  # bắt buộc cho stream-json trong -p
        if STREAM_PARTIAL:
            cmd.append("--include-partial-messages")
    if allowed:
        cmd += ["--allowedTools", " ".join(allowed)]
    perm_mode = session.get("permission_mode") or DEFAULT_PERMISSION_MODE
    if perm_mode:
        cmd += ["--permission-mode", perm_mode]
    if session.get("model"):
        cmd += ["--model", session["model"]]
    effort = session.get("effort") or DEFAULT_EFFORT
    # Chỉ truyền --effort nếu CLI chấp nhận; effort lạ (vd 'xhigh' từ DB cũ) sẽ làm claude
    # thoát ngay với returncode 1 → bỏ qua để không kéo cả run xuống fail.
    if effort in EFFORT_LEVELS:
        cmd += ["--effort", effort]

    cwd = session.get("cwd") or None
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=STREAM_LIMIT,  # tránh LimitOverrunError khi 1 dòng NDJSON > 64KB
    )

    if not stream:
        stdout, stderr = await proc.communicate(input=prompt.encode("utf-8"))
        return _parse_final(proc.returncode, stdout, stderr, session_id)

    # Streaming: gửi prompt qua stdin rồi đọc stdout theo từng dòng NDJSON.
    proc.stdin.write(prompt.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()

    stderr_chunks: list[bytes] = []

    async def _drain_stderr():
        # readline() thủ công + nuốt LimitOverrunError để 1 dòng stderr quá khổ
        # không giết task drain (bỏ phần thừa của dòng đó, đọc tiếp).
        while True:
            try:
                raw = await proc.stderr.readline()
            except (asyncio.LimitOverrunError, ValueError):
                continue
            if not raw:
                break
            stderr_chunks.append(raw)

    stderr_task = asyncio.create_task(_drain_stderr())
    final = None
    oversized = False  # đang ở giữa 1 dòng vượt STREAM_LIMIT → chỉ cảnh báo 1 lần
    try:
        while True:
            try:
                raw = await proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as e:
                # 1 dòng NDJSON vượt cả STREAM_LIMIT (rất hiếm): asyncio cắt dòng thành
                # nhiều mảnh ≤ limit — các mảnh này json.loads fail sẽ bị bỏ ở dưới.
                # Không để LimitOverrunError giết cả run; chỉ cảnh báo 1 lần mỗi dòng.
                if not oversized:
                    oversized = True
                    try:
                        await on_event("error", f"dòng output quá lớn (> {STREAM_LIMIT // (1024*1024)}MB), bỏ qua",
                                       {"error": str(e)})
                    except Exception:  # noqa: BLE001
                        pass
                continue
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            oversized = False  # đọc trọn 1 dòng (kết bằng \n) → reset cờ
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict) and ev.get("type") == "result":
                final = ev
            # Một event lỗi (parse/hiển thị) KHÔNG được giết cả run — nuốt lỗi, đi tiếp.
            try:
                display = _iter_display_events(ev)
            except Exception as e:  # noqa: BLE001
                display = [("error", f"parse event lỗi: {e}", {"line": _trunc(line, 500)})]
            for kind, summary, payload in display:
                try:
                    await on_event(kind, summary, payload)
                except Exception:  # noqa: BLE001 — không để lỗi UI làm hỏng run
                    pass
    finally:
        await proc.wait()
        await stderr_task

    stderr_txt = b"".join(stderr_chunks).decode("utf-8", "replace")
    if proc.returncode != 0 and final is None:
        # claude chết trước khi phát event nào (vd sai --effort, --model). stderr là lý do
        # thật — ghi vào run_events + raw để hiện trên UI/DB, không nuốt mất như trước.
        err = stderr_txt.strip()[:2000] or f"claude exited với mã {proc.returncode}"
        if on_event:
            await on_event("error", _trunc(err, 500), {"stderr": err, "returncode": proc.returncode})
        return {"ok": False, "result": err, "session_id": session_id, "tokens": 0,
                "raw": {"returncode": proc.returncode, "stderr": err}}
    if final is None:
        err = stderr_txt.strip()[:2000]
        if on_event:
            await on_event("error", "không nhận được event 'result' từ claude", {"stderr": err})
        return {"ok": False, "result": "không nhận được event 'result' từ claude.",
                "session_id": session_id, "tokens": 0, "raw": {"stderr": err}}
    usage = final.get("usage") or {}
    return {
        "ok": final.get("is_error", False) is False,
        "result": final.get("result", ""),
        "session_id": final.get("session_id", session_id),
        "tokens": int(usage.get("output_tokens", 0) or 0),
        "raw": final,
    }


def _parse_final(returncode, stdout, stderr, session_id):
    """Parse kết quả cho chế độ --output-format json (không stream)."""
    if returncode != 0:
        return {"ok": False, "result": (stderr or b"").decode("utf-8", "replace")[:2000],
                "session_id": session_id, "tokens": 0, "raw": {"returncode": returncode}}
    try:
        data = json.loads((stdout or b"").decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {"ok": False, "result": "Không parse được JSON output từ claude.",
                "session_id": session_id, "tokens": 0,
                "raw": {"stdout": (stdout or b"").decode("utf-8", "replace")[:2000]}}
    usage = data.get("usage") or {}
    return {
        "ok": data.get("is_error", False) is False,
        "result": data.get("result", ""),
        "session_id": data.get("session_id", session_id),
        "tokens": int(usage.get("output_tokens", 0) or 0),
        "raw": data,
    }


def _skills_dir(cwd):
    """Thư mục skills của project = <cwd>/.claude/skills. cwd rỗng → fallback cạnh file .py."""
    base = Path(cwd) if cwd else Path(__file__).parent
    return base / ".claude" / "skills"


def _skill_path(cwd, name):
    return _skills_dir(cwd) / name / "SKILL.md"


def _role_skill(cwd, name):
    """Đọc SKILL của role theo convention: <cwd>/.claude/skills/<name>/SKILL.md.
    Trả '' nếu không có file (role không cần playbook riêng)."""
    try:
        return _skill_path(cwd, name).read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_role_skill(cwd, name, content):
    """Ghi init_prompt thành SKILL của role (vật thể hoá → đọc lại mỗi signal, không trôi).
    Ghi đè nếu đã có. content rỗng → bỏ qua (không tạo SKILL rỗng)."""
    if not content.strip():
        return
    p = _skill_path(cwd, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _prepend_role(cwd, name, message):
    """Ghim role + playbook vào MỖI signal inject → role không trôi khi history dài/compact.
    Lazy-load: chỉ SKILL của role này, không nhồi mọi skill. Không có SKILL → chỉ prepend tên role."""
    skill = _role_skill(cwd, name)
    if skill:
        return f"[Role: {name}]\n{skill}\n\n---\n\n{message}"
    return f"[Role: {name}]\n{message}"


# ─── Skill templates (liệt kê vai/role cho dropdown spawn) ────────────────────

TEMPLATES_DIR = Path(__file__).parent / ".claude" / "skills"


def _list_skill_templates():
    """Template hợp lệ = SKILL.md có placeholder <X>. Trả [{name, description}].
    Stub rỗng (0 placeholder) bị bỏ — không phải template điền được."""
    out = []
    for d in sorted(TEMPLATES_DIR.glob("*/")):
        if d.name.rstrip("/") == "game-director":
            continue  # vai orch-only: seed tự động khi đăng ký orchestrator (api_register), không spawn như worker
        f = d / "SKILL.md"
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if not re.search(r"<[A-Z_]+>", text):
            continue
        m = re.search(r"description:\s*>?\s*\n?\s*(.+)", text)
        out.append({"name": d.name.rstrip("/"), "description": (m.group(1).strip()[:200] if m else "")})
    return out


def _build_init_prompt(name, init_prompt, workspace_id):
    """Dựng init/system prompt cho session mới.

    Nếu FE gửi init_prompt → dùng nguyên (đường chính: FE sở hữu toàn bộ nghiệp vụ). Nếu KHÔNG có
    init_prompt → seed generic 'ready' + nhắc workspace cho tenant (để signal đi đúng workspace)."""
    if init_prompt:
        return init_prompt
    prompt = (f"Bạn là agent '{name}' trong hệ thống multi-agent được điều phối. "
              f"Trả lời ngắn gọn 'ready'.")
    if bool(workspace_id) and workspace_id != DEFAULT_WORKSPACE:
        prompt += (f"\n\nBạn thuộc workspace '{workspace_id}'. Khi gọi tool signal "
                   f"(send_signal/compact_context), luôn truyền workspace_id='{workspace_id}'.")
    return prompt


async def spawn_session(name, project="", cwd="", allowed_tools=None, permission_mode="", init_prompt="",
                        model="", effort="", workspace_id=DEFAULT_WORKSPACE, engine="claude"):
    """Tạo một headless session mới bằng `claude -p`, lấy session_id, rồi register.

    model: '' = auto (claude tự chọn); hoặc alias 'opus'/'sonnet'/'haiku' / model id cụ thể.
    effort: '' = dùng ORCH_DEFAULT_EFFORT (high); hoặc low|medium|high|max.
    workspace_id: session thuộc workspace nào — cwd bị GHIM vào thư mục workspace đó (đa
        tenant). Chỉ workspace 'default' (single-tenant cũ) mới dùng cwd tự do truyền vào.
    Dry-run: tạo session_id giả để test UI mà không gọi claude.
    """
    # Cô lập file: mọi session trong 1 workspace (≠ default) chạy trong thư mục ghim của
    # workspace — KHÔNG nhận cwd tùy ý từ ngoài (chống trỏ ra ngoài đọc/sửa file tenant khác).
    is_tenant = bool(workspace_id) and workspace_id != DEFAULT_WORKSPACE
    if is_tenant:
        root = workspace_root(workspace_id)
        if not root:
            return {"error": f"workspace '{workspace_id}' không tồn tại"}
        cwd = root
    # Vật thể hoá init_prompt thành SKILL của role (<cwd>/.claude/skills/<name>/SKILL.md) → mỗi
    # signal sau prepend lại từ file này, role không trôi. Rỗng → bỏ qua. Ghi TRƯỚC khi seed generic.
    _write_role_skill(cwd, name, init_prompt)
    # Seed init/system prompt: init_prompt của FE nếu có, else generic 'ready'.
    init_prompt = _build_init_prompt(name, init_prompt, workspace_id)

    if DRY_RUN:
        sid = f"dry-{name}-{datetime.now().strftime('%H%M%S%f')}"
    else:
        # init_prompt qua STDIN (tránh lỗi khi prompt bắt đầu bằng '-', vd '---' frontmatter).
        cmd = [CLAUDE_BIN, "-p", "--output-format", "json"]
        if model:
            cmd += ["--model", model]
        perm_mode = permission_mode or DEFAULT_PERMISSION_MODE
        if perm_mode:
            cmd += ["--permission-mode", perm_mode]  # CLI 2.1.200: Manual mặc định chặn tool headless
        eff = effort or DEFAULT_EFFORT
        if eff in EFFORT_LEVELS:
            cmd += ["--effort", eff]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd or None, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate(input=init_prompt.encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            return {"error": f"không chạy được claude: {e}"}
        if proc.returncode != 0:
            return {"error": (stderr or b"").decode("utf-8", "replace")[:500]}
        try:
            data = json.loads((stdout or b"").decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return {"error": "không parse được output từ claude"}
        sid = data.get("session_id")
        if not sid:
            return {"error": "claude không trả session_id"}
    register_session(sid, name, project, cwd, allowed_tools or [], permission_mode, model, effort, workspace_id, engine or "claude")
    return get_session(sid)


def unregister_session(session_id):
    """Gỡ session khỏi orchestrator (giữ lại runs cho audit)."""
    conn = _conn()
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()


# ─── Compact context (đọc từ transcript ~/.claude/projects) ───────────────────

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _find_transcript(session_id):
    """Tìm file transcript <session_id>.jsonl trong mọi project dir của claude.
    Đoán theo cwd không đáng tin (claude đổi cả '/', '.', '_') → quét trực tiếp."""
    if not session_id or not CLAUDE_PROJECTS_DIR.exists():
        return None
    for proj in CLAUDE_PROJECTS_DIR.iterdir():
        f = proj / f"{session_id}.jsonl"
        if f.exists():
            return f
    return None


def _transcript_title(f):
    """Tiêu đề ngắn cho 1 transcript: dòng 'summary' đầu tiên, không có thì text user đầu tiên.
    Chỉ đọc tối đa 40 dòng đầu — đủ cho cả transcript đã compact nhiều lần."""
    try:
        with f.open(encoding="utf-8") as fh:
            for _ in range(40):
                line = fh.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = obj.get("type")
                if t == "summary" and obj.get("summary"):
                    return _trunc(str(obj["summary"]).replace("\n", " "), 80)
                if t == "user":
                    c = (obj.get("message") or {}).get("content")
                    if isinstance(c, list):
                        c = next((b.get("text") for b in c
                                  if isinstance(b, dict) and b.get("type") == "text"), "")
                    # bỏ message máy (<system-reminder>, <ide_opened_file>…) — không phải lời user
                    if isinstance(c, str) and c.strip() and not c.strip().startswith("<"):
                        return _trunc(c.strip().replace("\n", " "), 80)
    except OSError:
        pass
    return ""


def list_claude_sessions(cwd, limit=30):
    """Session Claude Code CLI của 1 project cwd (file ~/.claude/projects/<cwd-mã-hoá>/*.jsonl) —
    KHÁC bảng sessions DB (DB = agent do orchestrator spawn). UI dùng để chọn 1 session làm
    orchestrator chính của project. Trả [{id, title, mtime}] mới nhất trước.
    ponytail: mã hoá thư mục theo quy ước claude (ký tự ngoài [A-Za-z0-9] → '-'); 2 cwd khác nhau
    munge trùng thì lẫn session — hiếm, verify bằng field cwd trong jsonl khi thành vấn đề thật."""
    enc = re.sub(r"[^A-Za-z0-9]", "-", cwd or "")
    d = CLAUDE_PROJECTS_DIR / enc
    if not (enc and d.is_dir()):
        return []
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return [{"id": f.stem, "title": _transcript_title(f),
             "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")}
            for f in files]


def _extract_compact(session_id):
    """Trích compact context MỚI NHẤT từ transcript của 1 session.

    Trả dict: {found, boundary(meta), summary(text), transcript, mtime} hoặc {found: False}.
    Tối ưu: pre-filter chuỗi trước khi json.loads (transcript có thể >100MB nhưng scan <0.1s).
    Compact gồm 2 event liền nhau: system/compact_boundary (meta) + user/isCompactSummary (text).
    """
    f = _find_transcript(session_id)
    if not f:
        return {"found": False, "reason": "không tìm thấy transcript"}
    last_boundary = None
    last_summary_text = ""
    n_boundary = 0
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "compact_boundary" in line:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("subtype") == "compact_boundary":
                        last_boundary = d
                        n_boundary += 1
                elif "isCompactSummary" in line:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("isCompactSummary"):
                        msg = d.get("message") or {}
                        content = msg.get("content") if isinstance(msg, dict) else None
                        # content có thể là str hoặc list block {type:text,text}
                        if isinstance(content, str):
                            last_summary_text = content
                        elif isinstance(content, list):
                            last_summary_text = "\n".join(
                                b.get("text", "") for b in content if isinstance(b, dict))
    except OSError as e:
        return {"found": False, "reason": f"lỗi đọc transcript: {e}"}

    if not last_boundary and not last_summary_text:
        return {"found": False, "reason": "session chưa từng compact", "transcript": str(f),
                "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat()}

    meta = (last_boundary or {}).get("compactMetadata") or {}
    return {
        "found": True,
        "transcript": str(f),
        "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
        "compact_count": n_boundary,
        "boundary": {
            "trigger": meta.get("trigger"),            # manual | auto
            "pre_tokens": meta.get("preTokens"),
            "timestamp": (last_boundary or {}).get("timestamp"),
        },
        "summary": last_summary_text,
    }


# ─── AgentEngine: lớp trừu tượng chạy session ─────────────────────────────────
# Mọi coupling với backend chạy agent gom vào 3 method. ClaudeEngine ỦY QUYỀN xuống
# 3 hàm sẵn có (_run_claude/spawn_session/_extract_compact).


class AgentEngine:
    """Interface 1 engine chạy session. Contract (mọi engine phải khớp y hệt để
    process_signal/api_* gọi được không cần biết engine nào):

      spawn(...)        -> dict session (get_session) HOẶC {"error": str}
      run(session, prompt, on_event, dry_run) -> {ok, result, session_id, tokens, raw}
      get_compact(session_id) -> {found, summary, boundary, ...}  (đồng bộ)

    on_event(kind, summary, payload): async callback đẩy từng bước ra run_events + SSE.
    kind ∈ system|thinking|text|tool_use|tool_result|result|error.
    """

    name = "base"

    async def spawn(self, name, project="", cwd="", allowed_tools=None, permission_mode="",
                    init_prompt="", model="", effort="", workspace_id=DEFAULT_WORKSPACE):
        raise NotImplementedError

    async def run(self, session, prompt, on_event=None, dry_run=False):
        raise NotImplementedError

    def get_compact(self, session_id):
        raise NotImplementedError


class ClaudeEngine(AgentEngine):
    """Engine mặc định: chạy qua `claude` CLI. Chỉ ủy quyền xuống 3 hàm hiện hữu —
    không sao chép logic, không đổi hành vi. session['engine'] rỗng/không có ⇒ engine này."""

    name = "claude"

    async def spawn(self, name, project="", cwd="", allowed_tools=None, permission_mode="",
                    init_prompt="", model="", effort="", workspace_id=DEFAULT_WORKSPACE):
        return await spawn_session(name, project=project, cwd=cwd, allowed_tools=allowed_tools,
                                   permission_mode=permission_mode, init_prompt=init_prompt,
                                   model=model, effort=effort, workspace_id=workspace_id,
                                   engine=self.name)

    async def run(self, session, prompt, on_event=None, dry_run=False):
        return await _run_claude(session, prompt, on_event=on_event, dry_run=dry_run)

    def get_compact(self, session_id):
        return _extract_compact(session_id)


# Chỉ còn 1 engine: Claude CLI. Giữ engine_for() (cùng signature) để mọi caller
# (process_signal, api_spawn, api_get_compact) chạy nguyên — luôn trả ClaudeEngine.
_CLAUDE_ENGINE = ClaudeEngine()


def engine_for(session_or_name):
    """Trả instance engine để CHẠY 1 session (dict) hoặc theo tên (str). Chỉ còn Claude."""
    return _CLAUDE_ENGINE


# ─── Tool discovery (built-in + MCP servers của project) ──────────────────────

BUILTIN_TOOLS = ["Task", "Bash", "Glob", "Grep", "LS", "Read", "Edit", "MultiEdit",
                 "Write", "NotebookEdit", "WebFetch", "WebSearch", "TodoWrite"]


def _read_mcp_servers(cwd):
    """Đọc MCP servers cấu hình cho project: user scope (~/.claude.json mcpServers),
    local scope (projects[cwd].mcpServers), project scope (<cwd>/.mcp.json)."""
    servers = {}
    try:
        data = json.loads((Path.home() / ".claude.json").read_text())
        servers.update(data.get("mcpServers") or {})
        if cwd:
            proj = (data.get("projects") or {}).get(cwd, {})
            servers.update(proj.get("mcpServers") or {})
    except Exception:  # noqa: BLE001
        pass
    if cwd:
        try:
            data = json.loads((Path(cwd) / ".mcp.json").read_text())
            servers.update(data.get("mcpServers") or {})
        except Exception:  # noqa: BLE001
            pass
    return servers


async def _list_http_mcp_tools(url, timeout=6):
    """MCP handshake qua streamable-http → trả danh sách tên tool."""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    def parse(text):
        for line in text.strip().split("\n"):
            if line.startswith("data: "):
                return json.loads(line[6:])
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                               "clientInfo": {"name": "orchestrator", "version": "1"}}}, headers=headers)
        sid = r.headers.get("mcp-session-id")
        h2 = {**headers, **({"mcp-session-id": sid} if sid else {})}
        await c.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h2)
        r = await c.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=h2)
        data = parse(r.text) or {}
        return [t["name"] for t in data.get("result", {}).get("tools", [])]


async def discover_tools(cwd):
    """Trả tools khả dụng cho project: built-in + tools từ mỗi MCP server (đặt tên
    mcp__<server>__<tool>). Server không kết nối được → chỉ có wildcard mcp__<server>__*."""
    out = {"builtin": list(BUILTIN_TOOLS), "mcp": {}}
    for name, cfg in _read_mcp_servers(cwd).items():
        url = cfg.get("url")
        tools = []
        if url:
            try:
                raw = await _list_http_mcp_tools(url)
                tools = [f"mcp__{name}__{t}" for t in raw]
            except Exception:  # noqa: BLE001
                tools = []
        out["mcp"][name] = {"wildcard": f"mcp__{name}__*", "tools": tools}
    return out


# ─── Core (poller + lock/queue) ───────────────────────────────────────────────

_locks: dict[str, asyncio.Lock] = {}
_semaphore: asyncio.Semaphore | None = None

# Event bus for SSE (Phase B). Set of subscriber queues.
_subscribers: set = set()
def workspace_blocked(workspace_id):
    """Trả (True, reason) nếu workspace đang suspended. Workspace không tồn tại coi như không
    chặn (dữ liệu 'default' cũ / edge case)."""
    ws = get_workspace(workspace_id)
    if not ws:
        return False, ""
    if ws["status"] != "active":
        return True, f"workspace {ws['status']}"
    return False, ""


def publish(event: dict):
    """Đẩy event tới SSE subscriber, CÓ CÔ LẬP THEO WORKSPACE. Mỗi subscriber đăng ký kèm 1
    ws_filter (workspace_id nó muốn xem, hoặc None = admin xem tất cả). Event mang 'workspace_id'
    chỉ tới subscriber cùng workspace (và admin); event KHÔNG mang workspace_id (global) tới mọi
    subscriber. Nhờ vậy tenant A không bao giờ thấy event của tenant B."""
    event = {"ts": _now(), **event}
    ev_ws = event.get("workspace_id")
    for q, ws_filter in list(_subscribers):
        # ws_filter None (admin) → nhận hết. Event global (ev_ws None) → mọi người nhận.
        # Còn lại: chỉ nhận khi trùng workspace.
        if ws_filter is not None and ev_ws is not None and ev_ws != ws_filter:
            continue
        try:
            q.put_nowait(event)
        except Exception:  # noqa: BLE001
            pass


def _lock_for(session_id: str) -> asyncio.Lock:
    if session_id not in _locks:
        _locks[session_id] = asyncio.Lock()
    return _locks[session_id]


async def process_signal(signal):
    """Xử lý 1 signal: khóa session, inject, ghi audit, cập nhật trạng thái."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    wsid = signal.get("workspace_id") or DEFAULT_WORKSPACE
    target = get_session(signal["to_session"])
    if not target:
        set_signal_status(signal["id"], "failed", "session không tồn tại")
        record_run(signal["to_session"], signal["id"], signal["message"],
                   {"error": "session không tồn tại"}, "error", 0, _now(), _now(), wsid)
        publish({"type": "signal", "id": signal["id"], "status": "failed", "reason": "no session", "workspace_id": wsid})
        return {"signal": signal["id"], "status": "failed", "reason": "no session"}

    # Pause/stop-aware: không inject vào session đang paused/stopped — để signal chờ.
    if target["status"] in ("paused", "stopped"):
        return {"signal": signal["id"], "status": "skipped", "reason": f"session {target['status']}"}

    # Thứ tự khóa: lock SESSION trước (chờ agent bận không tốn slot), semaphore sau —
    # slot chỉ bị giữ bởi run đang chạy thật, agent rảnh không bao giờ bị đói slot
    # vì hàng đợi của 1 agent bận.
    async with _lock_for(target["id"]):
        async with _semaphore:
            # Kiểm tra lại sau khi có lock (trạng thái có thể đổi trong lúc chờ)
            target = get_session(target["id"])
            if target["status"] in ("paused", "stopped"):
                return {"signal": signal["id"], "status": "skipped", "reason": f"session {target['status']}"}

            # Workspace suspended / kill switch riêng → skip để chờ (activate/tắt kill là chạy lại,
            # không đánh 'blocked' vĩnh viễn như cap). Không đụng tenant khác.
            wblocked, wreason = workspace_blocked(wsid)
            if wblocked:
                return {"signal": signal["id"], "status": "skipped", "reason": wreason}

            # Circuit breaker: chặn lặp vô tận / vượt budget.
            exceeded, reason = cap_exceeded(target["id"])
            if exceeded:
                set_signal_status(signal["id"], "blocked", reason)
                set_session_status(target["id"], "idle")
                publish({"type": "signal", "id": signal["id"], "status": "blocked",
                         "session": target["id"], "reason": reason, "workspace_id": wsid})
                return {"signal": signal["id"], "status": "blocked", "reason": reason}

            set_signal_status(signal["id"], "processing")
            set_session_status(target["id"], "running")
            publish({"type": "signal", "id": signal["id"], "status": "processing", "session": target["id"], "workspace_id": wsid})
            started = _now()
            dry = bool(signal.get("dry_run"))

            # Mở run trước để có run_id, rồi stream từng bước vào run_events + SSE.
            run_id = start_run(target["id"], signal["id"], signal["message"], started, wsid)
            publish({"type": "run_start", "run_id": run_id, "session": target["id"],
                     "signal": signal["id"], "workspace_id": wsid})
            seq_box = [0]

            async def on_event(kind, summary, payload, _rid=run_id, _sid=target["id"], _sig=signal["id"], _ws=wsid):
                seq_box[0] += 1
                record_run_event(_rid, _sid, _sig, seq_box[0], kind, summary, payload)
                ev = {"type": "run_event", "run_id": _rid, "session": _sid,
                      "seq": seq_box[0], "kind": kind, "summary": summary, "workspace_id": _ws}
                # id=70: event 'text' mang thêm 'result' = FULL AI message (summary bị _trunc chỉ
                # dùng cho audit) → client SSE dựng bubble mỗi message không bị cắt.
                if kind == "text" and isinstance(payload, dict) and payload.get("text"):
                    ev["result"] = payload["text"]
                publish(ev)

            attempts = 0
            engine = engine_for(target)  # chọn engine theo session (default claude)
            # Prepend role + SKILL vào MỖI inject → role không trôi khi history dài (xem _prepend_role).
            inject_msg = _prepend_role(target.get("cwd", ""), target["name"], signal["message"])
            while True:
                try:
                    res = await engine.run(target, inject_msg, on_event=on_event, dry_run=dry)
                except Exception as e:  # noqa: BLE001
                    res = {"ok": False, "result": f"exception: {e}", "session_id": target["id"], "tokens": 0, "raw": {}}
                    await on_event("error", f"exception: {e}", {"error": str(e)})
                if res.get("ok") or attempts >= MAX_RETRIES:
                    break
                attempts += 1
                await on_event("error", f"retry {attempts}/{MAX_RETRIES} sau lỗi", {"attempt": attempts})
                await asyncio.sleep(RETRY_BACKOFF * attempts)
            ended = _now()

            status = "ok" if res.get("ok") else "error"
            finish_run(run_id, res.get("raw", {}), status, res.get("tokens", 0), ended)
            final = "done" if res.get("ok") else "failed"
            fail_reason = "" if res.get("ok") else _trunc(res.get("result", ""), 300)
            set_signal_status(signal["id"], final, fail_reason)
            set_session_status(target["id"], "idle")
            # Signal-driven: phát lifecycle 'run' + trạng thái 'signal'. Text agent (nội dung trả lời)
            # đi kèm signal done qua field 'result' — FULL, KHÔNG cắt (event 'text' SSE bị _trunc chỉ
            # dùng cho audit/dashboard). Client đọc result từ signal status=done để hiện đủ nội dung.
            publish({"type": "run", "run_id": run_id, "session": target["id"], "signal": signal["id"],
                     "status": status, "tokens": res.get("tokens", 0), "workspace_id": wsid})
            sig_ev = {"type": "signal", "id": signal["id"], "status": final,
                      "session": target["id"], "workspace_id": wsid}
            if res.get("ok"):
                sig_ev["result"] = res.get("result", "")  # text agent full, chỉ khi done
            publish(sig_ev)
            return {"signal": signal["id"], "status": final, "result": res.get("result", "")}


async def process_pending():
    """Poll 1 lần: xử lý tất cả signal eligible (song song, serialize theo session)."""
    signals = eligible_signals()
    if not signals:
        return []
    results = await asyncio.gather(*[process_signal(s) for s in signals])
    return results


_inflight: set = set()  # signal id đang có task xử lý (chờ lock hoặc đang chạy)


async def _process_one(sig):
    try:
        r = await process_signal(sig)
        if r:
            print(f"[orchestrator] signal #{r['signal']} → {r['status']}")
    except Exception as e:  # noqa: BLE001
        print(f"[orchestrator] signal #{sig['id']} error: {e}", file=sys.stderr)
    finally:
        _inflight.discard(sig["id"])


async def run_loop():
    """Mỗi poll: spawn task RIÊNG cho từng signal eligible chưa in-flight — agent khác nhau
    chạy SONG SONG ngay; signal tới agent đang bận chỉ đợi lock session của agent đó
    (không chặn cả batch như gather trước đây)."""
    print(f"[orchestrator] loop start (dry_run={DRY_RUN}, poll={POLL_INTERVAL}s, db={_db_path()})")
    while True:
        try:
            for sig in eligible_signals():
                if sig["id"] in _inflight:
                    continue
                _inflight.add(sig["id"])
                asyncio.create_task(_process_one(sig))
        except Exception as e:  # noqa: BLE001
            print(f"[orchestrator] loop error: {e}", file=sys.stderr)
        await asyncio.sleep(POLL_INTERVAL)


# ─── Control API (Phase B: REST + SSE) ────────────────────────────────────────


def build_app():
    """Starlette app: REST control + SSE live events + background poll loop.

    Ngoài API/dashboard của chính orchestrator, app này còn mount các MCP server nội
    bộ (signal, unity-dev) vào cùng 1 port để chỉ cần start 1 process:
      - /signal/mcp  → signal_mcp (send_signal, compact_context, list_agents)
      - /unity/mcp   → unity_dev (tools lập kế hoạch game)
    signal_mcp chạy in-process (gọi thẳng hàm orchestrator, không self-call HTTP).
    """
    from contextlib import AsyncExitStack, asynccontextmanager
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Mount, Route, WebSocketRoute
    from starlette.staticfiles import StaticFiles

    import signal_mcp
    import unity_dev
    import asset_fetch

    # signal_mcp gọi thẳng các hàm orchestrator thay vì POST HTTP về chính mình.
    signal_mcp._INPROC = True

    # streamable_http_app() tạo ASGI sub-app + session manager (lazy). Mỗi sub-app có
    # lifespan riêng (chạy session manager) — phải nối vào lifespan cha, nếu không
    # /mcp sẽ lỗi 500 vì session manager chưa khởi động.
    signal_app = signal_mcp.mcp.streamable_http_app()
    unity_app = unity_dev.mcp.streamable_http_app()
    asset_app = asset_fetch.mcp.streamable_http_app()

    async def health(request: Request):
        return JSONResponse({"status": "ok", "server": "Session-Orchestrator",
                             "dry_run": DRY_RUN,
                             "default_effort": DEFAULT_EFFORT,
                             "daily_allow_step": DAILY_ALLOW_STEP,
                             "limits": {"max_runs_per_session": MAX_RUNS_PER_SESSION,
                                        "max_runs_per_day": MAX_RUNS_PER_DAY,
                                        "session_token_budget": SESSION_TOKEN_BUDGET,
                                        "max_retries": MAX_RETRIES}})

    async def api_stats(request: Request):
        per = []
        for s in list_sessions():
            st = session_stats(s["id"])
            exceeded, reason = cap_exceeded(s["id"])
            per.append({"id": s["id"], "name": s["name"], **st, "blocked": exceeded, "reason": reason})
        return JSONResponse({
            "total_runs": sum(p["runs"] for p in per),
            "total_tokens": sum(p["tokens"] for p in per),
            "limits": {"max_runs_per_session": MAX_RUNS_PER_SESSION,
                       "session_token_budget": SESSION_TOKEN_BUDGET, "max_retries": MAX_RETRIES},
            "sessions": per,
        })

    # Workspaces (multi-tenant)
    async def api_workspaces(request: Request):
        """GET: list mọi workspace (kèm số session để dashboard hiển thị)."""
        counts = {}
        conn = _conn()
        for r in conn.execute("SELECT workspace_id, COUNT(*) c FROM sessions GROUP BY workspace_id").fetchall():
            counts[r["workspace_id"]] = r["c"]
        conn.close()
        out = [{**w, "sessions": counts.get(w["id"], 0)} for w in list_workspaces()]
        return JSONResponse(out)

    async def api_create_workspace(request: Request):
        """POST: tạo workspace mới — orchestrator sinh id + mkdir thư mục ghim. Trả {id, root_dir}."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        mrpd = body.get("max_runs_per_day")
        ws = create_workspace(body.get("name", ""), int(mrpd) if mrpd is not None else None)
        publish({"type": "workspace", "id": ws["id"], "status": "active", "workspace_id": ws["id"]})
        return JSONResponse(ws)

    async def api_lookup_workspace(request: Request):
        """POST {name}: lookup-or-create theo TÊN (idempotent). FE gửi tên tenant (vd user id/email),
        nhận lại 1 workspace ổn định — gọi lại cùng tên không tạo bản mới. Trả workspace + {created}.
        Đây là cửa để FE ánh xạ user → workspace mà không cần tự lưu ws_id nếu không muốn."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "name bắt buộc"}, status_code=400)
        mrpd = body.get("max_runs_per_day")
        ws, created = lookup_or_create_workspace(name, int(mrpd) if mrpd is not None else None)
        if created:
            publish({"type": "workspace", "id": ws["id"], "status": "active", "workspace_id": ws["id"]})
        return JSONResponse({**ws, "created": created})

    async def api_workspace_detail(request: Request):
        ws = get_workspace(request.path_params["wid"])
        if not ws:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(ws)

    async def _set_ws_status(request: Request, status: str):
        wid = request.path_params["wid"]
        if not get_workspace(wid):
            return JSONResponse({"error": "not found"}, status_code=404)
        set_workspace_status(wid, status)
        publish({"type": "workspace", "id": wid, "status": status, "workspace_id": wid})
        return JSONResponse({"id": wid, "status": status})

    async def api_suspend_workspace(request: Request):
        return await _set_ws_status(request, "suspended")

    async def api_activate_workspace(request: Request):
        return await _set_ws_status(request, "active")

    # Sessions
    async def api_sessions(request: Request):
        # Đính kèm trạng thái cap-theo-ngày để dashboard hiển thị "đã dùng/hạn mức" + nút Allow.
        # Filter theo ?workspace_id= để dashboard xem từng tenant (bỏ trống = tất cả, admin view).
        wsf = request.query_params.get("workspace_id")
        out = []
        for s in list_sessions():
            if wsf and s.get("workspace_id") != wsf:
                continue
            out.append({**s, **daily_stats(s["id"])})
        return JSONResponse(out)

    async def api_session_detail(request: Request):
        sid = request.path_params["sid"]
        s = get_session(sid)
        if not s:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(s)

    async def api_session_runs(request: Request):
        sid = request.path_params["sid"]
        conn = _conn()
        rows = conn.execute("SELECT * FROM runs WHERE session_id = ? ORDER BY id DESC LIMIT 100", (sid,)).fetchall()
        conn.close()
        return JSONResponse([dict(r) for r in rows])

    async def _set_status(request: Request, status: str):
        sid = request.path_params["sid"]
        s = get_session(sid)
        if not s:
            return JSONResponse({"error": "not found"}, status_code=404)
        set_session_status(sid, status)
        publish({"type": "session", "id": sid, "status": status, "workspace_id": s.get("workspace_id")})
        return JSONResponse({"id": sid, "status": status})

    async def api_pause(request: Request):
        return await _set_status(request, "paused")

    async def api_resume(request: Request):
        return await _set_status(request, "idle")

    async def api_stop(request: Request):
        return await _set_status(request, "stopped")

    def _validate_workspace(body):
        """Trả (workspace_id, error_response|None). Bỏ trống = 'default' (single-tenant cũ).
        Workspace phải tồn tại + đang active thì mới cho tạo/register session."""
        wid = body.get("workspace_id") or DEFAULT_WORKSPACE
        ws = get_workspace(wid)
        if not ws:
            return wid, JSONResponse({"error": f"workspace '{wid}' không tồn tại"}, status_code=404)
        if ws["status"] != "active":
            return wid, JSONResponse({"error": f"workspace '{wid}' đang {ws['status']}"}, status_code=409)
        return wid, None

    async def api_register(request: Request):
        body = await request.json()
        if not body.get("id") or not body.get("name"):
            return JSONResponse({"error": "id và name bắt buộc"}, status_code=400)
        wid, err = _validate_workspace(body)
        if err:
            return err
        # cwd: workspace ≠ default thì GHIM vào thư mục workspace (bỏ cwd tùy ý từ body).
        cwd = body.get("cwd", "")
        if wid != DEFAULT_WORKSPACE:
            cwd = workspace_root(wid) or cwd
        register_session(body["id"], body["name"], body.get("project", ""), cwd,
                         body.get("allowed_tools", []), body.get("permission_mode", ""),
                         body.get("model", ""), body.get("effort", ""), wid, "claude")
        # Đăng ký làm ORCHESTRATOR (flag từ setOrch trên UI): chưa có SKILL trong cwd → seed
        # playbook director từ template game-director (điền sẵn tên orch; các placeholder
        # khác — GAME_NAME/PROJECT_ID... — điền tay sau theo project).
        if body.get("seed_director_skill") and cwd and not _skill_path(cwd, body["name"]).exists():
            try:
                tpl = (TEMPLATES_DIR / "game-director" / "SKILL.md").read_text(encoding="utf-8")
                _write_role_skill(cwd, body["name"], tpl.replace("<ORCH_NAME>", body["name"]))
            except OSError:
                pass  # thiếu template → orch vẫn chạy, chỉ không có playbook
        publish({"type": "session", "id": body["id"], "status": "idle", "workspace_id": wid})
        return JSONResponse(get_session(body["id"]))

    async def api_spawn(request: Request):
        body = await request.json()
        if not body.get("name"):
            return JSONResponse({"error": "name bắt buộc"}, status_code=400)
        wid, err = _validate_workspace(body)
        if err:
            return err
        # engine.spawn tự ghim cwd theo workspace (bỏ cwd tùy ý cho ws ≠ default).
        res = await engine_for("claude").spawn(
            body["name"], project=body.get("project", ""), cwd=body.get("cwd", ""),
            allowed_tools=body.get("allowed_tools", []), permission_mode=body.get("permission_mode", ""),
            init_prompt=body.get("init_prompt", ""), model=body.get("model", ""),
            effort=body.get("effort", ""), workspace_id=wid)
        if res and res.get("error"):
            return JSONResponse(res, status_code=500)
        publish({"type": "session", "id": res["id"], "status": "idle", "workspace_id": wid})
        return JSONResponse(res)

    async def api_unregister(request: Request):
        sid = request.path_params["sid"]
        s = get_session(sid)
        if not s:
            return JSONResponse({"error": "not found"}, status_code=404)
        unregister_session(sid)
        publish({"type": "session", "id": sid, "status": "removed", "workspace_id": s.get("workspace_id")})
        return JSONResponse({"id": sid, "removed": True})

    async def api_available_tools(request: Request):
        cwd = request.query_params.get("cwd", "")
        return JSONResponse(await discover_tools(cwd))

    async def api_claude_sessions(request: Request):
        """Session Claude Code CLI (transcript ~/.claude/projects) của 1 cwd — cho UI chọn
        orchestrator của project. Không đụng DB sessions."""
        cwd = (request.query_params.get("cwd") or "").strip()
        if not cwd:
            return JSONResponse({"error": "cwd bắt buộc"}, status_code=400)
        return JSONResponse(list_claude_sessions(cwd))

    async def _claude_mcp(args: list[str], cwd: str) -> str:
        """Chạy `claude mcp <args>` tại cwd, trả stdout+stderr gộp (raw text cho UI)."""
        proc = await asyncio.create_subprocess_exec(
            CLAUDE_BIN, "mcp", *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 120)
        except asyncio.TimeoutError:
            proc.kill()
            return "timeout (120s): claude mcp " + " ".join(args)
        return out.decode("utf-8", errors="replace")

    async def api_fs_list(request: Request):
        """Duyệt thư mục server-side cho picker Working dir (form spawn).
        Chỉ liệt kê THƯ MỤC (không file), bỏ hidden. Mặc định: $HOME."""
        raw = (request.query_params.get("path") or "").strip() or str(Path.home())
        p = Path(raw).expanduser()
        try:
            p = p.resolve()
            if not p.is_dir():
                return JSONResponse({"error": f"không phải thư mục: {p}"}, status_code=400)
            dirs = sorted((d.name for d in p.iterdir()
                           if d.is_dir() and not d.name.startswith(".")), key=str.lower)[:300]
        except PermissionError:
            return JSONResponse({"error": f"không có quyền đọc: {p}"}, status_code=403)
        return JSONResponse({"path": str(p),
                             "parent": str(p.parent) if p != p.parent else None,
                             "dirs": dirs})

    async def api_mcp_list(request: Request):
        """MCP đã đăng ký cho project (`claude mcp list` tại cwd)."""
        cwd = (request.query_params.get("cwd") or "").strip()
        if not cwd or not os.path.isdir(cwd):
            return JSONResponse({"error": "cwd không hợp lệ"}, status_code=400)
        return JSONResponse({"out": await _claude_mcp(["list"], cwd)})

    async def api_mcp_add(request: Request):
        """Add MCP cho project: `claude mcp add <args người dùng nhập>` tại cwd."""
        body = await request.json()
        cwd = (body.get("cwd") or "").strip()
        args = (body.get("args") or "").strip()
        if not cwd or not os.path.isdir(cwd):
            return JSONResponse({"error": "cwd không hợp lệ"}, status_code=400)
        if not args:
            return JSONResponse({"error": "args bắt buộc (phần sau 'claude mcp add')"}, status_code=400)
        try:
            argv = shlex.split(args)
        except ValueError as e:
            return JSONResponse({"error": f"args không parse được: {e}"}, status_code=400)
        return JSONResponse({"out": await _claude_mcp(["add", *argv], cwd)})

    async def api_get_skill(request: Request):
        """SKILL hiện tại của role + path đích (<cwd>/.claude/skills/<name>/SKILL.md)."""
        s = get_session(request.path_params["sid"])
        if not s:
            return JSONResponse({"error": "not found"}, status_code=404)
        cwd, name = s.get("cwd") or "", s.get("name") or ""
        return JSONResponse({"skill": _role_skill(cwd, name), "path": str(_skill_path(cwd, name))})

    async def api_put_skill(request: Request):
        """Upsert SKILL của role vào project cwd: tạo thư mục nếu chưa có, đè nếu đã có."""
        s = get_session(request.path_params["sid"])
        if not s:
            return JSONResponse({"error": "not found"}, status_code=404)
        body = await request.json()
        content = body.get("content") or ""
        if not content.strip():
            return JSONResponse({"error": "content rỗng — không ghi"}, status_code=400)
        cwd, name = s.get("cwd") or "", s.get("name") or ""
        _write_role_skill(cwd, name, content)
        return JSONResponse({"path": str(_skill_path(cwd, name)), "bytes": len(content.encode("utf-8"))})

    async def ws_terminal(websocket):
        """Terminal thật trong browser (xterm.js): spawn `claude --resume <sid>` trong PTY tại cwd
        của session, bơm 2 chiều qua WebSocket. Client gửi JSON {t:'i', d:<keys>} (input) và
        {t:'r', c, r} (resize); server gửi bytes thô cho xterm ghi thẳng.
        Mỗi kết nối 1 PTY + 1 thread đọc (os.read blocking); đóng WS là giết child."""
        import fcntl
        import pty
        import struct
        import termios

        # Cùng chính sách auth với /api/*: ORCH_API_KEY set thì bắt ?api_key= khớp.
        if ORCH_API_KEY and not secrets.compare_digest(
                websocket.query_params.get("api_key", ""), ORCH_API_KEY):
            await websocket.close(code=4401)
            return
        sid = websocket.query_params.get("session", "")
        s = get_session(sid)
        if not s:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        cwd = (s.get("cwd") or "").strip() or str(Path.home())
        pid, fd = pty.fork()
        if pid == 0:  # child: chạy claude interactive trong PTY
            try:
                os.chdir(cwd)
            except OSError:
                pass
            os.environ["TERM"] = "xterm-256color"
            try:
                os.execvp(CLAUDE_BIN, [CLAUDE_BIN, "--resume", sid])
            finally:
                os._exit(1)  # execvp fail — không được rơi ngược vào event loop của cha

        loop = asyncio.get_running_loop()

        def _read():
            try:
                return os.read(fd, 65536)
            except OSError:  # EIO khi child thoát — coi như EOF
                return b""

        async def pump_out():
            while True:
                data = await loop.run_in_executor(None, _read)
                if not data:
                    break
                await websocket.send_bytes(data)
            await websocket.close()

        out_task = asyncio.create_task(pump_out())
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("t") == "i":
                    os.write(fd, str(msg.get("d", "")).encode("utf-8"))
                elif msg.get("t") == "r":
                    try:
                        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                                    struct.pack("HHHH", int(msg.get("r", 24)), int(msg.get("c", 80)), 0, 0))
                    except (OSError, ValueError):
                        pass
        except Exception:  # noqa: BLE001 — WS đóng/lỗi đều đi đường dọn dẹp chung
            pass
        finally:
            out_task.cancel()
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass

    async def api_skill_templates(request: Request):
        return JSONResponse(_list_skill_templates())

    async def api_set_model(request: Request):
        """Đổi model của 1 session ngay trên bảng Sessions (không cần re-register)."""
        sid = request.path_params["sid"]
        if not get_session(sid):
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        set_session_model(sid, (body.get("model") or "").strip())
        s = get_session(sid)
        publish({"type": "session", "id": sid, "status": s["status"], "workspace_id": s.get("workspace_id")})
        return JSONResponse(s)

    async def api_set_effort(request: Request):
        """Đổi reasoning effort của 1 session ngay trên bảng Sessions."""
        sid = request.path_params["sid"]
        if not get_session(sid):
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        eff = (body.get("effort") or "").strip()
        if eff and eff not in EFFORT_LEVELS:
            return JSONResponse({"error": f"effort không hợp lệ; dùng: {', '.join(EFFORT_LEVELS)}"}, status_code=400)
        set_session_effort(sid, eff)
        s = get_session(sid)
        publish({"type": "session", "id": sid, "status": s["status"], "workspace_id": s.get("workspace_id")})
        return JSONResponse(s)

    async def api_get_compact(request: Request):
        """Đọc compact context MỚI NHẤT của 1 session từ transcript (metadata + full summary)."""
        sid = request.path_params["sid"]
        s = get_session(sid)
        if not s:
            return JSONResponse({"error": "not found"}, status_code=404)
        out = engine_for(s).get_compact(sid)
        # SKILL của role (playbook _prepend_role nhồi mỗi signal) — UI hiện kèm trong drawer Context.
        out["skill"] = _role_skill(s.get("cwd") or "", s.get("name") or "")
        return JSONResponse(out)

    async def api_compact(request: Request):
        """Nén context của 1 session: enqueue signal '/compact' (đi qua per-session lock,
        không nén khi đang có prompt in-flight). Focus tùy chọn để giữ lại nội dung trọng tâm."""
        sid = request.path_params["sid"]
        s = get_session(sid)
        if not s:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        focus = (body.get("focus") or "").strip()
        msg = "/compact" + (f" {focus}" if focus else "")
        ws = s.get("workspace_id") or DEFAULT_WORKSPACE
        sig = enqueue_signal(sid, msg, "human", 0, 0, ws)
        publish({"type": "signal", "id": sig, "status": "pending", "to_session": sid, "workspace_id": ws})
        return JSONResponse({"id": sig, "compact": True, "to_session": sid})

    async def api_allow(request: Request):
        """Nới hạn mức run/ngày cho 1 session thêm DAILY_ALLOW_STEP, rồi tự đưa các signal
        đang 'blocked' của session đó về 'pending' để poller chạy tiếp trong hạn mức mới."""
        sid = request.path_params["sid"]
        s = get_session(sid)
        if not s:
            return JSONResponse({"error": "not found"}, status_code=404)
        ws = s.get("workspace_id")
        new_limit = grant_daily_allowance(sid)
        # Bỏ chặn các signal đang blocked của session này (mỗi run tốn đúng 1 slot hạn mức).
        conn = _conn()
        blocked = [r["id"] for r in conn.execute(
            "SELECT id FROM signals WHERE to_session = ? AND status = 'blocked'", (sid,)).fetchall()]
        conn.close()
        for bid in blocked:
            set_signal_status(bid, "pending")
            publish({"type": "signal", "id": bid, "status": "pending", "session": sid, "workspace_id": ws})
        st = daily_stats(sid)
        publish({"type": "session", "id": sid, "status": get_session(sid)["status"], "workspace_id": ws})
        return JSONResponse({"id": sid, "daily_limit": new_limit, "requeued": blocked, **st})

    def _paging(request, default_limit=10, max_limit=200):
        """Đọc ?limit=&offset= an toàn (clamp về [1, max_limit] / >=0). Dùng cho signals + runs."""
        try:
            limit = int(request.query_params.get("limit", default_limit))
        except ValueError:
            limit = default_limit
        try:
            offset = int(request.query_params.get("offset", 0))
        except ValueError:
            offset = 0
        return max(1, min(limit, max_limit)), max(0, offset)

    # Signals
    async def api_signals(request: Request):
        # Phân trang (?limit=&offset=) + lọc theo ?workspace_id= để xem queue từng tenant.
        # Trả {items, has_more, offset, limit} — has_more = còn record cũ hơn để bấm "+".
        wsf = request.query_params.get("workspace_id") or None
        limit, offset = _paging(request)
        items, has_more = list_signals(limit, offset, wsf)
        return JSONResponse({"items": items, "has_more": has_more, "offset": offset, "limit": limit})

    async def api_enqueue(request: Request):
        body = await request.json()
        ref = body.get("to_session") or body.get("to_role")
        if not ref or not body.get("message"):
            return JSONResponse({"error": "to_session/to_role và message bắt buộc"}, status_code=400)
        # Resolve trong phạm vi workspace nếu có (chống signal đi nhầm tenant khi trùng role).
        wid = body.get("workspace_id") or None
        target = resolve_session_id(ref, wid)
        if not target:
            scope = f" trong workspace '{wid}'" if wid else ""
            return JSONResponse({"error": f"không tìm thấy session cho '{ref}'{scope}"}, status_code=404)
        # Signal thừa hưởng workspace của session đích (nguồn sự thật là session).
        target_ws = get_session(target).get("workspace_id") or DEFAULT_WORKSPACE
        # id=14/Q1: nếu message mang 'ticket' của 1 ask_user_choice đang chờ → service TỰ đóng signal
        # auto-signal đó (FE khỏi gọi approve, tránh double-close/inject lại). Chỉ đóng đúng signal
        # ask_user_choice cùng ticket + cùng workspace; KHÔNG đụng signal WRITE/điều khiển khác.
        _ticket = _extract_ticket(body.get("message"))
        if _ticket:
            for cid in close_ask_user_choice_by_ticket(_ticket, target_ws):
                publish({"type": "signal", "id": cid, "status": "done",
                         "to_session": target, "workspace_id": target_ws})
        sid = enqueue_signal(target, body["message"],
                             body.get("from_session", "") or body.get("from_role", ""),
                             int(body.get("requires_approval", 0)), int(body.get("dry_run", 0)), target_ws)
        publish({"type": "signal", "id": sid, "status": "pending", "to_session": target, "workspace_id": target_ws})
        return JSONResponse({"id": sid, "status": "pending", "to_session": target, "workspace_id": target_ws})

    async def _resolve_signal(request: Request, status: str):
        sig_id = int(request.path_params["sig_id"])
        conn = _conn()
        row = conn.execute("SELECT * FROM signals WHERE id = ?", (sig_id,)).fetchone()
        conn.close()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        set_signal_status(sig_id, status)
        publish({"type": "signal", "id": sig_id, "status": status, "workspace_id": row["workspace_id"]})
        return JSONResponse({"id": sig_id, "status": status})

    async def api_approve(request: Request):
        return await _resolve_signal(request, "approved")

    async def api_deny(request: Request):
        return await _resolve_signal(request, "denied")

    # Chỉ signal đã "kết thúc lỗi" mới re-run được — chặn re-run signal đang chạy dở.
    RERUNNABLE = ("failed", "denied", "blocked")

    async def api_rerun(request: Request):
        """Re-run 1 signal đã thất bại: đưa về 'pending' (reset delivered_at) để poller
        nhặt lại. KHÔNG set thẳng 'processing' — chỉ poller mới được đặt trạng thái đó,
        và eligible_signals() chỉ chọn 'pending'/'approved' nên 'processing' thủ công sẽ kẹt."""
        sig_id = int(request.path_params["sig_id"])
        conn = _conn()
        row = conn.execute("SELECT * FROM signals WHERE id = ?", (sig_id,)).fetchone()
        conn.close()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        if row["status"] not in RERUNNABLE:
            return JSONResponse(
                {"error": f"chỉ re-run được signal ở trạng thái {', '.join(RERUNNABLE)}; "
                          f"signal #{sig_id} đang '{row['status']}'"}, status_code=409)
        set_signal_status(sig_id, "pending")
        publish({"type": "signal", "id": sig_id, "status": "pending", "session": row["to_session"], "workspace_id": row["workspace_id"]})
        return JSONResponse({"id": sig_id, "status": "pending", "rerun": True})

    # Không xóa signal đang chạy dở (poller có thể đang inject) — chỉ signal đã kết thúc.
    DELETABLE = ("failed", "denied", "blocked", "done")

    async def api_delete_signal(request: Request):
        """Xóa 1 signal đã kết thúc + toàn bộ audit log (runs + run_events) của nó."""
        sig_id = int(request.path_params["sig_id"])
        conn = _conn()
        row = conn.execute("SELECT * FROM signals WHERE id = ?", (sig_id,)).fetchone()
        conn.close()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        if row["status"] not in DELETABLE:
            return JSONResponse(
                {"error": f"chỉ xóa được signal ở trạng thái {', '.join(DELETABLE)}; "
                          f"signal #{sig_id} đang '{row['status']}'"}, status_code=409)
        deleted = delete_signal(sig_id)
        publish({"type": "signal", "id": sig_id, "status": "removed", "session": row["to_session"], "workspace_id": row["workspace_id"]})
        return JSONResponse({"id": sig_id, "removed": True, "deleted": deleted})

    # Runs (audit)
    async def api_runs(request: Request):
        # Phân trang (?limit=&offset=) + lọc ?workspace_id=. Trả {items, has_more, offset, limit}.
        wsf = request.query_params.get("workspace_id") or None
        limit, offset = _paging(request)
        items, has_more = list_runs(limit, offset, wsf)
        return JSONResponse({"items": items, "has_more": has_more, "offset": offset, "limit": limit})

    async def api_run_events(request: Request):
        rid = int(request.path_params["rid"])
        return JSONResponse(list_run_events(rid))

    # SSE live events — cô lập theo workspace.
    async def api_events(request: Request):
        # ?workspace_id= → chỉ nhận event của tenant đó (FE mỗi user mở 1 stream riêng).
        # Bỏ trống = admin view, nhận mọi event. Tồn tại thì mới lọc; không thì trả 404.
        ws_filter = request.query_params.get("workspace_id") or None
        if ws_filter is not None and not get_workspace(ws_filter):
            return JSONResponse({"error": f"workspace '{ws_filter}' không tồn tại"}, status_code=404)
        q: asyncio.Queue = asyncio.Queue()
        sub = (q, ws_filter)
        _subscribers.add(sub)

        async def gen():
            try:
                yield "event: ready\ndata: {}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                _subscribers.discard(sub)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @asynccontextmanager
    async def lifespan(app):
        init_db()  # idempotent — cũng migrate bảng/cột mới cho DB cũ (vd run_events)
        async with AsyncExitStack() as stack:
            # Khởi động session manager của từng MCP sub-app (bắt buộc cho /mcp).
            await stack.enter_async_context(signal_app.router.lifespan_context(app))
            await stack.enter_async_context(unity_app.router.lifespan_context(app))
            await stack.enter_async_context(asset_app.router.lifespan_context(app))
            task = asyncio.create_task(run_loop())
            print(f"[orchestrator] API on http://{ORCH_HOST}:{ORCH_PORT} (dry_run={DRY_RUN})")
            print("[orchestrator] MCP mounted: /signal/mcp, /unity/mcp")
            try:
                yield
            finally:
                task.cancel()

    class ApiKeyMiddleware(BaseHTTPMiddleware):
        """Chặn /api/* nếu thiếu/sai API key. Chỉ bật khi ORCH_API_KEY được set (mặc định tắt
        để dev localhost như cũ). Key nhận qua header 'X-API-Key' hoặc query '?api_key=' (cho
        EventSource/SSE không gắn được custom header). Không đụng dashboard tĩnh, /health, MCP mount."""
        async def dispatch(self, request, call_next):
            if ORCH_API_KEY and request.url.path.startswith("/api/"):
                key = request.headers.get("x-api-key") or request.query_params.get("api_key", "")
                if not secrets.compare_digest(key, ORCH_API_KEY):
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    routes = [
        Route("/health", health),
        # Workspaces (multi-tenant)
        Route("/api/workspaces", api_workspaces),
        Route("/api/workspaces", api_create_workspace, methods=["POST"]),
        # lookup phải đứng TRƯỚC "/{wid}" để không bị nuốt thành wid="lookup".
        Route("/api/workspaces/lookup", api_lookup_workspace, methods=["POST"]),
        Route("/api/workspaces/{wid}", api_workspace_detail),
        Route("/api/workspaces/{wid}/suspend", api_suspend_workspace, methods=["POST"]),
        Route("/api/workspaces/{wid}/activate", api_activate_workspace, methods=["POST"]),
        Route("/api/sessions", api_sessions),
        Route("/api/sessions", api_register, methods=["POST"]),
        Route("/api/sessions/spawn", api_spawn, methods=["POST"]),
        Route("/api/available-tools", api_available_tools),
        Route("/api/claude-sessions", api_claude_sessions),
        Route("/api/fs", api_fs_list),
        Route("/api/mcp", api_mcp_list),
        Route("/api/mcp", api_mcp_add, methods=["POST"]),
        WebSocketRoute("/ws/terminal", ws_terminal),
        Route("/api/skills/templates", api_skill_templates),
        Route("/api/sessions/{sid}", api_session_detail),
        Route("/api/sessions/{sid}/unregister", api_unregister, methods=["POST"]),
        Route("/api/sessions/{sid}/runs", api_session_runs),
        Route("/api/sessions/{sid}/pause", api_pause, methods=["POST"]),
        Route("/api/sessions/{sid}/resume", api_resume, methods=["POST"]),
        Route("/api/sessions/{sid}/stop", api_stop, methods=["POST"]),
        Route("/api/sessions/{sid}/skill", api_get_skill),
        Route("/api/sessions/{sid}/skill", api_put_skill, methods=["POST"]),
        Route("/api/sessions/{sid}/compact", api_get_compact),
        Route("/api/sessions/{sid}/compact", api_compact, methods=["POST"]),
        Route("/api/sessions/{sid}/model", api_set_model, methods=["POST"]),
        Route("/api/sessions/{sid}/effort", api_set_effort, methods=["POST"]),
        Route("/api/sessions/{sid}/allow", api_allow, methods=["POST"]),
        Route("/api/signals", api_signals),
        Route("/api/signals", api_enqueue, methods=["POST"]),
        Route("/api/signals/{sig_id}/approve", api_approve, methods=["POST"]),
        Route("/api/signals/{sig_id}/deny", api_deny, methods=["POST"]),
        Route("/api/signals/{sig_id}/rerun", api_rerun, methods=["POST"]),
        Route("/api/signals/{sig_id}", api_delete_signal, methods=["DELETE"]),
        Route("/api/runs", api_runs),
        Route("/api/runs/{rid}/events", api_run_events),
        Route("/api/stats", api_stats),
        Route("/api/events", api_events),
        # MCP server nội bộ mount chung port (đặt trước static "/"): /signal/mcp, /unity/mcp.
        Mount("/signal", app=signal_app),
        Mount("/unity", app=unity_app),
        Mount("/assets", app=asset_app),
    ]

    # Dashboard (Phase C): serve static UI at "/" (must be last — catches the rest).
    static_dir = Path(__file__).parent / "static" / "orchestrator"
    if static_dir.exists():
        routes.append(Mount("/", app=StaticFiles(directory=str(static_dir), html=True)))

    middleware = [Middleware(ApiKeyMiddleware)] if ORCH_API_KEY else []
    return Starlette(routes=routes, lifespan=lifespan, middleware=middleware)


def serve():
    import uvicorn
    # Nếu server được start từ TRONG một session Claude Code (vd agent restart hộ), env mang
    # marker session con → mọi `claude` spawn (PTY terminal + headless -p) tưởng mình là child
    # session và TẮT lưu transcript (~/.claude/projects) — vỡ compact/resume/chọn-orchestrator.
    # Strip 1 lần ở đây: mọi child kế thừa env sạch.
    for k in ("CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
        os.environ.pop(k, None)
    # Dọn trạng thái kẹt từ lần chạy trước (crash/kill giữa run): không run nào sống qua
    # restart — session 'running' → idle (kẻo card orch khóa vĩnh viễn), run 'running' → error.
    _ensure_db()
    conn = _conn()
    conn.execute("UPDATE sessions SET status = 'idle' WHERE status = 'running'")
    conn.execute("UPDATE runs SET status = 'error', ended_at = ? WHERE status = 'running'", (_now(),))
    conn.commit()
    conn.close()
    uvicorn.run(build_app(), host=ORCH_HOST, port=ORCH_PORT)


# ─── CLI ──────────────────────────────────────────────────────────────────────


def _print(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description="Session Orchestrator (Phase A)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("once")
    sub.add_parser("loop")
    sub.add_parser("serve")
    sub.add_parser("list-sessions")
    sub.add_parser("list-signals")
    sub.add_parser("list-runs")

    args = p.parse_args()
    if args.cmd == "init":
        init_db()
        print(f"DB tạo tại {_db_path()}")
    elif args.cmd == "once":
        _print(asyncio.run(process_pending()))
    elif args.cmd == "loop":
        asyncio.run(run_loop())
    elif args.cmd == "serve":
        serve()
    elif args.cmd == "list-sessions":
        _print(list_sessions())
    elif args.cmd == "list-signals":
        _print(list_signals()[0])   # (items, has_more) → chỉ in items
    elif args.cmd == "list-runs":
        _print(list_runs()[0])


if __name__ == "__main__":
    main()
