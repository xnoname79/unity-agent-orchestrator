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
ORCH_HOST = os.environ.get("ORCH_HOST", "0.0.0.0")
ORCH_PORT = int(os.environ.get("ORCH_PORT", "8992"))


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

            set_signal_status(signal["id"], "processing")
            set_session_status(target["id"], "running")
            publish({"type": "signal", "id": signal["id"], "status": "processing", "session": target["id"]})
            started = _now()
            try:
                res = await _run_claude(target, signal["message"])
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "result": f"exception: {e}", "session_id": target["id"], "tokens": 0, "raw": {}}
            ended = _now()

            status = "ok" if res.get("ok") else "error"
            run_id = record_run(target["id"], signal["id"], signal["message"], res.get("raw", {}),
                                status, res.get("tokens", 0), started, ended)
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
    from starlette.routing import Route

    async def health(request: Request):
        return JSONResponse({"status": "ok", "server": "Session-Orchestrator",
                             "dry_run": DRY_RUN, "kill_switch": _kill_switch})

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

    # Signals
    async def api_signals(request: Request):
        return JSONResponse(list_signals())

    async def api_enqueue(request: Request):
        body = await request.json()
        if not body.get("to_session") or not body.get("message"):
            return JSONResponse({"error": "to_session và message bắt buộc"}, status_code=400)
        sid = enqueue_signal(body["to_session"], body["message"], body.get("from_session", ""),
                             int(body.get("requires_approval", 0)))
        publish({"type": "signal", "id": sid, "status": "pending"})
        return JSONResponse({"id": sid, "status": "pending"})

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
        _ensure_db()
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
        Route("/api/sessions/{sid}", api_session_detail),
        Route("/api/sessions/{sid}/runs", api_session_runs),
        Route("/api/sessions/{sid}/pause", api_pause, methods=["POST"]),
        Route("/api/sessions/{sid}/resume", api_resume, methods=["POST"]),
        Route("/api/sessions/{sid}/stop", api_stop, methods=["POST"]),
        Route("/api/signals", api_signals),
        Route("/api/signals", api_enqueue, methods=["POST"]),
        Route("/api/signals/{sig_id}/approve", api_approve, methods=["POST"]),
        Route("/api/signals/{sig_id}/deny", api_deny, methods=["POST"]),
        Route("/api/runs", api_runs),
        Route("/api/stop-all", api_stop_all, methods=["POST"]),
        Route("/api/resume-all", api_resume_all, methods=["POST"]),
        Route("/api/events", api_events),
    ]
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
