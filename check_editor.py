#!/usr/bin/env python3
"""Guards for the editor card (nvim).

The card replaced `code serve-web`, and the whole point was to stop owning process state:

  - the argv must go through tmux when tmux exists, so closing the browser tab DETACHES instead
    of killing the buffer. Drop the tmux wrapper and every reload silently loses unsaved work;
  - `tmux ls` is the registry. If open/close does not round-trip through it, the dashboard and
    the machine disagree about what is running — which is exactly the failure the VS Code card
    had (a dict in RAM, orphans after every restart);
  - a tmux session whose orchestrator session was deleted must NOT produce a card. It would be a
    ghost the user cannot act on;
  - shutting the orchestrator down must LEAVE the sessions alone. That is the feature: restart the
    server, reopen the dashboard, the buffer is still there.

    python3 check_editor.py
"""
import asyncio
import os
import shutil
import subprocess
import sys
import time
import tempfile
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

tmp = Path(tempfile.mkdtemp())
# Repo git thật: :DiffviewOpen ngoài repo chỉ báo lỗi, không mở gì — test sẽ xanh giả nếu chỉ
# kiểm "lệnh đã gửi đi".
subprocess.run(["git", "init", "-q"], cwd=tmp, check=False)
(tmp / "tracked.txt").write_text("one\n", encoding="utf-8")
subprocess.run(["git", "add", "-A"], cwd=tmp, check=False)
subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
               cwd=tmp, check=False)
(tmp / "tracked.txt").write_text("two\n", encoding="utf-8")
(tmp / "dirty.txt").write_text("untracked\n", encoding="utf-8")

os.environ["ORCH_DB"] = "check_editor"
os.environ["ORCH_DRY_RUN"] = "1"
os.environ["ORCH_WORKSPACES_ROOT"] = str(tmp / "ws")
os.environ["ORCH_CLAUDE_CONFIG"] = str(tmp / "claude.json")

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

# tmux trên SOCKET RIÊNG (-L). Hai lý do, cả hai bắt buộc:
#   1. test được phép `kill-server` — mà giết server mặc định là giết sạch tmux của người dùng;
#   2. chỉ khi KHÔNG có server sẵn thì `new-session` mới phải TỰ DỰNG một cái, và đó chính là
#      điều kiện làm lộ bug ống stdout bị kế thừa (xem _tmux_run). Máy đang chạy tmux thì bug
#      im lặng biến mất, nên test không có socket riêng là test xanh giả.
_wrap = tmp / "tmux-guard"
_real = shutil.which("tmux")
if _real:
    _wrap.write_text(f'#!/bin/sh\nexec {_real} -L orchguard "$@"\n', encoding="utf-8")
    _wrap.chmod(0o755)
    so.TMUX_BIN = str(_wrap)

db = so.DB_DIR / "check_editor.db"
db.unlink(missing_ok=True)
so._ensure_db()

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


SID = "editor-guard-1"
NAME = so._editor_tmux_name(SID)


# ── argv ─────────────────────────────────────────────────────────────────────
session = {"id": SID, "name": "alpha", "cwd": str(tmp)}
argv = so.editor_argv(session)
have_tmux = bool(so._tmux())

if have_tmux:
    check("tmux wraps nvim so closing the tab only detaches",
          argv[1:3] == ["new-session", "-A"] and argv[-1] == so.NVIM_BIN, str(argv))
    check("the session is named after the orchestrator session",
          "-s" in argv and argv[argv.index("-s") + 1] == NAME, str(argv))
    check("and it starts in the session's cwd",
          "-c" in argv and argv[argv.index("-c") + 1] == str(tmp), str(argv))
else:
    check("no tmux on this machine → plain nvim", argv == [so.NVIM_BIN], str(argv))

# tmux từ chối '.' và ':' trong tên phiên — id lạ không được đẻ ra tên hỏng.
check("odd session ids cannot produce an illegal tmux name",
      "." not in so._editor_tmux_name("a.b:c")[len(so.EDITOR_PREFIX):]
      and ":" not in so._editor_tmux_name("a.b:c"), so._editor_tmux_name("a.b:c"))

# cwd rỗng → HOME, không phải chuỗi rỗng (tmux -c "" thất bại, và nvim mở ở đâu thì tuỳ may).
check("a session with no cwd falls back to HOME",
      str(Path.home()) in so.editor_argv({"id": SID, "name": "x", "cwd": ""}) + [str(Path.home())],
      str(so.editor_argv({"id": SID, "name": "x", "cwd": ""})))


# Runner CI không cài neovim, và Windows cũng không có tmux. Phần argv/tên phiên vẫn kiểm được
# ở mọi nơi; phần gọi API thật thì cần nvim có thật, nên bỏ qua VÀ NÓI RÕ là đã bỏ qua — im lặng
# đi qua là kiểu test tự lừa mình.
HAVE_NVIM = bool(shutil.which(so.NVIM_BIN))


