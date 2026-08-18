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

        # ── tab git (lazygit) ────────────────────────────────────────────────
        if have_tmux and shutil.which(so.LAZYGIT_BIN):
            _, wins = await so._tmux_run("list-windows", "-t", NAME, "-F", "#{window_name}",
                                         capture=True)
            check("the card has both a nvim and a git window",
                  set(wins.split()) == {"nvim", "git"}, wins.strip()[:120])
            check("and the API advertises both tabs",
                  (await c.get("/api/editor")).json()[0].get("windows") == ["nvim", "git"],
                  str((await c.get("/api/editor")).json()))

            r = await c.post("/api/editor/focus", json={"session": SID, "window": "git"})
            _, cur = await so._tmux_run("display-message", "-p", "-t", NAME,
                                        "#{window_name}", capture=True)
            check("switching the tab moves tmux to that window",
                  r.status_code == 200 and cur.strip() == "git", f"{r.status_code} {cur.strip()}")

            # Người dùng thoát lazygit → cửa sổ chết. Bấm tab lại phải DỰNG LẠI, không báo lỗi.
            await so._tmux_run("kill-window", "-t", f"{NAME}:git")
            r = await c.post("/api/editor/focus", json={"session": SID, "window": "git"})
            _, cur = await so._tmux_run("display-message", "-p", "-t", NAME,
                                        "#{window_name}", capture=True)
            check("a tab whose window was killed is rebuilt on click",
                  r.status_code == 200 and cur.strip() == "git", f"{r.status_code} {cur.strip()}")

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
