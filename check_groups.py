#!/usr/bin/env python3
"""Guards for groups — the drawer that holds workspaces.

A group is only a label for the Home screen: it must not reach into anything a workspace owns.
The ways that goes wrong are all quiet ones:

  - deleting a group must delete the LABEL, not the workspaces filed under it. A workspace holds
    agents, signals and a folder of real work on disk, so a delete that cascaded would turn one
    misclick into lost work;
  - every workspace must always be in a group that exists. A workspace filed under a group id
    nobody has is invisible on Home while still running agents;
  - the default group is where migrated and API-created workspaces land, so it cannot be deleted;
  - a group is not a tenancy boundary. Moving a workspace between groups must leave its sessions,
    its folder and its signals exactly where they were.

Runs the real app in-process over ASGI against a throwaway DB.

    python3 check_groups.py
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
# DB + thư mục workspace riêng: check này tạo/xoá workspace, KHÔNG được đụng dữ liệu thật.
os.environ["ORCH_DB"] = "check_groups"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()
os.environ["ORCH_DRY_RUN"] = "1"

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)

db = so.DB_DIR / "check_groups.db"
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
        groups = (await c.get("/api/groups")).json()
        check("the default group exists out of the box",
              [g for g in groups if g["id"] == so.DEFAULT_GROUP], str(groups))

        # Workspace tạo qua API mà không nói group → phải rơi vào group mặc định, không mồ côi.
        plain = (await c.post("/api/workspaces", json={"name": "no-group-given"})).json()
        check("a workspace created without a group lands in the default one",
              plain["group_id"] == so.DEFAULT_GROUP, str(plain.get("group_id")))

        work = (await c.post("/api/groups", json={"name": "work"})).json()
        hobby = (await c.post("/api/groups", json={"name": "hobby"})).json()
        check("two groups can share nothing but the list",
              work["id"] != hobby["id"] and work["name"] == "work", str(work))
        r = await c.post("/api/groups", json={"name": "   "})
        check("a nameless group is refused", r.status_code == 400, str(r.status_code))

        a = (await c.post("/api/workspaces",
                          json={"name": "team-a", "group_id": work["id"]})).json()
        b = (await c.post("/api/workspaces",
                          json={"name": "team-b", "group_id": work["id"]})).json()
        check("a workspace is created straight into a group",
              a["group_id"] == work["id"], str(a.get("group_id")))
        ghost = (await c.post("/api/workspaces",
                              json={"name": "team-ghost", "group_id": "grp_nope"})).json()
        check("an unknown group falls back to the default instead of orphaning the workspace",
              ghost["group_id"] == so.DEFAULT_GROUP, str(ghost.get("group_id")))

        counts = {g["id"]: g["workspaces"] for g in (await c.get("/api/groups")).json()}
        check("the listing counts what is filed where", counts[work["id"]] == 2, str(counts))

        # Chuyển group: workspace giữ NGUYÊN mọi thứ của nó, chỉ đổi cái nhãn.
        before = so.get_workspace(b["id"])
        r = (await c.post(f"/api/workspaces/{b['id']}/group",
                          json={"group_id": hobby["id"]})).json()
        after = so.get_workspace(b["id"])
        check("moving a workspace changes its group", r["group_id"] == hobby["id"], str(r))
        check("and nothing else about it",
              {k: v for k, v in after.items() if k != "group_id"}
              == {k: v for k, v in before.items() if k != "group_id"}, str(after))
        r = await c.post(f"/api/workspaces/{b['id']}/group", json={"group_id": "grp_nope"})
        check("moving into a group that does not exist is refused",
              r.status_code == 400, f"{r.status_code} {r.text[:80]}")
        r = await c.post("/api/workspaces/ws_nope/group", json={"group_id": work["id"]})
        check("moving a workspace that does not exist is 404", r.status_code == 404,
              str(r.status_code))

        r = (await c.post(f"/api/groups/{work['id']}", json={"name": "day job"})).json()
        check("a group can be renamed", r["name"] == "day job", str(r))
        r = await c.post(f"/api/groups/{work['id']}", json={"name": ""})
        check("renaming it to nothing is refused", r.status_code == 400, str(r.status_code))

        # ── xoá group = xoá CÁI NHÃN ─────────────────────────────────────────
        r = (await c.delete(f"/api/groups/{work['id']}")).json()
        check("deleting a group reports what it re-filed", r.get("workspaces") == 1, str(r))
        check("the group is gone", so.get_group(work["id"]) is None)
        check("but the workspace in it is NOT — it moved to the default group",
              (so.get_workspace(a["id"]) or {}).get("group_id") == so.DEFAULT_GROUP,
              str(so.get_workspace(a["id"])))
        check("and its folder on disk was never touched",
              Path(so.get_workspace(a["id"])["root_dir"]).is_dir())

        r = await c.delete(f"/api/groups/{so.DEFAULT_GROUP}")
        check("the default group is refused — it is where everything falls back to",
              r.status_code == 400, f"{r.status_code} {r.text[:80]}")
        r = await c.delete("/api/groups/grp_nope")
        check("an unknown group is 404", r.status_code == 404, str(r.status_code))

        left = {g["id"] for g in (await c.get("/api/groups")).json()}
        check("only the groups that should be left are left",
              left == {so.DEFAULT_GROUP, hobby["id"]}, str(sorted(left)))

asyncio.run(main())
db.unlink(missing_ok=True)

if fails:
    print("\n" + "\n".join(fails))
    sys.exit(1)
print("\nall group checks passed")
