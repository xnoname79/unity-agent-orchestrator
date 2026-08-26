#!/usr/bin/env python3
"""Guards for listing the CLI sessions recorded in a folder, and resuming one from a card.

The terminal card lets you pick any past session of that folder instead of the session the card
belongs to. Two things can go wrong quietly:

  - the wrong folder's sessions get listed. Claude's transcript directory name is a LOSSY slug of
    the cwd ('/x/a_b' and '/x/a-b' both become '-x-a-b'), so the real cwd recorded inside the file
    is the only trustworthy filter;
  - the picked id lands in argv. It must be an id the target CLI actually has on disk, or a
    terminal turns into an arbitrary argument for `claude`/`codex`. A pin whose transcript is
    later deleted has to fall back, not kill the card.

The pin is one column (sessions.resume_id) precisely so the terminal and the background runs open
the SAME transcript — split them and you type into one file while signals land in another.

Also checks the preview label, since a picker whose rows all read the same is no picker: the
prompt the orchestrator seeds at spawn is skipped in favour of what the human typed next.

    python3 check_cli_sessions.py
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
os.environ["ORCH_DB"] = "check_cli_sessions"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()
os.environ["ORCH_DRY_RUN"] = "1"

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)

db = so.DB_DIR / "check_cli_sessions.db"
db.unlink(missing_ok=True)
so._ensure_db()

# Transcript giả trong thư mục tạm — check này KHÔNG được đọc ~/.claude, ~/.codex thật.
fake = Path(tempfile.mkdtemp())
so.CLAUDE_PROJECTS_DIR = fake / "claude" / "projects"
so.CODEX_SESSIONS_DIR = fake / "codex" / "sessions"
CWD = "/x/a_b"
OTHER = "/x/a-b"          # slug trùng CWD ('_' và '-' cùng thành '-') nhưng là thư mục KHÁC
SLUG = so.CLAUDE_PROJECTS_DIR / "-x-a-b"
SLUG.mkdir(parents=True)
(so.CODEX_SESSIONS_DIR / "2026" / "08" / "11").mkdir(parents=True)

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


def claude_file(sid, cwd, texts, mtime):
    f = SLUG / f"{sid}.jsonl"
    lines = [{"type": "queue-operation", "sessionId": sid}]
    lines += [{"type": "user", "cwd": cwd, "message": {"role": "user", "content": t}} for t in texts]
    f.write_text("\n".join(json.dumps(o) for o in lines), encoding="utf-8")
    os.utime(f, (mtime, mtime))
    return f


def codex_file(sid, cwd, texts, mtime):
    f = so.CODEX_SESSIONS_DIR / "2026" / "08" / "11" / f"rollout-2026-08-11T21-36-40-{sid}.jsonl"
    lines = [{"type": "session_meta", "payload": {"session_id": sid, "cwd": cwd}}]
    lines += [{"type": "event_msg", "payload": {"type": "user_message", "message": t}} for t in texts]
    f.write_text("\n".join(json.dumps(o) for o in lines), encoding="utf-8")
    os.utime(f, (mtime, mtime))
    return f


SEED = "Bạn là agent 'be' trong hệ thống multi-agent được điều phối. Trả lời ngắn gọn 'ready'."
claude_file("11111111-1111-4111-8111-111111111111", CWD, [SEED, "fix the login bug"], 3000)
claude_file("22222222-2222-4222-8222-222222222222", CWD, ["<command-name>/init</command-name>",
                                                          "write the readme"], 2000)
claude_file("33333333-3333-4333-8333-333333333333", OTHER, ["not this folder"], 4000)
codex_file("44444444-4444-4444-8444-444444444444", CWD, [SEED, "port the parser"], 1000)


async def main():
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        rows = so.list_cli_sessions(CWD, "claude")
        check("only this folder's claude sessions", [r["id"][:2] for r in rows] == ["11", "22"],
              [r["id"][:8] for r in rows])
        check("newest first", rows and rows[0]["id"].startswith("11"), rows and rows[0]["id"][:8])
        check("preview skips the seeded prompt", rows and rows[0]["preview"] == "fix the login bug",
              rows and rows[0]["preview"])
        check("preview skips system tags", len(rows) > 1 and rows[1]["preview"] == "write the readme",
              len(rows) > 1 and rows[1]["preview"])

        cx = so.list_cli_sessions(CWD, "codex")
        check("codex sessions read from session_meta",
              [r["id"][:2] for r in cx] == ["44"] and cx[0]["preview"] == "port the parser",
              [(r["id"][:8], r["preview"]) for r in cx])
        check("a folder with nothing recorded lists nothing",
              so.list_cli_sessions("/x/empty", "claude") == [])

        # Ghim phiên: id đi thẳng vào argv nên chỉ nhận id mà CLI thật sự có.
        sid = "55555555-5555-4555-8555-555555555555"
        pick = "11111111-1111-4111-8111-111111111111"
        so.register_session(sid, "term", cwd=CWD, model="claude-opus-5")
        r = await c.post(f"/api/sessions/{sid}/resume-id", json={"resume_id": pick})
        check("pinning a session of this folder is accepted",
              r.status_code == 200 and r.json()["resume_id"] == pick, r.text[:200])
        s = so.get_session(sid)
        check("terminal resumes the pinned session", so.terminal_argv(s, "claude")[-1] == pick,
              so.terminal_argv(s, "claude"))
        check("headless resumes the same one", so.resume_target(s, "claude") == pick,
              so.resume_target(s, "claude"))

        r = await c.post(f"/api/sessions/{sid}/resume-id",
                         json={"resume_id": "--dangerously-skip-permissions"})
        check("an id the CLI does not have is refused", r.status_code == 400, r.text[:200])
        r = await c.post(f"/api/sessions/{sid}/resume-id",
                         json={"resume_id": "44444444-4444-4444-8444-444444444444"})
        check("a codex transcript is refused for a claude session", r.status_code == 400,
              r.text[:200])
        check("a refused pin leaves the old one alone", so.get_session(sid)["resume_id"] == pick,
              so.get_session(sid)["resume_id"])

        # Transcript bị xoá ngoài đĩa: rơi về phiên của session, KHÔNG để CLI chết vì id lạ.
        (SLUG / f"{pick}.jsonl").rename(SLUG / "moved-away")
        check("a pin whose transcript is gone falls back to the session's own",
              so.resume_target(so.get_session(sid), "claude") == sid)
        (SLUG / "moved-away").rename(SLUG / f"{pick}.jsonl")

        r = await c.post(f"/api/sessions/{sid}/resume-id", json={"resume_id": ""})
        check("unpinning goes back to the session's own transcript",
              r.status_code == 200 and so.terminal_argv(so.get_session(sid), "claude")[-1] == sid,
              r.text[:200])

        r = await c.get(f"/api/cli-sessions?session={sid}")
        check("endpoint takes cwd + CLI from the session",
              r.status_code == 200 and r.json()["cwd"] == CWD
              and [x["id"][:2] for x in r.json()["sessions"]] == ["11", "22"], r.text[:200])
        r = await c.get(f"/api/cli-sessions?session={sid}&cli=codex")
        check("endpoint honours ?cli=", r.status_code == 200
              and [x["id"][:2] for x in r.json()["sessions"]] == ["44"], r.text[:200])
        r = await c.get("/api/cli-sessions?session=nope")
        check("no session and no cwd is refused", r.status_code == 400, r.text[:200])


asyncio.run(main())
db.unlink(missing_ok=True)
print()
print(f"{len(fails)} failed" if fails else "all good")
sys.exit(1 if fails else 0)
