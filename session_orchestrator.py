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
  ORCH_DEFAULT_EFFORT  reasoning effort mặc định mọi session (default "high"; xem EFFORT_LADDER)
  ORCH_DEFAULT_PERMISSION_MODE  permission mode fallback khi session không set (default "bypassPermissions")
  CLAUDE_BIN           đường dẫn claude CLI (default "claude")
  ORCH_CODEX_BIN       đường dẫn codex CLI (default "codex")

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
import shutil
import socket
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx

# ── Console UTF-8: PHẢI chạy trước mọi print ─────────────────────────────────
# Console Windows mặc định cp1252. Mọi thông báo có dấu tiếng Việt sẽ ném
# UnicodeEncodeError và GIẾT cả tiến trình — đã đo trên runner: `init` chết ở dòng "DB tạo tại",
# `serve` chết giữa lifespan sau khi đã mở 3 MCP session manager.
# errors="replace" là lưới thứ hai: console lạ đến mấy cũng không được phép giết tiến trình chỉ
# vì một ký tự không in nổi.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass  # stream bị thay bằng thứ không reconfigure được (pytest capture, pipe lạ)


# ── MỘT module, KHÔNG hai bản ────────────────────────────────────────────────
# Chạy bằng `python session_orchestrator.py serve` (và cả binary PyInstaller, entry cũng là
# file này) thì module này mang tên '__main__'. signal_mcp lại `import session_orchestrator`
# → Python KHÔNG thấy tên đó trong sys.modules nên nạp lại FILE LẦN HAI thành một module
# khác, với bộ biến toàn cục RIÊNG. Hậu quả đã đo được: người dùng gõ terminal nhúng ghi mốc
# vào _round_at của bản '__main__', còn send_signal qua MCP đọc _round_at của bản import —
# luôn rỗng — nên trần ping-pong không bao giờ được mở lại và agent báo hết lượt dù vừa được
# giao việc mới. Đăng ký bí danh NGAY ĐÂY, trước khi có ai kịp import.
if __name__ == "__main__":
    sys.modules.setdefault("session_orchestrator", sys.modules[__name__])


# ── .env loader (stdlib, không thêm dep) ─────────────────────────────────────
# ── Đường dẫn: chạy từ source hay từ BẢN ĐÓNG GÓI (PyInstaller) đều đúng ──────
# Hai thư mục KHÁC NHAU khi đóng gói, lẫn là hỏng âm thầm:
#   _bundle_dir: tài nguyên đi kèm (static/, template skill) — PyInstaller giải nén vào 1 thư mục
#                TẠM (sys._MEIPASS) rồi XOÁ lúc thoát. Chỉ đọc, đừng ghi gì vào đây.
#   _app_dir   : nơi NGƯỜI DÙNG để file của họ (.env, skill tự viết) — cạnh file thực thi, còn
#                nguyên giữa các lần chạy.
def _bundle_dir():
    return Path(getattr(sys, "_MEIPASS", None) or Path(__file__).parent)


def _app_dir():
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent


# Đọc file .env cạnh chương trình NẾU có, chạy TRƯỚC mọi os.environ.get bên dưới.
# setdefault → env thật (shell export / systemd EnvironmentFile) LUÔN thắng;
# .env chỉ lấp biến còn trống. Dòng trống / bắt đầu '#' / không có '=' bị bỏ qua.
def _load_dotenv(path=None):
    path = path or _app_dir() / ".env"
    if not path.exists():
        return
    # encoding + errors: không truyền thì đọc theo locale, và trên Windows locale là cp1252 —
    # một dấu gạch dài trong comment hay đường dẫn có dấu là ném UnicodeDecodeError NGAY ở dòng
    # này, tức orchestrator chết lúc khởi động. errors="replace" để một byte lạ cùng lắm làm hỏng
    # một giá trị, chứ không chặn cả server khởi động.
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

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
CODEX_BIN = os.environ.get("ORCH_CODEX_BIN", "codex")
ORCH_HOST = os.environ.get("ORCH_HOST", "0.0.0.0")
ORCH_PORT = int(os.environ.get("ORCH_PORT", "8992"))
# Service-to-service auth. Để TRỐNG = tắt (localhost/dev như cũ). Set = mọi /api/* yêu cầu
# header 'X-API-Key' (hoặc query ?api_key= cho SSE) khớp. Web app backend giữ key này.
ORCH_API_KEY = os.environ.get("ORCH_API_KEY", "")
# CORS cho app chạy TRONG TRÌNH DUYỆT gọi /v1 (React/Vue…). Trình duyệt gửi preflight OPTIONS
# trước mọi request có header lạ (Authorization, Content-Type: application/json) — không trả lời
# preflight thì nó chặn, log hiện "OPTIONS ... 405".
# Danh sách origin ngăn cách bằng dấu phẩy; '*' = mọi origin; để TRỐNG = tắt hẳn CORS.
# CẢNH BÁO: '*' + ORCH_API_KEY trống nghĩa là BẤT KỲ trang web nào người dùng mở cũng sai khiến
# được agent trên máy này (agent chạy shell với bypassPermissions). Xem cảnh báo lúc khởi động.
CORS_ORIGINS = [o.strip() for o in os.environ.get("ORCH_CORS_ORIGINS", "*").split(",") if o.strip()]
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
# ── Reasoning effort: 1 THANG DÙNG CHUNG, clamp theo trần của từng engine/model ───
# UI chỉ hiện thang này; mức nào engine không chịu nổi thì HẠ xuống trần của nó (clamp) chứ
# không bỏ cờ âm thầm — hạ 1 nấc vẫn đúng ý "cố hết sức", còn bỏ cờ là rơi về default của CLI.
EFFORT_LADDER = ("low", "medium", "high", "xhigh", "max", "ultra")
# Trần ĐÃ ĐO: `claude --help` → "Effort level ... (low, medium, high, xhigh, max)".
CLAUDE_MAX_EFFORT = "max"
# Trần codex ĐO THEO TỪNG MODEL, không theo engine: `codex debug models` trả
# supported_reasoning_levels. terra=ultra, luna=max, gpt-5.5 & gpt-5.4-mini CHỈ tới xhigh.
# Model không có trong bảng (kể cả 'codex' auto — CLI tự chọn theo config.toml, không đoán được)
# → lấy trần thấp nhất đã biết. Thà hạ nhầm 1 nấc còn hơn gửi mức model không có rồi hỏng run.
CODEX_MAX_EFFORT = {"gpt-5.6-terra": "ultra", "gpt-5.6-luna": "max"}
CODEX_MAX_EFFORT_DEFAULT = "xhigh"
DEFAULT_EFFORT = os.environ.get("ORCH_DEFAULT_EFFORT", "high")  # high mặc định


def clamp_effort(effort, ceiling):
    """Hạ effort về mức cao nhất engine/model nhận. Trả '' nếu effort không thuộc thang (giá trị
    lạ / DB cũ) → caller bỏ cờ để CLI dùng mặc định của nó thay vì thoát với mã 1."""
    if effort not in EFFORT_LADDER:
        return ""
    return effort if EFFORT_LADDER.index(effort) <= EFFORT_LADDER.index(ceiling) else ceiling
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
    if "is_orch" not in scols:
        conn.execute("ALTER TABLE sessions ADD COLUMN is_orch INTEGER NOT NULL DEFAULT 0")
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


def set_session_workspace(session_id, workspace_id, name=None):
    """Chuyển session sang workspace khác (kèm đổi tên vai nếu cần vì trùng tên bên đó).

    CHỈ đụng cột workspace_id. KHÔNG viết lại signals/runs cũ: mỗi dòng đóng dấu nơi cuộc trao
    đổi ĐÃ xảy ra — sửa là làm giả audit log. Hệ quả đã biết: lịch sử ở lại workspace cũ, và
    ngân sách ping-pong (đếm theo signals.workspace_id) bắt đầu lại từ đầu bên mới."""
    conn = _conn()
    if name:
        conn.execute("UPDATE sessions SET workspace_id = ?, name = ?, last_active = ? WHERE id = ?",
                     (workspace_id, name, _now(), session_id))
    else:
        conn.execute("UPDATE sessions SET workspace_id = ?, last_active = ? WHERE id = ?",
                     (workspace_id, _now(), session_id))
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


