#!/usr/bin/env python3
"""Guard: the Media tools panel must tell the truth about this machine.

The orchestrator ships no ffmpeg. Agents edit video by typing commands in their own terminal, so
a missing tool only shows up as "command not found" halfway through a run — which is why the
panel exists. Two ways it could quietly lie:

  - the ImageMagick command is `magick` on version 7 and `convert` on the version Debian and
    Ubuntu still ship. Report the wrong one and the agent writes a command that cannot run;
  - the red dot on the topbar must mean "something you need is missing". If an optional tool
    lights it, people learn to ignore it, and then it means nothing.

PATH here is a fake directory with empty files in it — `shutil.which` only asks whether the name
is there, so that is enough to drive every branch without installing anything.

    python3 check_tools.py
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["ORCH_DB"] = "check_tools"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()
os.environ["ORCH_DRY_RUN"] = "1"

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
db = so.DB_DIR / "check_tools.db"
db.unlink(missing_ok=True)
so._ensure_db()

WIN = sys.platform.startswith("win")
REAL_PATH = os.environ.get("PATH", "")
ALL = ("ffmpeg", "ffprobe", "magick", "yt-dlp", "sox", "exiftool")
fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


def with_path(*names):
    """media_tools() as it would read on a machine where exactly `names` are installed.

    The package managers are always in there. Without them _install_cmd goes quiet on purpose
    — it does not hand a dnf box an apt command — and the "missing tools come with a command"
    checks below would then pass by saying nothing. as_os() covers that behaviour on its own.
    """
    d = Path(tempfile.mkdtemp())
    for n in (*names, "apt", "brew", "winget", "python3", "py"):
        f = d / (n + ".exe" if WIN else n)
        f.write_bytes(b"")
        if not WIN:
            f.chmod(0o755)
    os.environ["PATH"] = str(d)
    try:
        return {r["name"]: r for r in so.media_tools()["tools"]}, so.media_tools()["missing"]
    finally:
        os.environ["PATH"] = REAL_PATH


def test_shape():
    rows, _ = with_path(*ALL)
    need = {r["name"] for r in so.media_tools()["tools"] if r["required"]}
    check("ffmpeg, ffprobe and ImageMagick are the ones that count as required",
          need == {"ffmpeg", "ffprobe", "ImageMagick"}, str(sorted(need)))
    check("every tool says what an agent uses it for and where to get it",
          all(r["what"] and r["site"].startswith("https://") for r in rows.values()),
          str([n for n, r in rows.items() if not (r["what"] and r["site"])]))


def test_found_and_missing():
    rows, missing = with_path(*ALL)
    check("all installed: nothing is reported missing", missing == 0, str(missing))
    check("all installed: every row carries the path it was found at",
          all(r["path"] for r in rows.values()),
          str([n for n, r in rows.items() if not r["path"]]))
    check("all installed: no install command is pushed at you",
          not any(r["install"] for r in rows.values()),
          str([n for n, r in rows.items() if r["install"]]))

    rows, missing = with_path()
    check("empty PATH: the three required tools are counted", missing == 3, str(missing))
    check("empty PATH: every missing tool comes with a command to fix it",
          all(r["install"] for r in rows.values()),
          str([n for n, r in rows.items() if not r["install"]]))


def test_red_dot_means_something():
    _, missing = with_path("ffmpeg", "ffprobe", "magick")
    check("the dot stays off when only optional tools are absent", missing == 0, str(missing))
    _, missing = with_path("ffprobe", "magick", "yt-dlp", "sox", "exiftool")
    check("the dot comes on for a missing required tool", missing == 1, str(missing))


def test_imagemagick_version():
    """IM6 answers to `convert`, IM7 to `magick`. Tell the agent the one that exists HERE."""
    rows, _ = with_path("convert")
    im = rows["ImageMagick"]
    check("ImageMagick 6 is found through `convert`", im["path"] and im["command"] == "convert",
          f"{im['command']} {im['path']}")
    check("...and says so, since an agent writing `magick` would fail",
          "convert" in im["note"] and "magick" in im["note"], im["note"])

    rows, _ = with_path("magick")
    im = rows["ImageMagick"]
    check("ImageMagick 7 is found through `magick`, with nothing to warn about",
          im["command"] == "magick" and not im["note"], f"{im['command']} {im['note']!r}")

    rows, _ = with_path()
    im = rows["ImageMagick"]
    check("not installed at all: it offers the modern command, not the legacy one",
          im["command"] == "magick" and not im["note"] and not im["path"], im["command"])


def as_os(platform, *names):
    """media_tools() as it reads on `platform` with nothing installed but `names`.

    Every spelling of the name is laid down because shutil.which switches to Windows rules the
    moment sys.platform says so, and this also runs on Linux, where the filesystem is case
    sensitive and the default PATHEXT it falls back to is upper case.
    """
    d = Path(tempfile.mkdtemp())
    for n in names:
        for f in (d / n, d / (n + ".exe"), d / (n + ".EXE")):
            f.write_bytes(b"")
            f.chmod(0o755)
    os.environ["PATH"] = str(d)
    # Windows rules need PATHEXT, and shutil splits it on os.pathsep — ';' on Windows but ':'
    # here, so the real value would come back as one nonsense extension. Pin it to the one
    # extension these fakes use; on a Windows runner it is just as true.
    real_ext = os.environ.get("PATHEXT")
    if platform == "win32":
        os.environ["PATHEXT"] = ".EXE"
    real = so.sys.platform
    so.sys.platform = platform
    try:
        return {r["name"]: r for r in so.media_tools()["tools"]}
    finally:
        so.sys.platform = real
        os.environ["PATH"] = REAL_PATH
        if real_ext is None:
            os.environ.pop("PATHEXT", None)
        else:
            os.environ["PATHEXT"] = real_ext


def test_windows():
    rows = as_os("win32", "winget")
    check("Windows: ffmpeg is installed with winget",
          rows["ffmpeg"]["install"] == "winget install Gyan.FFmpeg", rows["ffmpeg"]["install"])
    check("Windows: no apt or brew command gets through",
          not any("apt" in r["install"] or "brew" in r["install"] for r in rows.values()),
          str([r["install"] for r in rows.values()]))
    check("Windows: SoX has no winget id, so it is left to its download page",
          not rows["SoX"]["install"] and rows["SoX"]["site"], rows["SoX"]["install"])
    rows = as_os("win32")
    check("Windows without winget: nothing is suggested that cannot run",
          not any(r["install"] for r in rows.values()),
          str([n for n, r in rows.items() if r["install"]]))


def test_macos():
    rows = as_os("darwin", "brew", "python3")
    check("macOS: ffmpeg is installed with Homebrew",
          rows["ffmpeg"]["install"] == "brew install ffmpeg", rows["ffmpeg"]["install"])
    check("macOS: SoX and ExifTool come from Homebrew too",
          rows["SoX"]["install"] == "brew install sox"
          and rows["ExifTool"]["install"] == "brew install exiftool", "")
    rows = as_os("darwin")
    check("macOS without Homebrew: it does not tell you to run brew",
          not any("brew" in r["install"] for r in rows.values()),
          str([r["install"] for r in rows.values() if r["install"]]))
    check("...and the download page is still there to click",
          all(r["site"].startswith("https://") for r in rows.values()), "")


def test_gate_is_per_command_not_per_os():
    """yt-dlp comes from pip on every OS. Gating by OS instead of by the command itself would
    have taken the pip line away from a Mac with no Homebrew, which has nothing to do with it."""
    rows = as_os("darwin", "python3")
    check("macOS without Homebrew still gets the pip line for yt-dlp",
          rows["yt-dlp"]["install"] == "python3 -m pip install --upgrade yt-dlp",
          rows["yt-dlp"]["install"])
    check("...and ffmpeg, which really does need Homebrew, stays quiet",
          not rows["ffmpeg"]["install"], rows["ffmpeg"]["install"])
    rows = as_os("win32", "winget", "py")
    check("Windows reaches pip through the py launcher",
          rows["yt-dlp"]["install"] == "py -m pip install --upgrade yt-dlp",
          rows["yt-dlp"]["install"])


def test_no_guessed_commands():
    """A wrong install command costs the user a round trip they cannot debug. Silence is better."""
    rows = as_os("linux", "apt")
    check("Debian/Ubuntu: apt commands are given",
          rows["ffmpeg"]["install"] == "sudo apt install ffmpeg", rows["ffmpeg"]["install"])
    rows = as_os("linux")                   # dnf, pacman, zypper...
    apt = [n for n, r in rows.items() if r["install"].startswith("sudo apt")]
    check("no apt on the machine: it does not hand out apt commands", not apt, str(apt))
    check("...but the download page is still there to click",
          all(r["site"] for r in rows.values()), "")


def test_wiring():
    """A button calling a function that does not exist is a dead button no test would notice."""
    html = Path("static/orchestrator/index.html").read_text(encoding="utf-8")
    js = Path("static/orchestrator/app.js").read_text(encoding="utf-8")
    check("the topbar button exists and opens the panel",
          'id="tools-btn"' in html and 'onclick="toolsOpen()"' in html, "")
    check("toolsOpen is defined and exported to the inline onclick",
          "function toolsOpen()" in js and "window.toolsOpen = toolsOpen" in js, "")
    check("the dot is asked for at boot", "toolsBadge();" in js, "")


async def main():
    test_shape()
    test_found_and_missing()
    test_red_dot_means_something()
    test_imagemagick_version()
    test_no_guessed_commands()
    test_windows()
    test_macos()
    test_gate_is_per_command_not_per_os()
    test_wiring()
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8992") as c:
        r = await c.get("/api/tools")
        body = r.json()
        check("/api/tools answers the dashboard",
              r.status_code == 200 and len(body["tools"]) == len(so.MEDIA_TOOLS)
              and isinstance(body["missing"], int), f"{r.status_code} {r.text[:90]}")

    db.unlink(missing_ok=True)
    print()
    if fails:
        print(f"{len(fails)} FAILED")
        sys.exit(1)
    print("all good")


asyncio.run(main())
