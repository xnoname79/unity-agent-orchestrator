#!/usr/bin/env python3
"""Guards for performance — the things that made the computer slow after a few hours of work.

The quiet ways it goes wrong:

  - a closed terminal stays behind as a zombie process until the orchestrator restarts (36 after
    2.4 hours, measured); a program that ignores SIGTERM must still be killed;
  - /api/sessions runs on every dashboard refresh: it used ~4 database connections per agent and
    blocked the server for 170 ms; it must use a handful, and its daily numbers must not change;
  - the queries that run all the time (runs of today, signals every 5 s) must use an index —
    those tables only grow;
  - the agent tool servers (MCP) kept one session per connection and nothing ever closed them;
  - deleting an agent left its editor (nvim + language servers) running in tmux with no card left
    to close it;
  - Tools › Performance must say whose terminal or run each process is, and count zombies;
  - the dashboard must not refresh on every step of every run, nor for other workspaces.

    python3 check_perf.py
"""
import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["ORCH_DB"] = "check_perf"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()

import httpx                          # noqa: E402
import psutil                         # noqa: E402
import session_orchestrator as so     # noqa: E402

for name in ("httpx", "mcp", "uvicorn"):
    logging.getLogger(name).setLevel(logging.WARNING)
fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("ok   " if ok else "FAIL ") + name + (f" — {detail}" if not ok else ""))


def state(pid):
    try:
        return psutil.Process(pid).status()
    except psutil.NoSuchProcess:
        return "gone"


def wait_gone(pid, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end and state(pid) != "gone":
        time.sleep(0.1)
    return state(pid)


def plans(fn, *args):
    """Query plan của mọi SELECT mà fn chạy — bắt câu SQL thật, không chép lại tay."""
    sql, real = [], so._conn

    def tracing():
        c = real()
        c.set_trace_callback(lambda q: sql.append(q) if q.lstrip().upper().startswith("SELECT") else None)
        return c
    so._conn = tracing
    try:
        fn(*args)
    finally:
        so._conn = real
    c = real()
    out = [" ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN " + q)) for q in sql]
    c.close()
    return out


