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

DB_DIR = Path.home() / ".session_orch_db"
DB_NAME = os.environ.get("ORCH_DB", "orchestrator")
DRY_RUN = os.environ.get("ORCH_DRY_RUN", "0") == "1"
POLL_INTERVAL = int(os.environ.get("ORCH_POLL_INTERVAL", "5"))
MAX_CONCURRENT = int(os.environ.get("ORCH_MAX_CONCURRENT", "3"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")


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
            status TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|processing|done|failed|denied
            created_at TEXT NOT NULL,
            delivered_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            signal_id INTEGER,
            prompt TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,              -- ok | error
            tokens INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL DEFAULT ''
        );
    """)
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


# signals

def enqueue_signal(to_session, message, from_session="", requires_approval=0):
    _ensure_db()
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO signals (from_session, to_session, message, requires_approval, status, created_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (from_session, to_session, message, int(requires_approval), _now()),
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


def list_runs(limit=50):
    _ensure_db()
    conn = _conn()
    rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── Executor ─────────────────────────────────────────────────────────────────


async def _run_claude(session, prompt):
    """Chạy `claude -p --resume <id>` với allowlist. Trả dict kết quả.

    Dry-run (ORCH_DRY_RUN=1): trả stub, không gọi claude — để test pipeline.
    """
    session_id = session["id"]
    if DRY_RUN:
        return {
            "ok": True,
            "result": f"[dry-run] would inject to {session['name']}: {prompt}",
            "session_id": session_id,
            "tokens": 0,
            "raw": {"dry_run": True},
        }

    cmd = [CLAUDE_BIN, "-p", prompt, "--resume", session_id, "--output-format", "json"]
    allowed = json.loads(session.get("allowed_tools") or "[]")
    if allowed:
        cmd += ["--allowedTools", " ".join(allowed)]
    if session.get("permission_mode"):
        cmd += ["--permission-mode", session["permission_mode"]]

    cwd = session.get("cwd") or None
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return {
            "ok": False,
            "result": (stderr or b"").decode("utf-8", "replace")[:2000],
            "session_id": session_id,
            "tokens": 0,
            "raw": {"returncode": proc.returncode},
        }
    try:
        data = json.loads((stdout or b"").decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {
            "ok": False,
            "result": "Không parse được JSON output từ claude.",
            "session_id": session_id,
            "tokens": 0,
            "raw": {"stdout": (stdout or b"").decode("utf-8", "replace")[:2000]},
        }
    usage = data.get("usage") or {}
    tokens = int(usage.get("output_tokens", 0) or 0)
    return {
        "ok": data.get("is_error", False) is False,
        "result": data.get("result", ""),
        "session_id": data.get("session_id", session_id),
        "tokens": tokens,
        "raw": data,
    }


# ─── Core (poller + lock/queue) ───────────────────────────────────────────────

_locks: dict[str, asyncio.Lock] = {}
_semaphore: asyncio.Semaphore | None = None


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
        return {"signal": signal["id"], "status": "failed", "reason": "no session"}

    async with _semaphore:
        async with _lock_for(target["id"]):
            set_signal_status(signal["id"], "processing")
            set_session_status(target["id"], "running")
            started = _now()
            try:
                res = await _run_claude(target, signal["message"])
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "result": f"exception: {e}", "session_id": target["id"], "tokens": 0, "raw": {}}
            ended = _now()

            status = "ok" if res.get("ok") else "error"
            record_run(target["id"], signal["id"], signal["message"], res.get("raw", {}),
                       status, res.get("tokens", 0), started, ended)
            set_signal_status(signal["id"], "done" if res.get("ok") else "failed")
            set_session_status(target["id"], "idle")
            return {"signal": signal["id"], "status": "done" if res.get("ok") else "failed",
                    "result": res.get("result", "")}


async def process_pending():
    """Poll 1 lần: xử lý tất cả signal eligible (song song, serialize theo session)."""
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


# ─── CLI ──────────────────────────────────────────────────────────────────────


def _print(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description="Session Orchestrator (Phase A)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("once")
    sub.add_parser("loop")
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
    elif args.cmd == "list-sessions":
        _print(list_sessions())
    elif args.cmd == "list-signals":
        _print(list_signals())
    elif args.cmd == "list-runs":
        _print(list_runs())


if __name__ == "__main__":
    main()
