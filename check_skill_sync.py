#!/usr/bin/env python3
"""Guard for resyncing a role's SKILL into CLI roots that were added later.

_write_role_skill writes every root in CLI_SKILL_ROOTS, but only at spawn time and when the
SKILL is saved. A card created before a root existed (.agents landed after .claude/.codex)
keeps missing that copy forever, and the CLI reading that root runs with no playbook and says
nothing. The card shows a resync button driven by _skill_missing_roots; these checks cover the
detection, the copy, and the fact that copying twice changes nothing.

Runs the real app in-process over ASGI against a throwaway DB, in a temp cwd.

    python3 check_skill_sync.py
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
os.environ["ORCH_DB"] = "check_skill_sync"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()
os.environ["ORCH_DRY_RUN"] = "1"

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)

db = so.DB_DIR / "check_skill_sync.db"
db.unlink(missing_ok=True)
so._ensure_db()

# cwd giả: check này ghi file SKILL thật, không được đụng vào project nào của người dùng.
CWD = tempfile.mkdtemp()

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


def skill_of(role, root):
    return so._skill_path(CWD, role, root)


async def main():
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        async def sessions():
            return {s["id"]: s for s in (await c.get("/api/sessions")).json()}

        # 1. Card đủ bộ: save SKILL ghi cho MỌI root → không thiếu gì, không có nút.
        sid = str(uuid.uuid4())
        so.register_session(sid, "dev", cwd=CWD, model="claude-opus-5")
        r = await c.post(f"/api/sessions/{sid}/skill", json={"content": "playbook vai dev"})
        check("save writes every CLI root", r.status_code == 200
              and all(skill_of("dev", root).exists() for root in so.CLI_SKILL_ROOTS), r.text[:200])
        check("nothing missing right after a save",
              (await sessions())[sid]["skill_missing"] == [],
              (await sessions())[sid]["skill_missing"])

        # 2. Card kiểu cũ: xoá bản .agents = card ra đời trước khi root đó tồn tại.
        canon = skill_of("dev", ".claude").read_text(encoding="utf-8")
        skill_of("dev", ".agents").unlink()
        check("missing root is reported on the session list",
              (await sessions())[sid]["skill_missing"] == [".agents"],
              (await sessions())[sid]["skill_missing"])

        # 3. Nút resync: chép canon sang đúng root thiếu, KHÔNG đụng nội dung.
        r = await c.post(f"/api/sessions/{sid}/skill/sync")
        check("sync reports the root it wrote",
              r.status_code == 200 and r.json()["written"] == [str(skill_of("dev", ".agents"))],
              r.text[:200])
        check("the copy is byte-identical to the canonical SKILL",
              skill_of("dev", ".agents").read_text(encoding="utf-8") == canon)
        check("canonical SKILL untouched by the sync",
              skill_of("dev", ".claude").read_text(encoding="utf-8") == canon)
        check("nothing missing after the sync",
              (await sessions())[sid]["skill_missing"] == [],
              (await sessions())[sid]["skill_missing"])

        # 4. Bấm lại: không còn gì để chép → 400, và file không đổi (frontmatter không bị chèn 2 lần).
        r = await c.post(f"/api/sessions/{sid}/skill/sync")
        check("second sync refuses instead of rewriting",
              r.status_code == 400 and "nothing to sync" in r.text, r.text[:200])
        check("files unchanged by the refused sync",
              all(skill_of("dev", root).read_text(encoding="utf-8") == canon
                  for root in so.CLI_SKILL_ROOTS))

        # 5. Vai chưa từng có playbook: không có canon để nhân bản → không nút, không ghi bừa.
        sid2 = str(uuid.uuid4())
        so.register_session(sid2, "blank", cwd=CWD, model="claude-opus-5")
        check("a role with no SKILL at all shows no button",
              (await sessions())[sid2]["skill_missing"] == [],
              (await sessions())[sid2]["skill_missing"])
        r = await c.post(f"/api/sessions/{sid2}/skill/sync")
        check("sync on a role with no SKILL is refused", r.status_code == 400, r.text[:200])
        check("refused sync created no directory",
              not any(skill_of("blank", root).exists() for root in so.CLI_SKILL_ROOTS))

        # 6. Session không tồn tại → 404, không phải 500.
        r = await c.post(f"/api/sessions/{uuid.uuid4()}/skill/sync")
        check("sync on an unknown session is 404", r.status_code == 404, r.text[:200])


asyncio.run(main())
db.unlink(missing_ok=True)
print()
print(f"{len(fails)} failed" if fails else "all good")
sys.exit(1 if fails else 0)
