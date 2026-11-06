#!/usr/bin/env python3
"""Guards for the editor card (nvim) and the folder card next to it.

The folder half fails in ways nobody notices:

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

The editor card replaced `code serve-web`, and the whole point was to stop owning process state:

  - the argv must go through tmux when tmux exists, so closing the browser tab DETACHES instead
    of killing the buffer. Drop the tmux wrapper and every reload silently loses unsaved work;
  - `tmux ls` is the registry, on a PRIVATE server. If open/close does not round-trip through it,
    the dashboard and the machine disagree about what is running; if it leaves the private socket,
    the user's own tmux sessions are in play;
  - a tmux session whose orchestrator session was deleted must NOT produce a card. It would be a
    ghost the user cannot act on;
  - shutting the orchestrator down must LEAVE the sessions alone. That is the feature: restart the
    server, reopen the dashboard, the buffer is still there.

It also downloads and runs a program, so the download is guarded on its own:

  - nvim is pinned by hash. A file that fails the hash, or an archive that writes outside its
    folder, must leave nothing installed, and two opens at once must not corrupt the install;
  - nvim on PATH is never used: old versions break the bundled plugins (a machine with 0.9.0 gets
    a screen full of Lua errors). ORCH_NVIM_BIN is the one way to choose another binary;
  - tab switching goes over nvim's RPC, because Windows has no tmux to send keys through.

    python3 check_editor.py
    ORCH_CHECK_EDITOR_LIVE=1 python3 check_editor.py   # also downloads and runs the pinned nvim
"""
import asyncio
import http.server
import hashlib
import io
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import tempfile
import zipfile
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# resolve(): nvim và git trả đường dẫn thật. Máy macOS có /tmp là symlink, runner Windows có TEMP
# dạng 8.3 (RUNNER~1); không chuẩn hoá ở đây thì so sánh đường dẫn sai dù tính năng đúng.
tmp = Path(tempfile.mkdtemp()).resolve()
# Repo git thật: mở diff ngoài repo chỉ báo lỗi, không mở gì — test sẽ xanh giả nếu chỉ kiểm "lệnh
# đã gửi đi".
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
os.environ.pop("ORCH_NVIM_BIN", None)
# Thư mục socket riêng cho tmux. Hai lý do, cả hai bắt buộc:
#   1. test được phép `kill-server` — mà server -L orch-nvim thật là card đang mở của người dùng;
#   2. chỉ khi KHÔNG có server sẵn thì `new-session` mới phải TỰ DỰNG một cái, và đó chính là
#      điều kiện làm lộ bug ống stdout bị kế thừa (xem _tmux_run).
os.environ["TMUX_TMPDIR"] = str(tmp)

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

REAL_DB_DIR = so.DB_DIR               # bản nvim thật của phần live được giữ lại giữa các lần chạy
db = so.DB_DIR / "check_editor.db"
db.unlink(missing_ok=True)
so._ensure_db()

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


def case(name, fn):
    """Một ca độc lập: gọi một hàm không còn tồn tại (code cũ) là FAIL của riêng ca đó."""
    try:
        ok, detail = fn()
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"{type(e).__name__}: {e}"
    check(name, ok, detail)


SID = "editor-guard-1"
NAME = so._editor_tmux_name(SID)
LIVE = os.environ.get("ORCH_CHECK_EDITOR_LIVE") == "1"


# ── argv ─────────────────────────────────────────────────────────────────────
session = {"id": SID, "name": "alpha", "cwd": str(tmp)}
have_tmux = bool(so._tmux())
# Phần argv không cần nvim thật, chỉ cần tên binary: không mạng, không tải.
os.environ["ORCH_NVIM_BIN"] = sys.executable
argv = so.editor_argv(session)

if have_tmux:
    check("tmux wraps nvim so closing the tab only detaches",
          "new-session" in argv and "-A" in argv and sys.executable in argv, str(argv))
    check("the session is named after the orchestrator session",
          "-s" in argv and argv[argv.index("-s") + 1] == NAME, str(argv))
    check("and it starts in the session's cwd",
          "-c" in argv and argv[argv.index("-c") + 1] == str(tmp), str(argv))
    case("argv goes through the private tmux socket and config, never the default server",
         lambda: (argv[1:3] == ["-L", "orch-nvim"] and argv[3] == "-f"
                  and argv[4].endswith("tmux.conf") and Path(argv[4]).is_file(), str(argv)))
    case("the private tmux config frees Ctrl+B and passes truecolor and OSC 52",
         lambda: (all(x in Path(argv[4]).read_text(encoding="utf-8")
                      for x in ("prefix None", "mouse on", "Tc", "set-clipboard on", "escape-time 0")),
                  argv[4]))
else:
    print("SKIP the tmux argv checks — no tmux on this machine")

