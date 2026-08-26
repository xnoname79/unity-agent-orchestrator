#!/usr/bin/env python3
"""Guards for changing a session's model when the change also changes its ENGINE.

The session id is the primary key: runs, signals and history all point at it, so switching
engines must keep the id. Whether that is safe depends on the target CLI:

  - claude can adopt a foreign id (`claude -p --session-id <uuid>`), so codex -> claude works,
    but only for an id that is a real UUID (a session registered under some other id is not);
  - codex has no flag to pick a thread id, so a session may only go back to codex if its
    rollout file is still on disk;
  - a session already on the target engine must not be re-adopted (the CLI rejects a
    session id that is already in use).

Runs the real app in-process over ASGI against a throwaway DB. No CLI is spawned (dry-run).

    python3 check_model_switch.py
"""
import asyncio
import logging
import os
import sys
import tempfile
import uuid
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["ORCH_DB"] = "check_model_switch"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()
os.environ["ORCH_DRY_RUN"] = "1"

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)

db = so.DB_DIR / "check_model_switch.db"
db.unlink(missing_ok=True)
so._ensure_db()

# Transcript của hai CLI trỏ vào thư mục tạm: check này quyết định theo file có/không tồn tại,
# không được đọc ~/.claude và ~/.codex thật của người dùng.
fake = Path(tempfile.mkdtemp())
so.CLAUDE_PROJECTS_DIR = fake / "claude"
so.CODEX_SESSIONS_DIR = fake / "codex"
(so.CLAUDE_PROJECTS_DIR / "proj").mkdir(parents=True)
(so.CODEX_SESSIONS_DIR / "2026" / "08" / "11").mkdir(parents=True)

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


def codex_born(sid):
    (so.CODEX_SESSIONS_DIR / "2026" / "08" / "11" / f"rollout-2026-08-11T21-36-40-{sid}.jsonl").touch()


def claude_born(sid):
    (so.CLAUDE_PROJECTS_DIR / "proj" / f"{sid}.jsonl").touch()


async def main():
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        async def set_model(sid, model):
            return await c.post(f"/api/sessions/{sid}/model", json={"model": model})

        # 1. codex -> claude: id là uuid, claude nhận nuôi được → cho đổi.
        sid = str(uuid.uuid4())
        so.register_session(sid, "cx", model="codex:gpt-5.6-luna")
        codex_born(sid)
        r = await set_model(sid, "claude-opus-5")
        check("codex -> claude allowed", r.status_code == 200, r.text[:200])
        check("model persisted", so.get_session(sid)["model"] == "claude-opus-5",
              so.get_session(sid)["model"])

        # 2. quay lại codex: rollout còn trên đĩa → cho về.
        r = await set_model(sid, "codex")
        check("claude -> codex allowed when rollout exists", r.status_code == 200, r.text[:200])

        # 3. session claude thuần → codex: codex chưa từng thấy id này, chặn.
        sid2 = str(uuid.uuid4())
        so.register_session(sid2, "cl", model="claude-opus-5")
        claude_born(sid2)
        r = await set_model(sid2, "codex")
        check("claude -> codex blocked without rollout", r.status_code == 400, r.text[:200])
        check("session unchanged after block", so.get_session(sid2)["model"] == "claude-opus-5",
              so.get_session(sid2)["model"])

        # 4. id KHÔNG phải uuid (session đăng ký sẵn với id riêng) → claude: `--session-id` từ
        #    chối, không nhận nuôi được, chặn kèm lý do.
        sid3 = "sess-" + uuid.uuid4().hex
        so.register_session(sid3, "odd", model="codex:gpt-5.6-luna")
        codex_born(sid3)
        r = await set_model(sid3, "claude-opus-5")
        check("non-UUID id -> claude blocked (id not a UUID)",
              r.status_code == 400 and "not a UUID" in r.text, r.text[:200])

        # 5. cùng engine → không đụng gì tới CLI, luôn cho đổi.
        r = await set_model(sid2, "claude-haiku-4-5")
        check("claude -> claude allowed", r.status_code == 200, r.text[:200])
        r = await set_model(sid3, "codex:gpt-5.6-terra")
        check("codex -> codex allowed", r.status_code == 200, r.text[:200])

        # 6. adopt KHÔNG chạy lại khi transcript đã có (CLI trả 'already in use').
        check("adopt is a no-op for a session claude already knows",
              await so._adopt_session_for_claude(so.get_session(sid2)) == "")


asyncio.run(main())
db.unlink(missing_ok=True)
print()
print(f"{len(fails)} failed" if fails else "all good")
sys.exit(1 if fails else 0)
