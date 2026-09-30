#!/usr/bin/env python3
"""Guards for the editor card (nvim) and the folder button that replaced it by default.

The "open this project" button has two modes. Without `--dev` it hands the folder to the OS file
manager; with `--dev` it opens the nvim card. The nvim half must keep its old promises, and the
folder half fails in ways nobody notices:

  - every OS needs its own command, and a developer can only run one of the three. `explorer.exe`
    returning 1 ON SUCCESS is the trap: check the exit code there and every click reports an error
    while the window is already open;
  - a headless orchestrator (container, remote server) has no file manager. `xdg-open` exits
    quietly, so the button becomes a dead button that never says why — it has to say why;
  - the folder comes from the session row in the DB. The moment a path in the request body can
    reach the file manager, the endpoint opens ANY folder on the host for anyone who can reach
    the dashboard;
  - the folder CARD reads the tree over HTTP, so its two endpoints are the ones that must stay
    locked inside the project: `..`, an absolute path and a symlink out of the tree are all the
    same bug, which is "read any file on the host over a GET". resolve() catches all three, a
    string check on ".." catches only the first;
  - a file is served with a content type THIS code picks. Hand a project's .html or .svg back as
    itself and the browser runs it same-origin with the dashboard, which is stored XSS.

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


# ── mở thư mục bằng OS: chế độ MẶC ĐỊNH, chạy ở mọi máy vì không cần nvim ────
# Giả lập cả ba OS. Người viết code chỉ chạy được một cái, mà cả ba đều phải đúng — và cái sai
# im lặng nhất (explorer.exe trả 1 khi thành công) chỉ lộ ra trên Windows.
_PLAT, _WHICH = sys.platform, shutil.which
BIN = tmp / "bin"
BIN.mkdir()
LOG = tmp / "opened.log"
# Trình quản lý file giả: GHI LẠI path nó được đưa (để chứng minh path lấy từ DB, không từ body)
# và trả đúng mã lỗi mà test yêu cầu qua FAKE_RC.
for nm in ("xdg-open", "explorer", "open"):
    (BIN / nm).write_text(f'#!/bin/sh\necho "$1" >> {LOG}\nexit ${{FAKE_RC:-0}}\n',
                          encoding="utf-8")
    (BIN / nm).chmod(0o755)
os.environ["PATH"] = str(BIN) + os.pathsep + os.environ.get("PATH", "")
for _k in ("DISPLAY", "WAYLAND_DISPLAY"):
    os.environ.pop(_k, None)


def _bin_which(n):
    """shutil.which() ĐỌC sys.platform: giả lập win32 là nó đi tìm explorer.EXE theo PATHEXT và
    không thấy script /bin/sh của test. Quirk CỦA TEST, không phải của tính năng — Windows thật
    có explorer.EXE nằm sẵn trên PATH. Nên tra thẳng trong BIN trước."""
    return str(BIN / n) if (BIN / n).exists() else _WHICH(n)


def _as(platform, which=None):
    """Chạy tiếp như thể đang trên OS này (và tuỳ chọn: không tìm thấy binary nào)."""
    so.sys.platform = platform
    so.shutil.which = which or _bin_which


def _restore_platform():
    so.sys.platform, so.shutil.which = _PLAT, _WHICH


_as("darwin")
check("mac hands the folder to Finder", so.folder_open_argv() == ["open"],
      str(so.folder_open_argv()))
check("and does not ask mac for a DISPLAY", so.folder_open_why() == "", so.folder_open_why())
_as("win32")
check("Windows hands it to File Explorer", so.folder_open_argv() == ["explorer"],
      str(so.folder_open_argv()))
check("and does not ask Windows for a DISPLAY either", so.folder_open_why() == "",
      so.folder_open_why())
_as("linux")
check("Linux goes through xdg-open", so.folder_open_argv() == ["xdg-open"],
      str(so.folder_open_argv()))
check("a headless orchestrator SAYS it has no desktop instead of opening nothing",
      "desktop" in so.folder_open_why(), so.folder_open_why() or "(said nothing)")
os.environ["DISPLAY"] = ":0"
check("with a desktop session it goes ahead", so.folder_open_why() == "", so.folder_open_why())
_as("linux", lambda n: None)
check("a file manager that is not installed is named, not swallowed",
      "xdg-open" in so.folder_open_why(), so.folder_open_why() or "(said nothing)")

if os.name == "posix":
    _as("win32")
    os.environ["FAKE_RC"] = "1"
    try:
        asyncio.run(so.folder_open(tmp))
        win_ok, win_why = True, ""
    except OSError as e:
        win_ok, win_why = False, str(e)
    check("explorer.exe exiting 1 is SUCCESS on Windows — the window is already open",
          win_ok, win_why)
    _as("linux")
    try:
        asyncio.run(so.folder_open(tmp))
        lin_ok = False
    except OSError:
        lin_ok = True
    check("but a non-zero exit anywhere else is a real failure", lin_ok,
          "exit 1 was swallowed — a broken xdg-open would look like success")
    os.environ.pop("FAKE_RC", None)
else:
    print("SKIP the exit-code checks — the fake file manager needs a POSIX shell")
_restore_platform()

FSID = "folder-guard-1"


async def main_folder():
    """Endpoint mở thư mục. Không cần nvim, nên chạy ở mọi máy."""
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        check("/health tells the dashboard which mode the server is in",
              "dev_mode" in (await c.get("/health")).json(),
              str((await c.get("/health")).json())[:160])

        r = await c.post("/api/folder/open", json={"session": "does-not-exist"})
        check("opening the folder of an unknown session is 404", r.status_code == 404,
              f"{r.status_code} {r.text[:80]}")

        await c.post("/api/sessions", json={"id": FSID, "name": "folder", "cwd": str(tmp)})
        if os.name == "posix":
            LOG.unlink(missing_ok=True)
            # Gửi kèm một path KHÁC trong body: nó phải bị bỏ qua hoàn toàn. Nếu không, endpoint
            # này mở được thư mục bất kỳ trên máy chủ cho bất cứ ai gọi được dashboard.
            r = await c.post("/api/folder/open", json={"session": FSID, "path": "/etc"})
            opened = LOG.read_text(encoding="utf-8").split() if LOG.exists() else []
            check("it opens the folder recorded on the session",
                  r.status_code == 200 and r.json().get("path") == str(tmp),
                  f"{r.status_code} {r.text[:120]}")
            check("a path in the request body reaches nothing",
                  opened == [str(tmp)], str(opened))

        # ── card thư mục: liệt kê + đọc file, KHOÁ trong cwd của session ──────
        (tmp / "sub").mkdir(exist_ok=True)
        (tmp / "sub" / "deep.txt").write_text("deeper\n", encoding="utf-8")
        (tmp / "page.html").write_text("<script>alert(1)</script>", encoding="utf-8")
        (tmp / "pic.svg").write_text('<svg onload="alert(1)"/>', encoding="utf-8")
        (tmp / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)
        (tmp / "blob.bin").write_bytes(b"MZ\0\0binary junk")

        r = (await c.get(f"/api/folder/list?session={FSID}")).json()
        names = [d["name"] for d in r["dirs"]] + [f["name"] for f in r["files"]]
        check("the card lists the project's own folders and files",
              "sub" in names and "tracked.txt" in names, str(names)[:160])
        check("hidden entries stay out of the browser — .git and .env are not for reading here",
              not any(n.startswith(".") for n in names), str(names)[:160])
        check("files carry a size and a timestamp to show",
              all("size" in f and f["mtime"] for f in r["files"]), str(r["files"])[:160])
        check("the root reports itself as the empty path", r["rel"] == "", str(r["rel"]))

        r = (await c.get(f"/api/folder/list?session={FSID}&path=sub")).json()
        check("descending a level works and says where it is",
              r["rel"] == "sub" and [f["name"] for f in r["files"]] == ["deep.txt"], str(r)[:160])

        # ── cả ba đường thoát khỏi project, cùng một cái chốt ─────────────────
        for bad, why in ((".." , "a parent directory"), ("../..", "two levels up"),
                         ("sub/../../etc", "a detour back out through .."),
                         ("/etc", "an absolute path")):
            r = await c.get(f"/api/folder/list?session={FSID}&path={bad}")
            check(f"listing {why} is refused", r.status_code == 404,
                  f"{bad} → {r.status_code} {r.text[:80]}")
        try:
            (tmp / "escape").symlink_to("/etc")
        except OSError:
            print("SKIP the symlink escape check — this machine will not create one")
        else:
            r = await c.get(f"/api/folder/list?session={FSID}&path=escape")
            check("a symlink pointing out of the project is refused too",
                  r.status_code == 404, f"{r.status_code} {r.text[:80]}")
            r = await c.get(f"/api/folder/file?session={FSID}&path=escape/hostname")
            check("and nothing can be read through it", r.status_code == 404,
                  f"{r.status_code} {r.text[:80]}")

        # ── đọc file: content-type do MÌNH chọn ──────────────────────────────
        r = await c.get(f"/api/folder/file?session={FSID}&path=tracked.txt")
        check("a text file comes back as text", r.status_code == 200
              and r.headers["content-type"].startswith("text/plain") and "two" in r.text,
              f"{r.status_code} {r.headers.get('content-type')} {r.text[:40]!r}")
        check("with nosniff, so the browser cannot guess a richer type back",
              r.headers.get("x-content-type-options") == "nosniff", str(dict(r.headers))[:160])

        for name in ("page.html", "pic.svg"):
            r = await c.get(f"/api/folder/file?session={FSID}&path={name}")
            check(f"{name} is served as PLAIN TEXT, never as itself",
                  r.headers["content-type"].startswith("text/plain"),
                  f"{name} → {r.headers.get('content-type')} — same-origin with the dashboard, "
                  f"so serving it as itself is stored XSS")

        r = await c.get(f"/api/folder/file?session={FSID}&path=img.png")
        check("an image is served as an image so the card can show it",
              r.headers["content-type"] == "image/png", str(r.headers.get("content-type")))

        r = await c.get(f"/api/folder/file?session={FSID}&path=blob.bin")
        check("a binary file SAYS it is binary instead of dumping bytes into the page",
              r.json().get("binary") is True, r.text[:80])

        r = await c.get(f"/api/folder/file?session={FSID}&path=sub")
        check("asking for a folder as a file is 404", r.status_code == 404, str(r.status_code))

        # ── trần: hạ tạm xuống để không phải đẻ 501 file / một file 2MB ───────
        _ents, _read = so.FOLDER_MAX_ENTRIES, so.FOLDER_MAX_READ
        so.FOLDER_MAX_ENTRIES = 2
        r = (await c.get(f"/api/folder/list?session={FSID}")).json()
        check("a huge folder is cut and SAYS it was cut",
              r["truncated"] and len(r["dirs"]) + len(r["files"]) == 2, str(r)[:140])
        so.FOLDER_MAX_ENTRIES = _ents
        so.FOLDER_MAX_READ = 2          # tracked.txt dài 4 byte, nên 2 là CHẮC CHẮN bị cắt
        r = await c.get(f"/api/folder/file?session={FSID}&path=tracked.txt")
        check("a long file is cut and the header says so",
              len(r.content) == 2 and r.headers.get("x-orch-truncated") == "1",
              f"{len(r.content)}B {r.headers.get('x-orch-truncated')}")
        so.FOLDER_MAX_READ = _read

        r = await c.get("/api/folder/list?session=does-not-exist")
        check("browsing an unknown session is 404", r.status_code == 404, str(r.status_code))

        # cwd trỏ vào thư mục không tồn tại → 400 nói rõ, không phải mở im lặng thất bại.
        conn = so._conn()
        conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (str(tmp / "gone"), FSID))
        conn.commit()
        conn.close()
        r = await c.post("/api/folder/open", json={"session": FSID})
        check("a session whose folder is gone is refused with a reason",
              r.status_code == 400 and "not a folder" in r.text, f"{r.status_code} {r.text[:100]}")
        r = await c.get(f"/api/folder/list?session={FSID}")
        check("and the card refuses it too, rather than falling back to some other folder",
              r.status_code == 400, f"{r.status_code} {r.text[:100]}")


asyncio.run(main_folder())


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
