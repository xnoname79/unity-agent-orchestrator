#!/usr/bin/env python3
"""Guards for moving an agent between workspaces, renaming one, and deleting one.

An agent moves so it can signal the agents in another workspace — routing resolves a signal by
(role, workspace), so this one column decides who can reach whom. Every check here is a way the
move could go wrong quietly rather than loudly:

  - a duplicate role name in the target workspace makes signals land on whichever session was
    active last, silently, because names are not unique and lookup is ORDER BY last_active;
  - a suspended workspace blocks every signal, so an agent moved into one just goes quiet.

Renaming goes wrong just as quietly:

  - the role's playbook lives in <cwd>/<root>/skills/<name>/, so a rename that leaves it behind
    leaves the agent without its playbook, and nothing says so;
  - other agents, and the renamed agent itself, still remember the old name from their
    transcripts. A signal sent to (or signed with) the old name must still reach the card.

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

        # Xoá workspace: gỡ mọi agent rồi xoá cái vỏ. Điểm của nó là không phải Unregister tay
        # từng cái — nên phải chứng minh session biến mất THẬT mà audit thì không.
        # Workspace RIÊNG cho phần xoá: dựa vào ai còn sót lại sau các phép move ở trên là đếm
        # sai ngay khi thêm/bớt một fixture phía trước.
        doomed = (await c.post("/api/workspaces", json={"name": "team-doomed"})).json()
        await register("s-doomed", "developer", doomed["id"])
        await c.post("/api/signals", json={"to_session": "s-doomed", "message": "bye",
                                           "from_role": "developer"})
        r = await c.delete(f"/api/workspaces/{doomed['id']}")
        check("deleting a workspace unregisters the agents still in it",
              r.status_code == 200 and r.json().get("sessions") == 1, f"{r.status_code} {r.text[:90]}")
        check("its agents are gone from the orchestrator", so.get_session("s-doomed") is None)
        check("the workspace itself is gone", so.get_workspace(doomed["id"]) is None)
        check("but its signals stay for audit", bool(so.list_signals(50, 0, doomed["id"])[0]))

        # ── Đổi tên ──────────────────────────────────────────────────────────────
        proj = tempfile.mkdtemp()
        tmpl = ("# <ROLE_NAME>\nPlaybook for the <ROLE_NAME> role.\n"
                'send_signal(to_role="x", from_role="<ROLE_NAME>")\nA designer reviews every frame.\n')

        async def agent(sid, name, wid, skill=True):
            r = await c.post("/api/sessions", json={"id": sid, "name": name, "workspace_id": wid,
                                                    "cwd": proj})
            assert r.status_code < 400, r.text
            if skill:
                so._write_role_skill(proj, name, tmpl)

        async def rename(sid, name):
            return await c.post(f"/api/sessions/{sid}/name", json={"name": name})

        await agent("s-ren", "designer", a["id"])
        await agent("s-peer", "peer", a["id"], skill=False)
        r = await rename("s-ren", "art-director")
        check("renaming a card works", r.status_code == 200 and r.json()["name"] == "art-director"
              and r.json()["renamed_from"] == "designer", f"{r.status_code} {r.text[:90]}")
        check("signals to the new name reach it",
              so.resolve_session_id("art-director", a["id"]) == "s-ren")
        check("signals to the old name still reach it (agents remember the old name)",
              so.resolve_session_id("designer", a["id"]) == "s-ren")
        moved = all(so._skill_path(proj, "art-director", root).exists()
                    and not so._skill_path(proj, "designer", root).parent.exists()
                    for root in so.CLI_SKILL_ROOTS)
        check("its playbook moves with it, for every CLI", moved)
        txt = so._role_skill(proj, "art-director")
        check("the playbook names the new role",
              "name: art-director" in txt and "# art-director" in txt
              and 'from_role="art-director"' in txt and "the art-director role" in txt, txt[:200])
        check("but the rest of the playbook is left alone",
              "A designer reviews every frame." in txt, txt)

        r = await c.post("/api/signals", json={"to_role": "peer", "message": "done",
                                               "from_role": "designer", "workspace_id": a["id"]})
        row = [x for x in so.list_signals(50, 0, a["id"])[0] if x["id"] == r.json().get("id")]
        check("a signal signed with the old name is recorded under the new one",
              row and row[0]["from_session"] == "art-director", str(row)[:120])
        import signal_mcp
        check("the MCP tools accept the old name as a sender",
              signal_mcp._check_from_role(so, "designer") == ""
              and signal_mcp._sender_workspace(so, "designer") == a["id"])

        r = await rename("s-ren", "peer")
        check("a name already taken in the workspace is refused", r.status_code == 409,
              f"{r.status_code} {r.text[:90]}")
        for bad in ("", "../escape", "a/b"):
            r = await rename("s-ren", bad)
            check(f"a bad name {bad!r} is refused", r.status_code == 400, str(r.status_code))
        so.set_session_status("s-ren", "running")
        r = await rename("s-ren", "busy-now")
        check("a running card is refused", r.status_code == 409, str(r.status_code))
        so.set_session_status("s-ren", "idle")

        await agent("s-new", "designer", a["id"], skill=False)
        check("a new card that takes the old name gets it back",
              so.resolve_session_id("designer", a["id"]) == "s-new")

        # Thư mục skills của một vai khác (workspace khác, cùng cwd) mang đúng tên định đổi sang.
        await agent("s-other", "taken", b["id"])
        await agent("s-col", "col", a["id"])
        r = await rename("s-col", "taken")
        check("a rename that would overwrite another role's playbook is refused",
              r.status_code == 409 and so.get_session("s-col")["name"] == "col"
              and so._skill_path(proj, "col").exists(), f"{r.status_code} {r.text[:90]}")

        # Cùng cwd, cùng tên, hai workspace = MỘT playbook dùng chung.
        await agent("s-sh1", "shared", a["id"])
        await agent("s-sh2", "shared", b["id"], skill=False)
        await rename("s-sh1", "shared-a")
        check("renaming one of two cards that share a playbook copies it instead of taking it",
              so._skill_path(proj, "shared").exists() and so._skill_path(proj, "shared-a").exists())

        await agent("s-mv", "mover", a["id"])
        r = await move("s-mv", b["id"], name="mover-b")
        check("moving and renaming keeps the playbook",
              r.status_code == 200 and so._skill_path(proj, "mover-b").exists()
              and not so._skill_path(proj, "mover").exists(), f"{r.status_code} {r.text[:90]}")

        so.unregister_session("s-ren")
        check("removing a card drops its old names", not so.alias_sessions("designer")
              and so.resolve_session_id("designer", a["id"]) == "s-new")

        r = await c.delete("/api/workspaces/default")
        check("the default workspace is refused", r.status_code == 400, f"{r.status_code} {r.text[:90]}")
        r = await c.delete("/api/workspaces/ws_does_not_exist")
        check("an unknown workspace is refused", r.status_code == 404, str(r.status_code))

asyncio.run(main())
db.unlink(missing_ok=True)

if fails:
    print("\n" + "\n".join(fails))
    sys.exit(1)
print("\nall workspace-move checks passed")