case("nvim starts isolated: own app name, bundled config, and XDG_CONFIG_HOME left alone",
     lambda: ("NVIM_APPNAME" in so._editor_env() and so._editor_env()["NVIM_APPNAME"] == "orch-nvim"
              and so._editor_env().get("XDG_CONFIG_HOME") == os.environ.get("XDG_CONFIG_HOME")
              and "-u" in so._nvim_cmd(SID)
              and Path(so._nvim_cmd(SID)[so._nvim_cmd(SID).index("-u") + 1]).is_file(),
              str(so._nvim_cmd(SID))))
case("nvim listens on a per-session address that fits a Unix socket path",
     lambda: (so._nvim_cmd(SID)[so._nvim_cmd(SID).index("--listen") + 1] == so._editor_addr(SID)
              and so._editor_addr(SID) == so._editor_addr(SID) and len(so._editor_addr(SID)) < 104,
              so._editor_addr(SID)))

# tmux từ chối '.' và ':' trong tên phiên — id lạ không được đẻ ra tên hỏng.
check("odd session ids cannot produce an illegal tmux name",
      "." not in so._editor_tmux_name("a.b:c")[len(so.EDITOR_PREFIX):]
      and ":" not in so._editor_tmux_name("a.b:c"), so._editor_tmux_name("a.b:c"))

# cwd rỗng → HOME, không phải chuỗi rỗng (tmux -c "" thất bại, và nvim mở ở đâu thì tuỳ may).
if have_tmux:
    check("a session with no cwd falls back to HOME",
          str(Path.home()) in so.editor_argv({"id": SID, "name": "x", "cwd": ""}),
          str(so.editor_argv({"id": SID, "name": "x", "cwd": ""})))
os.environ.pop("ORCH_NVIM_BIN", None)


# ── nvim: chọn binary, tải, kiểm hash, giải nén ──────────────────────────────
class _Mirror(http.server.BaseHTTPRequestHandler):
    """Mirror giả qua ORCH_NVIM_MIRROR: phục vụ đúng các byte test đưa, đếm số lần bị tải."""
    payload = b""
    hits = 0
    delay = 0.0

    def do_GET(self):
        type(self).hits += 1
        time.sleep(self.delay)
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *a):
        pass


_mirror = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Mirror)
threading.Thread(target=_mirror.serve_forever, daemon=True).start()
_MIRROR_URL = f"http://127.0.0.1:{_mirror.server_address[1]}"
# getattr: chép file này lên code cũ thì từng ca phải FAIL với tên của nó, không phải chết ở đây.
_ASSETS = getattr(so, "NVIM_ASSETS", {})
_REAL_ASSETS = dict(_ASSETS)
_REAL_CHECK = getattr(so, "_nvim_check", None)
_KEY = (sys.platform, platform.machine().lower())
_ASSET = _ASSETS.get(_KEY, ("nvim-linux-x86_64.tar.gz", ""))[0]
_FOLDER = _ASSET.split(".")[0]
_EXE = "nvim.exe" if _ASSET.endswith(".zip") else "nvim"
_FAKE_NVIM = b'#!/bin/sh\ncase "$1" in --version) echo "NVIM v0.12.6";; *) exec sleep 300;; esac\n'


def _archive(extra=()):
    """Archive đúng khuôn của release, loại file theo asset của OS này. extra = (tên, nội dung)."""
    files = [(f"{_FOLDER}/bin/{_EXE}", _FAKE_NVIM), *extra]
    buf = io.BytesIO()
    if _ASSET.endswith(".zip"):
        with zipfile.ZipFile(buf, "w") as zf:
            for n, data in files:
                zf.writestr(n, data)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for n, data in files:
                ti = tarfile.TarInfo(n)
                ti.size, ti.mode = len(data), 0o755
                tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _serve(payload, sha=None, delay=0.0):
    """Từ giờ 'release' của máy này là payload. Mỗi ca bắt đầu với một DB_DIR trống."""
    _Mirror.payload, _Mirror.hits, _Mirror.delay = payload, 0, delay
    so.NVIM_ASSETS[_KEY] = (_ASSET, sha or hashlib.sha256(payload).hexdigest())
    os.environ["ORCH_NVIM_MIRROR"] = _MIRROR_URL
    so.DB_DIR = Path(tempfile.mkdtemp(dir=tmp))
    so._nvim_ok.clear()
    # Windows: exe giả không chạy được; phần chạy thử do bản nvim thật ở phần live chứng minh.
    so._nvim_check = (lambda exe: None) if os.name == "nt" else _REAL_CHECK


def _installed():
    """Những gì nằm dưới DB_DIR/nvim, kể cả thư mục tạm bỏ sót."""
    root = so.DB_DIR / "nvim"
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


