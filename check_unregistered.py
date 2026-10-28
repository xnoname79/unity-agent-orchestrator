#!/usr/bin/env python3
"""Guards for Tools › Unregistered agents — deleting an unregistered agent for good.

Unregister only removes the card: runs, signals and the CLI transcript stay. The panel lists what
is left and deletes it, and that delete cannot be undone, so the ways it goes wrong are all about
deleting the WRONG thing:

  - a registered agent, or the private thread a board still runs on, must never be listed or
    deleted. The browser sends ids, so the server must re-check each one itself;
  - a transcript that a live card resumes (its pin) must survive, or that card silently loses
    its whole conversation on its next run;
  - an agent with a run still in progress must be refused — its events would keep landing in rows
    that were just deleted;
  - signals it SENT are stored by role name, not id. A live agent with the same name owns rows
    with the same sender, so they must stay;
  - the role SKILL file lives in the user's project folder and may be committed: never touched.

Also checks that Unregister now records the name and folder, since without them the list can
only show a bare id.

    python3 check_unregistered.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["ORCH_DB"] = "check_unregistered"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()
os.environ["ORCH_DRY_RUN"] = "1"

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)

db = so.DB_DIR / "check_unregistered.db"
db.unlink(missing_ok=True)
so._ensure_db()

# Transcript giả trong thư mục tạm — check này KHÔNG được đọc/xoá ~/.claude, ~/.codex thật.
fake = Path(tempfile.mkdtemp())
so.CLAUDE_PROJECTS_DIR = fake / "claude" / "projects"
so.CODEX_SESSIONS_DIR = fake / "codex" / "sessions"
so.AGY_CONVERSATIONS_DIR = fake / "agy" / "conversations"
so.AGY_SUMMARIES_DB = fake / "agy" / "conversation_summaries.db"
CWD = str(fake / "proj")
SLUG = so.CLAUDE_PROJECTS_DIR / "-proj"
SLUG.mkdir(parents=True)
(so.CODEX_SESSIONS_DIR / "2026" / "10" / "27").mkdir(parents=True)

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


def claude_file(sid):
    f = SLUG / f"{sid}.jsonl"
    f.write_text(json.dumps({"type": "user", "cwd": CWD, "message": {"role": "user", "content": "hi"}}),
                 encoding="utf-8")
    return f


def codex_file(sid):
    f = so.CODEX_SESSIONS_DIR / "2026" / "10" / "27" / f"rollout-2026-10-27T10-00-00-{sid}.jsonl"
    f.write_text(json.dumps({"type": "session_meta", "payload": {"session_id": sid, "cwd": CWD}}),
                 encoding="utf-8")
    return f


def run(sid, prompt, status="ok"):
    rid = so.start_run(sid, None, prompt, so._now())
    so.record_run_event(rid, sid, None, 1, "text", "step", {})
    if status != "running":
        so.finish_run(rid, {}, status, 0, so._now())


def count(sql, *args):
    conn = so._conn()
    n = conn.execute(sql, args).fetchone()[0]
    conn.close()
    return n


GONE = "11111111-1111-4111-8111-111111111111"     # gỡ bằng Unregister (có removed_sessions)
OLD = "22222222-2222-4222-8222-222222222222"      # gỡ trước khi có removed_sessions: chỉ còn runs
PINNED = "33333333-3333-4333-8333-333333333333"   # transcript mà card còn sống đang ghim
BUSY = "44444444-4444-4444-8444-444444444444"     # còn run đang chạy
LIVE = "55555555-5555-4555-8555-555555555555"     # card còn đăng ký
BOARD = "66666666-6666-4666-8666-666666666666"    # thread riêng của board
OWN_PIN = "77777777-7777-4777-8777-777777777777"  # phiên GONE ghim — không ai khác dùng
CX = "019a0000-0000-7000-8000-000000000001"       # đã unregister, transcript ở codex

so.register_session(GONE, "writer", cwd=CWD)
so.register_session(LIVE, "editor", cwd=CWD)
so.register_session(BOARD, "board-thread", cwd=CWD)
so.register_session(CX, "porter", cwd=CWD, model="gpt-5.5")
conn = so._conn()
conn.execute("UPDATE sessions SET resume_id = ? WHERE id = ?", (OWN_PIN, GONE))
conn.execute("INSERT INTO boards (id, session_id, owner_id, name, created_at) VALUES ('brd_x', ?, ?, 'b', ?)",
             (BOARD, LIVE, so._now()))
conn.commit()
conn.close()
for sid, prompt in ((GONE, "write the intro"), (OLD, "[Role: painter]\n[Signal from: user]\npaint"),
                    (OLD, "add shadows"), (PINNED, "old work"), (LIVE, "edit"), (BOARD, "plan"),
                    (CX, "port it")):
    run(sid, prompt)
run(BUSY, "still going", status="running")
so.enqueue_signal(GONE, "please write", from_session="editor")
so.enqueue_signal(LIVE, "from writer", from_session="writer")   # signal GONE đã GỬI, theo tên
so.grant_daily_allowance(GONE)
for sid in (GONE, OLD, PINNED, LIVE, OWN_PIN):
    claude_file(sid)
cxf = codex_file(CX)
env = so.CLAUDE_PROJECTS_DIR.parent / "session-env" / GONE
env.mkdir(parents=True)
skill = Path(CWD) / ".claude" / "skills" / "writer" / "SKILL.md"
skill.parent.mkdir(parents=True)
skill.write_text("# writer", encoding="utf-8")
so.unregister_session(GONE)
so.unregister_session(CX)
conn = so._conn()
conn.execute("UPDATE sessions SET resume_id = ? WHERE id = ?", (PINNED, LIVE))   # LIVE ghim PINNED
conn.commit()
conn.close()


async def main():
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        r = await c.get("/api/unregistered")
        rows = {x["id"]: x for x in r.json().get("agents", [])}
        check("lists every unregistered agent and nothing else",
              set(rows) == {GONE, OLD, PINNED, BUSY, CX}, sorted(x[:2] for x in rows))
        g = rows.get(GONE, {})
        check("Unregister keeps the name and folder", (g.get("name"), g.get("cwd")) == ("writer", CWD), g)
        check("counts its runs and signals", (g.get("runs"), g.get("signals")) == (1, 1), g)
        check("finds its transcript and the one it pinned", g.get("transcripts") == 2, g)
        check("preview is the last thing it was asked", g.get("preview") == "write the intro", g)
        o = rows.get(OLD, {})
        check("an old agent's name is read back from its prompts", o.get("name") == "painter", o)
        check("an old agent's preview is its latest prompt", o.get("preview") == "add shadows", o)
        check("an old agent's folder comes from its transcript", o.get("cwd") == CWD, o)
        check("a transcript a live card resumes is marked in use", rows.get(PINNED, {}).get("in_use") == ["editor"],
              rows.get(PINNED))
        check("a running agent is marked running", rows.get(BUSY, {}).get("running") == 1, rows.get(BUSY))

        async def rm(*ids):
            return await c.post("/api/unregistered/delete", json={"ids": list(ids)})

        r = await c.post("/api/unregistered/delete", json={"ids": "x"})
        check("rejects a body without a list of ids", r.status_code == 400, r.status_code)
        r = (await rm(LIVE, BOARD, "../../etc", PINNED, BUSY)).json()
        check("refuses registered, board, unknown, pinned and running ids",
              r["deleted"] == [] and set(r["errors"]) == {LIVE, BOARD, "../../etc", PINNED, BUSY}, r)
        check("the refused ones keep their runs",
              count("SELECT COUNT(*) FROM runs WHERE session_id IN (?, ?, ?, ?)", LIVE, BOARD, PINNED, BUSY) == 4)
        check("a pinned transcript survives", (SLUG / f"{PINNED}.jsonl").exists())

        r = (await rm(GONE, OLD, CX)).json()
        check("deletes the unregistered ones", sorted(x["id"] for x in r["deleted"]) == sorted([GONE, OLD, CX])
              and not r["errors"], r)
        for t, sql in (("its runs", "SELECT COUNT(*) FROM runs WHERE session_id = ?"),
                       ("its run steps", "SELECT COUNT(*) FROM run_events WHERE session_id = ?"),
                       ("signals sent to it", "SELECT COUNT(*) FROM signals WHERE to_session = ?"),
                       ("its extra daily runs", "SELECT COUNT(*) FROM daily_allowance WHERE session_id = ?"),
                       ("its removed record", "SELECT COUNT(*) FROM removed_sessions WHERE id = ?")):
            check(f"{t}: gone", count(sql, GONE) == 0)
        check("its transcript, its pin and its session-env are gone",
              not (SLUG / f"{GONE}.jsonl").exists() and not (SLUG / f"{OWN_PIN}.jsonl").exists()
              and not env.exists())
        check("a codex transcript is deleted too", not cxf.exists())
        check("signals it SENT stay (stored by role name)",
              count("SELECT COUNT(*) FROM signals WHERE from_session = 'writer'") == 1)
        check("the role SKILL file in the project folder stays", skill.exists())
        check("live agents keep their runs and transcripts",
              count("SELECT COUNT(*) FROM runs WHERE session_id = ?", LIVE) == 1
              and (SLUG / f"{LIVE}.jsonl").exists())
        r = (await rm(GONE)).json()
        check("deleting twice is refused, not a crash", list(r["errors"]) == [GONE], r)
        r = await c.get("/api/unregistered")
        check("the list shows only what is left",
              {x["id"] for x in r.json()["agents"]} == {PINNED, BUSY}, r.text[:200])


asyncio.run(main())
db.unlink(missing_ok=True)
print()
print(f"{len(fails)} failed" if fails else "all good")
sys.exit(1 if fails else 0)
