#!/usr/bin/env python3
"""Guards for moving an agent between workspaces.

An agent moves so it can signal the agents in another workspace — routing resolves a signal by
(role, workspace), so this one column decides who can reach whom. Every check here is a way the
move could go wrong quietly rather than loudly:

  - a duplicate role name in the target workspace makes signals land on whichever session was
    active last, silently, because names are not unique and lookup is ORDER BY last_active;
  - a suspended workspace blocks every signal, so an agent moved into one just goes quiet.

Runs the real app in-process over ASGI against a throwaway DB.

    python3 check_workspace_move.py
"""
import asyncio
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
# DB + thư mục workspace riêng: check này tạo/sửa/xoá session, KHÔNG được đụng dữ liệu thật.
os.environ["ORCH_DB"] = "check_workspace_move"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()
os.environ["ORCH_DRY_RUN"] = "1"

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)   # một dòng INFO mỗi request = lấp mất kết quả

db = so.DB_DIR / "check_workspace_move.db"
db.unlink(missing_ok=True)            # chạy lại phải cho cùng kết quả
so._ensure_db()

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


async def main():
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        a = (await c.post("/api/workspaces", json={"name": "team-a"})).json()
        b = (await c.post("/api/workspaces", json={"name": "team-b"})).json()
        dead = (await c.post("/api/workspaces", json={"name": "team-dead"})).json()
        await c.post(f"/api/workspaces/{dead['id']}/suspend")

        async def register(sid, name, wid, model=""):
            r = await c.post("/api/sessions", json={"id": sid, "name": name,
                                                    "workspace_id": wid, "model": model,
                                                    "cwd": os.environ["ORCH_WORKSPACES_ROOT"]})
            assert r.status_code < 400, r.text
            return r.json()

        async def move(sid, wid, name=None):
            body = {"workspace_id": wid}
            if name:
                body["name"] = name
            return await c.post(f"/api/sessions/{sid}/workspace", json=body)

        await register("s-dev", "developer", a["id"])
        await register("s-clash", "developer", b["id"])       # b đã có 'developer'

        # Signal cũ, để chứng minh lịch sử KHÔNG bị viết lại theo session.
        await c.post("/api/signals", json={"to_session": "s-dev", "message": "hi",
                                           "from_role": "developer"})

        r = await move("s-dev", b["id"])
        check("a duplicate role name is refused", r.status_code == 409, f"{r.status_code} {r.text[:90]}")

        r = await move("s-dev", b["id"], name="developer-2")
        check("moving and renaming in one step works", r.status_code == 200, f"{r.status_code} {r.text[:90]}")
        s = so.get_session("s-dev")
        check("the session now lives in the target workspace",
              s["workspace_id"] == b["id"] and s["name"] == "developer-2", str(s))

        check("its folder did not move",
              s["cwd"] == os.environ["ORCH_WORKSPACES_ROOT"], s["cwd"])

        # Đây là ĐIỂM của cả tính năng: gửi được cho nhóm mới, và không còn thuộc nhóm cũ.
        check("signals now resolve it in the new workspace",
              so.resolve_session_id("developer-2", b["id"]) == "s-dev")
        check("and no longer in the old one",
              so.resolve_session_id("developer-2", a["id"]) is None)
        check("a role id from the old workspace stops resolving there",
              so.resolve_session_id("s-dev", a["id"]) is None)

        old = [x for x in so.list_signals(50, 0, a["id"])[0] if x["to_session"] == "s-dev"]
        check("its past signals stay in the old workspace (audit is not rewritten)", bool(old))

        r = await move("s-clash", dead["id"])
        check("a suspended workspace is refused", r.status_code == 409, f"{r.status_code} {r.text[:90]}")

        so.set_session_status("s-clash", "running")
        r = await move("s-clash", a["id"])
        check("a running session is refused", r.status_code == 409, f"{r.status_code} {r.text[:90]}")
        so.set_session_status("s-clash", "idle")

        r = await move("s-clash", "ws_does_not_exist")
        check("an unknown workspace is refused", r.status_code == 404, str(r.status_code))
        r = await move("nope", a["id"])
        check("an unknown session is refused", r.status_code == 404, str(r.status_code))

asyncio.run(main())
db.unlink(missing_ok=True)

if fails:
    print("\n" + "\n".join(fails))
    sys.exit(1)
print("\nall workspace-move checks passed")