def _refused(contains):
    try:
        so.nvim_install()
    except so.NvimUnavailable as e:
        return (contains in str(e) and "ORCH_NVIM_BIN" in str(e) and _installed() == [],
                f"{e} | left behind: {_installed()}")
    return False, f"nothing refused it; installed: {_installed()}"


def _case_good_install():
    _serve(_archive())
    exe = so.nvim_install()
    return (Path(exe).is_file() and _installed() == [so.NVIM_VERSION]
            and Path(exe).parent.parent.parent.name == so.NVIM_VERSION
            and _Mirror.hits == 1, f"{exe} {_installed()} hits={_Mirror.hits}")


def _case_second_open():
    so.nvim_install()
    return _Mirror.hits == 1, f"hits={_Mirror.hits}"


def _case_hash_mismatch():
    _serve(_archive(), sha="0" * 64)
    return _refused("integrity")


def _malicious(extra):
    def run():
        _serve(_archive(extra))
        outside = so.DB_DIR / "nvim" / "evil.txt"
        ok, detail = _refused("unsafe")
        return ok and not outside.exists() and not (so.DB_DIR / "evil.txt").exists(), detail
    return run


def _case_symlink_escape():
    if _ASSET.endswith(".zip"):
        return True, ""      # zip không có symlink khi giải nén bằng zipfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo(f"{_FOLDER}/bin/{_EXE}")
        ti.size, ti.mode = len(_FAKE_NVIM), 0o755
        tf.addfile(ti, io.BytesIO(_FAKE_NVIM))
        link = tarfile.TarInfo(f"{_FOLDER}/out")
        link.type, link.linkname = tarfile.SYMTYPE, "../../../../../etc"
        tf.addfile(link)
    _serve(buf.getvalue())
    return _refused("unsafe")


def _case_concurrent_opens():
    _serve(_archive(), delay=0.3)
    out, errs = [], []

    def one():
        try:
            out.append(so.nvim_install())
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))
    ts = [threading.Thread(target=one) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return (not errs and len(set(out)) == 1 and Path(out[0]).is_file() and _Mirror.hits == 1
            and _installed() == [so.NVIM_VERSION], f"{errs} {set(out)} hits={_Mirror.hits} {_installed()}")


def _case_two_processes():
    # _nvim_fetch không có khoá trong tiến trình: hai orchestrator cùng máy chính là thế này.
    _serve(_archive(), delay=0.3)
    out, errs = [], []

    def one():
        try:
            out.append(so._nvim_fetch())
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))
    ts = [threading.Thread(target=one) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return (not errs and len(set(out)) == 1 and Path(out[0]).is_file()
            and _installed() == [so.NVIM_VERSION], f"{errs} {set(out)} {_installed()}")


def _case_system_nvim_not_chosen():
    _serve(_archive())
    fake = tmp / "syspath"
    fake.mkdir(exist_ok=True)
    (fake / "nvim").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake / "nvim").chmod(0o755)
    old = os.environ["PATH"]
    os.environ["PATH"] = str(fake) + os.pathsep + old
    try:
        before = so.nvim_find()
        exe = so.nvim_install()
    finally:
        os.environ["PATH"] = old
    return before is None and str(so.DB_DIR) in exe, f"before={before} exe={exe}"


def _case_override_wins():
    _serve(_archive())
    os.environ["ORCH_NVIM_BIN"] = sys.executable
    try:
        exe = so.nvim_install()
    finally:
        os.environ.pop("ORCH_NVIM_BIN")
    return exe == shutil.which(sys.executable) and _Mirror.hits == 0, f"{exe} hits={_Mirror.hits}"


def _case_bad_override():
    os.environ["ORCH_NVIM_BIN"] = str(tmp / "no-such-nvim")
    try:
        so.nvim_install()
    except so.NvimUnavailable as e:
        return "ORCH_NVIM_BIN" in str(e), str(e)
    finally:
        os.environ.pop("ORCH_NVIM_BIN")
    return False, "a missing ORCH_NVIM_BIN was accepted"


def _case_no_network():
    _serve(_archive())
    os.environ["ORCH_NVIM_MIRROR"] = "http://127.0.0.1:9"     # không ai nghe ở đây
    return _refused("Could not download")


def _case_unsupported():
    so.NVIM_ASSETS.pop(_KEY, None)
    so.DB_DIR = Path(tempfile.mkdtemp(dir=tmp))
    try:
        so.nvim_install()
    except so.NvimUnavailable as e:
        return "ORCH_NVIM_BIN" in str(e), str(e)
    return False, "an unsupported machine installed something"


