#!/usr/bin/env python3
"""Guards for paging the Team tab: GET /api/team?sub_limit=&sub_offset=&filter=&dec_limit=&dec_offset=.

The Team tab loads 10 sub-agent cards and 10 decisions at a time and fetches more on scroll. What
can go wrong quietly:
  - pages out of order or overlapping, so scrolling shows a card twice or skips one;
  - "more" staying true at the end, so the dashboard keeps asking for empty pages;
  - column headers counting the loaded page instead of the whole filtered set;
  - the call WITHOUT paging params changing shape — Home and the director's panel rely on it.

    python3 check_team_paging.py
"""
import asyncio
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TMP = Path(tempfile.mkdtemp(prefix="check_team_paging_"))
os.environ.update({"ORCH_DB": "check_team_paging", "ORCH_WORKSPACES_ROOT": str(TMP / "ws"),
                   "ORCH_DRY_RUN": "1", "CLAUDE_BIN": sys.executable, "ORCH_AGY_BIN": sys.executable})
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx                        # noqa: E402

import session_orchestrator as so   # noqa: E402
import team                         # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


db = so.DB_DIR / "check_team_paging.db"
for ext in ("", "-wal", "-shm"):
    Path(str(db) + ext).unlink(missing_ok=True)
so.init_db()

WS = so.DEFAULT_WORKSPACE
so.register_session("dir-1", "lead", cwd=str(TMP), model="", workspace_id=WS)
so.register_session("dir-2", "peer", cwd=str(TMP), model="", workspace_id=WS)

STATUSES = ["queued", "running", "fixing", "verifying", "passed", "failed", "cancelled"]
now = datetime.now()
seeded = []
for i in range(25):
    st, created = STATUSES[i % len(STATUSES)], (now - timedelta(minutes=100 - i)).isoformat()
    # Hai việc passed cũ (2 ngày): không thuộc "Active and recent", không vào "passed · 24 h".
    old = st == "passed" and i < 10
    ended = (now - timedelta(days=2)).isoformat() if old else created
    sid = f"st_{i:04d}"
    team._exec("INSERT INTO subtasks (id, workspace_id, director_id, kind, title, status, attempts, "
               "created_at, updated_at, ended_at) VALUES (?, ?, ?, 'subagent', ?, ?, ?, ?, ?, ?)",
               (sid, WS, "dir-1" if i % 5 else "dir-2", f"task {i}", st, 1 if i % 4 == 0 else 0,
                created, ended, "" if st in team.ACTIVE else ended))
    seeded.append({"id": sid, "status": st, "old": old, "director": "dir-1" if i % 5 else "dir-2"})
team._exec("INSERT INTO subtasks (id, workspace_id, director_id, kind, status, created_at, updated_at) "
           "VALUES ('st_reflect', ?, 'dir-1', 'reflect', 'failed', ?, ?)", (WS, now.isoformat(), now.isoformat()))
for i in range(25):
    team._exec("INSERT INTO team_decisions (workspace_id, session_id, decision, ts) VALUES (?, 'dir-1', ?, ?)",
               (WS, f"decision {i}", now.isoformat()))

newest = [s["id"] for s in reversed(seeded)]
want = {k: sum(s["status"] in v for s in seeded) for k, v in
        {"building": ("queued", "running", "fixing"), "verifying": ("verifying",), "passed": ("passed",),
         "failed": ("failed",), "cancelled": ("cancelled",)}.items()}


