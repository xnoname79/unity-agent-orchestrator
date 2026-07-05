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
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import httpx

DB_DIR = Path.home() / ".session_orch_db"
DB_NAME = os.environ.get("ORCH_DB", "orchestrator")
DRY_RUN = os.environ.get("ORCH_DRY_RUN", "0") == "1"
POLL_INTERVAL = int(os.environ.get("ORCH_POLL_INTERVAL", "5"))
MAX_CONCURRENT = int(os.environ.get("ORCH_MAX_CONCURRENT", "3"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
ORCH_HOST = os.environ.get("ORCH_HOST", "0.0.0.0")
ORCH_PORT = int(os.environ.get("ORCH_PORT", "8992"))
# Phase D — safety caps (0 = tắt/không giới hạn)
MAX_RUNS_PER_SESSION = int(os.environ.get("ORCH_MAX_RUNS_PER_SESSION", "0"))
SESSION_TOKEN_BUDGET = int(os.environ.get("ORCH_SESSION_TOKEN_BUDGET", "0"))
MAX_RETRIES = int(os.environ.get("ORCH_MAX_RETRIES", "0"))
RETRY_BACKOFF = float(os.environ.get("ORCH_RETRY_BACKOFF", "2"))
# Streaming — hiển thị chi tiết (thinking/tool_use/text) của headless agent theo thời gian thực.
STREAM = os.environ.get("ORCH_STREAM", "1") == "1"          # 1 = dùng --output-format stream-json
STREAM_PARTIAL = os.environ.get("ORCH_STREAM_PARTIAL", "0") == "1"  # 1 = thêm --include-partial-messages (token-level)
EVENT_TRUNC = int(os.environ.get("ORCH_EVENT_TRUNC", "2000"))  # cắt payload event để tránh phình DB/lộ dữ liệu


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
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,              -- claude session_id
            name TEXT NOT NULL,               -- role/label
            project TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'idle',   -- idle | running | paused | stopped
            allowed_tools TEXT NOT NULL DEFAULT '[]',
            permission_mode TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            last_active TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_session TEXT NOT NULL DEFAULT '',
            to_session TEXT NOT NULL,          -- target session_id
            message TEXT NOT NULL,
            requires_approval INTEGER NOT NULL DEFAULT 0,
            dry_run INTEGER NOT NULL DEFAULT 0,      -- 1 = preview, không gọi claude thật
            status TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|processing|done|failed|denied|blocked
            created_at TEXT NOT NULL,
            delivered_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    """)
    # migrate: thêm cột dry_run cho signals nếu DB cũ chưa có
    cols = [r[1] for r in conn.execute("PRAGMA table_info(signals)").fetchall()]
    if "dry_run" not in cols:
        conn.execute("ALTER TABLE signals ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()


def _ensure_db():
    if not os.path.exists(_db_path()):
        init_db()


def _now():
    return datetime.now().isoformat()


# sessions

def register_session(session_id, name, project="", cwd="", allowed_tools=None, permission_mode=""):
    _ensure_db()
    conn = _conn()
    conn.execute(
        "INSERT INTO sessions (id, name, project, cwd, status, allowed_tools, permission_mode, created_at, last_active) "
        "VALUES (?, ?, ?, ?, 'idle', ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, project=excluded.project, cwd=excluded.cwd, "
        "allowed_tools=excluded.allowed_tools, permission_mode=excluded.permission_mode",
        (session_id, name, project, cwd, json.dumps(allowed_tools or []), permission_mode, _now(), _now()),
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


def get_session_by_name(name):
    _ensure_db()
    conn = _conn()
    row = conn.execute("SELECT * FROM sessions WHERE name = ? ORDER BY last_active DESC LIMIT 1", (name,)).fetchone()
    conn.close()
    return dict(row) if row else None


def resolve_session_id(ref):
    """ref = session_id (exact match) HOẶC role/name → trả session_id, None nếu không thấy."""
    if not ref:
        return None
    if get_session(ref):
        return ref
    s = get_session_by_name(ref)
    return s["id"] if s else None


# signals

def enqueue_signal(to_session, message, from_session="", requires_approval=0, dry_run=0):
    _ensure_db()
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO signals (from_session, to_session, message, requires_approval, dry_run, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (from_session, to_session, message, int(requires_approval), int(dry_run), _now()),
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


def set_signal_status(signal_id, status):
    conn = _conn()
    delivered = _now() if status in ("done", "failed") else ""
    conn.execute("UPDATE signals SET status = ?, delivered_at = ? WHERE id = ?", (status, delivered, signal_id))
    conn.commit()
    conn.close()


def list_signals(limit=50):
    _ensure_db()
    conn = _conn()
    rows = conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# runs (audit)

def record_run(session_id, signal_id, prompt, result_json, status, tokens, started_at, ended_at):
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO runs (session_id, signal_id, prompt, result_json, status, tokens, started_at, ended_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, signal_id, prompt, json.dumps(result_json, ensure_ascii=False), status, tokens, started_at, ended_at),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def start_run(session_id, signal_id, prompt, started_at):
    """Mở 1 run ở trạng thái 'running' TRƯỚC khi chạy — để stream event vào ngay lúc chạy."""
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO runs (session_id, signal_id, prompt, result_json, status, tokens, started_at) "
        "VALUES (?, ?, ?, '{}', 'running', 0, ?)",
        (session_id, signal_id, prompt, started_at),
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


def list_runs(limit=50):
    _ensure_db()
    conn = _conn()
    rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def session_stats(session_id):
    """Số run + tổng token đã dùng của 1 session (để check cap/budget)."""
    _ensure_db()
    conn = _conn()
    row = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(tokens),0) t FROM runs WHERE session_id = ?", (session_id,)
    ).fetchone()
    conn.close()
    return {"runs": row["c"], "tokens": row["t"]}


def cap_exceeded(session_id):
    """Trả (True, reason) nếu session vượt cap run hoặc budget token."""
    st = session_stats(session_id)
    if MAX_RUNS_PER_SESSION and st["runs"] >= MAX_RUNS_PER_SESSION:
        return True, f"đạt trần {MAX_RUNS_PER_SESSION} runs"
    if SESSION_TOKEN_BUDGET and st["tokens"] >= SESSION_TOKEN_BUDGET:
        return True, f"đạt budget {SESSION_TOKEN_BUDGET} tokens"
    return False, ""


# ─── Executor ─────────────────────────────────────────────────────────────────


def _trunc(s, n=None):
    n = EVENT_TRUNC if n is None else n
    s = str(s or "")
    return s if len(s) <= n else s[:n] + f"… (+{len(s) - n} ký tự)"


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


def _iter_display_events(ev):
    """Chuyển 1 event NDJSON của claude thành list (kind, summary, payload) để hiển thị.

    1 message assistant có thể có nhiều content block → tách thành nhiều event con
    (thinking / text / tool_use) cho timeline mượt.
    """
    t = ev.get("type")
    out = []
    if t == "system":
        model = ev.get("model") or "?"
        tools = ev.get("tools") or []
        out.append(("system", f"session bắt đầu · model={model} · {len(tools)} tools",
                    {"subtype": ev.get("subtype"), "tools": tools[:60]}))
    elif t == "assistant":
        for b in (ev.get("message") or {}).get("content", []):
            bt = b.get("type")
            if bt == "text":
                tx = (b.get("text") or "").strip()
                if tx:
                    out.append(("text", _trunc(tx, 500), {"text": _trunc(tx)}))
            elif bt == "thinking":
                th = (b.get("thinking") or "").strip()
                if th:
                    out.append(("thinking", _trunc(th, 500), {"thinking": _trunc(th)}))
            elif bt == "tool_use":
                inp = _trunc(json.dumps(b.get("input", {}), ensure_ascii=False), 300)
                out.append(("tool_use", f"{b.get('name', '?')}({inp})",
                            {"name": b.get("name"), "input": b.get("input")}))
    elif t == "user":
        for b in (ev.get("message") or {}).get("content", []):
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
    if session.get("permission_mode"):
        cmd += ["--permission-mode", session["permission_mode"]]

    cwd = session.get("cwd") or None
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
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
        async for line in proc.stderr:
            stderr_chunks.append(line)

    stderr_task = asyncio.create_task(_drain_stderr())
    final = None
    try:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "result":
                final = ev
            for kind, summary, payload in _iter_display_events(ev):
                try:
                    await on_event(kind, summary, payload)
                except Exception:  # noqa: BLE001 — không để lỗi UI làm hỏng run
                    pass
    finally:
        await proc.wait()
        await stderr_task

    stderr_txt = b"".join(stderr_chunks).decode("utf-8", "replace")
    if proc.returncode != 0 and final is None:
        return {"ok": False, "result": stderr_txt[:2000] or "claude exited nonzero",
                "session_id": session_id, "tokens": 0, "raw": {"returncode": proc.returncode}}
    if final is None:
        return {"ok": False, "result": "không nhận được event 'result' từ claude.",
                "session_id": session_id, "tokens": 0, "raw": {"stderr": stderr_txt[:2000]}}
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


async def spawn_session(name, project="", cwd="", allowed_tools=None, permission_mode="", init_prompt=""):
    """Tạo một headless session mới bằng `claude -p`, lấy session_id, rồi register.

    Dry-run: tạo session_id giả để test UI mà không gọi claude.
    """
    init_prompt = init_prompt or (
        f"Bạn là agent '{name}' trong hệ thống multi-agent được điều phối. "
        f"Trả lời ngắn gọn 'ready'."
    )
    if DRY_RUN:
        sid = f"dry-{name}-{datetime.now().strftime('%H%M%S%f')}"
    else:
        # init_prompt qua STDIN (tránh lỗi khi prompt bắt đầu bằng '-', vd '---' frontmatter).
        cmd = [CLAUDE_BIN, "-p", "--output-format", "json"]
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
    register_session(sid, name, project, cwd, allowed_tools or [], permission_mode)
    return get_session(sid)


def unregister_session(session_id):
    """Gỡ session khỏi orchestrator (giữ lại runs cho audit)."""
    conn = _conn()
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()


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
# Global kill switch — when True, poller không xử lý signal nào (stop-all).
_kill_switch = False


def set_kill_switch(on: bool):
    global _kill_switch
    _kill_switch = on
    publish({"type": "kill_switch", "on": on})


def kill_switch_on() -> bool:
    return _kill_switch


def publish(event: dict):
    """Đẩy event tới mọi SSE subscriber (no-op nếu không có ai)."""
    event = {"ts": _now(), **event}
    for q in list(_subscribers):
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

    target = get_session(signal["to_session"])
    if not target:
        set_signal_status(signal["id"], "failed")
        record_run(signal["to_session"], signal["id"], signal["message"],
                   {"error": "session không tồn tại"}, "error", 0, _now(), _now())
        publish({"type": "signal", "id": signal["id"], "status": "failed", "reason": "no session"})
        return {"signal": signal["id"], "status": "failed", "reason": "no session"}

    # Pause/stop-aware: không inject vào session đang paused/stopped — để signal chờ.
    if target["status"] in ("paused", "stopped"):
        return {"signal": signal["id"], "status": "skipped", "reason": f"session {target['status']}"}

    async with _semaphore:
        async with _lock_for(target["id"]):
            # Kiểm tra lại sau khi có lock (trạng thái có thể đổi trong lúc chờ)
            target = get_session(target["id"])
            if target["status"] in ("paused", "stopped"):
                return {"signal": signal["id"], "status": "skipped", "reason": f"session {target['status']}"}

            # Circuit breaker: chặn lặp vô tận / vượt budget.
            exceeded, reason = cap_exceeded(target["id"])
            if exceeded:
                set_signal_status(signal["id"], "blocked")
                set_session_status(target["id"], "idle")
                publish({"type": "signal", "id": signal["id"], "status": "blocked",
                         "session": target["id"], "reason": reason})
                return {"signal": signal["id"], "status": "blocked", "reason": reason}

            set_signal_status(signal["id"], "processing")
            set_session_status(target["id"], "running")
            publish({"type": "signal", "id": signal["id"], "status": "processing", "session": target["id"]})
            started = _now()
            dry = bool(signal.get("dry_run"))

            # Mở run trước để có run_id, rồi stream từng bước vào run_events + SSE.
            run_id = start_run(target["id"], signal["id"], signal["message"], started)
            publish({"type": "run_start", "run_id": run_id, "session": target["id"],
                     "signal": signal["id"]})
            seq_box = [0]

            async def on_event(kind, summary, payload, _rid=run_id, _sid=target["id"], _sig=signal["id"]):
                seq_box[0] += 1
                record_run_event(_rid, _sid, _sig, seq_box[0], kind, summary, payload)
                publish({"type": "run_event", "run_id": _rid, "session": _sid,
                         "seq": seq_box[0], "kind": kind, "summary": summary})

            attempts = 0
            while True:
                try:
                    res = await _run_claude(target, signal["message"], on_event=on_event, dry_run=dry)
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
            set_signal_status(signal["id"], final)
            set_session_status(target["id"], "idle")
            publish({"type": "run", "run_id": run_id, "session": target["id"], "signal": signal["id"],
                     "status": status, "tokens": res.get("tokens", 0)})
            publish({"type": "signal", "id": signal["id"], "status": final, "session": target["id"]})
            return {"signal": signal["id"], "status": final, "result": res.get("result", "")}


async def process_pending():
    """Poll 1 lần: xử lý tất cả signal eligible (song song, serialize theo session)."""
    if _kill_switch:
        return []
    signals = eligible_signals()
    if not signals:
        return []
    results = await asyncio.gather(*[process_signal(s) for s in signals])
    return results


async def run_loop():
    print(f"[orchestrator] loop start (dry_run={DRY_RUN}, poll={POLL_INTERVAL}s, db={_db_path()})")
    while True:
        try:
            results = await process_pending()
            if results:
                for r in results:
                    print(f"[orchestrator] signal #{r['signal']} → {r['status']}")
        except Exception as e:  # noqa: BLE001
            print(f"[orchestrator] loop error: {e}", file=sys.stderr)
        await asyncio.sleep(POLL_INTERVAL)


# ─── Control API (Phase B: REST + SSE) ────────────────────────────────────────


def build_app():
    """Starlette app: REST control + SSE live events + background poll loop."""
    from contextlib import asynccontextmanager
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    async def health(request: Request):
        return JSONResponse({"status": "ok", "server": "Session-Orchestrator",
                             "dry_run": DRY_RUN, "kill_switch": _kill_switch,
                             "limits": {"max_runs_per_session": MAX_RUNS_PER_SESSION,
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

    # Sessions
    async def api_sessions(request: Request):
        return JSONResponse(list_sessions())

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
        if not get_session(sid):
            return JSONResponse({"error": "not found"}, status_code=404)
        set_session_status(sid, status)
        publish({"type": "session", "id": sid, "status": status})
        return JSONResponse({"id": sid, "status": status})

    async def api_pause(request: Request):
        return await _set_status(request, "paused")

    async def api_resume(request: Request):
        return await _set_status(request, "idle")

    async def api_stop(request: Request):
        return await _set_status(request, "stopped")

    async def api_register(request: Request):
        body = await request.json()
        if not body.get("id") or not body.get("name"):
            return JSONResponse({"error": "id và name bắt buộc"}, status_code=400)
        register_session(body["id"], body["name"], body.get("project", ""), body.get("cwd", ""),
                         body.get("allowed_tools", []), body.get("permission_mode", ""))
        publish({"type": "session", "id": body["id"], "status": "idle"})
        return JSONResponse(get_session(body["id"]))

    async def api_spawn(request: Request):
        body = await request.json()
        if not body.get("name"):
            return JSONResponse({"error": "name bắt buộc"}, status_code=400)
        res = await spawn_session(body["name"], body.get("project", ""), body.get("cwd", ""),
                                  body.get("allowed_tools", []), body.get("permission_mode", ""),
                                  body.get("init_prompt", ""))
        if res and res.get("error"):
            return JSONResponse(res, status_code=500)
        publish({"type": "session", "id": res["id"], "status": "idle"})
        return JSONResponse(res)

    async def api_unregister(request: Request):
        sid = request.path_params["sid"]
        if not get_session(sid):
            return JSONResponse({"error": "not found"}, status_code=404)
        unregister_session(sid)
        publish({"type": "session", "id": sid, "status": "removed"})
        return JSONResponse({"id": sid, "removed": True})

    async def api_available_tools(request: Request):
        cwd = request.query_params.get("cwd", "")
        return JSONResponse(await discover_tools(cwd))

    # Signals
    async def api_signals(request: Request):
        return JSONResponse(list_signals())

    async def api_enqueue(request: Request):
        body = await request.json()
        ref = body.get("to_session") or body.get("to_role")
        if not ref or not body.get("message"):
            return JSONResponse({"error": "to_session/to_role và message bắt buộc"}, status_code=400)
        target = resolve_session_id(ref)
        if not target:
            return JSONResponse({"error": f"không tìm thấy session cho '{ref}'"}, status_code=404)
        sid = enqueue_signal(target, body["message"],
                             body.get("from_session", "") or body.get("from_role", ""),
                             int(body.get("requires_approval", 0)), int(body.get("dry_run", 0)))
        publish({"type": "signal", "id": sid, "status": "pending", "to_session": target})
        return JSONResponse({"id": sid, "status": "pending", "to_session": target})

    async def _resolve_signal(request: Request, status: str):
        sig_id = int(request.path_params["sig_id"])
        conn = _conn()
        row = conn.execute("SELECT * FROM signals WHERE id = ?", (sig_id,)).fetchone()
        conn.close()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        set_signal_status(sig_id, status)
        publish({"type": "signal", "id": sig_id, "status": status})
        return JSONResponse({"id": sig_id, "status": status})

    async def api_approve(request: Request):
        return await _resolve_signal(request, "approved")

    async def api_deny(request: Request):
        return await _resolve_signal(request, "denied")

    # Runs (audit)
    async def api_runs(request: Request):
        return JSONResponse(list_runs())

    async def api_run_events(request: Request):
        rid = int(request.path_params["rid"])
        return JSONResponse(list_run_events(rid))

    # Kill switch
    async def api_stop_all(request: Request):
        set_kill_switch(True)
        return JSONResponse({"kill_switch": True})

    async def api_resume_all(request: Request):
        set_kill_switch(False)
        return JSONResponse({"kill_switch": False})

    # SSE live events
    async def api_events(request: Request):
        q: asyncio.Queue = asyncio.Queue()
        _subscribers.add(q)

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
                _subscribers.discard(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @asynccontextmanager
    async def lifespan(app):
        init_db()  # idempotent — cũng migrate bảng/cột mới cho DB cũ (vd run_events)
        task = asyncio.create_task(run_loop())
        print(f"[orchestrator] API on http://{ORCH_HOST}:{ORCH_PORT} (dry_run={DRY_RUN})")
        try:
            yield
        finally:
            task.cancel()

    routes = [
        Route("/health", health),
        Route("/api/sessions", api_sessions),
        Route("/api/sessions", api_register, methods=["POST"]),
        Route("/api/sessions/spawn", api_spawn, methods=["POST"]),
        Route("/api/available-tools", api_available_tools),
        Route("/api/sessions/{sid}", api_session_detail),
        Route("/api/sessions/{sid}/unregister", api_unregister, methods=["POST"]),
        Route("/api/sessions/{sid}/runs", api_session_runs),
        Route("/api/sessions/{sid}/pause", api_pause, methods=["POST"]),
        Route("/api/sessions/{sid}/resume", api_resume, methods=["POST"]),
        Route("/api/sessions/{sid}/stop", api_stop, methods=["POST"]),
        Route("/api/signals", api_signals),
        Route("/api/signals", api_enqueue, methods=["POST"]),
        Route("/api/signals/{sig_id}/approve", api_approve, methods=["POST"]),
        Route("/api/signals/{sig_id}/deny", api_deny, methods=["POST"]),
        Route("/api/runs", api_runs),
        Route("/api/runs/{rid}/events", api_run_events),
        Route("/api/stats", api_stats),
        Route("/api/stop-all", api_stop_all, methods=["POST"]),
        Route("/api/resume-all", api_resume_all, methods=["POST"]),
        Route("/api/events", api_events),
    ]

    # Dashboard (Phase C): serve static UI at "/" (must be last — catches the rest).
    static_dir = Path(__file__).parent / "static" / "orchestrator"
    if static_dir.exists():
        routes.append(Mount("/", app=StaticFiles(directory=str(static_dir), html=True)))

    return Starlette(routes=routes, lifespan=lifespan)


def serve():
    import uvicorn
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
        _print(list_signals())
    elif args.cmd == "list-runs":
        _print(list_runs())


if __name__ == "__main__":
    main()