async def main():
    so.init_db()
    w = so.create_workspace("perf", max_runs_per_day=5)
    wid = w["id"]
    tmp = tempfile.mkdtemp()
    for i in range(12):
        so.register_session(f"perf-{i}", f"agent-{i}", cwd=tmp, workspace_id=wid if i % 2 else so.DEFAULT_WORKSPACE)
    today = datetime.now()
    c = so._conn()
    for i, when in enumerate([today, today, today - timedelta(days=1), today]):
        c.execute("INSERT INTO runs (workspace_id, session_id, prompt, status, started_at) VALUES (?, ?, 'x', 'ok', ?)",
                  (wid, "perf-1" if i < 3 else "perf-2", when.isoformat()))
    c.execute("INSERT INTO daily_allowance (session_id, day, extra) VALUES ('perf-1', ?, 3)", (so._today(),))
    c.commit()
    c.close()

    # ── /api/sessions: ít kết nối, số liệu ngày như cũ ───────────────────────
    sessions = so.list_sessions()
    bulk = so.daily_stats_all(sessions)
    check("daily numbers from one query match daily_stats for every agent (cap, allowance, yesterday)",
          all(bulk[s["id"]] == so.daily_stats(s["id"]) for s in sessions)
          and bulk["perf-1"]["used_today"] == 2 and bulk["perf-1"]["daily_limit"] == 8,
          str({k: v for k, v in bulk.items() if k in ("perf-1", "perf-2")}))
    app = so.build_app()
    opened, real = [0], so._conn

    def counting():
        opened[0] += 1
        return real()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"http://127.0.0.1:{so.ORCH_PORT}") as cl:
        so._conn = counting
        r = await cl.get("/api/sessions")
        so._conn = real
        check("/api/sessions opens a few database connections, not ~4 per agent",
              r.status_code == 200 and len(r.json()) == 12 and opened[0] <= 6, f"{opened[0]} for 12 agents")

        # ── Câu hỏi chạy liên tục phải dùng index ────────────────────────────
        p = plans(so.daily_stats_all, sessions) + plans(so.runs_today, "perf-1")
        check("runs of today are found through an index", all("idx_runs_started" in x for x in p
                                                             if "FROM runs" in x or "runs" in x), str(p))
        p = plans(so.eligible_signals)
        check("the signal queue checked every 5 s uses an index", p and all("idx_signals_status" in x for x in p), str(p))

        # ── MCP không giữ phiên ──────────────────────────────────────────────
        hdr = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "check", "version": "1"}}}
        for name in ("signal", "library", "boards", "voice"):
            r1 = await cl.post(f"/{name}/mcp", json=init, headers=hdr, follow_redirects=True)
            r2 = await cl.post(f"/{name}/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                               headers=hdr, follow_redirects=True)
            check(f"/{name}/mcp keeps no session, and works without one",
                  r1.status_code == 200 and "mcp-session-id" not in r1.headers
                  and r2.status_code == 200 and '"tools"' in r2.text, f"{r1.status_code} {r2.status_code} {r2.text[:120]}")

        # ── Xoá agent thì đóng editor của nó ─────────────────────────────────
        stopped, real_stop = [], so.editor_stop

        async def fake_stop(sid=None):
            stopped.append(sid)
            return 0
        so.editor_stop = fake_stop
        r = await cl.post("/api/sessions/perf-11/unregister")
        ws2 = (await cl.post("/api/workspaces", json={"name": "gone"})).json()["id"]
        so.register_session("perf-gone", "g", cwd=tmp, workspace_id=ws2)
        await cl.delete(f"/api/workspaces/{ws2}")
        so.editor_stop = real_stop
        check("removing an agent, or its workspace, closes its editor",
              r.status_code == 200 and stopped == ["perf-11", "perf-gone"], str(stopped))

        # ── /api/perf ────────────────────────────────────────────────────────
        procs = []
        if os.name == "posix":
            pty = so._PosixPty(["sleep", "30"], tmp, dict(os.environ))
            so._PTYS[pty.pid] = {"session": "perf-3", "kind": "terminal"}
        run = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "--resume", "perf-5"])
        zombie = subprocess.Popen([sys.executable, "-c", "pass"])
        procs += [run, zombie]
        time.sleep(1.0)
        d = (await cl.get("/api/perf")).json()
        rows = {p["pid"]: p for p in d.get("processes", [])}
        check("/api/perf names a run by its agent and workspace",
              rows.get(run.pid, {}).get("kind") == "run" and rows[run.pid]["agent"] == "agent-5"
              and rows[run.pid]["workspace"] == "perf" and rows[run.pid]["ram"] > 0, str(rows.get(run.pid)))
        if os.name == "posix":
            check("/api/perf names a terminal by its agent", rows.get(pty.pid, {}).get("kind") == "terminal"
                  and rows[pty.pid]["agent"] == "agent-3", str(rows.get(pty.pid)))
            check("/api/perf counts processes nobody cleaned up", d["zombies"] >= 1, str(d["zombies"]))
        check("/api/perf reports the computer, the server and how long it froze",
              d["system"]["ram_total"] > 0 and d["server"]["ram"] > 0 and "lag_ms" in d["server"], str(d.get("server")))
        sys.modules["psutil"] = None
        r = await cl.get("/api/perf")
        sys.modules["psutil"] = psutil
        check("without psutil the panel says how to install it, the server keeps running",
              r.status_code == 409 and "pip install psutil" in r.json()["error"], r.text[:120])
        for p in procs:
            p.kill()
            p.wait()

    # ── Terminal đóng không để lại zombie ────────────────────────────────────
    if os.name == "posix":
        pty.close()
        so._PTYS.pop(pty.pid, None)
        check("a closed terminal is cleaned up, not left as a zombie", wait_gone(pty.pid, 3) == "gone",
              state(pty.pid))
        stubborn = so._PosixPty(["sh", "-c", "trap '' TERM HUP; while :; do sleep 0.1; done"], tmp, dict(os.environ))
        time.sleep(0.3)
        stubborn.close()
        check("a terminal that ignores SIGTERM is killed after the grace period",
              wait_gone(stubborn.pid, 8) == "gone", state(stubborn.pid))

    # ── Dashboard ────────────────────────────────────────────────────────────
    js = (HERE / "static/orchestrator/app.js").read_text(encoding="utf-8")
    html = (HERE / "static/orchestrator/index.html").read_text(encoding="utf-8")
    src = (HERE / "session_orchestrator.py").read_text(encoding="utf-8")
    for want, where, blob in (('new EventSource("/api/events" + wsQuery())', "app.js", js),
                              ('if (ev && ev.type !== "run_event") scheduleRefresh();', "app.js", js),
                              ("refreshNext ??= refreshRun.then", "app.js", js),
                              ("if (unseen()) { refreshDirty = true; return; }", "app.js", js),
                              ("scrollback: 1000", "app.js", js),
                              ('onclick="perfOpen()"', "index.html", html),
                              ("edgeflow 1s steps(5) infinite", "index.html", html),
                              ("asyncio.Queue(maxsize=1000)", "session_orchestrator.py", src),
                              ("access_log=False", "session_orchestrator.py", src),
                              ("run_in_executor(_PTY_POOL, term.read)", "session_orchestrator.py", src)):
        check(f"{where} has {want}", want in blob, "not found")


asyncio.run(main())
(so.DB_DIR / "check_perf.db").unlink(missing_ok=True)
print()
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("all performance checks passed")