async def main():
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        async def get(**p):
            r = await c.get("/api/team", params={"workspace_id": WS, **p})
            return r.json() if r.status_code == 200 else {"error": r.status_code}

        old = await get()
        p1 = await get(sub_limit=10, sub_offset=0, filter="all", dec_limit=10, dec_offset=0)
        ids = [s["id"] for s in p1.get("subtasks", [])]
        check("page 1 is the 10 newest subtasks, newest first", ids == newest[:10], str(ids))
        check("page 1 says there are more subtasks", p1.get("subtasks_more") is True, str(p1.get("subtasks_more")))
        dec = [d["decision"] for d in p1.get("decisions", [])]
        check("page 1 is the 10 newest decisions", dec == [f"decision {i}" for i in range(24, 14, -1)], str(dec))
        check("page 1 says there are more decisions", p1.get("decisions_more") is True,
              str(p1.get("decisions_more")))

        p2 = await get(sub_limit=10, sub_offset=10, filter="all", dec_limit=10, dec_offset=10)
        check("page 2 continues where page 1 ended",
              [s["id"] for s in p2.get("subtasks", [])] == newest[10:20])
        p3 = await get(sub_limit=10, sub_offset=20, filter="all", dec_limit=10, dec_offset=20)
        ids3 = [s["id"] for s in p3.get("subtasks", [])]
        check("the last page holds the remaining 5 and says no more",
              ids3 == newest[20:] and p3.get("subtasks_more") is False, f"{ids3} {p3.get('subtasks_more')}")
        dec3 = [d["decision"] for d in p3.get("decisions", [])]
        check("the last decision page holds 5 and says no more",
              len(dec3) == 5 and dec3[-1] == "decision 0" and p3.get("decisions_more") is False, str(dec3))

        cnt = {k: (p1.get("counts") or {}).get(k) for k in want}
        check("counts cover the whole filtered set, not the page", cnt == want, f"{cnt} != {want}")
        check("counts do not change with the page",
              all((p.get("counts") or {}).get(k) == want[k] for p in (p2, p3) for k in want))
        st = p1.get("stats") or {}
        check("stats count passed / failed / sent back in the last 24 h",
              (st.get("passed_24h"), st.get("failed_24h"), st.get("sent_back_24h"))
              == (sum(s["status"] == "passed" and not s["old"] for s in seeded),
                  want["failed"], sum(i % 4 == 0 and not seeded[i]["old"] for i in range(25))), str(st))
        check("the reflect subtask never shows in the Team board",
              all(s["kind"] != "reflect" for p in (p1, p2, p3) for s in p.get("subtasks", [])))

        f = await get(sub_limit=10, filter="failed")
        check("filter=failed returns only failed ones, all of them",
              [s["id"] for s in f.get("subtasks", [])] == [s for s in newest if seeded[int(s[3:])]["status"] == "failed"]
              and f.get("counts", {}).get("failed") == want["failed"] and f.get("counts", {}).get("passed") == 0,
              str([s["status"] for s in f.get("subtasks", [])]))
        a = await get(sub_limit=50, filter="active")
        check("filter=active drops cancelled and long-finished ones",
              {s["id"] for s in a.get("subtasks", [])}
              == {s["id"] for s in seeded if s["status"] != "cancelled" and not s["old"]})
        d = await get(sub_limit=50, filter="all", director_id="dir-2")
        check("director_id narrows the board to one director's subtasks",
              {s["id"] for s in d.get("subtasks", [])} == {s["id"] for s in seeded if s["director"] == "dir-2"}
              and d.get("counts", {}).get("total") == 5 and sorted(d.get("givers") or []) == ["dir-1", "dir-2"])

        bad = await get(sub_limit="x", sub_offset="-4", filter="nope", dec_limit="9999")
        check("bad paging values fall back to defaults instead of erroring",
              len(bad.get("subtasks", [])) <= 10 and len(bad.get("decisions", [])) == 25, str(bad)[:200])

        check("the call without paging params keeps its keys and caps",
              set(old) == {"directors", "subtasks", "decisions", "proposals", "edges", "graph_mode", "models",
                           "engines", "playbooks", "max_active", "max_attempts", "now"}
              and len(old["subtasks"]) == 26 and len(old["decisions"]) == 25, str(sorted(old))[:300])
        for _ in range(185):
            team._exec("INSERT INTO team_decisions (workspace_id, session_id, decision, ts) VALUES (?, 'dir-1', 'x', ?)",
                       (WS, now.isoformat()))
        check("the call without paging params still caps decisions at 60", len((await get())["decisions"]) == 60)


asyncio.run(main())
for ext in ("", "-wal", "-shm"):
    Path(str(db) + ext).unlink(missing_ok=True)
print(f"\n{len(fails)} failure(s)" if fails else "\nall team paging checks passed")
sys.exit(1 if fails else 0)