def _case_will_not_run():
    if os.name == "nt":
        return True, ""
    _serve(_archive())
    so._nvim_check = _REAL_CHECK
    bad = _archive()
    with tarfile.open(fileobj=io.BytesIO(bad)) as tf:      # cùng cây, nhưng binary thoát với mã 3
        names = tf.getnames()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo(names[0])
        body = b"#!/bin/sh\nexit 3\n"
        ti.size, ti.mode = len(body), 0o755
        tf.addfile(ti, io.BytesIO(body))
    _serve(buf.getvalue())
    return _refused("will not run")


case("a good archive installs under DB_DIR/nvim/<version>", _case_good_install)
case("a second open does not download again", _case_second_open)
case("a hash mismatch refuses and leaves nothing installed", _case_hash_mismatch)
case("an archive path climbing out with .. is refused and leaves nothing",
     _malicious([("../evil.txt", b"x")]))
case("an absolute archive path is refused and leaves nothing", _malicious([("/tmp/evil.txt", b"x")]))
case("a Windows-style archive path climbing out is refused and leaves nothing",
     _malicious([("..\\evil.txt", b"x")]))
case("a symlink pointing out of the archive is refused and leaves nothing", _case_symlink_escape)
case("concurrent opens download once and do not corrupt the install", _case_concurrent_opens)
case("two processes installing at once both end with one good install", _case_two_processes)
case("a binary that will not run is refused and leaves nothing", _case_will_not_run)
case("nvim on PATH is not chosen", _case_system_nvim_not_chosen)
case("ORCH_NVIM_BIN wins and skips the download", _case_override_wins)
case("a wrong ORCH_NVIM_BIN says so in plain words", _case_bad_override)
case("no network gives a plain message naming ORCH_NVIM_BIN", _case_no_network)
case("an unsupported machine gives a plain message naming ORCH_NVIM_BIN", _case_unsupported)

_ASSETS.clear()
_ASSETS.update(_REAL_ASSETS)
so._nvim_check = _REAL_CHECK
os.environ.pop("ORCH_NVIM_MIRROR", None)
so.DB_DIR = REAL_DB_DIR

# Dữ liệu người dùng đọc: asset phải khớp bảng băm đã xác nhận với release, và đóng gói phải kèm.
case("every published platform has a pinned 64-hex digest",
     lambda: (len(_ASSETS) >= 5 and all(len(h) == 64 for _, h in _ASSETS.values()), str(_ASSETS)))
case("the packaged binary carries editor/", lambda: (
    '("editor", "editor")' in (HERE / "build.spec").read_text(encoding="utf-8"), "build.spec"))
case("CI runs the live part on the Windows runner", lambda: (
    'ORCH_CHECK_EDITOR_LIVE: "1"' in (HERE / ".github/workflows/build.yml").read_text(encoding="utf-8"),
    "build.yml"))
case("--dev is still accepted, because the live server starts with it", lambda: (
    subprocess.run([sys.executable, str(HERE / "session_orchestrator.py"), "--dev", "list-sessions"],
                   capture_output=True, timeout=60, cwd=tmp,
                   env={**os.environ, "ORCH_DEV_MODE": "1"}).returncode == 0, "--dev list-sessions failed"))