def set_session_orch(session_id, on):
    """Bật/tắt cờ terminal cho 1 session. Bật: tắt cờ ở mọi session CÙNG cwd (1 terminal/project).

    is_orch KHÔNG còn liên quan routing signal (alias 'orch' đã bỏ — đích báo cáo là người gửi,
    xem _reply_rule). Giờ nó chỉ nói: card này mở terminal nhúng và được kéo giãn. Backend không
    đọc nó trên đường prompt nữa; giữ tên cột để khỏi migration."""
    _ensure_db()
    conn = _conn()
    if on:
        row = conn.execute("SELECT cwd FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row:
            conn.execute("UPDATE sessions SET is_orch = 0 WHERE cwd = ? AND is_orch = 1", (row["cwd"],))
    conn.execute("UPDATE sessions SET is_orch = ?, last_active = ? WHERE id = ?",
                 (1 if on else 0, _now(), session_id))
    conn.commit()
    conn.close()


def resolve_session_id(ref, workspace_id=None, from_ref=None):
    """ref = session_id (exact match) HOẶC role/name → trả session_id, None nếu không thấy.

    workspace_id != None: chỉ resolve trong phạm vi workspace đó (chống signal đi nhầm tenant
    khi hai workspace trùng role). Nếu ref là session_id thì cũng phải thuộc đúng workspace.
    from_ref: GIỮ cho tương thích chữ ký (mọi caller đang truyền) — không còn ảnh hưởng kết quả
    từ khi bỏ alias; đích signal luôn là một tên vai/session id có thật."""
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


# id=94 TRẦN PING-PONG. Hai agent nhắn qua lại tối đa PAIR_SIGNAL_CAP lượt (nguồn→đích, rồi
# đích→nguồn) là DỪNG: agent nguồn tổng hợp và báo cáo cho NGƯỜI DÙNG quyết định bước kế. Chống
# vòng signal tự đẻ việc, kéo scope ra ngoài thứ người dùng yêu cầu.
PAIR_SIGNAL_CAP = int(os.environ.get("ORCH_PAIR_SIGNAL_CAP", "2"))
# Người gửi KHÔNG phải agent: không tính vào trần, và là MỐC RESET (người dùng giao việc mới thì
# hai bên có lại đủ lượt). '' = FE BFF/hệ thống, 'human' = chat trên dashboard.
_HUMAN_SENDERS = ("", "human", "user")
# CỬA SỔ THỜI GIAN — chỉ đếm signal gần đây. Vì sao BẮT BUỘC có: vai điều phối (director/worker)
# nhận việc từ người dùng qua TERMINAL (PTY claude --resume), đường đó KHÔNG đẻ dòng signal nào →
# không bao giờ có mốc reset kiểu "signal do người gửi" → bộ đếm gộp cả lịch sử nhiều ngày và chặn
# oan (đo thật trên 1 cặp: 73 signal trải 10 ngày, 0 signal do người gửi → báo 59/4).
# Vòng ping-pong xấu diễn ra trong vài phút, nên cắt theo giờ là đủ bắt mà không đụng lịch sử cũ.
PAIR_SIGNAL_WINDOW_MIN = float(os.environ.get("ORCH_PAIR_SIGNAL_WINDOW_MIN", "60"))
# VÒNG VIỆC (id=94b). Mốc reset KHÔNG gắn theo "vai được người dùng chạm" mà theo CHUỖI việc:
#   người dùng gõ cho vai S  → mở vòng mới ở S
#   A gửi signal cho B       → B THỪA HƯỞNG vòng của A
# Vì luồng thật là 3 chặng: người dùng → (terminal) director → worker A → worker B. Việc mới rơi
# vào director chứ không vào worker, nên mốc gắn theo vai thì cặp worker không bao giờ thấy → hết
# ngân sách từ request trước là kẹt luôn ở request sau. Lan theo chuỗi thì việc mới giao cho orch
# mở lại ngân sách cho MỌI cặp phía sau; còn vòng lặp không ai trông (không có input người) thì mốc
# đứng yên và trần vẫn cắn ở lượt thứ PAIR_SIGNAL_CAP. Mỗi CẶP có ngân sách riêng.
# Giữ RAM: nó chỉ NỚI trần, mất khi restart cũng không sai (cửa sổ thời gian đỡ bên dưới).
_round_at: dict = {}   # (workspace_id, tên vai) → ISO ts mở vòng việc hiện tại


def _round_key(workspace_id, name):
    return (workspace_id or DEFAULT_WORKSPACE, name or "")


def note_human_touch(name, workspace_id=DEFAULT_WORKSPACE):
    """Người dùng gõ thẳng / giao việc cho vai này → MỞ VÒNG VIỆC MỚI tại vai đó."""
    if name:
        _round_at[_round_key(workspace_id, name)] = _now()


def is_user_typing(data):
    r"""Byte từ WS terminal có phải NGƯỜI đang giao việc không.

    KHÔNG được coi mọi input là người gõ. xterm.js TỰ trả lời truy vấn của chương trình TUI —
    đã đo trên đúng bản đang vendor: ESC[6n → ESC[1;1R, ESC[c → ESC[?1;2c, ESC[>c →
    ESC[>0;276;0c, ESC[5n → ESC[0n; và khi chương trình bật ESC[?1004h thì chỉ cần click ra rồi
    click lại vào cửa sổ trình duyệt là phát ESC[O / ESC[I. Mọi byte đó đều đi qua onData như
    input thật, nên trước đây mỗi cái mở một VÒNG VIỆC mới → trần ping-pong của cặp agent tự tụt
    về 1 giữa lúc không ai đụng vào terminal.

    Mốc là phím Enter ('\r'): đó mới là lúc người dùng GIAO xong một việc. Không lời đáp tự động
    nào chứa '\r' (đã đo), nên nó lọc luôn cả mũi tên, Esc, Ctrl-C — bấm mấy phím đó không phải
    là ra đề bài mới."""
    return "\r" in str(data or "")


def _session_name(session_id):
    conn = _conn()
    row = conn.execute("SELECT name FROM sessions WHERE id = ?", (session_id,)).fetchone()
    conn.close()
    return (row["name"] if row else "") or ""


class SignalPairCapExceeded(Exception):
    """enqueue_signal từ chối: cặp agent đã hết lượt trao đổi trong chu kỳ này."""


def _pair_key(a, b):
    """Khoá cặp KHÔNG thứ tự → A→B và B→A cùng một cặp."""
    return tuple(sorted((a or "", b or "")))


def pair_signal_count(from_role, to_session, workspace_id):
    """Đếm signal đã trao đổi giữa CẶP (from_role ↔ vai đích) kể từ lần NGƯỜI DÙNG chạm vào gần
    nhất. Trả (số lượt, tên vai đích).

    Quy về TÊN VAI cả hai phía: cột from_session lưu from_role (tên) còn to_session lưu session_id
    — hai đầu khác hệ, không so trực tiếp được.

    Chu kỳ tính từ mốc GẦN NHẤT trong 3 mốc, cái nào mới hơn thắng:
      1. cửa sổ PAIR_SIGNAL_WINDOW_MIN phút đổ lại (chặn gộp lịch sử cũ)
      2. VÒNG VIỆC của 1 trong 2 vai (_round_at — người dùng mở, rồi lan theo chuỗi signal)
      3. signal do người gửi (from_session ∈ _HUMAN_SENDERS) tới 1 trong 2 vai
    Duyệt từ mới về cũ, chạm mốc thì dừng đếm. Không cần bảng/cột trạng thái nào.

    Signal điều khiển '/compact' KHÔNG tính — nó là lệnh vận hành, không phải một lượt trao đổi.

    Phép đếm nằm TRỌN trong pair_counts(); hàm này chỉ tra ra một cặp. Dashboard cần con số của
    mọi cặp, mà hai bản cài đặt song song thì sớm muộn lệch nhau — và lệch ở đây nghĩa là UI báo
    1/2 trong khi backend chặn ở 2/2. Cùng một vòng quét 200 dòng nên tra một cặp không đắt hơn."""
    _ensure_db()
    conn = _conn()
    to_row = conn.execute("SELECT name FROM sessions WHERE id = ?", (to_session,)).fetchone()
    conn.close()
    to_name = (to_row["name"] if to_row else "") or ""
    return pair_counts(workspace_id).get(_pair_key(from_role, to_name), 0), to_name


def pair_counts(workspace_id):
    """{(vai_a, vai_b): số lượt} cho MỌI cặp còn trong chu kỳ hiện tại — MỘT lượt quét.

    Dashboard cần con số này cho từng cặp trên mỗi lần refresh. Gọi pair_signal_count cho
    từng cặp thì 10 agent = 45 cặp × 200 dòng mỗi lần SSE bắn — nên gộp vào một vòng.

    Dùng ĐÚNG câu truy vấn, LIMIT và ba mốc cutoff như pair_signal_count: số hiển thị phải
    khớp số đem đi chặn, lệch một cái là UI báo 1/2 trong khi backend chặn ở 2/2.
    """
    _ensure_db()
    conn = _conn()
    rows = conn.execute(
        "SELECT s.from_session AS f, sess.name AS t, s.created_at AS ts, "
        "substr(s.message, 1, 8) AS head "
        "FROM signals s LEFT JOIN sessions sess ON sess.id = s.to_session "
        "WHERE s.workspace_id = ? ORDER BY s.id DESC LIMIT 200", (workspace_id,)).fetchall()
    conn.close()
    window = (datetime.now() - timedelta(minutes=PAIR_SIGNAL_WINDOW_MIN)).isoformat()

    def cutoff(pair):
        c = window
        for role in pair:
            opened = _round_at.get(_round_key(workspace_id, role))
            if opened and opened > c:
                c = opened
        return c

    counts, closed, touched = {}, set(), set()
    for r in rows:
        f, t = r["f"] or "", r["t"] or ""
        if f in _HUMAN_SENDERS:
            # Người dùng giao việc cho t → mọi cặp chứa t bắt đầu chu kỳ mới TỪ ĐÂY; các dòng
            # cũ hơn (duyệt sau, vì đang đi từ mới về cũ) không tính nữa.
            touched.add(t)
            continue
        # t rỗng = session id đã xoay, LEFT JOIN không ra tên. Không quy được về cặp nào.
        if not f or not t or f == t:
            continue
        pair = _pair_key(f, t)
        if pair in closed or f in touched or t in touched:
            continue
        if (r["ts"] or "") < cutoff(pair):
            closed.add(pair)      # ra ngoài chu kỳ của CẶP NÀY, cặp khác vẫn đếm tiếp
            continue
        if (r["head"] or "") != "/compact":
            counts[pair] = counts.get(pair, 0) + 1
    return counts


def enqueue_signal(to_session, message, from_session="", requires_approval=0, dry_run=0,
                   workspace_id=DEFAULT_WORKSPACE):
    _ensure_db()
    # id=94: chặn NGAY Ở HÀM GHI (mọi đường vào signal đều qua đây: MCP send_signal, POST
    # /api/signals, và bất kỳ caller mới nào) thay vì rào ở từng caller — rào lẻ thì thêm đường
    # mới là thủng. Người gửi là NGƯỜI thì không chặn; '/compact' là lệnh vận hành, cũng không.
    if from_session in _HUMAN_SENDERS:
        # Người / hệ thống giao việc (chat dashboard) → mở vòng việc mới ở vai nhận.
        note_human_touch(_session_name(to_session), workspace_id)
    elif not str(message or "").startswith("/compact"):
        n, to_name = pair_signal_count(from_session, to_session, workspace_id)
        if n < PAIR_SIGNAL_CAP:
            # LAN VÒNG: đích thừa hưởng vòng của nguồn. Nhờ vậy chuỗi director→worker→worker cùng
            # một vòng, và việc mới người dùng giao cho director mở lại ngân sách cho cặp phía sau.
            src = _round_at.get(_round_key(workspace_id, from_session), "")
            if src > _round_at.get(_round_key(workspace_id, to_name), ""):
                _round_at[_round_key(workspace_id, to_name)] = src
        if n >= PAIR_SIGNAL_CAP:
            raise SignalPairCapExceeded(
                f"⛔ HẾT LƯỢT SIGNAL: '{from_session}' và '{to_name or to_session}' đã trao đổi "
                f"{n}/{PAIR_SIGNAL_CAP} lượt cho việc này. DỪNG gửi signal cho nhau. "
                f"Việc phải làm NGAY: tổng hợp kết quả đã có + nêu rõ còn vướng gì, rồi BÁO CÁO "
                f"cho NGƯỜI DÙNG bằng text trong lượt trả lời này để họ quyết định bước kế. "
                f"KHÔNG thử gửi lại, KHÔNG vòng qua agent khác để nhắn hộ. "
                f"Bộ đếm tự mở lại khi người dùng giao việc mới.")
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


# Proc claude đang chạy per session — cho /api/sessions/{sid}/kill giết run treo/chạy mãi.
# 1 proc/session (signal serialize qua per-session lock). Entry stale sau exception vẫn vô hại:
# endpoint check returncode, run mới đè entry cũ.
ACTIVE_PROCS = {}
# Session user vừa bấm kill — process_signal thấy thì KHÔNG retry run bị giết.
KILLED_SESSIONS = set()

# ─── MCP servers: đăng ký server MCP cho MỌI session claude, ngay trên dashboard ──
# Thay cho việc bắt người dùng gõ `claude mcp add` trong terminal. Scope user, tức là mọi phiên
# claude sau đó đều thấy — không thuộc workspace nào, nên nó nằm ở topbar chứ không ở canvas.
#
# Vì sao GHI THẲNG ~/.claude.json thay vì gọi CLI: server có token thì token phải đi trong header
# Authorization, mà CLI chỉ nhận header qua cờ `--header` — tức là qua ARGV. `ps` đọc được, shell
# lưu vào history. Đã ĐO shape trên máy này bằng chính CLI rồi đọc lại file:
#     "<name>": {"type": "http", "url": "…", "headers": {"Authorization": "Bearer …"}}
# Ghi thẳng cũng tránh cái bẫy commander của `claude mcp add`: `--header` là variadic nên nó NUỐT
# mọi tham số vị trí đứng sau nó — đặt sai thứ tự thì lệnh sai mà không báo lỗi gì.
MCP_TIMEOUT = float(os.environ.get("ORCH_MCP_TIMEOUT", "6"))
# Phiên bản khai lúc initialize. Cứ khai bản cũ: server trả về phiên bản CỦA NÓ trong response,
# thoả thuận xong là dùng được, còn khai bản mới hơn server thì có server từ chối thẳng.
MCP_PROTOCOL = "2024-11-05"
# Chính orchestrator cũng mount vài MCP server nội bộ. Chúng KHÔNG hiện trong modal: người dùng
# không tự đăng ký chúng ở đây, mà bấm Remove thì agent mất luôn đường signal.
MCP_OWN_PATHS = ("/signal/mcp", "/unity/mcp")
# ~/.claude.json, KHÔNG phải ~/.claude/ — CLAUDE_CONFIG_DIR không dời file này.
CLAUDE_CONFIG_FILE = Path(os.environ.get("ORCH_CLAUDE_CONFIG", str(Path.home() / ".claude.json")))
# Tên đăng ký thành KHOÁ trong ~/.claude.json. Chặn ký tự lạ để không đẻ ra khoá kỳ quặc trong
# file cấu hình của người dùng.
MCP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _claude_config():
    """Chưa có file / file hỏng đều coi như rỗng — KHÔNG được ném, vì UI gọi hàm này."""
    try:
        cfg = json.loads(CLAUDE_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _save_claude_config(cfg):
    """Ghi qua file tạm rồi os.replace. File này giữ TOÀN BỘ cấu hình Claude Code của người dùng
    (mọi project, mọi MCP server) — ghi dở dang là mất sạch, nên không ghi đè trực tiếp.
    ponytail: không khoá file. `claude` chạy song song mà cùng ghi thì một bên mất update; thêm
    lock nếu thực tế có va."""
    tmp = CLAUDE_CONFIG_FILE.with_name(CLAUDE_CONFIG_FILE.name + ".orch-tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CLAUDE_CONFIG_FILE)


def _mcp_servers():
    d = _claude_config().get("mcpServers")
    return d if isinstance(d, dict) else {}


def _entry_token(entry):
    auth = ((entry or {}).get("headers") or {}).get("Authorization") or ""
    return auth[7:] if auth.startswith("Bearer ") else ""


def _mask(token):
    """Token ĐÃ LƯU không bao giờ được trả nguyên vẹn ra khỏi tiến trình này — chỉ 4 ký tự cuối,
    đủ để người dùng nhận ra mình đã dán cái nào."""
    return ("•" * 8 + token[-4:]) if len(token or "") > 4 else ("•" * 8 if token else "")


def _count_tools(body):
    """Đếm tool trong câu trả lời tools/list. MCP streamable-http trả JSON thường HOẶC khung SSE
    ('data: {...}') tuỳ server, nên thử cả hai kiểu."""
    for chunk in [body] + [ln[5:] for ln in body.splitlines() if ln.startswith("data:")]:
        try:
            tools = ((json.loads(chunk) or {}).get("result") or {}).get("tools")
        except (ValueError, AttributeError):
            continue
        if isinstance(tools, list):
            return len(tools)
    return 0


def _probe_http_error(r):
    """Đọc mã HTTP thành (state, tools, detail), hoặc None nếu ổn."""
    if r.status_code in (401, 403):
        return "rejected", 0, f"the server refused this token (HTTP {r.status_code})"
    if r.status_code == 426:
        return "error", 0, "the server requires https (HTTP 426)"
    if not 200 <= r.status_code < 300:
        return "error", 0, f"the server answered HTTP {r.status_code}"
    return None


async def _mcp_probe(url, token):
    """Bắt tay MCP rồi gọi tools/list — BẰNG CHỨNG duy nhất rằng server sống và token dùng được.
    "Đã lưu" không phải là "đang chạy". Trả (state, tools, detail); detail đi thẳng ra UI nên
    TUYỆT ĐỐI không chứa token."""
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        async with httpx.AsyncClient(timeout=MCP_TIMEOUT) as c:
            r = await c.post(url, headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": MCP_PROTOCOL, "capabilities": {},
                           "clientInfo": {"name": "session-orchestrator", "version": "1"}}})
            bad = _probe_http_error(r)
            if bad:
                return bad
            # Server có phiên phát mcp-session-id ở đây và BẮT các lượt sau mang theo. Thiếu nó là
            # 400 "Missing session ID" — đo được với chính /signal/mcp của orchestrator. Server
            # không phiên thì không phát header này, bỏ qua là xong, không cần rẽ nhánh theo loại.
            sid = r.headers.get("mcp-session-id")
            if sid:
                headers["Mcp-Session-Id"] = sid
                await c.post(url, headers=headers,
                             json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            r = await c.post(url, headers=headers,
                             json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    except httpx.HTTPError:
        return "unreachable", 0, "could not reach the server"
    return _probe_http_error(r) or ("connected", _count_tools(r.text), "")


# ─── Neovim trong trình duyệt (card trên canvas) ──────────────────────────────
# nvim là TUI, nên card editor chạy trên ĐÚNG hạ tầng terminal đã có: PTY + xterm.js. Không
# server HTTP, không cấp cổng, không iframe khác origin, không tải 100MB server lúc mở lần đầu —
# đó là toàn bộ lý do bỏ `code serve-web`.
#
# Phiên sống trong TMUX chứ không phải PTY trần: đóng tab dashboard chỉ là detach, mở lại là
# nguyên buffer, con trỏ, undo tree. Hệ quả quan trọng hơn: orchestrator KHÔNG giữ state nào cả —
# `tmux ls` chính là sổ đăng ký, và nó sống qua cả restart orchestrator. Bản VS Code cũ giữ dict
# trong RAM, nên restart xong là mấy tiến trình serve-web thành mồ côi mà UI không còn thấy để
# đóng.
NVIM_BIN = os.environ.get("ORCH_NVIM_BIN", "nvim")
TMUX_BIN = os.environ.get("ORCH_TMUX_BIN", "tmux")
# Git UI trong card = diffview.nvim, tức là một PLUGIN CỦA CHÍNH NVIM chứ không phải chương
# trình riêng. Nên tab git KHÔNG phải cửa sổ tmux thứ hai: nó chỉ là một lệnh ex gõ vào đúng
# nvim đang mở. Ít máy móc hơn hẳn bản lazygit trước đó — một tiến trình, một cửa sổ, và diff
# mở ra ngay trong buffer nên dùng chung LSP, theme, phím tắt của người dùng.
# (Zed thì không nhúng được kiểu nào: CLI 1.15.0 không có chế độ phục vụ HTTP, mọi tuỳ chọn đều
# mở cửa sổ desktop.)
EDITOR_VIEWS = {"edit": "DiffviewClose", "git": "DiffviewOpen"}
EDITOR_PREFIX = "orch-nvim-"     # tiền tố tên phiên tmux — để không đụng phiên tmux của người dùng

# Máy không có tmux (Windows là chính) vẫn mở được card, nhưng là PTY trần: đóng tab là nvim
# chết, và "đang mở" chỉ còn là ghi nhớ trong RAM, mất khi restart.
_editors: set = set()


def _tmux():
    """Đường dẫn tmux, '' nếu máy không có."""
    return shutil.which(TMUX_BIN) or ""


def _editor_tmux_name(sid):
    # tmux cấm '.' và ':' trong tên phiên. session id là uuid nên hiếm khi dính, thay cho chắc.
    return EDITOR_PREFIX + str(sid).replace(".", "_").replace(":", "_")


async def _tmux_run(*args, capture=False):
    """Chạy tmux → (returncode, stdout). Không có tmux / spawn lỗi / quá hạn → (127, '').

    capture=False là MẶC ĐỊNH và quan trọng: `tmux new-session` khởi động tmux SERVER, mà server
    đó daemon hoá và KẾ THỪA luôn ống stdout mình mở. communicate() chờ EOF trên ống ấy, còn ống
    ấy chỉ đóng khi server chết — tức là treo vĩnh viễn, và /api/editor/open treo theo. Đã dính
    đúng bẫy này: request đứng im, không lỗi, không log. Chỉ mở ống cho lệnh THỰC SỰ cần đọc
    output (list-sessions — lệnh này không bao giờ tự dựng server).

    wait_for là chốt chặn cuối: không có lệnh tmux nào đáng chạy quá 10 giây, và treo ở đây là
    treo cả một request của dashboard."""
    tmux = _tmux()
    if not tmux:
        return 127, ""
    pipe = asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL
    try:
        proc = await asyncio.create_subprocess_exec(
            tmux, *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=pipe, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
    except (OSError, asyncio.TimeoutError):
        return 127, ""
    return proc.returncode, (out or b"").decode("utf-8", "replace")


async def editor_open_ids():
    """session_id đang có card editor. Nguồn sự thật là tmux; không có tmux thì tập trong RAM."""
    if not _tmux():
        return set(_editors)
    # rc != 0 khi CHƯA có tmux server nào chạy — đó là 'chưa mở card nào', không phải lỗi.
    _, out = await _tmux_run("list-sessions", "-F", "#{session_name}", capture=True)
    return {n[len(EDITOR_PREFIX):] for n in out.split() if n.startswith(EDITOR_PREFIX)}


def _editor_views():
    """Tab card có được. Đổi tab = gửi phím vào nvim, mà gửi phím thì phải qua tmux — không có
    tmux thì không có tab nào cả, card chỉ là một PTY nvim trần."""
    return list(EDITOR_VIEWS) if _tmux() else ["edit"]


async def editor_states():
    """Card editor đang mở, cho FE dựng node trên canvas."""
    wins = _editor_views()
    out = []
    for sid in sorted(await editor_open_ids()):
        s = get_session(sid)
        if not s:
            continue    # session bị xoá mà phiên tmux còn sót → đừng dựng card ma
        out.append({"open": True, "session": sid, "name": s["name"],
                    "cwd": (s.get("cwd") or "").strip() or str(Path.home()),
                    "persistent": bool(_tmux()), "windows": wins})
    return out


def editor_argv(session):
    """Lệnh mở nvim cho card của session này.

    Có tmux → `new-session -A`: có phiên thì attach, chưa có thì tạo. MỘT lệnh lo cả hai nên WS
    không phải hỏi trước phiên tồn tại chưa, và hai tab mở cùng lúc không đua nhau tạo trùng."""
    cwd = (session.get("cwd") or "").strip() or str(Path.home())
    tmux = _tmux()
    if not tmux:
        return [NVIM_BIN]
    return [tmux, "new-session", "-A", "-s", _editor_tmux_name(session["id"]),
            "-c", cwd, NVIM_BIN]


async def editor_start(session):
    """Mở card editor. Có tmux thì tạo phiên detached ngay để card hiện lên trước khi ai attach."""
    sid = session["id"]
    if not shutil.which(NVIM_BIN):
        raise OSError(f"could not find '{NVIM_BIN}' — install neovim or set ORCH_NVIM_BIN")
    if _tmux():
        cwd = (session.get("cwd") or "").strip() or str(Path.home())
        name = _editor_tmux_name(sid)
        rc, _ = await _tmux_run("new-session", "-d", "-s", name, "-c", cwd, "-n", "nvim", NVIM_BIN)
        # rc != 0 gần như luôn là "duplicate session" = card đã mở sẵn → coi như thành công.
        if rc != 0 and sid not in await editor_open_ids():
            raise OSError("tmux refused to start the editor session")
    else:
        _editors.add(sid)
    info = next((c for c in await editor_states() if c["session"] == sid), {"open": False})
    publish({"type": "editor", **info})
    return info


async def editor_focus(session_id, view):
    """Đổi tab card = gõ một lệnh ex vào nvim qua tmux send-keys.

    Escape đi TRƯỚC: người dùng có thể đang ở insert mode, và lúc đó ':DiffviewOpen' sẽ bị chèn
    thẳng vào file thay vì chạy. Ở normal mode Escape là no-op nên gửi thừa không hại gì.

    Không dùng RPC của nvim (--listen + --server --remote-send): sẽ phải cấp và dọn socket cho
    từng session, đổi lấy một thứ mà send-keys đã làm đủ tốt."""
    cmd = EDITOR_VIEWS.get(view)
    if not cmd or not _tmux():
        return False
    rc, _ = await _tmux_run("send-keys", "-t", f"{_editor_tmux_name(session_id)}:nvim",
                            "Escape", f":{cmd}", "Enter")
    return rc == 0


async def editor_stop(session_id=None):
    """Đóng card: giết phiên tmux (nvim chết theo). None → đóng hết. Trả số card đã đóng."""
    ids = [session_id] if session_id else sorted(await editor_open_ids())
    n = 0
    for sid in ids:
        had = sid in _editors
        _editors.discard(sid)
        rc, _ = await _tmux_run("kill-session", "-t", _editor_tmux_name(sid))
        if rc == 0 or had:
            n += 1
        publish({"type": "editor", "open": False, "session": sid})
    return n


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
    # Mức lạ (DB cũ) → clamp_effort trả '' → bỏ cờ, vì effort sai làm claude thoát ngay mã 1.
    effort = clamp_effort(session.get("effort") or DEFAULT_EFFORT, CLAUDE_MAX_EFFORT)
    if effort:
        cmd += ["--effort", effort]

    cwd = session.get("cwd") or None
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=STREAM_LIMIT,  # tránh LimitOverrunError khi 1 dòng NDJSON > 64KB
    )

    ACTIVE_PROCS[session_id] = proc  # cho nút 🛑 kill từ dashboard

    if not stream:
        try:
            stdout, stderr = await proc.communicate(input=prompt.encode("utf-8"))
        finally:
            ACTIVE_PROCS.pop(session_id, None)
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
                        await on_event("error", f"output line too large (> {STREAM_LIMIT // (1024*1024)}MB), skipped",
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
                display = [("error", f"could not parse event: {e}", {"line": _trunc(line, 500)})]
            for kind, summary, payload in display:
                try:
                    await on_event(kind, summary, payload)
                except Exception:  # noqa: BLE001 — không để lỗi UI làm hỏng run
                    pass
    finally:
        await proc.wait()
        await stderr_task
        ACTIVE_PROCS.pop(session_id, None)

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
            await on_event("error", "no 'result' event received from claude", {"stderr": err})
        return {"ok": False, "result": "No 'result' event received from claude.",
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


# Thư mục gốc mà CLI tự quét skill trong project. ĐÃ ĐO trên codex 0.147.0 (canary trong
# <cwd>/.codex/skills hiện ra ở section '## Skills', canary trong .claude/skills thì không):
# mỗi CLI CHỈ đọc thư mục của mình → phải ghi cả hai bản, không share được.
# '.claude' đứng ĐẦU = bản canon để đọc lại (_role_skill / _prepend_role / drawer Context).
CLI_SKILL_ROOTS = (".claude", ".codex")


def _skills_dir(cwd, root=CLI_SKILL_ROOTS[0]):
    """Thư mục skills của project = <cwd>/<root>/skills. cwd rỗng → fallback cạnh chương trình."""
    base = Path(cwd) if cwd else _app_dir()
    return base / root / "skills"


def _skill_path(cwd, name, root=CLI_SKILL_ROOTS[0]):
    return _skills_dir(cwd, root) / name / "SKILL.md"


def _role_skill(cwd, name):
    """Đọc SKILL của role theo convention: <cwd>/.claude/skills/<name>/SKILL.md.
    Trả '' nếu không có file (role không cần playbook riêng)."""
    try:
        return _skill_path(cwd, name).read_text(encoding="utf-8")
    except OSError:
        return ""


# Placeholder của template SKILL chưa điền, vd <MUC_TIEU>. Vừa là điều kiện nhận template hợp lệ
# (_list_skill_templates) vừa là CỜ ONE-TIME của bootstrap: còn placeholder = chưa điền → chạy;
# agent điền xong = không khớp nữa → không bao giờ chạy lại. Không cần cột DB / file cờ, sống qua
# restart orchestrator.
_PLACEHOLDER_RE = re.compile(r"<[A-Z_]+>")


def _skill_has_placeholder(cwd, name):
    """SKILL của role còn placeholder chưa điền. File không có → False (không có gì để điền)."""
    return bool(_PLACEHOLDER_RE.search(_role_skill(cwd, name)))


def _skill_frontmatter(name, content):
    """CẢ claude lẫn codex chỉ nhận SKILL.md có frontmatter YAML (name + description) — thiếu thì
    file nằm đó mà CLI không đưa vào catalog, tức là im lặng vô hiệu. init_prompt gõ tay thường
    không có → tự chèn. Description phải nói RÕ 'kích hoạt với mọi tin nhắn', vì skill là
    progressive-disclosure: CLI chỉ đọc nội dung khi description khớp việc đang làm."""
    if content.lstrip().startswith("---"):
        return content
    return (f"---\nname: {name}\ndescription: Playbook vai '{name}' trong hệ multi-agent do "
            f"orchestrator điều phối. KÍCH HOẠT với MỌI tin nhắn tới session này (chat người dùng "
            f"HOẶC signal từ agent khác).\n---\n\n{content}")


def _write_role_skill(cwd, name, content):
    """Vật thể hoá init_prompt thành SKILL của role, ghi CHO CẢ HAI CLI (xem CLI_SKILL_ROOTS).
    Ghi đè nếu đã có. content rỗng → bỏ qua (không tạo SKILL rỗng)."""
    # name đi thẳng vào path segment mà _validate_name không chặn ký tự nào → tự chặn escape ở đây,
    # chỗ duy nhất mọi caller đi qua.
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        print(f"[orchestrator] ⚠ tên vai {name!r} không dùng làm thư mục được → bỏ ghi SKILL")
        return
    if not content.strip():
        return
    # <ROLE_NAME> là placeholder DUY NHẤT orchestrator tự điền: template dùng chung được ghi vào
    # .claude/skills/<vai>/ nên tên trong frontmatter phải là tên VAI (id skill = tên thư mục),
    # và mọi ví dụ send_signal/list_agents trong playbook phải mang đúng vai đó. Điền ở đây thì
    # SKILL hợp lệ ngay từ giây đầu, không phải chờ bootstrap.
    content = content.replace("<ROLE_NAME>", name)
    content = _skill_frontmatter(name, content)
    for root in CLI_SKILL_ROOTS:
        p = _skill_path(cwd, name, root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


# Roster peer đổi liên tục (agent được spawn/gỡ bất cứ lúc nào) → KHÔNG nướng danh sách vai vào
# SKILL: đó là cache của dữ liệu động, sai ngay lần đầu có agent rời đi và không ai đi sửa lại.
# Thay bằng đúng một dòng ghim vào MỌI signal, bảo agent tự hỏi roster sống. Không bao giờ cũ,
# không cần đồng bộ chéo giữa các agent.
_PEER_RULE = ('[Peers] Cần gửi signal cho một vai mà bạn chưa chắc có thật? Gọi '
              'list_agents(from_role="<vai của bạn>") để lấy danh sách agent đang sống trong '
              'workspace. ĐỪNG nhớ tên vai từ lượt trước — danh sách trong đầu bạn là bản cũ.')

# Gửi signal giữa lượt là tự mở cửa cho một run thứ hai vào CÙNG session mình: vai kia làm xong
# sớm, báo cáo về lúc mình còn đang chạy, và hai tiến trình cùng ghi một transcript. Lock
# per-session không cứu được trường hợp agent đang chạy trong terminal PTY nhúng — PTY đó không
# đi qua lock. Rẻ nhất là ghim luật vào mọi signal, kể cả vai không có SKILL riêng.
_TIMING_RULE = ('[Thời điểm] send_signal là thao tác CUỐI của lượt. Làm XONG việc rồi mới gửi — '
                'gửi giữa chừng thì báo cáo của vai kia có thể về lúc bạn CÒN ĐANG CHẠY, mở THÊM '
                'một tiến trình trên CÙNG session bạn và hai bên cùng ghi một transcript. Nhiều '
                'signal (báo cáo + bàn giao) thì gửi liền nhau ở cuối lượt.')


def _reply_rule(name, from_role):
    """Đích báo cáo = NGƯỜI GỬI signal này, không phải một vai cố định.

    Playbook cũ dạy báo cáo về một alias cố định; alias đó chỉ resolve khi có session được đánh
    dấu orchestrator, không có thì mọi báo cáo chết lặng. Lấy from_role của chính signal thì luôn
    có đích thật, và chuỗi A→B→C tự báo cáo ngược đúng mắt xích."""
    if not from_role or from_role.strip().lower() in _HUMAN_SENDERS:
        return ("[Báo cáo] Signal này do NGƯỜI DÙNG gửi, không phải agent. Trả lời bằng text ngay "
                "trong lượt này — KHÔNG gọi send_signal để 'báo cáo' (không có agent nào để nhận).")
    return (f'[Báo cáo] Signal này do vai "{from_role}" giao. Xong việc: '
            f'send_signal(to_role="{from_role}", from_role="{name}", '
            f'message="[REPORT] <kết quả> + <bằng chứng: path/số liệu/test> + <còn hở gì>"). '
            f'Đích báo cáo LUÔN là người giao việc — không phải một vai cố định nào. '
            f'Cần bàn giao bước kế cho vai khác thì đó là signal RIÊNG, không thay cho báo cáo. '
            f'KHÔNG gửi signal về chính mình.')


def _prepend_role(cwd, name, message, from_role=""):
    """Ghim role + playbook vào MỖI signal inject → role không trôi khi history dài/compact.
    Lazy-load: chỉ SKILL của role này, không nhồi mọi skill. Không có SKILL → chỉ prepend tên role."""
    parts = []
    skill = _role_skill(cwd, name)
    if skill:
        parts.append(skill)
    # Luôn có, kể cả vai không có SKILL riêng.
    parts.append(_PEER_RULE)
    parts.append(_TIMING_RULE)
    parts.append(_reply_rule(name, from_role))
    return (f"[Role: {name}]\n[Signal from: {from_role or 'user'}]\n"
            + "\n\n---\n\n".join(parts) + f"\n\n---\n\n{message}")


# ─── Skill templates (liệt kê vai/role cho dropdown spawn) ────────────────────

# Template vai: ưu tiên thư mục CẠNH CHƯƠNG TRÌNH (người dùng bản đóng gói thêm vai của mình vào
# đó được), không có thì dùng bộ đi kèm. Chạy từ source thì hai đường là một.
TEMPLATES_DIR = (_app_dir() / ".claude" / "skills") if (_app_dir() / ".claude" / "skills").is_dir() \
    else (_bundle_dir() / ".claude" / "skills")


def _safe_template_name(name):
    """Tên template/role dùng làm PATH SEGMENT dưới TEMPLATES_DIR — chặn traversal ('..', '/').
    Ký tự đầu alnum → loại '.' '..' và mọi biến thể ẩn; phần sau cho phép . _ - như tên skill thường."""
    return bool(name) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name))


def _template_skill(name):
    """Playbook mà repo giữ sẵn cho vai `name` (TEMPLATES_DIR/<name>/SKILL.md), '' nếu không có.
    Tên vai KHÔNG hợp lệ làm path segment → '' (chặn traversal, xem _safe_template_name)."""
    if not _safe_template_name(name):
        return ""
    try:
        return (TEMPLATES_DIR / name / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return ""


def _list_skill_templates():
    """Template hợp lệ = SKILL.md có placeholder <X>. Trả [{name, description}].
    Stub rỗng (0 placeholder) bị bỏ — không phải template điền được."""
    out = []
    for d in sorted(TEMPLATES_DIR.glob("*/")):
        f = d / "SKILL.md"
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if not _PLACEHOLDER_RE.search(text):
            continue
        m = re.search(r"description:\s*>?\s*\n?\s*(.+)", text)
        out.append({"name": d.name.rstrip("/"), "description": (m.group(1).strip()[:200] if m else "")})
    return out


def _bootstrap_skill_msg(name, cwd):
    """Prompt one-time nhờ chính agent điền placeholder trong SKILL của nó từ nội dung cwd thật."""
    paths = " và ".join(str(_skill_path(cwd, name, r)) for r in CLI_SKILL_ROOTS)
    return f"""[BOOTSTRAP SKILL — chạy MỘT LẦN duy nhất, không phải việc của người dùng]
Playbook vai '{name}' của bạn còn placeholder dạng <VIẾT_HOA> chưa điền. Việc DUY NHẤT của lượt này:

1. Đọc SKILL hiện tại: {_skill_path(cwd, name)}
2. Khảo sát thư mục làm việc {cwd or '.'} vừa đủ để hiểu project: README, cấu trúc thư mục, file
   config, ngôn ngữ/framework, lệnh build/test. Không đọc tràn lan cả repo.
3. Thay MỌI placeholder <VIẾT_HOA> bằng nội dung THẬT, đúng với vai '{name}' và project vừa khảo
   sát. Giữ nguyên frontmatter YAML đầu file (--- name/description ---) và mọi mục có sẵn — chỉ
   điền chỗ trống. Không được còn placeholder nào sót lại: đó là dấu hiệu bootstrap đã xong, còn
   sót thì lần spawn sau sẽ chạy lại lượt này.
4. Ghi nội dung Y HỆT NHAU vào CẢ HAI file bằng công cụ ghi file (Write/Edit), MỖI FILE MỘT LẦN:
   {paths}
   (Claude đọc .claude, Codex đọc .codex — lệch nhau là hai CLI chạy hai playbook khác nhau.)
   ĐỪNG copy bằng lệnh shell: `cp` không có trên Windows, agent ở đó sẽ ghi hụt file thứ hai
   mà không ai biết.

KHÔNG gọi send_signal trong lượt này — lượt này không do agent nào giao, không có ai để báo cáo.
KHÔNG sửa file nào khác ngoài hai file SKILL trên. Xong thì trả lời ngắn gọn: đã điền những
placeholder nào."""


def _maybe_bootstrap_skill(session, eng_name):
    """Sau spawn: SKILL vừa ghi còn placeholder → xếp hàng signal nhờ agent tự điền từ cwd.

    CHỈ engine chạy CLI có quyền đọc/ghi file (claude, codex). Engine chỉ-API (nội dung role do
    client gửi qua init_prompt, agent không đọc được file cục bộ) phải bị loại — nó không tự điền
    được và signal sẽ đốt một run vô ích.
    One-time do chính placeholder bảo đảm (xem _PLACEHOLDER_RE) — không cần cờ riêng.
    """
    cwd = (session or {}).get("cwd") or ""
    name = (session or {}).get("name") or ""
    if eng_name not in ("claude", "codex") or not _skill_has_placeholder(cwd, name):
        return
    # from_session rỗng = người gửi (_HUMAN_SENDERS): không ăn quota PAIR_SIGNAL_CAP của cặp agent
    # nào, và mở vòng việc mới cho vai này thay vì thừa hưởng vòng của ai đó.
    enqueue_signal(session["id"], _bootstrap_skill_msg(name, cwd),
                   workspace_id=session.get("workspace_id") or DEFAULT_WORKSPACE)
    print(f"[orchestrator] bootstrap SKILL vai '{name}' (cwd={cwd}) → đã xếp hàng signal", flush=True)


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
                        model="", effort="", workspace_id=DEFAULT_WORKSPACE, engine="claude", skill=""):
    """Tạo một headless session mới bằng `claude -p`, lấy session_id, rồi register.

    model: '' = auto (claude tự chọn); hoặc alias 'opus'/'sonnet'/'haiku' / model id cụ thể.
    effort: '' = dùng ORCH_DEFAULT_EFFORT (high); hoặc low|medium|high|max.
    workspace_id: session thuộc workspace nào (nhóm logic). cwd truyền vào được tôn trọng;
        bỏ trống + workspace ≠ default → mặc định thư mục ghim của workspace.
    Dry-run: tạo session_id giả để test UI mà không gọi claude.
    """
    # Workspace = nhóm logic; cwd truyền vào ĐƯỢC TÔN TRỌNG (mỗi agent một project được).
    # Bỏ trống cwd + workspace ≠ default → mặc định về thư mục ghim của workspace.
    # (Caller API vốn trusted — API key chung; không còn ép cách ly theo thư mục workspace.)
    is_tenant = bool(workspace_id) and workspace_id != DEFAULT_WORKSPACE
    if is_tenant:
        root = workspace_root(workspace_id)
        if not root:
            return {"error": f"workspace '{workspace_id}' does not exist"}
        if not cwd:
            cwd = root
    # Vật thể hoá init_prompt thành SKILL của role (<cwd>/.claude/skills/<name>/SKILL.md) → mỗi
    # signal sau prepend lại từ file này, role không trôi. Rỗng → bỏ qua. Ghi TRƯỚC khi seed generic.
    _write_role_skill(cwd, name, skill or init_prompt)
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
        eff = clamp_effort(effort or DEFAULT_EFFORT, CLAUDE_MAX_EFFORT)
        if eff:
            cmd += ["--effort", eff]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd or None, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate(input=init_prompt.encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            return {"error": f"could not run claude: {e}"}
        if proc.returncode != 0:
            return {"error": (stderr or b"").decode("utf-8", "replace")[:500]}
        try:
            data = json.loads((stdout or b"").decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return {"error": "could not parse claude output"}
        sid = data.get("session_id")
        if not sid:
            return {"error": "claude returned no session_id"}
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
                    init_prompt="", model="", effort="", workspace_id=DEFAULT_WORKSPACE, skill=""):
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
                    init_prompt="", model="", effort="", workspace_id=DEFAULT_WORKSPACE, skill=""):
        return await spawn_session(name, project=project, cwd=cwd, allowed_tools=allowed_tools,
                                   permission_mode=permission_mode, init_prompt=init_prompt, skill=skill,
                                   model=model, effort=effort, workspace_id=workspace_id,
                                   engine=self.name)

    async def run(self, session, prompt, on_event=None, dry_run=False):
        return await _run_claude(session, prompt, on_event=on_event, dry_run=dry_run)

    def get_compact(self, session_id):
        return _extract_compact(session_id)


# ─── Codex CLI engine (chạy bằng TÀI KHOẢN ChatGPT, không tốn API credits) ────
# `codex exec` = chế độ headless của Codex CLI: chạy 1 lượt tới hết, phát JSONL ra stdout
# (--json), resume bằng `codex exec resume <thread_id>`. Auth do NGƯỜI DÙNG lo một lần
# (`codex login` → ~/.codex/auth.json); orchestrator không đụng vào và không cầm token.
#
# KHÁC Claude ở 4 điểm phải nhớ:
#  1. KHÔNG có --mcp-config per-session: MCP của codex khai trong ~/.codex/config.toml (toàn cục),
#     bằng `codex mcp add signal --url http://127.0.0.1:<ORCH_PORT>/signal/mcp` — khai 1 lần, mọi
#     session codex dùng chung. KÈM ĐIỀU KIỆN: xem _codex_mcp_ok — headless chỉ gọi được MCP khi
#     bypass hết approval.
#  2. `codex exec` đòi cwd là git repo → luôn truyền --skip-git-repo-check.
#  3. Headless mà gặp approval prompt là FAIL NGAY → phải chốt sẵn chính sách sandbox/approval.
#  4. Không ghi compact_boundary vào transcript → get_compact luôn found=False.
#
# CẢNH BÁO auth: ~/.codex/auth.json chứa access token tài khoản ChatGPT — coi như mật khẩu.
# Không commit, không copy vào image Docker.

# Model của codex là 'gpt-5.6-terra', 'gpt-5.5'… — TRÙNG không gian tên với model API của
# OpenAI. Khai TƯỜNG MINH bằng tiền tố để không mơ hồ:
#   'codex'                  → codex CLI, model do CLI tự chọn (không truyền --model)
#   'codex:gpt-5.6-terra'    → codex CLI, model đó
#   'gpt-5.6-terra'          → KHÔNG có engine chạy (nhánh này chỉ có claude + codex)
CODEX_AUTO_MODEL = "codex"
# Env phải GỠ khỏi tiến trình con. Đã đo bằng `codex doctor`: còn OPENAI_API_KEY thì codex chạy
# "API-key mode" (tính tiền credits) dù đã `codex login`; gỡ đi thì "auth mode = chatgpt".
CODEX_DROP_ENV = ("OPENAI_API_KEY", "OPENAI_BASE_URL")


def _iter_codex_events(ev):
    """Chuyển 1 event JSONL của `codex exec --json` thành list (kind, summary, payload).

    Cùng bộ kind với _iter_display_events (system|thinking|text|tool_use|tool_result|result|
    error) để timeline/SSE/run_events không cần biết engine nào đang chạy.
    Tool 2 pha: item.started → tool_use, item.completed → tool_result (khớp cặp như Claude).
    """
    if not isinstance(ev, dict):
        return [("text", _trunc(str(ev), 500), {"raw": _trunc(str(ev))})]
    t = ev.get("type")
    if t == "thread.started":
        return [("system", f"session bắt đầu · thread={ev.get('thread_id', '?')}",
                 {"subtype": "init", "thread_id": ev.get("thread_id")})]
    if t == "turn.completed":
        u = ev.get("usage") or {}
        return [("result", f"xong · {u.get('output_tokens', '?')} output tokens",
                 {"output_tokens": u.get("output_tokens"), "input_tokens": u.get("input_tokens"),
                  "cached_input_tokens": u.get("cached_input_tokens")})]
    if t == "turn.failed":
        msg = (ev.get("error") or {}).get("message") or "turn failed"
        return [("error", _trunc(msg, 500), {"error": msg})]
    if t == "error":
        msg = ev.get("message") or "unknown error"
        return [("error", _trunc(msg, 500), {"error": msg})]
    if t not in ("item.started", "item.completed"):
        return []                      # turn.started, item.updated… = nhiễu
    item = ev.get("item") or {}
    it, done = item.get("type"), t == "item.completed"
    if it == "command_execution":
        if not done:
            return [("tool_use", f"bash({_trunc(str(item.get('command') or ''), 300)})",
                     {"name": "bash", "input": {"command": item.get("command")}})]
        out = _trunc(str(item.get("aggregated_output") or ""), 400)
        rc = item.get("exit_code")
        is_err = rc not in (0, None)
        return [("tool_result", ("⚠ " if is_err else "") + out,
                 {"result": out, "exit_code": rc, "is_error": is_err})]
    if it == "mcp_tool_call":
        label = f"{item.get('server', '?')}.{item.get('tool', '?')}"
        if not done:
            args = _trunc(json.dumps(item.get("arguments") or {}, ensure_ascii=False), 300)
            return [("tool_use", f"{label}({args})", {"name": label, "input": item.get("arguments")})]
        err = item.get("error")
        err = err.get("message") if isinstance(err, dict) else err
        txt = _trunc(json.dumps(item.get("result") or {}, ensure_ascii=False), 400)
        return [("tool_result", ("⚠ " + _trunc(str(err), 400)) if err else txt,
                 {"result": txt, "is_error": bool(err)})]
    if not done:
        return []                      # các item còn lại chỉ có nội dung khi đã xong
    if it == "agent_message":
        tx = (item.get("text") or "").strip()
        return [("text", _trunc(tx, 500), {"text": tx})] if tx else []
    if it == "reasoning":
        th = (item.get("text") or "").strip()
        return [("thinking", _trunc(th, 500), {"thinking": _trunc(th)})] if th else []
    if it == "file_change":
        paths = [c.get("path") for c in (item.get("changes") or []) if isinstance(c, dict)]
        return [("tool_use", f"edit({_trunc(', '.join(p for p in paths if p), 300)})",
                 {"name": "edit", "input": {"changes": item.get("changes")}})]
    if it == "web_search":
        return [("tool_use", f"web_search({_trunc(str(item.get('query') or ''), 200)})",
                 {"name": "web_search", "input": {"query": item.get("query")}})]
    if it == "error":
        msg = item.get("message") or "unknown error"
        return [("error", _trunc(msg, 500), {"error": msg})]
    return []


def _codex_model(model):
    """Model id thật để truyền --model. 'codex' → '' (để CLI tự chọn), 'codex:X' → 'X'."""
    return (model or "").strip().split(":", 1)[1].strip() if ":" in (model or "") else ""


def codex_effort_ceiling(model):
    """Mức reasoning cao nhất model codex này nhận (xem CODEX_MAX_EFFORT)."""
    return CODEX_MAX_EFFORT.get(_codex_model(model), CODEX_MAX_EFFORT_DEFAULT)


def _codex_env():
    """Env cho tiến trình con codex — GỠ key API để codex dùng tài khoản ChatGPT (xem
    CODEX_DROP_ENV). Không đụng env của chính orchestrator."""
    return {k: v for k, v in os.environ.items() if k not in CODEX_DROP_ENV}


def terminal_argv(session, cli=""):
    """Lệnh CLI interactive cho terminal của 1 session (nút 💻 trên dashboard).

    cli rỗng/lạ → theo engine của session (suy từ model). User chọn được CLI khác qua ?cli=.
    CHỈ resume được transcript do CHÍNH CLI đó tạo — session id của claude không phải thread của
    codex và ngược lại → CLI khác engine thì mở PHIÊN MỚI trong cùng cwd (bỏ --resume) thay vì
    cố resume rồi lỗi.
    """
    eng = engine_name_of_session(session)
    sid = session["id"]
    cli = (cli or "").strip().lower()
    if cli not in ("claude", "codex"):
        cli = "codex" if eng == "codex" else "claude"
    if cli == "codex":
        return [CODEX_BIN, "resume", sid] if eng == "codex" else [CODEX_BIN]
    return [CLAUDE_BIN, "--resume", sid] if eng == "claude" else [CLAUDE_BIN]


# ─── PTY cho terminal nhúng (xterm.js ↔ CLI interactive) ──────────────────────
# claude/codex là TUI toàn màn hình: không có PTY thì chúng không nhận phím tắt, không vẽ đúng,
# và nhiều CLI còn tự tắt chế độ tương tác khi thấy stdout không phải tty. Nên phải là PTY thật,
# không thay bằng subprocess + pipe được.
#   POSIX  → pty.fork() (stdlib).
#   Windows→ ConPTY qua pywinpty (stdlib KHÔNG bọc ConPTY). Cần Windows 10 1809+.
# Hai lớp dưới đây phơi cùng 4 hàm để ws_terminal không phải rẽ nhánh theo OS.


class _PosixPty:
    """PTY qua pty.fork(). read() trả bytes, write() nhận bytes."""

    def __init__(self, argv, cwd, env):
        import pty
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child: thành CLI interactive
            try:
                os.chdir(cwd)
            except OSError:
                pass
            try:
                os.execvpe(argv[0], argv, env)
            finally:
                os._exit(1)  # execvpe fail — không được rơi ngược vào event loop của cha

    def read(self):
        try:
            return os.read(self.fd, 65536)
        except OSError:  # EIO khi child thoát — coi như EOF
            return b""

    def write(self, data):
        os.write(self.fd, data)

    def resize(self, rows, cols):
        import fcntl
        import struct
        import termios
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def close(self):
        for fn in (lambda: os.kill(self.pid, 15), lambda: os.close(self.fd),
                   lambda: os.waitpid(self.pid, os.WNOHANG)):
            try:
                fn()
            except OSError:
                pass


class _WinPty:
    """PTY qua ConPTY (pywinpty). API của nó nói chuyện bằng str nên đổi mã ở biên."""

    def __init__(self, argv, cwd, env):
        from winpty import PtyProcess
        self.p = PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(24, 80))

    def read(self):
        try:
            return (self.p.read(65536) or "").encode("utf-8", "replace")
        except EOFError:  # tiến trình con đã thoát
            return b""

    def write(self, data):
        self.p.write(data.decode("utf-8", "replace"))

    def resize(self, rows, cols):
        self.p.setwinsize(rows, cols)

    def close(self):
        try:
            self.p.terminate(force=True)
        except Exception:  # noqa: BLE001 — đã chết rồi thì thôi
            pass


def pty_backend():
    """Lớp PTY dùng được trên máy này, hoặc None kèm lý do (để /health và UI nói thật)."""
    if os.name == "posix":
        return _PosixPty, ""
    try:
        import winpty  # noqa: F401
    except ImportError:
        return None, ("pywinpty chưa có — terminal nhúng cần ConPTY. "
                      "Cài bằng `pip install pywinpty` rồi khởi động lại.")
    return _WinPty, ""


def _codex_mcp_ok(permission_mode=""):
    """Session này có gọi được MCP tool (send_signal…) không.

    ĐÃ ĐO trên 0.147.0: trong `codex exec` headless, MCP tool call CHỈ chạy khi
    --dangerously-bypass-approvals-and-sandbox. Thử hết các đường khác đều trả
    'user cancelled MCP tool call': approval_policy never / on-failure / untrusted (kể cả khi
    project đã trust_level="trusted"), sandbox read-only lẫn workspace-write, có lẫn không
    sandbox_workspace_write.network_access. Không có cờ nào bật riêng phần duyệt MCP.
    → Session sandbox = agent KHÔNG signal được. Phải báo ra timeline, không để nó im lặng
    (agent thấy tool báo cancelled rồi tự nghĩ đã bàn giao xong).
    """
    return (permission_mode or DEFAULT_PERMISSION_MODE) == "bypassPermissions"


def _codex_flags(model="", permission_mode="", effort=""):
    """Cờ dùng chung cho mọi lệnh `codex exec` (spawn lẫn resume).

    CHỈ dùng cờ có ở CẢ `codex exec` LẪN `codex exec resume` (đã đối chiếu --help của 0.147.0):
    `--full-auto` không tồn tại, và `-s/--sandbox` chỉ có ở `exec` → chính sách sandbox phải đi
    qua `-c` (có ở cả hai). Giá trị hợp lệ cũng đã đo bằng `codex debug models -c ...`.
    """
    # --skip-git-repo-check: cwd session không nhất thiết là git repo (vd thư mục workspace).
    flags = ["--json", "--skip-git-repo-check"]
    # Approval trong headless = fail ngay → chốt sẵn. Map thẳng permission_mode của orchestrator:
    # bypassPermissions (mặc định) = toàn quyền, y như claude --permission-mode bypassPermissions.
    if _codex_mcp_ok(permission_mode):
        flags.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        # Ghi được trong workspace, chạy lệnh không hỏi, nhưng vẫn trong sandbox.
        flags += ["-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"']
    real = _codex_model(model)
    if real:
        flags += ["--model", real]
    eff = clamp_effort(effort or DEFAULT_EFFORT, codex_effort_ceiling(model))
    if eff:
        flags += ["-c", f'model_reasoning_effort="{eff}"']
    return flags


async def _codex_exec(cmd, cwd, session_id="", on_event=None):
    """Chạy 1 lệnh codex exec, đọc JSONL stdout, trả dict kết quả theo contract AgentEngine.

    Luôn --json nên không có nhánh "không stream" như Claude: on_event=None chỉ là không đẩy
    event, phần parse vẫn chạy. session_id != "" thì đăng ký ACTIVE_PROCS cho nút 🛑 kill.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd or None, env=_codex_env(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT)          # aggregated_output có thể làm 1 dòng JSONL rất lớn
    except (FileNotFoundError, NotADirectoryError, PermissionError) as e:
        err = (f"không chạy được codex CLI ('{CODEX_BIN}'): {e}. "
               "Cài `npm i -g @openai/codex` rồi `codex login` bằng tài khoản ChatGPT.")
        if on_event:
            await on_event("error", _trunc(err, 500), {"error": str(e)})
        return {"ok": False, "result": err, "session_id": session_id, "tokens": 0,
                "raw": {"engine": "codex", "error": "codex_not_found"}}
    if session_id:
        ACTIVE_PROCS[session_id] = proc

    thread_id, text, tokens, failed = "", "", 0, ""
    stderr_chunks: list[bytes] = []

    async def _drain_stderr():
        while True:
            try:
                raw = await proc.stderr.readline()
            except (asyncio.LimitOverrunError, ValueError):
                continue
            if not raw:
                break
            stderr_chunks.append(raw)

    stderr_task = asyncio.create_task(_drain_stderr())
    try:
        while True:
            try:
                raw = await proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError):
                continue                 # dòng vượt STREAM_LIMIT → bỏ mảnh, đọc tiếp
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue                 # codex có thể in dòng người-đọc lẫn vào stdout
            if not isinstance(ev, dict):
                continue
            t = ev.get("type")
            if t == "thread.started":
                thread_id = ev.get("thread_id") or thread_id
            elif t == "turn.completed":
                tokens = int((ev.get("usage") or {}).get("output_tokens", 0) or 0)
            elif t == "turn.failed":
                failed = (ev.get("error") or {}).get("message") or "turn failed"
            elif t == "error":
                failed = ev.get("message") or "unknown error"
            elif t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
                # Lượt có thể có nhiều agent_message; câu CUỐI là kết quả trả cho người gọi.
                text = (ev["item"].get("text") or "").strip() or text
            if on_event:
                for kind, summary, payload in _iter_codex_events(ev):
                    try:
                        await on_event(kind, summary, payload)
                    except Exception:  # noqa: BLE001 — lỗi UI không được giết run
                        pass
    finally:
        await proc.wait()
        await stderr_task
        if session_id:
            ACTIVE_PROCS.pop(session_id, None)

    stderr_txt = b"".join(stderr_chunks).decode("utf-8", "replace").strip()[:2000]
    ok = proc.returncode == 0 and not failed
    if not ok and not text:
        # codex chết trước khi phát event nào (vd chưa login, --model lạ): stderr là lý do thật.
        text = failed or stderr_txt or f"codex exited với mã {proc.returncode}"
        if on_event and not failed:      # failed đã được _iter_codex_events phát rồi
            await on_event("error", _trunc(text, 500),
                           {"stderr": stderr_txt, "returncode": proc.returncode})
    return {"ok": ok, "result": text, "session_id": thread_id or session_id, "tokens": tokens,
            "raw": {"engine": "codex", "returncode": proc.returncode, "thread_id": thread_id,
                    "stderr": "" if ok else stderr_txt}}


class CodexEngine(AgentEngine):
    """Engine chạy qua `codex exec` (Codex CLI). session id = thread_id của codex."""

    name = "codex"

    async def spawn(self, name, project="", cwd="", allowed_tools=None, permission_mode="",
                    init_prompt="", model="", effort="", workspace_id=DEFAULT_WORKSPACE, skill=""):
        # Ghim cwd theo workspace y hệt Claude (cwd truyền vào được tôn trọng).
        if bool(workspace_id) and workspace_id != DEFAULT_WORKSPACE:
            root = workspace_root(workspace_id)
            if not root:
                return {"error": f"workspace '{workspace_id}' does not exist"}
            if not cwd:
                cwd = root
        # Vật thể hoá role thành SKILL: ghi cả .codex/skills (codex tự quét, ĐÃ ĐO) lẫn
        # .claude/skills — bản .claude còn là nguồn _prepend_role đọc để nhồi vào từng signal.
        _write_role_skill(cwd, name, skill or init_prompt)
        prompt = _build_init_prompt(name, init_prompt, workspace_id)
        model = model or CODEX_AUTO_MODEL
        if DRY_RUN:
            sid = f"codex-dry-{name}-{datetime.now().strftime('%H%M%S%f')}"
        else:
            res = await _codex_exec(
                [CODEX_BIN, "exec", *_codex_flags(model, permission_mode, effort), "--", prompt],
                cwd)
            sid = res["raw"].get("thread_id")
            if not sid:
                return {"error": res.get("result") or "codex returned no thread_id"}
        register_session(sid, name, project, cwd, allowed_tools or [], permission_mode,
                         model, effort, workspace_id, self.name)
        return get_session(sid)

    async def run(self, session, prompt, on_event=None, dry_run=False):
        sid = session["id"]
        if DRY_RUN or dry_run:
            if on_event:
                await on_event("text", f"[dry-run] would inject: {_trunc(prompt, 300)}", {"dry_run": True})
            return {"ok": True, "result": f"[dry-run] would inject to {session['name']}: {prompt}",
                    "session_id": sid, "tokens": 0, "raw": {"dry_run": True, "engine": "codex"}}
        if on_event and not _codex_mcp_ok(session.get("permission_mode", "")):
            await on_event("system",
                           "⚠ session sandbox: codex sẽ HỦY mọi MCP tool call (kể cả send_signal). "
                           "Cần signal thì spawn lại với permission_mode=bypassPermissions.",
                           {"subtype": "codex_mcp_blocked"})
        # Prompt là positional SAU '--' để prompt bắt đầu bằng '-' (vd frontmatter '---') không bị
        # nuốt thành cờ — vai trò y như stdin bên _run_claude.
        cmd = [CODEX_BIN, "exec", "resume", sid,
               *_codex_flags(session.get("model", ""), session.get("permission_mode", ""),
                             session.get("effort", "")),
               "--", prompt]
        res = await _codex_exec(cmd, session.get("cwd") or "", sid, on_event)
        res["session_id"] = sid   # resume giữ nguyên thread → không để id trôi khỏi DB
        return res

    def get_compact(self, session_id):
        # Codex tự nén context bên trong, KHÔNG ghi compact_boundary ra transcript như Claude →
        # không trích được. Trả found=False để UI hiện "chưa có", không giả vờ có dữ liệu.
        return {"found": False, "reason": "engine codex không expose compact summary"}


# Registry engine + resolver. Thêm engine mới = thêm 1 dòng vào ENGINES.
ENGINES = {
    "claude": ClaudeEngine(),
    "codex": CodexEngine(),
}
DEFAULT_ENGINE = "claude"


def engine_from_model(model):
    """Suy tên engine ('claude'|'codex') TỪ tên model. FE chỉ cần gửi 'model', không cần gửi
    'engine' — service tự chọn. 'codex' hoặc 'codex:<model>' → codex CLI (tài khoản ChatGPT);
    MỌI thứ còn lại (kể cả rỗng và model lạ) → claude CLI, vì nhánh này không có engine nào khác.

    Tiền tố 'codex:' là BẮT BUỘC cho model cụ thể: slug của codex ('gpt-5.6-terra'…) cũng là tên
    model API hợp lệ, không có tiền tố thì không phân biệt được ý người dùng."""
    m = (model or "").strip().lower()
    return "codex" if m == "codex" or m.startswith("codex:") else "claude"


def resolve_engine_name(body):
    """Chọn tên engine cho 1 request spawn/register. Ưu tiên 'engine' tường minh nếu client gửi
    (tương thích ngược); nếu KHÔNG gửi engine → tự suy từ 'model' (engine_from_model)."""
    explicit = (body.get("engine") or "").strip()
    if explicit:
        return explicit  # tôn trọng client; hợp lệ hay không sẽ được validate ở tầng API
    return engine_from_model(body.get("model", ""))


def engine_name_of_session(session):
    """Tên engine của 1 session ĐÃ TỒN TẠI — LUÔN suy từ 'model' (model = nguồn sự thật duy nhất).
    KHÔNG đọc cột 'engine' trong DB: cột đó có thể ghi cứng sai (vd session cũ spawn trước khi có
    tự-suy-engine). Suy từ model đảm bảo nhất quán với /api/spawn (cũng tự suy từ model)."""
    return engine_from_model(session.get("model", ""))


def engine_for(session_or_name):
    """Trả instance engine để CHẠY 1 session (dict) hoặc theo tên engine (str).
    - dict (session): LUÔN suy từ 'model' (engine_name_of_session) — model là nguồn sự thật duy
      nhất, KHÔNG tin cột 'engine' (có thể ghi cứng sai).
    - str (tên engine): tra thẳng ENGINES; tên lạ/rỗng → engine mặc định (claude)."""
    name = engine_name_of_session(session_or_name) if isinstance(session_or_name, dict) \
        else (session_or_name or DEFAULT_ENGINE)
    return ENGINES.get(name, ENGINES[DEFAULT_ENGINE])


# ─── Tìm thư mục theo tên (ô Working dir của form spawn) ─────────────────────

# Thư mục KHÔNG bao giờ là cwd của agent nhưng chứa hàng vạn entry — bỏ qua để tìm kiếm khỏi
# chết chìm trong node_modules. Thư mục ẩn (.git, .venv, .cache…) bỏ theo tiền tố '.'.
FS_SKIP_DIRS = frozenset({"node_modules", "__pycache__", "venv", "env", "dist", "build",
                          "target", "vendor", "Pods", "site-packages", "Library"})


def _search_dirs(root, q, depth=4, cap=60, max_visit=8000):
    """Tìm thư mục có TÊN chứa `q` (không phân biệt hoa thường), quét rộng-trước từ `root`.

    Rộng-trước để kết quả gần root nổi lên trước — gõ 'alone' phải ra ~/alone chứ không phải
    một thư mục con sâu 4 tầng trùng tên.
    # ponytail: quét đồng bộ, chặn bằng depth/cap/max_visit. Cây lớn hơn thì đổi sang index
    # (mlocate/fd) — đừng nới trần, vì handler này chạy trong event loop.
    """
    q = q.lower()
    out, seen = [], 0
    queue = [(root, 0)]
    while queue and len(out) < cap and seen < max_visit:
        cur, d = queue.pop(0)
        try:
            entries = sorted((e for e in os.scandir(cur) if e.is_dir(follow_symlinks=False)),
                             key=lambda e: e.name.lower())
        except (OSError, PermissionError):
            continue
        for e in entries:
            seen += 1
            if e.name.startswith(".") or e.name in FS_SKIP_DIRS:
                continue
            if q in e.name.lower() and len(out) < cap:
                out.append(e.path)
            if d + 1 < depth:
                queue.append((e.path, d + 1))
    return out


# ─── Tool discovery (built-in + MCP servers của project) ──────────────────────

BUILTIN_TOOLS = ["Task", "Bash", "Glob", "Grep", "LS", "Read", "Edit", "MultiEdit",
                 "Write", "NotebookEdit", "WebFetch", "WebSearch", "TodoWrite"]


def _read_mcp_servers(cwd):
    """Đọc MCP servers cấu hình cho project: user scope (~/.claude.json mcpServers),
    local scope (projects[cwd].mcpServers), project scope (<cwd>/.mcp.json)."""
    servers = {}
    try:
        # encoding="utf-8": hai file JSON này do CLI ghi ra bằng UTF-8 và chứa đường dẫn project.
        # Thiếu nó thì trên Windows read_text ném UnicodeDecodeError, bị `except` bên dưới nuốt,
        # và tool picker lặng lẽ báo không có MCP server nào — không một dòng lỗi.
        data = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
        servers.update(data.get("mcpServers") or {})
        if cwd:
            proj = (data.get("projects") or {}).get(cwd, {})
            servers.update(proj.get("mcpServers") or {})
    except Exception:  # noqa: BLE001
        pass
    if cwd:
        try:
            data = json.loads((Path(cwd) / ".mcp.json").read_text(encoding="utf-8"))
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
# Ctrl-C: uvicorn CHỜ mọi response đang dở chạy xong ("Waiting for connections to close").
# Stream SSE của dashboard thì không bao giờ xong — vòng lặp chỉ thoát khi CLIENT ngắt, mà lúc
# tắt máy client vẫn đang mở tab. Kết quả là treo tới khi force quit. Cách rẻ nhất: bắn một
# sentinel None vào hàng đợi của từng subscriber để chính q.get() đang chờ trả về ngay.
_stopping = False


def _stop_streams():
    """Đánh thức mọi SSE subscriber để generator kết thúc → uvicorn đóng được response."""
    global _stopping
    _stopping = True
    for q, _ in list(_subscribers):
        try:
            q.put_nowait(None)
        except Exception:  # noqa: BLE001
            pass
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
                   {"error": "session does not exist"}, "error", 0, _now(), _now(), wsid)
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
            # from_session lưu TÊN VAI người gửi (xem enqueue_signal) → đưa thẳng vào prompt để
            # agent biết báo cáo ngược cho ai. Rỗng/'user' = người dùng chat, không phải agent.
            inject_msg = _prepend_role(target.get("cwd", ""), target["name"], signal["message"],
                                       signal.get("from_session", ""))
            while True:
                try:
                    res = await engine.run(target, inject_msg, on_event=on_event, dry_run=dry)
                except Exception as e:  # noqa: BLE001
                    res = {"ok": False, "result": f"exception: {e}", "session_id": target["id"], "tokens": 0, "raw": {}}
                    await on_event("error", f"exception: {e}", {"error": str(e)})
                # User bấm 🛑 kill → run chết chủ động, KHÔNG retry (retry = tự chạy lại cái vừa giết).
                if res.get("ok") or attempts >= MAX_RETRIES or target["id"] in KILLED_SESSIONS:
                    break
                attempts += 1
                await on_event("error", f"retry {attempts}/{MAX_RETRIES} after failure", {"attempt": attempts})
                await asyncio.sleep(RETRY_BACKOFF * attempts)
            ended = _now()

            was_killed = target["id"] in KILLED_SESSIONS
            KILLED_SESSIONS.discard(target["id"])
            status = "ok" if res.get("ok") else "error"
            finish_run(run_id, res.get("raw", {}), status, res.get("tokens", 0), ended)
            final = "done" if res.get("ok") else "failed"
            fail_reason = "" if res.get("ok") else \
                ("run bị kill thủ công từ dashboard" if was_killed else _trunc(res.get("result", ""), 300))
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
            return {"signal": signal["id"], "status": final, "result": res.get("result", ""),
                    "tokens": res.get("tokens", 0), "run_id": run_id}


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


# ─── Chat API tương thích OpenAI (/v1) ───────────────────────────────────────
# Để app ngoài (SDK openai, LangChain, n8n…) chat thẳng với agent trong orchestrator mà không
# phải học API riêng: chỉ đổi base_url + model là chạy.
#
# KHÁC OpenAI ở 2 chỗ PHẢI biết trước khi dùng, không phải chi tiết vặt:
#  1. CÓ TRẠNG THÁI. OpenAI stateless (client gửi lại cả mảng messages mỗi lượt); ở đây ngữ cảnh
#     nằm trong transcript của CLI phía server. Nên chỉ phần MỚI kể từ lượt assistant gần nhất
#     được inject (xem _chat_prompt) — gửi lại cả lịch sử là nhân đôi, tốn token và làm agent rối.
#  2. 1 LƯỢT = 1 RUN THẬT của agent: đi qua khoá session, trần run/ngày, audit, run_events. Hai
#     request tới cùng agent XẾP HÀNG chứ không chạy song song. Lượt có thể kéo dài phút.
CHAT_TIMEOUT = float(os.environ.get("ORCH_CHAT_TIMEOUT", "900"))  # trần 1 lượt chat (giây)
# model = "<workspace_id>/<agent_alias>". SDK OpenAI chuẩn CHỈ gửi được 'model', nên đây là đường
# duy nhất để client không-sửa-code chọn agent; agent_alias/workspace_id rời vẫn nhận (ưu tiên hơn).
CHAT_MODEL_SEP = "/"


def _msg_text(content):
    """content của 1 message OpenAI → text. Nhận cả dạng chuỗi lẫn mảng block (vision format);
    block không phải text (image_url…) bị bỏ vì agent CLI nhận prompt text."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


def _chat_prompt(messages):
    """messages OpenAI → 1 prompt inject cho agent.

    Lấy phần MỚI kể từ lượt 'assistant' gần nhất, không lấy cả mảng: session giữ ngữ cảnh phía
    server rồi. Lượt đầu → gồm cả system + user (client muốn áp instruction thì nó tới được);
    lượt sau → chỉ đúng tin nhắn mới. Không đoán mò, không lặp lịch sử."""
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    last_ai = max((i for i, m in enumerate(msgs) if m.get("role") == "assistant"), default=-1)
    parts = []
    for m in msgs[last_ai + 1:]:
        text = _msg_text(m.get("content"))
        if not text:
            continue
        role = m.get("role", "user")
        parts.append(text if role == "user" else f"[{role}]\n{text}")
    return "\n\n".join(parts).strip()


def _chat_agent_ref(body, params=None):
    """(agent_alias, workspace_id) lấy từ body → query → field 'model'. Trả workspace rỗng thì
    caller tự áp DEFAULT_WORKSPACE."""
    params = params or {}
    alias = str(body.get("agent_alias") or params.get("agent_alias") or "").strip()
    wsid = str(body.get("workspace_id") or params.get("workspace_id") or "").strip()
    model = str(body.get("model") or "").strip()
    if not alias and model:
        head, sep, tail = model.partition(CHAT_MODEL_SEP)
        alias, wsid = (tail.strip(), wsid or head.strip()) if sep else (model, wsid)
    return alias, wsid


def _chat_id():
    return "chatcmpl-" + secrets.token_hex(12)


def _chat_created():
    return int(datetime.now().timestamp())


def _chat_chunk(cid, model, created, delta=None, finish=None, usage=None):
    """1 chunk SSE dạng chat.completion.chunk."""
    out = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
           "choices": [{"index": 0, "delta": delta if delta is not None else {},
                        "finish_reason": finish}]}
    if usage is not None:
        out["usage"] = usage
    return out


def _chat_usage(prompt, text, tokens):
    """usage: completion_tokens là số THẬT engine báo; prompt_tokens ước lượng 4 ký tự/token —
    orchestrator không đếm được prompt sau khi CLI ghép ngữ cảnh. Ghi rõ để đừng dùng để tính tiền."""
    ptok = max(1, len(prompt) // 4)
    ctok = int(tokens or 0) or max(1, len(text) // 4)
    return {"prompt_tokens": ptok, "completion_tokens": ctok, "total_tokens": ptok + ctok,
            "estimated": True}


async def chat_run_stream(session, prompt, workspace_id):
    """Chạy 1 lượt chat với agent, YIELD ('text', <mảnh>) theo tiến độ rồi ('end', <dict kết quả>).

    Đi qua ĐÚNG đường signal của orchestrator (khoá session, trần run, audit, run_events) — không
    gọi thẳng engine, để chat qua API và chat qua dashboard chịu chung một luật.
    from_session='user' → được tính là NGƯỜI dùng chạm: mở vòng việc mới, không dính trần
    ping-pong id=94 (trần đó để chặn agent nhắn lòng vòng với nhau, không phải chặn người).
    """
    try:
        sig_id = enqueue_signal(session["id"], prompt, "user", 0, 0, workspace_id)
    except SignalPairCapExceeded as e:
        yield ("end", {"status": "blocked", "result": str(e), "tokens": 0})
        return
    # Ghi _inflight NGAY (không await xen giữa) để run_loop không nhặt trùng signal này.
    _inflight.add(sig_id)
    sig_row = {"id": sig_id, "to_session": session["id"], "message": prompt,
               "workspace_id": workspace_id, "requires_approval": 0, "dry_run": 0}

    async def _run():
        try:
            return await process_signal(sig_row)
        finally:
            _inflight.discard(sig_id)

    q: asyncio.Queue = asyncio.Queue()
    sub = (q, None)          # None = nhận mọi event; lọc theo run_id của chính lượt này bên dưới
    _subscribers.add(sub)
    task = asyncio.create_task(_run())
    my_run, sent_any = None, False
    try:
        deadline = asyncio.get_running_loop().time() + CHAT_TIMEOUT
        while True:
            if task.done() and q.empty():
                break
            if asyncio.get_running_loop().time() > deadline:
                task.cancel()
                yield ("end", {"status": "timeout", "tokens": 0,
                               "result": f"quá {CHAT_TIMEOUT:.0f}s chưa xong — agent vẫn chạy tiếp "
                                         f"phía server, xem run #{my_run} trên dashboard."})
                return
            try:
                ev = await asyncio.wait_for(q.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            # run_event KHÔNG mang signal id → bám theo run_id lấy từ run_start của chính signal này.
            # Thiếu bước này là hứng nhầm text của agent khác đang chạy song song.
            if ev.get("type") == "run_start" and ev.get("signal") == sig_id:
                my_run = ev.get("run_id")
            elif (ev.get("type") == "run_event" and my_run is not None
                  and ev.get("run_id") == my_run and ev.get("kind") == "text" and ev.get("result")):
                sent_any = True
                yield ("text", ev["result"])
    finally:
        _subscribers.discard(sub)

    res = task.result() if not task.cancelled() else {"status": "failed", "result": "bị huỷ"}
    res = res or {"status": "failed", "result": "không chạy được"}
    # Chưa phát mảnh nào (engine không stream, hoặc lỗi) → trả nguyên kết quả cuối làm 1 mảnh.
    if not sent_any and res.get("result"):
        yield ("text", res["result"])
    yield ("end", res)


# ─── OpenAPI: mô tả /v1 cho app ngoài (GET /openapi.json, xem ở GET /docs) ────
# CHỈ đặc tả /v1 + vài endpoint control cần để dựng/quản agent. KHÔNG đặc tả toàn bộ ~40 route
# nội bộ của dashboard: chúng đổi theo UI, viết vào đây là cầm chắc tài liệu sai sự thật.
# check_openapi.py đối chiếu mọi path ở đây với bảng route thật → spec lệch là test đỏ.

_OA_MSG = {"type": "object", "properties": {
    "role": {"type": "string", "enum": ["system", "user", "assistant"]},
    "content": {"description": "A string, or an OpenAI-style array of {type:'text',text:...} blocks"}},
    "required": ["role", "content"]}
_OA_ERR = {"type": "object", "properties": {"error": {"type": "object", "properties": {
    "message": {"type": "string"}, "type": {"type": "string"}, "code": {"type": "string"}}}}}
_OA_ERR_RESP = {"description": "Error (OpenAI shape)",
                "content": {"application/json": {"schema": _OA_ERR}}}


def _oa_json(schema=None, desc=""):
    return {"description": desc, "content": {"application/json": {"schema": schema or {"type": "object"}}}}


def _oa_query(name, desc=""):
    return {"name": name, "in": "query", "schema": {"type": "string"}, "description": desc}


def _oa_intro():
    """Phần mô tả ở đầu trang /docs — nơi ghi 2 điểm KHÁC OpenAI mà app ngoài phải biết trước."""
    return (
        "Talk to an orchestrated agent through an **OpenAI-compatible API** — an existing app "
        "only has to change its `base_url`.\n\n"
        "```python\nfrom openai import OpenAI\n"
        f"cli = OpenAI(base_url='http://{ORCH_HOST}:{ORCH_PORT}/v1', api_key='<ORCH_API_KEY>')\n"
        "cli.chat.completions.create(\n"
        "    model='<workspace_id>/<agent_alias>',\n"
        "    messages=[{'role': 'user', 'content': 'hello'}], stream=True)\n```\n\n"
        "### Two ways this differs from OpenAI — know these before you build\n"
        "1. **It is stateful.** OpenAI is stateless: the client resends the whole `messages` array "
        "every turn. Here the conversation lives in the CLI's own transcript on the server, so only "
        "the part **new since the last `assistant` message** is sent to the agent. Resending the "
        "history duplicates context rather than restoring it.\n"
        "2. **One request is one real agent run**: it goes through the session lock, the daily run "
        "cap and the audit log. Two requests to the SAME agent **queue** rather than run in "
        "parallel, and a single turn can take minutes "
        f"(`ORCH_CHAT_TIMEOUT`, currently {CHAT_TIMEOUT:.0f}s).\n\n"
        "### Choosing an agent\n"
        "`agent_alias` and `workspace_id` are accepted in the body, as query parameters, or packed "
        "into `model` as `\"<workspace_id>/<agent_alias>\"` (the OpenAI SDKs can only send "
        "`model`).\n\n"
        "### Authentication\n"
        "Enabled by setting the `ORCH_API_KEY` environment variable. Send it as "
        "`Authorization: Bearer <key>`, `X-API-Key`, or `?api_key=`. Leaving it unset means any "
        "client can drive your agents — only reasonable when bound to localhost.\n\n"
        "### Out of scope here\n"
        "The dashboard uses many more `/api/*` routes (signals, runs, terminal…). They change with "
        "the UI and are deliberately not specified.")


def _oa_chat_body():
    return {"type": "object", "required": ["messages"], "properties": {
        "model": {"type": "string", "examples": ["ws_ab12/game-artist", "game-artist"],
                  "description": "`<workspace_id>/<agent_alias>`, or just the alias"},
        "agent_alias": {"type": "string", "description": "Takes precedence over `model`"},
        "workspace_id": {"type": "string", "description": "Empty means 'default'"},
        "messages": {"type": "array", "minItems": 1, "items": _OA_MSG},
        "stream": {"type": "boolean", "default": False},
        "stream_options": {"type": "object",
                           "properties": {"include_usage": {"type": "boolean"}}}}}


def _oa_spawn_body():
    return {"type": "object", "required": ["name"], "properties": {
        "name": {"type": "string", "description": "the agent's alias"},
        "workspace_id": {"type": "string"},
        "cwd": {"type": "string", "description": "project folder the agent works in"},
        "model": {"type": "string",
                  "description": "'' or opus/sonnet/haiku… → Claude CLI; "
                                 "'codex' / 'codex:<slug>' → Codex CLI"},
        "effort": {"type": "string", "enum": list(EFFORT_LADDER),
                   "description": "a level above the engine/model ceiling is clamped down automatically"},
        "template": {"type": "string",
                     "description": "template under .claude/skills/ to seed the playbook from "
                                    "(see GET /api/skills/templates); empty falls back to 'name'"},
        "init_prompt": {"type": "string",
                        "description": "playbook written out in full, saved as SKILL.md in cwd. "
                                       "Takes precedence over 'template'"},
        "permission_mode": {"type": "string",
                            "description": "bypassPermissions (the default) grants full access"}}}


def _oa_usage_schema():
    return {"type": "object", "properties": {
        "prompt_tokens": {"type": "integer"},
        "completion_tokens": {"type": "integer", "description": "the REAL count reported by the engine"},
        "estimated": {"const": True,
                      "description": "prompt_tokens is an ESTIMATE (4 chars/token) — "
                                     "do not bill from it"}}}


def _oa_completion_schema():
    return {"type": "object", "properties": {
        "id": {"type": "string"}, "object": {"const": "chat.completion"},
        "created": {"type": "integer"}, "model": {"type": "string"},
        "choices": {"type": "array", "items": {"type": "object"}},
        "usage": _oa_usage_schema()}}


def _oa_path_chat():
    ok = _oa_json(_oa_completion_schema(), "The agent's reply")
    ok["content"]["text/event-stream"] = {
        "schema": {"type": "string"},
        "example": 'data: {"object":"chat.completion.chunk",...}\n\ndata: [DONE]\n\n'}
    return {"post": {
        "tags": ["chat"], "summary": "Chat with one agent (streaming or not)",
        "description": "Every request is one real agent run. `stream:true` yields SSE "
                       "`chat.completion.chunk` frames, ending with `data: [DONE]`.",
        "parameters": [_oa_query("agent_alias", "Alternative to the body field"),
                       _oa_query("workspace_id")],
        "requestBody": {"required": True,
                        "content": {"application/json": {"schema": _oa_chat_body()}}},
        "responses": {"200": ok,
                      "400": _OA_ERR_RESP, "401": _OA_ERR_RESP, "404": _OA_ERR_RESP,
                      "409": dict(_OA_ERR_RESP, description="Agent is paused/stopped, or the "
                                                            "workspace is suspended"),
                      "502": dict(_OA_ERR_RESP, description="The agent run failed")}}}


def openapi_spec():
    """Đặc tả OpenAPI 3.1. Dựng trong hàm để hằng runtime (host/port/timeout) luôn khớp thực tế."""
    return {
        "openapi": "3.1.0",
        "info": {"title": "Session Orchestrator API", "version": "1.0.0",
                 "description": _oa_intro()},
        "servers": [{"url": f"http://{ORCH_HOST}:{ORCH_PORT}"}],
        "security": [{"bearerAuth": []}, {"apiKeyHeader": []}],
        "components": {"securitySchemes": {
            "bearerAuth": {"type": "http", "scheme": "bearer"},
            "apiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}},
        "tags": [{"name": "chat", "description": "OpenAI-compatible"},
                 {"name": "agents", "description": "Create and inspect agents"}],
        "paths": {
            "/v1/chat/completions": _oa_path_chat(),
            "/v1/models": {"get": {
                "tags": ["chat"], "summary": "List agents as OpenAI models",
                "parameters": [_oa_query("workspace_id", "Empty means every workspace")],
                "responses": {"200": _oa_json(desc="id = `<workspace_id>/<agent_alias>`")}}},
            "/api/sessions": {"get": {
                "tags": ["agents"], "summary": "List agents (native shape, all fields)",
                "parameters": [_oa_query("workspace_id")],
                "responses": {"200": _oa_json({"type": "array"}, "Array of sessions")}}},
            "/api/sessions/spawn": {"post": {
                "tags": ["agents"], "summary": "Create an agent (runs the CLI to obtain a session id)",
                "requestBody": {"required": True,
                                "content": {"application/json": {"schema": _oa_spawn_body()}}},
                "responses": {"200": _oa_json(desc="The session just created"),
                              "400": _oa_json(desc="Missing name, or unsupported engine"),
                              "500": _oa_json(desc="The CLI returned no session id")}}},
            "/api/workspaces": {
                "get": {"tags": ["agents"], "summary": "List workspaces",
                        "responses": {"200": _oa_json({"type": "array"}, "Array of workspaces")}},
                "post": {"tags": ["agents"], "summary": "Create a workspace",
                         "requestBody": {"content": {"application/json": {"schema": {
                             "type": "object",
                             "properties": {"name": {"type": "string"}}}}}},
                         "responses": {"200": _oa_json(desc="The workspace just created")}}},
            "/health": {"get": {"summary": "Liveness check plus the running configuration", "security": [],
                                "responses": {"200": _oa_json(desc="ok")}}},
        },
    }



# Không có mạng thì trang báo rõ và chỉ sang /openapi.json (import được vào Postman/Insomnia).
_DOCS_HTML = """<!doctype html><html lang="vi"><head><meta charset="utf-8">
<title>Session Orchestrator — API</title><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
<style>body{margin:0}#off{display:none;font:14px/1.6 system-ui;padding:32px;max-width:640px}
code{background:#eee;padding:2px 5px;border-radius:4px}</style></head><body>
<div id="swagger"></div>
<div id="off"><h2>Swagger UI could not load</h2><p>This page pulls Swagger UI from a CDN, so it
needs network access. Offline, use the spec directly:
<a href="/openapi.json"><code>/openapi.json</code></a> — it imports into Postman, Insomnia or
<code>editor.swagger.io</code>.</p></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"
        onerror="document.getElementById('off').style.display='block'"></script>
<script>window.onload=function(){ if(!window.SwaggerUIBundle) return;
  SwaggerUIBundle({url:"/openapi.json",dom_id:"#swagger",deepLinking:true}); };</script>
</body></html>"""


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
        # embedded_terminal: CI Windows kiểm tra cờ này để bắt trường hợp pywinpty không được
        # đóng gói vào binary — lỗi đó chỉ lộ ra khi user bấm 💻, quá muộn.
        _pty, _pty_why = pty_backend()
        return JSONResponse({"status": "ok", "server": "Session-Orchestrator",
                             "dry_run": DRY_RUN,
                             "embedded_terminal": _pty is not None,
                             "embedded_terminal_reason": _pty_why,
                             "default_effort": DEFAULT_EFFORT,
                             "daily_allow_step": DAILY_ALLOW_STEP,
                             # Mẫu số cho "3/4" ở inspector — UI không tự đoán được trần.
                             "pair_signal_cap": PAIR_SIGNAL_CAP,
                             "pair_signal_window_min": PAIR_SIGNAL_WINDOW_MIN,
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
            return JSONResponse({"error": "name is required"}, status_code=400)
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
        # Ngân sách ping-pong: tính MỘT lần cho cả workspace rồi phát cho từng vai, thay vì
        # thêm một endpoint nữa cho dashboard phải fetch (2 pane mở cùng lúc đã sát trần 6
        # kết nối/origin của HTTP/1.1). Chỉ có khi lọc theo 1 workspace — view admin gộp mọi
        # tenant thì con số không có nghĩa.
        pairs = pair_counts(wsf) if wsf else {}
        out = []
        for s in list_sessions():
            if wsf and s.get("workspace_id") != wsf:
                continue
            name = s.get("name") or ""
            # Chỉ liệt kê cặp ĐÃ trao đổi trong chu kỳ này; cặp 0 lượt là nhiễu.
            peers = sorted(
                ({"peer": b if a == name else a, "n": n}
                 for (a, b), n in pairs.items() if name in (a, b)),
                key=lambda p: (-p["n"], p["peer"]))
            out.append({**s, **daily_stats(s["id"]), "pairs": peers})
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

    async def api_editor(request: Request):
        """Các card editor đang mở (list rỗng = chưa mở cái nào)."""
        return JSONResponse(await editor_states())

    async def api_editor_open(request: Request):
        """Mở nvim tại cwd của session. Các card khác vẫn mở nguyên."""
        body = await request.json()
        s = get_session(body.get("session") or "")
        if not s:
            return JSONResponse({"error": "session does not exist"}, status_code=404)
        if not (s.get("cwd") or "").strip():
            return JSONResponse({"error": "session has no cwd to open"}, status_code=400)
        try:
            return JSONResponse(await editor_start(s))
        except OSError as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_editor_focus(request: Request):
        """Đổi tab của card ({"session": id, "window": "edit"|"git"})."""
        body = await request.json()
        ok = await editor_focus(body.get("session") or "", body.get("window") or "")
        return JSONResponse({"ok": ok}, status_code=200 if ok else 400)

    async def api_editor_close(request: Request):
        """Đóng 1 card ({"session": id}) hoặc tất cả (body rỗng)."""
        try:
            body = await request.json()
        except Exception:      # body rỗng / không phải JSON → đóng hết
            body = {}
        return JSONResponse({"ok": True, "closed": await editor_stop(body.get("session") or None)})

    async def api_kill(request: Request):
        """Giết proc claude của run đang chạy (session treo/chạy mãi). Run kết thúc theo luồng
        lỗi thường: run=error, signal=failed (reason kill), status về idle; KHÔNG retry."""
        sid = request.path_params["sid"]
        proc = ACTIVE_PROCS.get(sid)
        if not proc or proc.returncode is not None:
            return JSONResponse({"error": "no run is currently active on this session"}, status_code=409)
        KILLED_SESSIONS.add(sid)
        try:
            proc.kill()
        except ProcessLookupError:
            pass  # vừa tự thoát xong — coi như đã kill
        return JSONResponse({"ok": True})

    def _validate_workspace(body):
        """Trả (workspace_id, error_response|None). Bỏ trống = 'default' (single-tenant cũ).
        Workspace phải tồn tại + đang active thì mới cho tạo/register session."""
        wid = body.get("workspace_id") or DEFAULT_WORKSPACE
        ws = get_workspace(wid)
        if not ws:
            return wid, JSONResponse({"error": f"workspace '{wid}' does not exist"}, status_code=404)
        if ws["status"] != "active":
            return wid, JSONResponse({"error": f"workspace '{wid}' is {ws['status']}"}, status_code=409)
        return wid, None

    def _validate_name(name, wid, session_id=None):
        """Tên role phải unique trong workspace (routing theo tên — trùng là signal đi nhầm
        session). session_id: bỏ qua chính nó khi re-register."""
        dup = get_session_by_name(name, wid)
        if dup and dup["id"] != session_id:
            return JSONResponse({"error": f"role '{name}' already exists in this workspace"}, status_code=409)
        return None

    async def api_register(request: Request):
        body = await request.json()
        if not body.get("id") or not body.get("name"):
            return JSONResponse({"error": "id and name are required"}, status_code=400)
        wid, err = _validate_workspace(body)
        if err:
            return err
        name = body["name"].strip()
        err = _validate_name(name, wid, body["id"])
        if err:
            return err
        # cwd truyền vào được tôn trọng; bỏ trống + workspace ≠ default → thư mục workspace.
        cwd = body.get("cwd", "")
        if not cwd and wid != DEFAULT_WORKSPACE:
            cwd = workspace_root(wid) or ""
        register_session(body["id"], name, body.get("project", ""), cwd,
                         body.get("allowed_tools", []), body.get("permission_mode", ""),
                         body.get("model", ""), body.get("effort", ""), wid, "claude")
        # Register CLI session làm worker: ghi SKILL role vào cwd nếu CHƯA có (không đè bản
        # chỉnh tay). `template` = seed từ TEMPLATES_DIR; `init_prompt` = nội dung custom role.
        if cwd and not _skill_path(cwd, name).exists():
            # Thiếu template → session vẫn đăng ký, chỉ không có playbook.
            _write_role_skill(cwd, name,
                              _template_skill(body.get("template", "")) or body.get("init_prompt", ""))
        publish({"type": "session", "id": body["id"], "status": "idle", "workspace_id": wid})
        return JSONResponse(get_session(body["id"]))

    async def api_orch_toggle(request: Request):
        """Bật/tắt terminal nhúng cho 1 session DB. Bật: tắt terminal của session khác cùng cwd
        (1 terminal/project). Tắt: card về dạng headless. Xem set_session_orch."""
        sid = request.path_params["sid"]
        s = get_session(sid)
        if not s:
            return JSONResponse({"error": "not found"}, status_code=404)
        body = await request.json()
        on = bool(body.get("on", True))
        set_session_orch(sid, on)
        publish({"type": "session", "id": sid, "status": s["status"], "workspace_id": s.get("workspace_id")})
        return JSONResponse(get_session(sid))

    async def api_spawn(request: Request):
        body = await request.json()
        if not body.get("name"):
            return JSONResponse({"error": "name is required"}, status_code=400)
        wid, err = _validate_workspace(body)
        if err:
            return err
        err = _validate_name(body["name"].strip(), wid)
        if err:
            return err
        # Playbook nguồn = field 'template' (dashboard gửi; tên vai và template là hai thứ khác
        # nhau — nhiều agent dùng chung một template được). Fallback về tên vai cho client cũ vốn
        # đặt tên vai trùng tên template.
        #
        # SKILL và prompt đầu tiên là HAI thứ khác nhau — trước đây gộp làm một và nội dung
        # template bị gửi thẳng vào `claude -p`. Template có câu mô tả cách placeholder được điền,
        # model đọc thành mệnh lệnh và tự khảo sát + ghi SKILL NGAY trong lượt init: việc xong
        # nhưng không có run nào để xem lại, không vào audit, không chịu trần run/ngày, và
        # _maybe_bootstrap_skill sau đó thấy file đã sạch placeholder nên bỏ qua.
        #   skill        → nội dung ghi ra SKILL.md
        #   init_prompt  → tin nhắn đầu tiên; rỗng thì _build_init_prompt seed generic 'ready'
        # Client gửi init_prompt tường minh (đường API) vẫn giữ nguyên hành vi cũ: dùng cho cả hai.
        fe_prompt = body.get("init_prompt", "")
        skill = fe_prompt or _template_skill(
            (body.get("template") or "").strip() or body["name"].strip())
        # Engine suy TỪ MODEL (model 'codex'/'codex:<slug>' → Codex CLI, còn lại → Claude CLI).
        eng_name = resolve_engine_name(body)
        if eng_name not in ENGINES:
            return JSONResponse({"error": f"engine '{eng_name}' is not supported (available: {', '.join(ENGINES)})"},
                                status_code=400)
        # engine.spawn: cwd truyền vào giữ nguyên; rỗng thì tự về thư mục workspace (≠ default).
        res = await engine_for(eng_name).spawn(
            body["name"], project=body.get("project", ""), cwd=body.get("cwd", ""),
            allowed_tools=body.get("allowed_tools", []), permission_mode=body.get("permission_mode", ""),
            init_prompt=fe_prompt, skill=skill, model=body.get("model", ""),
            effort=body.get("effort", ""), workspace_id=wid)
        if res and res.get("error"):
            return JSONResponse(res, status_code=500)
        publish({"type": "session", "id": res["id"], "status": "idle", "workspace_id": wid})
        # Signal one-time nhờ agent tự điền SKILL từ cwd. Chỉ XẾP HÀNG — run_loop nhặt chạy nền,
        # spawn trả về ngay (bootstrap là một run CLI đầy đủ, chặn ở đây là treo cả UI).
        _maybe_bootstrap_skill(res, eng_name)
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

    async def api_fs_list(request: Request):
        """Duyệt thư mục server-side cho picker Working dir (form spawn).
        Chỉ liệt kê THƯ MỤC (không file), bỏ hidden. Mặc định: $HOME.
        `q` → tìm theo TÊN thư mục thay vì liệt kê 1 cấp (xem _search_dirs)."""
        raw = (request.query_params.get("path") or "").strip() or str(Path.home())
        q = (request.query_params.get("q") or "").strip()
        if q:
            base = Path(raw).expanduser()
            if not base.is_dir():
                base = Path.home()
            return JSONResponse({"path": str(base), "parent": None, "dirs": [],
                                 "matches": _search_dirs(base, q)})
        p = Path(raw).expanduser()
        try:
            p = p.resolve()
            if not p.is_dir():
                return JSONResponse({"error": f"not a directory: {p}"}, status_code=400)
            dirs = sorted((d.name for d in p.iterdir()
                           if d.is_dir() and not d.name.startswith(".")), key=str.lower)[:300]
        except PermissionError:
            return JSONResponse({"error": f"permission denied: {p}"}, status_code=403)
        return JSONResponse({"path": str(p),
                             "parent": str(p.parent) if p != p.parent else None,
                             "dirs": dirs})

    async def api_get_skill(request: Request):
        """SKILL hiện tại của role + path đích (<cwd>/.claude/skills/<name>/SKILL.md)."""
        s = get_session(request.path_params["sid"])
        if not s:
            return JSONResponse({"error": "not found"}, status_code=404)
        cwd, name = s.get("cwd") or "", s.get("name") or ""
        return JSONResponse({"skill": _role_skill(cwd, name), "path": str(_skill_path(cwd, name)),
                             "paths": [str(_skill_path(cwd, name, r)) for r in CLI_SKILL_ROOTS]})

    async def api_put_skill(request: Request):
        """Upsert SKILL của role vào project cwd: tạo thư mục nếu chưa có, đè nếu đã có."""
        s = get_session(request.path_params["sid"])
        if not s:
            return JSONResponse({"error": "not found"}, status_code=404)
        body = await request.json()
        content = body.get("content") or ""
        if not content.strip():
            return JSONResponse({"error": "content is empty — nothing written"}, status_code=400)
        cwd, name = s.get("cwd") or "", s.get("name") or ""
        _write_role_skill(cwd, name, content)
        return JSONResponse({"path": str(_skill_path(cwd, name)),
                             "paths": [str(_skill_path(cwd, name, r)) for r in CLI_SKILL_ROOTS],
                             "bytes": len(content.encode("utf-8"))})

    async def _ws_pty(websocket, argv_for):
        """PTY thật trong browser (xterm.js): spawn argv_for(session) tại cwd của session, bơm 2
        chiều qua WebSocket.
        Client gửi JSON {t:'i', d:<keys>} (input) và {t:'r', c, r} (resize); server gửi bytes thô.
        Mỗi kết nối 1 PTY + 1 thread đọc (read blocking); đóng WS là giết child.

        Dùng chung cho hai loại card — terminal của agent (claude/codex) và editor (nvim) — vì
        chúng khác nhau ĐÚNG một chỗ: argv. Mọi thứ còn lại y hệt.

        PTY lấy từ pty_backend(): pty.fork() trên POSIX, ConPTY trên Windows. Thiếu backend thì
        BÁO THẲNG ra màn hình xterm — để import lỗi rồi WS đứt câm là kiểu hỏng khó đoán nhất."""
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
        Pty, why = pty_backend()
        if Pty is None:
            await websocket.send_text(f"\r\n\x1b[33mEmbedded terminal unavailable\x1b[0m — {why}\r\n")
            await websocket.close(code=4403)
            return
        cwd = (s.get("cwd") or "").strip() or str(Path.home())
        argv = argv_for(s)
        env = dict(os.environ, TERM="xterm-256color")
        # codex mở từ terminal PHẢI gỡ key API y như đường headless (_codex_env), nếu không nó
        # chạy "API-key mode" và đốt credits trong khi đường signal dùng gói ChatGPT.
        if argv[0] == CODEX_BIN:
            for k in CODEX_DROP_ENV:
                env.pop(k, None)
        try:
            term = Pty(argv, cwd, env)
        except Exception as e:  # noqa: BLE001 — CLI thiếu / ConPTY từ chối / cwd sai
            await websocket.send_text(
                f"\r\n\x1b[31mCould not start {argv[0]}\x1b[0m: {e}\r\n")
            await websocket.close(code=4500)
            return

        loop = asyncio.get_running_loop()

        async def pump_out():
            while True:
                data = await loop.run_in_executor(None, term.read)
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
                    # id=94: người dùng đang gõ thẳng cho vai này = đang cầm lái → mốc reset trần
                    # ping-pong. Vai điều phối nhận việc qua terminal chứ không qua signal, không
                    # đánh dấu ở đây thì bộ đếm của chúng không bao giờ về 0.
                    note_human_touch(s.get("name"), s.get("workspace_id"))
                    term.write(str(msg.get("d", "")).encode("utf-8"))
                elif msg.get("t") == "r":
                    try:
                        term.resize(int(msg.get("r", 24)), int(msg.get("c", 80)))
                    except (OSError, ValueError):
                        pass
        except Exception:  # noqa: BLE001 — WS đóng/lỗi đều đi đường dọn dẹp chung
            pass
        finally:
            out_task.cancel()
            term.close()

    async def api_skill_templates(request: Request):
        return JSONResponse(_list_skill_templates())

    # ── MCP servers (xem khối MCP_* ở đầu file) ──────────────────────────────
    async def _body(request):
        try:
            return await request.json()
        except (ValueError, TypeError):
            return {}

    async def api_mcp(request: Request):
        """Các server MCP đang đăng ký ở scope user. KHÔNG gọi ra ngoài: danh sách phải hiện
        ngay, không treo chờ timeout của một server có thể đang tắt. Kiểm thật là /api/mcp/check.
        `checkable` = có gọi tools/list qua HTTP được không; server stdio thì không."""
        out = []
        for name, entry in sorted(_mcp_servers().items()):
            entry = entry if isinstance(entry, dict) else {}
            url = (entry.get("url") or "").split("?")[0].rstrip("/")
            if url.endswith(MCP_OWN_PATHS):   # server nội bộ của chính orchestrator
                continue
            kind = entry.get("type") or ("stdio" if entry.get("command") else "")
            out.append({"name": name, "type": kind, "url": entry.get("url") or "",
                        "command": entry.get("command") or "",
                        "token_hint": _mask(_entry_token(entry)),
                        "checkable": kind in ("http", "sse") and bool(entry.get("url"))})
        return JSONResponse(out)

    async def api_mcp_check(request: Request):
        """Kiểm thật bằng tools/list. Gửi `name` = kiểm cái đang lưu; gửi `url`(+`token`) = thử
        một bộ mới TRƯỚC KHI lưu."""
        b = await _body(request)
        url, token = (b.get("url") or "").strip(), b.get("token")
        if b.get("name") and not url:
            entry = _mcp_servers().get(b["name"])
            if not isinstance(entry, dict):
                return JSONResponse({"error": "not found"}, status_code=404)
            if not (entry.get("url") and entry.get("type") in ("http", "sse")):
                return JSONResponse({"name": b["name"], "state": "unsupported", "tools": 0,
                                     "detail": "only http/sse servers can be checked from here",
                                     "checked_at": _now()})
            url, token = entry["url"], _entry_token(entry)
        if not url:
            return JSONResponse({"error": "url or a known name is required"}, status_code=400)
        state, tools, detail = await _mcp_probe(url, token or "")
        return JSONResponse({"name": b.get("name") or "", "url": url, "state": state,
                             "tools": tools, "detail": detail, "checked_at": _now()})

    async def api_mcp_connect(request: Request):
        """Kiểm TRƯỚC, ghi SAU. Server không trả lời hoặc từ chối token thì KHÔNG file nào bị
        đụng tới — nửa vời còn tệ hơn, vì cấu hình trông đúng mà mọi lượt gọi tool đều hỏng."""
        b = await _body(request)
        name = (b.get("name") or "").strip()
        url = (b.get("url") or "").strip()
        token = (b.get("token") or "").strip()
        if not MCP_NAME_RE.match(name):
            return JSONResponse({"error": "name must be letters, digits, dot, dash or underscore"},
                                status_code=400)
        if not url.startswith(("http://", "https://")):
            return JSONResponse({"error": "the server must be an http:// or https:// URL"},
                                status_code=400)
        state, tools, detail = await _mcp_probe(url, token)
        if state != "connected":
            return JSONResponse({"error": detail or state, "state": state}, status_code=400)
        cfg = _claude_config()
        entry = {"type": "http", "url": url}
        if token:
            entry["headers"] = {"Authorization": "Bearer " + token}
        cfg.setdefault("mcpServers", {})[name] = entry
        _save_claude_config(cfg)
        return JSONResponse({"name": name, "url": url, "state": state, "tools": tools,
                             "token_hint": _mask(token), "checked_at": _now()})

    async def api_mcp_disconnect(request: Request):
        """Gỡ đúng một khoá khỏi mcpServers, phần còn lại của ~/.claude.json giữ nguyên."""
        b = await _body(request)
        name = (b.get("name") or "").strip()
        cfg = _claude_config()
        removed = (cfg.get("mcpServers") or {}).pop(name, None) is not None
        if removed:
            _save_claude_config(cfg)
        return JSONResponse({"name": name, "removed": removed})

    async def ws_terminal(websocket):
        """Terminal của agent: claude/codex interactive trong PTY (xem terminal_argv)."""
        return await _ws_pty(
            websocket, lambda s: terminal_argv(s, websocket.query_params.get("cli", "")))

    async def ws_editor(websocket):
        """Card editor: nvim trong PTY, bọc tmux nếu máy có (xem editor_argv)."""
        return await _ws_pty(websocket, editor_argv)

    async def api_set_model(request: Request):
        """Đổi model của 1 session ngay trên bảng Sessions (không cần re-register)."""
        sid = request.path_params["sid"]
        s = get_session(sid)
        if not s:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        model = (body.get("model") or "").strip()
        # Engine suy TỪ model, mà session id gắn chặt với engine đã tạo ra nó (uuid của claude
        # không resume được bằng codex và ngược lại). Cho đổi xuyên engine = biến session thành
        # xác: mọi lần resume sau đó đều lỗi. Chặn ở đây, muốn đổi thì spawn session mới.
        old, new = engine_name_of_session(s), engine_from_model(model)
        if old != new:
            return JSONResponse(
                {"error": f"cannot switch between '{new}' and '{old}' — one CLI cannot resume the "
                          f"other's session id. Spawn a new session instead."},
                status_code=400)
        set_session_model(sid, model)
        s = get_session(sid)
        publish({"type": "session", "id": sid, "status": s["status"], "workspace_id": s.get("workspace_id")})
        return JSONResponse(s)

    async def api_set_workspace(request: Request):
        """Chuyển 1 session sang workspace khác — để nó signal được với agent của nhóm bên đó.

        Routing signal resolve theo (role, workspace), nên đổi đúng một cột này là agent nhìn
        thấy và gửi được cho nhóm mới. cwd KHÔNG đổi: với claude/codex, cwd vốn độc lập với
        workspace (mỗi agent một project được), nên file của agent nằm nguyên chỗ cũ.

        Đổi tên vai kèm theo qua body 'name' — cần khi workspace đích đã có vai trùng tên.
        """
        sid = request.path_params["sid"]
        s = get_session(sid)
        if not s:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        wid = (body.get("workspace_id") or "").strip()
        if not wid:
            return JSONResponse({"error": "workspace_id is required"}, status_code=400)
        ws = get_workspace(wid)
        if not ws:
            return JSONResponse({"error": f"workspace '{wid}' does not exist"}, status_code=404)
        if ws["status"] != "active":
            # Chuyển vào workspace suspended = agent đứng hình câm: workspace_blocked() chặn mọi
            # signal, mà UI không có chỗ nào nói vì sao.
            return JSONResponse({"error": f"workspace '{wid}' is {ws['status']}"}, status_code=409)
        if s.get("status") == "running":
            return JSONResponse({"error": "session is running — wait for the current turn to finish"},
                                status_code=409)
        name = (body.get("name") or "").strip() or s["name"]
        # Tên vai KHÔNG unique trong DB, và get_session_by_name lấy bản last_active mới nhất — hai
        # vai trùng tên trong một workspace là signal đi lúc bản này lúc bản kia, im lặng, không lỗi.
        clash = get_session_by_name(name, wid)
        if clash and clash["id"] != sid:
            return JSONResponse(
                {"error": f"workspace '{wid}' already has a session named '{name}' — "
                          f"pass a different 'name' to move and rename in one step"},
                status_code=409)
        old_ws = s.get("workspace_id") or DEFAULT_WORKSPACE
        set_session_workspace(sid, wid, name if name != s["name"] else None)
        s = get_session(sid)
        # Hai event: pane cũ bỏ card đi, pane mới dựng lên. publish() lọc theo workspace_id nên
        # một event chỉ tới được một bên.
        publish({"type": "session", "id": sid, "status": s["status"], "workspace_id": old_ws})
        publish({"type": "session", "id": sid, "status": s["status"], "workspace_id": wid})
        return JSONResponse({**s, "moved_from": old_ws})

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
        # Nhận cả thang chung; mức vượt trần engine/model sẽ được clamp lúc chạy (clamp_effort),
        # không chặn ở đây — đổi model sau đó là mức cũ lại dùng được.
        if eff and eff not in EFFORT_LADDER:
            return JSONResponse({"error": f"invalid effort; use one of: {', '.join(EFFORT_LADDER)}"}, status_code=400)
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
            return JSONResponse({"error": "to_session/to_role and message are required"}, status_code=400)
        # Resolve trong phạm vi workspace nếu có (chống signal đi nhầm tenant khi trùng role).
        wid = body.get("workspace_id") or None
        target = resolve_session_id(ref, wid, body.get("from_session") or body.get("from_role"))
        if not target:
            scope = f" trong workspace '{wid}'" if wid else ""
            return JSONResponse({"error": f"no session found for '{ref}'{scope}"}, status_code=404)
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
        try:
            sid = enqueue_signal(target, body["message"],
                                 body.get("from_session", "") or body.get("from_role", ""),
                                 int(body.get("requires_approval", 0)), int(body.get("dry_run", 0)), target_ws)
        except SignalPairCapExceeded as e:
            # id=94: 429 = "đúng đường nhưng hết lượt". Body mang nguyên lời hướng dẫn để caller
            # đọc được phải làm gì thay vì chỉ thấy mã lỗi.
            return JSONResponse({"error": str(e), "code": "pair_signal_cap",
                                 "cap": PAIR_SIGNAL_CAP}, status_code=429)
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
                {"error": f"only signals in {', '.join(RERUNNABLE)} can be re-run; "
                          f"signal #{sig_id} is '{row['status']}'"}, status_code=409)
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
                {"error": f"only signals in {', '.join(DELETABLE)} can be deleted; "
                          f"signal #{sig_id} is '{row['status']}'"}, status_code=409)
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
            return JSONResponse({"error": f"workspace '{ws_filter}' does not exist"}, status_code=404)
        q: asyncio.Queue = asyncio.Queue()
        sub = (q, ws_filter)
        _subscribers.add(sub)

        async def gen():
            try:
                yield "event: ready\ndata: {}\n\n"
                while not _stopping:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        if ev is None:      # sentinel của _stop_streams: server đang tắt
                            break
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                _subscribers.discard(sub)

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ── /v1: chat tương thích OpenAI (xem block "Chat API tương thích OpenAI") ──

    def _oa_err(msg, status=400, typ="invalid_request_error", code=None):
        """Lỗi ĐÚNG shape OpenAI — SDK client bóc e.message/e.code, trả shape khác là nó nuốt."""
        return JSONResponse({"error": {"message": msg, "type": typ, "param": None, "code": code}},
                            status_code=status)

    async def _chat_resolve(request):
        """(session, workspace_id, prompt, body) hoặc (None, response lỗi). Gom mọi kiểm tra đầu
        vào vào 1 chỗ để stream và non-stream không lệch luật nhau."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return None, _oa_err("body phải là JSON")
        if not isinstance(body, dict):
            return None, _oa_err("body phải là JSON object")
        alias, wsid = _chat_agent_ref(body, request.query_params)
        if not alias:
            return None, _oa_err("thiếu agent: truyền 'agent_alias' (hoặc model="
                                 "'<workspace_id>/<agent_alias>')", code="agent_alias_required")
        wsid = wsid or DEFAULT_WORKSPACE
        if not get_workspace(wsid):
            return None, _oa_err(f"workspace '{wsid}' không tồn tại", 404, code="workspace_not_found")
        blocked, why = workspace_blocked(wsid)
        if blocked:
            return None, _oa_err(f"workspace '{wsid}': {why}", 409, code="workspace_blocked")
        sid = resolve_session_id(alias, wsid)
        session = get_session(sid) if sid else None
        if not session:
            return None, _oa_err(f"không có agent '{alias}' trong workspace '{wsid}'", 404,
                                 typ="model_not_found", code="agent_not_found")
        if session["status"] in ("paused", "stopped"):
            return None, _oa_err(f"agent '{alias}' đang {session['status']} — resume trên dashboard "
                                 f"rồi gọi lại", 409, code="agent_" + session["status"])
        prompt = _chat_prompt(body.get("messages"))
        if not prompt:
            return None, _oa_err("'messages' phải có ít nhất 1 message còn nội dung text")
        return (session, wsid, prompt, body), None

    async def api_chat_completions(request: Request):
        """POST /v1/chat/completions — 1 request = 1 lượt chạy thật của agent."""
        ok, err = await _chat_resolve(request)
        if err:
            return err
        session, wsid, prompt, body = ok
        model = str(body.get("model") or "").strip() or f"{wsid}{CHAT_MODEL_SEP}{session['name']}"
        cid, created = _chat_id(), _chat_created()
        want_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        if not body.get("stream"):
            parts, res = [], {}
            async for kind, data in chat_run_stream(session, prompt, wsid):
                (parts.append(data) if kind == "text" else res.update(data))
            text = "\n\n".join(parts)
            if res.get("status") not in ("done", None):
                return _oa_err(text or res.get("result") or "agent chạy lỗi", 502,
                               typ="upstream_error", code=res.get("status"))
            return JSONResponse({
                "id": cid, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": _chat_usage(prompt, text, res.get("tokens")),
            })

        async def gen():
            sse = lambda d: f"data: {json.dumps(d, ensure_ascii=False)}\n\n"  # noqa: E731
            yield sse(_chat_chunk(cid, model, created, {"role": "assistant", "content": ""}))
            text, res = "", {}
            try:
                async for kind, data in chat_run_stream(session, prompt, wsid):
                    if kind == "text":
                        # Agent phát từng MESSAGE (không phải từng token) → chèn ngăn cách như
                        # lúc gộp ở nhánh non-stream, để 2 chế độ ra cùng một nội dung.
                        chunk = ("\n\n" + data) if text else data
                        text += chunk
                        yield sse(_chat_chunk(cid, model, created, {"content": chunk}))
                    else:
                        res = data
            except Exception as e:  # noqa: BLE001 — stream đã mở, không trả HTTP status được nữa
                yield sse({"error": {"message": f"agent run failed: {e}", "type": "upstream_error"}})
            # Lỗi giữa chừng: KHÔNG im lặng đóng stream (client sẽ tưởng agent trả lời xong).
            if res.get("status") not in ("done", None):
                yield sse({"error": {"message": res.get("result") or "agent run failed",
                                     "type": "upstream_error", "code": res.get("status")}})
            yield sse(_chat_chunk(cid, model, created, {}, finish="stop"))
            if want_usage:
                yield sse(_chat_chunk(cid, model, created, usage=_chat_usage(prompt, text, res.get("tokens"))))
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def api_openapi(request: Request):
        return JSONResponse(openapi_spec())

    async def api_docs(request: Request):
        from starlette.responses import HTMLResponse
        return HTMLResponse(_DOCS_HTML)

    async def api_chat_models(request: Request):
        """GET /v1/models — mỗi agent là 1 'model' id '<workspace_id>/<name>' để SDK chọn được."""
        wsf = request.query_params.get("workspace_id") or None
        items = [s for s in list_sessions() if not wsf or s.get("workspace_id") == wsf]
        return JSONResponse({"object": "list", "data": [
            {"id": f"{s.get('workspace_id') or DEFAULT_WORKSPACE}{CHAT_MODEL_SEP}{s['name']}",
             "object": "model", "created": _chat_created(), "owned_by": "orchestrator",
             "agent_alias": s["name"], "workspace_id": s.get("workspace_id") or DEFAULT_WORKSPACE,
             "engine": engine_name_of_session(s), "status": s["status"]}
            for s in items]})

    @asynccontextmanager
    async def lifespan(app):
        init_db()  # idempotent — cũng migrate bảng/cột mới cho DB cũ (vd run_events)
        async with AsyncExitStack() as stack:
            # Khởi động session manager của từng MCP sub-app (bắt buộc cho /mcp).
            await stack.enter_async_context(signal_app.router.lifespan_context(app))
            await stack.enter_async_context(unity_app.router.lifespan_context(app))
            await stack.enter_async_context(asset_app.router.lifespan_context(app))
            task = asyncio.create_task(run_loop())
            print(f"[orchestrator] Dashboard: http://{ORCH_HOST}:{ORCH_PORT}")
            print(f"[orchestrator] API docs:  http://{ORCH_HOST}:{ORCH_PORT}/docs"
                  + (f"  (dry_run={DRY_RUN})" if DRY_RUN else ""))
            print("[orchestrator] MCP mounted: /signal/mcp, /unity/mcp")
            # Agent chạy shell với bypassPermissions. CORS mở + không có key = bất kỳ trang web
            # nào người dùng mở cũng POST được /v1/chat/completions và sai khiến agent trên máy này.
            # In thành KHỐI có viền: người double-click .exe chỉ thấy console vài giây trước khi
            # log uvicorn đẩy trôi, một dòng lẫn giữa log khác là không ai đọc.
            if CORS_ORIGINS == ["*"] and not ORCH_API_KEY:
                bar = "!" * 78
                print(f"\n{bar}\n"
                      "!! SECURITY: CORS is open to ANY origin and ORCH_API_KEY is not set.\n"
                      "!! Agents here run shell commands with permissions bypassed, so ANY website\n"
                      "!! you visit can drive them and read/write files on this machine.\n"
                      "!! Fix: set ORCH_API_KEY=<secret>, or ORCH_CORS_ORIGINS=http://localhost:3000\n"
                      "!! in a .env file next to the executable.\n"
                      f"{bar}\n", file=sys.stderr)
            try:
                yield
            finally:
                task.cancel()
                # KHÔNG đóng card editor ở đây. Phiên nvim sống trong tmux và CỐ Ý sống tiếp qua
                # restart — mở lại dashboard là còn nguyên buffer. Đây chính là chỗ bản VS Code cũ
                # phải dọn tay: serve-web mồ côi mà UI không còn thấy để đóng. tmux không có vấn
                # đề đó vì `tmux ls` luôn tìm lại được chúng.

    class ApiKeyMiddleware(BaseHTTPMiddleware):
        """Chặn /api/* và /v1/* nếu thiếu/sai API key. Chỉ bật khi ORCH_API_KEY được set (mặc định
        tắt để dev localhost như cũ). Key nhận qua header 'X-API-Key', 'Authorization: Bearer <key>'
        (SDK OpenAI chỉ gửi được kiểu này), hoặc query '?api_key=' (cho EventSource/SSE không gắn
        được custom header). Không đụng dashboard tĩnh, /health, MCP mount."""
        async def dispatch(self, request, call_next):
            path = request.url.path
            # OPTIONS = preflight, chuẩn CORS cấm gửi kèm Authorization → không đòi key ở đây
            # (CORSMiddleware ở ngoài đã trả lời trước, đây chỉ là rào phòng khi tắt CORS).
            if ORCH_API_KEY and request.method != "OPTIONS" and path.startswith(("/api/", "/v1/")):
                bearer = request.headers.get("authorization", "")
                bearer = bearer[7:].strip() if bearer[:7].lower() == "bearer " else ""
                key = (request.headers.get("x-api-key") or bearer
                       or request.query_params.get("api_key", ""))
                if not secrets.compare_digest(key, ORCH_API_KEY):
                    # Shape lỗi của OpenAI cho /v1 (SDK bóc e.message), shape cũ cho /api.
                    if path.startswith("/v1/"):
                        return JSONResponse({"error": {"message": "invalid or missing API key",
                                                       "type": "invalid_request_error",
                                                       "code": "invalid_api_key"}}, status_code=401)
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
        Route("/api/fs", api_fs_list),
        WebSocketRoute("/ws/terminal", ws_terminal),
        WebSocketRoute("/ws/editor", ws_editor),
        Route("/api/skills/templates", api_skill_templates),
        Route("/api/mcp", api_mcp),
        Route("/api/mcp/check", api_mcp_check, methods=["POST"]),
        Route("/api/mcp/connect", api_mcp_connect, methods=["POST"]),
        Route("/api/mcp/disconnect", api_mcp_disconnect, methods=["POST"]),
        Route("/api/sessions/{sid}", api_session_detail),
        Route("/api/sessions/{sid}/unregister", api_unregister, methods=["POST"]),
        Route("/api/sessions/{sid}/runs", api_session_runs),
        Route("/api/sessions/{sid}/pause", api_pause, methods=["POST"]),
        Route("/api/sessions/{sid}/resume", api_resume, methods=["POST"]),
        Route("/api/sessions/{sid}/stop", api_stop, methods=["POST"]),
        Route("/api/sessions/{sid}/kill", api_kill, methods=["POST"]),
        Route("/api/editor", api_editor),
        Route("/api/editor/open", api_editor_open, methods=["POST"]),
        Route("/api/editor/focus", api_editor_focus, methods=["POST"]),
        Route("/api/editor/close", api_editor_close, methods=["POST"]),
        Route("/api/sessions/{sid}/orch", api_orch_toggle, methods=["POST"]),
        Route("/api/sessions/{sid}/skill", api_get_skill),
        Route("/api/sessions/{sid}/skill", api_put_skill, methods=["POST"]),
        Route("/api/sessions/{sid}/compact", api_get_compact),
        Route("/api/sessions/{sid}/compact", api_compact, methods=["POST"]),
        Route("/api/sessions/{sid}/model", api_set_model, methods=["POST"]),
        Route("/api/sessions/{sid}/effort", api_set_effort, methods=["POST"]),
        Route("/api/sessions/{sid}/workspace", api_set_workspace, methods=["POST"]),
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
        # Chat tương thích OpenAI cho app ngoài (base_url = http://<host>:<port>/v1)
        Route("/v1/chat/completions", api_chat_completions, methods=["POST"]),
        Route("/v1/models", api_chat_models),
        # Tài liệu: /docs xem bằng Swagger UI, /openapi.json để import vào Postman/codegen.
        # KHÔNG nằm sau ApiKeyMiddleware (chỉ chặn /api/ + /v1/) — đọc tài liệu không cần key.
        Route("/openapi.json", api_openapi),
        Route("/docs", api_docs),
        # MCP server nội bộ mount chung port (đặt trước static "/"): /signal/mcp, /unity/mcp.
        Mount("/signal", app=signal_app),
        Mount("/unity", app=unity_app),
        Mount("/assets", app=asset_app),
    ]

    # Dashboard (Phase C): serve static UI at "/" (must be last — catches the rest).
    static_dir = _bundle_dir() / "static" / "orchestrator"
    if static_dir.exists():
        routes.append(Mount("/", app=StaticFiles(directory=str(static_dir), html=True)))

    # THỨ TỰ QUAN TRỌNG: CORS phải NGOÀI CÙNG. Preflight OPTIONS của trình duyệt KHÔNG mang
    # Authorization (theo chuẩn), nên nếu ApiKeyMiddleware chạy trước thì preflight ăn 401 và
    # request thật không bao giờ được gửi.
    middleware = []
    if CORS_ORIGINS:
        from starlette.middleware.cors import CORSMiddleware
        middleware.append(Middleware(
            CORSMiddleware, allow_origins=CORS_ORIGINS,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["authorization", "content-type", "x-api-key"],
            # False: dùng '*' kèm credentials là trình duyệt tự chặn, và API này xác thực bằng
            # header tường minh chứ không bằng cookie.
            allow_credentials=False, max_age=600))
    if ORCH_API_KEY:
        middleware.append(Middleware(ApiKeyMiddleware))
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
    # Ctrl-C: KHÔNG đặt _stop_streams() trong lifespan được. Thứ tự của uvicorn là
    #   nhận tín hiệu → NGỪNG nhận request mới → CHỜ mọi response đang dở đóng → mới chạy
    #   lifespan shutdown.
    # Stream SSE là một response "đang dở" không bao giờ tự đóng, nên nó treo ở bước 3 và lifespan
    # không bao giờ tới lượt. Đo được: hook trong lifespan cho 5.3s (chính là timeout ép), hook ở
    # handle_exit cho 0.1s. Phải cắt stream NGAY khi nhận tín hiệu — handle_exit là móc đó.
    class _Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            _stop_streams()
            super().handle_exit(sig, frame)

    # timeout_graceful_shutdown giữ lại làm chốt chặn cuối: một response dở dang nào KHÁC cũng đủ
    # giữ Ctrl-C lại vô hạn.
    try:
        _Server(uvicorn.Config(build_app(), host=ORCH_HOST, port=ORCH_PORT,
                               timeout_graceful_shutdown=5)).run()
    except KeyboardInterrupt:
        # Ctrl-C là cách tắt BÌNH THƯỜNG, không phải lỗi. Server.run() gọi asyncio.run(), và
        # asyncio.run() của 3.11 dựng lại KeyboardInterrupt sau khi loop dừng — không nuốt ở đây
        # thì mỗi lần tắt máy người dùng ăn một traceback trông y như crash, dù shutdown đã chạy
        # xong sạch sẽ. Tiến trình vẫn thoát theo tín hiệu (uvicorn raise_signal lại), đúng quy ước.
        print("\n[orchestrator] stopped", file=sys.stderr)


# ─── CLI ──────────────────────────────────────────────────────────────────────


def _print(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description="Session Orchestrator (Phase A)")
    # KHÔNG required: chạy không tham số = `serve`. Người dùng Windows double-click file .exe
    # không truyền được argv — argparse required=True sẽ in usage rồi exit(2), cửa sổ console
    # nháy một cái là mất, không kịp đọc gì. 'serve' cũng là lệnh thực tế 99% người dùng cần.
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("init")
    sub.add_parser("once")
    sub.add_parser("loop")
    sub.add_parser("serve")
    sub.add_parser("list-sessions")
    sub.add_parser("list-signals")
    sub.add_parser("list-runs")

    args = p.parse_args()
    cmd = args.cmd or "serve"
    if cmd == "init":
        init_db()
        print(f"DB tạo tại {_db_path()}")
    elif cmd == "once":
        _print(asyncio.run(process_pending()))
    elif cmd == "loop":
        asyncio.run(run_loop())
    elif cmd == "serve":
        serve()
    elif cmd == "list-sessions":
        _print(list_sessions())
    elif cmd == "list-signals":
        _print(list_signals()[0])   # (items, has_more) → chỉ in items
    elif cmd == "list-runs":
        _print(list_runs()[0])


def _keep_console_open(err):
    """Double-click trên Windows: tiến trình chết là cửa sổ console đóng ngay, người dùng không
    đọc được lỗi (cổng bận, thiếu quyền…). Giữ cửa sổ lại cho tới khi họ bấm Enter.

    CHỈ khi: binary đóng gói + Windows + chạy KHÔNG tham số (double-click). Chạy từ terminal có
    gõ lệnh thì đừng chặn — script/CI sẽ treo."""
    if not (getattr(sys, "frozen", False) and os.name == "nt" and len(sys.argv) == 1):
        return
    print(f"\n{err}", file=sys.stderr)
    try:
        input("\nNhấn Enter để đóng cửa sổ…")
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001 — chỉ để KỊP HIỆN lỗi trước khi console đóng
        _keep_console_open(f"LỖI: {e}")
        raise