async def main():
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        await c.post("/api/sessions", json={"id": SID, "name": "alpha", "cwd": str(tmp)})
        check("nothing is open to begin with", (await c.get("/api/editor")).json() == [],
              str((await c.get("/api/editor")).json()))

        # KHÔNG có tmux server nào chạy → new-session phải tự dựng một cái. Đây là đường mà
        # `communicate()` treo vĩnh viễn nếu server kế thừa ống stdout của mình.
        await so._tmux_run("kill-server")
        t0 = time.monotonic()
        r = await c.post("/api/editor/open", json={"session": SID})
        took = time.monotonic() - t0
        check("opening reports the card", r.status_code == 200 and r.json().get("open") is True,
              f"{r.status_code} {r.text[:120]}")
        check("opening does not hang while tmux boots its server", took < 5,
              f"took {took:.1f}s — tmux server inherited the stdout pipe, so communicate() "
              f"waits for an EOF that only arrives when the server dies")
        listed = (await c.get("/api/editor")).json()
        check("and it shows up in the list",
              [x["session"] for x in listed] == [SID], str(listed))
        check("carrying the session's cwd", listed and listed[0]["cwd"] == str(tmp), str(listed))

        if have_tmux:
            # Sổ đăng ký PHẢI là tmux, không phải một dict trong RAM: đó là thứ sống qua restart.
            rc, out = await so._tmux_run("list-sessions", "-F", "#{session_name}", capture=True)
            check("the real tmux server is the registry", NAME in out.split(), out.strip()[:200])
            check("the in-RAM set stays empty when tmux is in charge", not so._editors,
                  str(so._editors))

        # Mở lại card đang mở = không sao, không đẻ phiên thứ hai.
        r = await c.post("/api/editor/open", json={"session": SID})
        again = (await c.get("/api/editor")).json()
        check("opening twice is harmless and does not duplicate",
              r.status_code == 200 and len(again) == 1, f"{r.status_code} {again}")

        # ── tab git (diffview.nvim) ──────────────────────────────────────────
        # Không kiểm "đã gửi phím" — gửi được mà nvim không mở gì thì tính năng vẫn hỏng.
        # Đọc thẳng màn hình tmux: Diffview có hiện ra hay không.
        if have_tmux and HAVE_NVIM:
            check("the API advertises both tabs",
                  (await c.get("/api/editor")).json()[0].get("windows") == ["edit", "git"],
                  str((await c.get("/api/editor")).json()))

            r = await c.post("/api/editor/focus", json={"session": SID, "window": "git"})
            check("switching to the git tab is accepted", r.status_code == 200, str(r.status_code))
            pane = ""
            for _ in range(40):          # nvim + diffview cần một nhịp để vẽ
                await asyncio.sleep(0.5)
                _, pane = await so._tmux_run("capture-pane", "-p", "-t", f"{NAME}:nvim",
                                             capture=True)
                if "Diffview" in pane or "dirty.txt" in pane:
                    break
            check("diffview actually opens inside nvim",
                  "Diffview" in pane or "dirty.txt" in pane,
                  "pane never showed the diff — " + " ".join(pane.split())[:180])

            r = await c.post("/api/editor/focus", json={"session": SID, "window": "edit"})
            for _ in range(20):
                await asyncio.sleep(0.5)
                _, pane = await so._tmux_run("capture-pane", "-p", "-t", f"{NAME}:nvim",
                                             capture=True)
                if "DiffviewFilePanel" not in pane:
                    break
            # Đừng dò chuỗi "Diffview": nvim in lại chính lệnh ":DiffviewClose" vừa gõ ở dòng
            # lệnh, nên nó có mặt kể cả khi panel đã đóng.
            check("and that closes the diff panel again",
                  r.status_code == 200 and "DiffviewFilePanel" not in pane,
                  " ".join(pane.split())[:160])

            r = await c.post("/api/editor/focus", json={"session": SID, "window": "bogus"})
            check("an unknown tab name is refused", r.status_code == 400, str(r.status_code))

        # Tắt orchestrator KHÔNG được đụng phiên nvim — đó chính là tính năng.
        async with app.router.lifespan_context(app):
            pass
        still = (await c.get("/api/editor")).json()
        check("shutting the orchestrator down leaves the editor running",
              [x["session"] for x in still] == [SID], str(still))

        # Session bị xoá mà phiên tmux còn sót → KHÔNG được dựng card ma.
        conn = so._conn()          # _conn() mở connection MỚI mỗi lần gọi — phải giữ đúng một cái,
        conn.execute("DELETE FROM sessions WHERE id = ?", (SID,))   # không thì commit rơi vào
        conn.commit()                                               # connection khác và DELETE bị
        conn.close()                                                # rollback lúc gc.
        ghost = (await c.get("/api/editor")).json()
        check("a tmux session with no orchestrator session is not listed", ghost == [], str(ghost))

        r = await c.post("/api/editor/close", json={"session": SID})
        check("closing kills it", r.json().get("closed") == 1, str(r.json()))
        if have_tmux:
            _, out = await so._tmux_run("list-sessions", "-F", "#{session_name}", capture=True)
            check("and the tmux session is really gone", NAME not in out.split(), out.strip()[:200])

        r = await c.post("/api/editor/open", json={"session": "does-not-exist"})
        check("opening an unknown session is 404", r.status_code == 404, str(r.status_code))


try:
    if HAVE_NVIM:
        asyncio.run(main())
    else:
        print(f"SKIP live open/close round-trip — '{so.NVIM_BIN}' is not installed here")
finally:
    asyncio.run(so._tmux_run("kill-session", "-t", NAME))    # đừng bỏ rác lại trên máy
    db.unlink(missing_ok=True)
    shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print("\n" + "\n".join(fails))
    sys.exit(1)
print("\nall editor checks passed")