# ── mở thư mục bằng OS: chạy ở mọi máy vì không cần nvim ─────────────────────
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
        check("/health no longer reports a dev mode",
              "dev_mode" not in (await c.get("/health")).json(),
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


# Non-live: nvim giả (đủ để kiểm đăng ký/đóng card). Tính năng thật của nvim do phần live chứng minh.
_FAKE_BIN = tmp / "fake-bin"
_FAKE_BIN.mkdir()
_RPC_LOG = tmp / "rpc.log"
if os.name == "posix":
    _FAKE = _FAKE_BIN / "nvim"
    _FAKE.write_text(f'#!/bin/sh\ncase "$1" in\n--version) echo "NVIM v0.12.6";;\n'
                     f'--server) echo "$@" >> {_RPC_LOG};;\n*) exec sleep 300;;\nesac\n',
                     encoding="utf-8")
    _FAKE.chmod(0o755)
    FAKE_NVIM = str(_FAKE)
else:
    FAKE_NVIM = sys.executable     # chạy được --version; Windows không có tmux nên không spawn gì


async def main():
    """Card qua API, với nvim giả: mở, sổ đăng ký, đóng. Có tmux thì tmux thật."""
    os.environ["ORCH_NVIM_BIN"] = FAKE_NVIM
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
        check("opening reports the card", r.status_code == 200 and r.json().get("open") is True
              and not r.json().get("state"), f"{r.status_code} {r.text[:120]}")
        check("opening does not hang while tmux boots its server", took < 5,
              f"took {took:.1f}s — tmux server inherited the stdout pipe, so communicate() "
              f"waits for an EOF that only arrives when the server dies")
        listed = (await c.get("/api/editor")).json()
        check("and it shows up in the list",
              [x["session"] for x in listed] == [SID], str(listed))
        check("carrying the session's cwd", listed and listed[0]["cwd"] == str(tmp), str(listed))
        check("both tabs are offered, with or without tmux",
              listed and listed[0].get("windows") == ["edit", "git"], str(listed))

        if have_tmux:
            # Sổ đăng ký PHẢI là tmux, không phải một dict trong RAM: đó là thứ sống qua restart.
            rc, out = await so._tmux_run("list-sessions", "-F", "#{session_name}", capture=True)
            check("the real tmux server is the registry", NAME in out.split(), out.strip()[:200])
            check("the in-RAM set stays empty when tmux is in charge", not so._editors,
                  str(so._editors))
            default = subprocess.run([so._tmux(), "list-sessions", "-F", "#{session_name}"],
                                     capture_output=True, text=True, encoding="utf-8", errors="replace")
            case("the card lands on the private tmux server, not the default one",
                 lambda: (NAME not in default.stdout.split(), default.stdout))

        # Mở lại card đang mở = không sao, không đẻ phiên thứ hai.
        r = await c.post("/api/editor/open", json={"session": SID})
        again = (await c.get("/api/editor")).json()
        check("opening twice is harmless and does not duplicate",
              r.status_code == 200 and len(again) == 1, f"{r.status_code} {again}")

        if os.name == "posix":
            _RPC_LOG.unlink(missing_ok=True)
            for view, cmd in (("git", "OrchGit"), ("edit", "OrchFiles")):
                r = await c.post("/api/editor/focus", json={"session": SID, "window": view})
                sent = _RPC_LOG.read_text(encoding="utf-8") if _RPC_LOG.exists() else ""
                case(f"the {view} tab switches over RPC, from any nvim mode",
                     lambda sent=sent, cmd=cmd: (r.status_code == 200 and "--remote-send" in sent
                                                 and so._editor_addr(SID) in sent
                                                 and f"<C-\\><C-N><Cmd>{cmd}<CR>" in sent, sent))
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
    os.environ.pop("ORCH_NVIM_BIN", None)


async def main_download():
    """Lần mở đầu tiên: API trả về ngay, card nói đang tải, rồi tự mở; hỏng thì card nói vì sao."""
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        sid = "editor-guard-dl"

        async def fresh(payload, **kw):
            """_serve đổi DB_DIR, mà DB nằm ở đó: session phải đăng ký lại."""
            _serve(payload, **kw)
            so._ensure_db()
            await c.post("/api/sessions", json={"id": sid, "name": "dl", "cwd": str(tmp)})

        async def state():
            rows = [x for x in (await c.get("/api/editor")).json() if x["session"] == sid]
            return rows[0] if rows else {}

        await fresh(_archive(), delay=1.0)
        t0 = time.monotonic()
        r = await c.post("/api/editor/open", json={"session": sid})
        took = time.monotonic() - t0
        case("the first open answers within a second and says it is downloading",
             lambda: (took < 1 and r.json().get("state") == "downloading", f"{took:.2f}s {r.text[:160]}"))
        for _ in range(100):
            if not (await state()).get("state"):
                break
            await asyncio.sleep(0.1)
        st = await state()
        case("the card opens by itself when the download ends",
             lambda: (st.get("open") is True and not st.get("state") and _installed() == [so.NVIM_VERSION],
                      str(st)))
        await c.post("/api/editor/close", json={"session": sid})

        await fresh(_archive(), sha="0" * 64)
        await c.post("/api/editor/open", json={"session": sid})
        for _ in range(100):
            if (await state()).get("state") != "downloading":
                break
            await asyncio.sleep(0.1)
        st = await state()
        case("a failed download shows a plain message on the card",
             lambda: (st.get("state") == "failed" and "ORCH_NVIM_BIN" in st.get("error", ""), str(st)))
        await c.post("/api/editor/close", json={"session": sid})
        st = await state()
        case("and the failed card can be closed", lambda: (st == {}, str(st)))
    _ASSETS.clear()
    _ASSETS.update(_REAL_ASSETS)
    so._nvim_check = _REAL_CHECK
    os.environ.pop("ORCH_NVIM_MIRROR", None)
    so.DB_DIR = REAL_DB_DIR


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


async def live_modes(exe, env, root):
    """Vim modes là mặc định, trên nvim thật. Có cờ tắt thì về typing-first; cờ cũ không còn tác dụng."""
    sid = "editor-guard-modes"
    addr = so._editor_addr(sid)
    project = root / "modes"
    project.mkdir()
    note = project / "note.txt"
    note.write_text("alpha 7\nbeta\n", encoding="utf-8")

    def start():
        cmd = so._nvim_cmd(sid, exe)
        return subprocess.Popen([exe, "--headless", *cmd[1:], "note.txt"], cwd=project, env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(p):
        p.kill()
        p.wait(10)
        so._editor_clear_stale(addr)

    def expr(e):
        r = subprocess.run([exe, "--server", addr, "--remote-expr", e], env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=20)
        return r.stdout.strip()

    def send(keys):
        subprocess.run([exe, "--server", addr, "--remote-send", keys], env=env, capture_output=True,
                       timeout=20)

    async def until(fn, seconds=10):
        for _ in range(int(seconds * 10)):
            if fn():
                return True
            await asyncio.sleep(0.1)
        return bool(fn())

    async def up():
        await until(lambda: expr("v:vim_did_enter") == "1", 30)
        await asyncio.sleep(0.5)     # typing-first vào insert ở vim.schedule sau VimEnter

    async def mode_is(m):
        return await until(lambda: expr("mode()") == m, 3)

    def bar():
        return expr("luaeval(\"vim.api.nvim_eval_statusline(vim.o.statusline, {}).str\")")

    proc = start()
    try:
        await up()
        state = Path(expr("stdpath('state')"))
        flag = state / "no-vim-modes"
        check("a fresh card opens in Normal mode", expr("mode()") == "n", expr("mode()"))
        check("a mouse drag gives Visual: selectmode is empty", expr("&selectmode") == "", expr("&selectmode"))
        check("the title gives Ctrl+V to nvim in Normal", expr("&titlestring").startswith("orch:vim:"),
              expr("&titlestring"))

        send("<C-\\><C-N><Cmd>set columns=94<CR>")
        await until(lambda: expr("&columns") == "94", 3)
        await until(lambda: "Press i" in bar() and "Ln 1" in bar(), 3)
        text = bar()
        check("the status bar shows the mode, the hint and Ln/Col at 94 columns",
              "NORMAL" in text and "Press i to type · Esc to stop · F1 for actions" in text and "Ln 1" in text, text)
        send("<C-\\><C-N><Cmd>set columns=46<CR>")
        await until(lambda: expr("&columns") == "46", 3)
        await until(lambda: "Press i" not in bar(), 3)
        text = bar()
        check("a narrow bar keeps the mode and Ln/Col", "NORMAL" in text and "Ln 1" in text, text)
        send("<C-\\><C-N><Cmd>set columns=94<CR>")
        await until(lambda: expr("&columns") == "94", 3)

        send("i")
        check("i enters Insert", await mode_is("i"), expr("mode()"))
        check("the title gives Ctrl+V to the browser in Insert",
              await until(lambda: expr("&titlestring").startswith("orch:typing:"), 3), expr("&titlestring"))
        send("<Esc>")
        check("Esc leaves Insert", await mode_is("n"), expr("mode()"))
        check("and the title follows", await until(lambda: expr("&titlestring").startswith("orch:vim:"), 3),
              expr("&titlestring"))
        await until(lambda: "Press i" not in bar(), 3)
        text = bar()
        check("the hint is gone after the first insert, and the full bar is back",
              "Press i" not in text and "NORMAL" in text and "Spaces: " in text, text)
        send("v")
        check("v enters Visual", await mode_is("v"), expr("mode()"))
        check("Ctrl+V still belongs to nvim in Visual", await until(lambda: expr("&titlestring").startswith("orch:vim:"), 3),
              expr("&titlestring"))
        send("<Esc>")
        await mode_is("n")

        for key, modes in (("<C-s>", "nix"), ("<C-z>", "nixo"), ("<C-_>", "nix"), ("<C-b>", "ni")):
            missing = [m for m in modes if not expr(f"maparg('{key}', '{m}')")]
            check(f"{key} is mapped in Vim style ({modes})", not missing, f"missing in {missing}")
        for key, m in (("<C-a>", "n"), ("<C-x>", "n"), ("<C-p>", "i"), ("<C-c>", "n"), ("<C-c>", "i")):
            check(f"{key} stays native in mode {m}", expr(f"maparg('{key}', '{m}')") == "", expr(f"maparg('{key}', '{m}')"))

        send("gg0<C-a>")
        check("Ctrl+A adds to a number", await until(lambda: expr("getline(1)") == "alpha 8", 3), expr("getline(1)"))
        send("u")
        await until(lambda: expr("getline(1)") == "alpha 7", 3)

        send("<C-\\><C-N><Cmd>OrchVimModes<CR>")
        check("switching off writes no-vim-modes", await until(flag.exists, 3), str(flag))
        check("and types right away", await mode_is("i"), expr("mode()"))
        check("the title gives Ctrl+V to the browser", expr("&titlestring").startswith("orch:typing:"), expr("&titlestring"))
        send("<C-\\><C-N><Cmd>OrchVimModes<CR>")
        check("switching back removes it", await until(lambda: not flag.exists(), 3), str(flag))
        check("and lands in Normal", await mode_is("n"), expr("mode()"))
        check("Ctrl+Z is still mapped after switching back", bool(expr("maparg('<C-z>', 'n')")), "")

        send("ix<Esc>:q<CR>")
        await asyncio.sleep(0.5)
        check("a refused :q on unsaved text keeps nvim and brings the tree back",
              proc.poll() is None and await until(
                  lambda: expr("luaeval(\"require('nvim-tree.api').tree.is_visible()\")") in ("v:true", "true"), 3),
              expr("execute('messages')")[-200:])
        send("<C-\\><C-N><Cmd>silent write<CR>:q<CR>")
        check(":q on the last file quits nvim although the tree is open",
              await until(lambda: proc.poll() is not None, 5), "still running")
    finally:
        stop(proc)

    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("", encoding="utf-8")
    proc = start()
    try:
        await up()
        check("a no-vim-modes file starts typing-first", await mode_is("i"), expr("mode()"))
        check("with typing-first select behaviour", expr("&selectmode") == "mouse,key", expr("&selectmode"))
        check("and the title stays on typing", expr("&titlestring").startswith("orch:typing:"), expr("&titlestring"))
    finally:
        stop(proc)

    flag.unlink()
    (state / "use-vim-keys").write_text("", encoding="utf-8")
    long_branch = "feature/" + "a-very-long-branch-name-" * 3
    _git(project, "init", "-q", "-b", long_branch)
    proc = start()
    try:
        await up()
        check("the old use-vim-keys file means nothing", expr("mode()") == "n", expr("mode()"))
        for cols in (94, 48, 34):
            send(f"<C-\\><C-N><Cmd>set columns={cols}<CR>")
            await until(lambda: expr("&columns") == str(cols) and "Ln 1, Col 1" in bar(), 3)
            text = bar()
            check(f"a long branch does not cut Ln/Col or the mode at {cols} columns",
                  "NORMAL" in text and "Ln 1, Col 1" in text and "<" not in text, text)
        send("<C-\\><C-N><Cmd>set columns=94<CR>")
        await until(lambda: expr("&columns") == "94", 3)

        # Qua đúng F1: Telescope đẩy con trỏ sang phải một ô ngay trước khi rời insert.
        send("<C-\\><C-N>gg0<F1>")
        await until(lambda: expr("&filetype") == "TelescopePrompt", 5)
        send("Turn off")
        await asyncio.sleep(0.5)
        send("<CR>")
        await mode_is("i")
        await asyncio.sleep(0.3)
        check("Turn off Vim modes from column 1 keeps the cursor in column 1",
              expr("&filetype") != "TelescopePrompt" and expr("col('.')") == "1", expr("col('.')"))
    finally:
        stop(proc)


async def main_live():
    """nvim ghim thật + config bundled thật, không giao diện. Chạy ở CI Windows nên phải tự đủ."""
    exe = await asyncio.to_thread(so.nvim_install)
    print(f"LIVE pinned nvim at {exe}")
    ver = subprocess.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8",
                         errors="replace").stdout.splitlines()[0]
    check("the pinned nvim runs and is the pinned version", so.NVIM_VERSION in ver, ver)
    # Ngoài tmp: tmp là một repo, mà dự án con trong repo thì không còn là "không có repo nào".
    outside = Path(tempfile.mkdtemp()).resolve()
    project = outside / "two-repos"
    (project / "backend").mkdir(parents=True)
    (project / "web").mkdir()
    (project / "web" / "node_modules" / "dep").mkdir(parents=True)
    (project / "clean").mkdir()
    for repo, branch in ((project / "backend", "main"), (project / "web", "feature/login"),
                         (project / "web" / "node_modules" / "dep", "x"), (project / "clean", "stable")):
        _git(repo, "init", "-q", "-b", branch)
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        (repo / "old name.txt").write_text("rename me\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "init")
    (project / "backend" / "my file.py").write_text("print(1)\n", encoding="utf-8")
    (project / "backend" / "seed.txt").write_text("changed\n", encoding="utf-8")
    _git(project / "backend", "mv", "old name.txt", "new name.txt")
    (project / "web" / "web.js").write_text("x\n", encoding="utf-8")
    home = outside / "live-home"
    (home / ".config" / "nvim").mkdir(parents=True)
    marker = home / "user-config-ran"
    (home / ".config" / "nvim" / "init.lua").write_text(
        f"vim.fn.writefile({{'x'}}, [[{marker}]])\n", encoding="utf-8")
    # XDG_STATE_HOME: phần này ghi cờ no-vim-modes, không được rơi vào state thật của người dùng.
    env = {**os.environ, **so._editor_env(), "HOME": str(home), "USERPROFILE": str(home),
           "LOCALAPPDATA": str(home / "AppData"), "XDG_STATE_HOME": str(home / "state")}
    await live_modes(exe, env, outside)
    sid = "editor-guard-live"
    cmd = so._nvim_cmd(sid, exe)
    proc = subprocess.Popen([exe, "--headless", *cmd[1:]], cwd=project, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def expr(e):
        r = subprocess.run([exe, "--server", so._editor_addr(sid), "--remote-expr", e], env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        return r.stdout.strip()

    def panel():
        return expr("join(getbufline(bufnr('Source Control'),1,'$'),'\n')")

    async def until(fn, seconds=15):
        for _ in range(int(seconds * 10)):
            v = fn()
            if v:
                return v
            await asyncio.sleep(0.1)
        return fn()

    try:
        up = await until(lambda: expr("v:vim_did_enter") == "1", 30)
        check("nvim starts headless with the bundled config", up, "never answered over RPC")
        msgs = expr("execute('messages')")
        check("no error messages at startup", "E5" not in msgs and "rror" not in msgs, msgs[:400])
        check("the user's own nvim config is not read", not marker.exists(), str(marker))
        for mod in ("nvim-tree", "nvim-web-devicons", "telescope", "plenary", "gitsigns", "diffview",
                    "lualine", "vscode"):
            found = expr(f"luaeval(\"pcall(require, '{mod}')\")")
            check(f"plugin {mod} loads", found in ("v:true", "true", "1"), found)

        check("the git tab switches over RPC", await so.editor_focus(sid, "git"), "remote-send failed")
        await until(lambda: "changes" in panel())
        text = panel()
        check(":OrchGit lists both repos with their branches",
              "backend  main" in text and "web  feature/login" in text, text)
        check("with counts, and a clean repo says so", "3 changes" in text and "clean  stable  clean" in text
              and "web  feature/login  1 change" in text, text)
        check("a file name with a space and a rename are parsed, status letter included",
              "U  my file.py" in text and "R  new name.txt ← old name.txt" in text, text)
        check("a repo inside node_modules is not listed", "dep" not in text, text)

        row = next(i for i, l in enumerate(text.split("\n"), 1) if "my file.py" in l)
        subprocess.run([exe, "--server", so._editor_addr(sid), "--remote-send",
                        f"<C-\\><C-N><Cmd>{row}<CR><CR>"], env=env, capture_output=True, timeout=20)
        check("clicking a file opens diffview",
              await until(lambda: expr("tabpagenr('$')") == "2"), expr("tabpagenr('$')"))
        await asyncio.sleep(1)
        msgs = expr("execute('messages')")
        check("diffview works on the pinned nvim, with no errors", "E5" not in msgs and "rror" not in msgs,
              msgs[:400])

        check("the files tab switches over RPC", await so.editor_focus(sid, "edit"), "remote-send failed")
        check("and closes the diff", await until(lambda: expr("tabpagenr('$')") == "1"),
              expr("tabpagenr('$')"))

        subprocess.run([exe, "--server", so._editor_addr(sid), "--remote-send",
                        "<C-\\><C-N><Cmd>cd backend<CR>"], env=env, capture_output=True, timeout=20)
        await asyncio.sleep(0.3)
        await so.editor_focus(sid, "git")
        check("one repo goes straight to diffview", await until(lambda: expr("tabpagenr('$')") == "2"),
              expr("tabpagenr('$')"))
        await so.editor_focus(sid, "edit")
        await until(lambda: expr("tabpagenr('$')") == "1")

        empty = outside / "no-repo"
        empty.mkdir()
        (empty / "a.txt").write_text("a\n", encoding="utf-8")
        subprocess.run([exe, "--server", so._editor_addr(sid), "--remote-send",
                        f"<C-\\><C-N><Cmd>cd {empty.as_posix()}<CR>"], env=env, capture_output=True, timeout=20)
        await asyncio.sleep(0.3)
        await so.editor_focus(sid, "git")
        await until(lambda: "No git" in panel())
        msgs = expr("execute('messages')")
        check("no repo shows a plain message and no Lua error", "No git" in panel() and "E5" not in msgs,
              panel() + msgs[:300])
    finally:
        proc.kill()
        proc.wait(10)
        proc.stderr.close()
        so._editor_clear_stale(so._editor_addr(sid))
        shutil.rmtree(outside, ignore_errors=True)


try:
    asyncio.run(main())
    if os.name == "posix":
        asyncio.run(main_download())
    if LIVE:
        print("LIVE part: running with the pinned nvim")
        asyncio.run(main_live())
    else:
        print("SKIP live part — set ORCH_CHECK_EDITOR_LIVE=1 to download and run the pinned nvim")
finally:
    asyncio.run(so._tmux_run("kill-server"))    # server riêng của test (TMUX_TMPDIR), đừng bỏ rác
    db.unlink(missing_ok=True)
    shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print("\n" + "\n".join(fails))
    sys.exit(1)
print("\nall editor checks passed")
