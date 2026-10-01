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

    `apt` is always in there: without it _install_cmd goes quiet on Linux (correctly — it will
    not hand a dnf machine an apt command), and then the "missing tools come with a command"
    checks below would pass on Windows and macOS while saying nothing on Linux.
    """
    d = Path(tempfile.mkdtemp())
    for n in (*names, "apt"):
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


def test_no_guessed_commands():
    """A wrong install command costs the user a round trip. Silence is better."""
    if WIN or sys.platform == "darwin":
        check("winget/brew commands are given on this OS",
              all(r["install"] for r in with_path()[0].values()), "")
        return
    d = Path(tempfile.mkdtemp())            # a Linux box with no apt: dnf, pacman, zypper...
    os.environ["PATH"] = str(d)
    try:
        rows = {r["name"]: r for r in so.media_tools()["tools"]}
    finally:
        os.environ["PATH"] = REAL_PATH
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
