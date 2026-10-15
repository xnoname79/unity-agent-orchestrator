#!/usr/bin/env python3
"""Guards for the Sounds panel and its MCP tools — search a video, pull its sound out with yt-dlp.

Text from strangers reaches yt-dlp's argv here: the search a user (or an agent) types, the link
they paste. Every way that goes wrong is a quiet one:

  - a search or link that starts with '-' is a FLAG to yt-dlp (`--exec` runs a command), unless
    it sits after '--'. Links must also be http(s), never file://;
  - the saved file is named by us, not by the video's title, and the file endpoints take only
    that exact shape — anything else could walk out of the sounds folder;
  - a failed extraction leaves .part files; left there, they pile up next to the library;
  - yt-dlp's duration filter skips a video SILENTLY (exit 0, no output), so "no output" must be
    reported as "too long", not as success or as a crash;
  - an old yt-dlp is the most common real failure (sites change), so the error must say how old
    it is and how to upgrade;
  - the panel and the MCP tools must share one library: what an agent extracts, the user sees.

yt-dlp itself is not run: _ytdlp is replaced by a fake that answers the way yt-dlp 2026.08 does
(measured), so this runs offline and on Windows.

    python3 check_sounds.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["ORCH_DB"] = "check_sounds"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402
import sound_mcp                      # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
so.SOUNDS_DIR = Path(tempfile.mkdtemp())
fails = []
calls = []                            # argv của mọi lần "chạy" yt-dlp


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("ok   " if ok else "FAIL ") + name + (f" — {detail}" if not ok else ""))


async def fake_ytdlp(args, timeout):
    """Trả lời như yt-dlp 2026.08: tìm → JSON từng dòng; tách → file + một dòng JSON."""
    calls.append(list(args))
    target = args[args.index("--") + 1]
    if target.startswith("ytsearch"):
        rows = [{"id": "a1", "title": "Rain loop", "url": "https://www.youtube.com/watch?v=a1",
                 "channel": "Renders", "duration": 42.0, "view_count": 1200,
                 "thumbnails": [{"url": "https://i.ytimg.com/vi/a1/hq720.jpg"}]},
                {"id": "b2", "title": "LIVE rain", "url": "https://www.youtube.com/watch?v=b2",
                 "live_status": "is_live"},
                {"id": "c3", "title": "10 hours of rain", "url": "https://www.youtube.com/watch?v=c3",
                 "duration": 36000.0}]
        return 0, "\n".join(json.dumps(r) for r in rows) + "\n", ""
    out_tmpl = args[args.index("-o") + 1]
    stem = Path(out_tmpl).name.split(".")[0]
    if "long" in target:
        return 0, "", ""                              # match-filters bỏ qua trong im lặng
    if "broken" in target:
        (so.SOUNDS_DIR / f"{stem}.webm.part").write_bytes(b"half")
        return 1, "", "ERROR: [youtube] x: The page needs to be reloaded.\n"
    fmt = args[args.index("--audio-format") + 1]
    dst = so.SOUNDS_DIR / f"{stem}.{fmt}"
    dst.write_bytes(b"ID3fake-audio")
    info = {"id": "a1", "title": 'Rain / thunder: "loop"', "channel": "Renders", "duration": 42,
            "webpage_url": target, "filepath": str(dst)}
    return 0, "Deprecated Feature: something\n" + json.dumps(info) + "\n", ""


real_which = so.shutil.which
present = {"yt-dlp": "/fake/bin/yt-dlp", "ffmpeg": "/fake/bin/ffmpeg"}


def fake_which(cmd, *a, **kw):
    return present.get(cmd) if cmd in ("yt-dlp", "ffmpeg") else real_which(cmd, *a, **kw)


so.shutil.which = fake_which
so._ytdlp_versions["/fake/bin/yt-dlp"] = "2099.01.01"     # mới: không cảnh báo


async def main():
    real_ytdlp = so._ytdlp
    so._ytdlp = fake_ytdlp
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8992") as c:
        # ── Tìm ──────────────────────────────────────────────────────────────
        r = await c.post("/api/sounds/search", json={"query": "rain", "limit": 12})
        body = r.json()
        check("search works", r.status_code == 200, f"{r.status_code} {r.text[:90]}")
        args = calls[-1]
        check("the search sits after '--', so it can never be read as a flag",
              args[-2:] == ["--", "ytsearch12:rain AI generated"], str(args))
        titles = [x["title"] for x in body["results"]]
        check("live streams are left out (nothing to extract)", "LIVE rain" not in titles, titles)
        long = [x for x in body["results"] if x["title"] == "10 hours of rain"]
        check("videos over the limit are flagged", long and long[0]["too_long"], str(long))
        await c.post("/api/sounds/search", json={"query": "AI generated rain"})
        check("'AI generated' is not added twice", calls[-1][-1] == "ytsearch10:AI generated rain",
              calls[-1][-1])
        await c.post("/api/sounds/search", json={"query": "rain", "ai_only": False})
        check("unticking AI-generated leaves the search alone", calls[-1][-1] == "ytsearch10:rain",
              calls[-1][-1])
        r = await c.post("/api/sounds/search", json={"query": "  "})
        check("an empty search is refused", r.status_code == 400, str(r.status_code))

        # ── Tách tiếng ───────────────────────────────────────────────────────
        for bad in ("file:///etc/passwd", "--exec=rm -rf ~", "javascript:alert(1)", ""):
            n = len(calls)
            r = await c.post("/api/sounds", json={"url": bad})
            check(f"link {bad!r} is refused before yt-dlp runs",
                  r.status_code == 400 and len(calls) == n, f"{r.status_code} {r.text[:80]}")
        r = await c.post("/api/sounds", json={"url": "https://youtu.be/a1", "format": "exe"})
        check("an unknown format is refused", r.status_code == 400, str(r.status_code))

        present["ffmpeg"] = None
        r = await c.post("/api/sounds", json={"url": "https://youtu.be/a1"})
        check("without ffmpeg the user is told to install it",
              r.status_code == 409 and "ffmpeg" in r.json()["error"], r.text[:120])
        present["ffmpeg"] = "/fake/bin/ffmpeg"

        r = await c.post("/api/sounds", json={"url": "https://youtu.be/a1", "format": "wav"})
        s = r.json()
        check("a sound is extracted", r.status_code == 200 and s["name"].endswith(".wav"),
              f"{r.status_code} {r.text[:120]}")
        args = calls[-1]
        check("the link sits after '--'", args[-2:] == ["--", "https://youtu.be/a1"], str(args))
        check("long videos and live streams are filtered",
              "duration<=1800 & !is_live" in args, str(args))
        check("the file is named by us, not by the video title",
              so._SOUND_NAME_OK.match(s["name"]) is not None, s["name"])
        check("the title and source are kept", s["title"].startswith("Rain") and s["by"] == "user"
              and s["source"] == "https://youtu.be/a1", str(s))

        r = await c.get("/api/sounds")
        check("it shows up in the library",
              any(i["name"] == s["name"] for i in r.json()["items"]), r.text[:120])
        r = await c.get(f"/api/sounds/{s['name']}")
        check("it plays in the browser", r.status_code == 200
              and r.headers["content-type"].startswith("audio/wav"), r.headers.get("content-type"))
        r = await c.get(f"/api/sounds/{s['name']}?download=1")
        cd = urllib.parse.unquote(r.headers.get("content-disposition", ""))
        check("download is named after the title, without characters a disk refuses",
              "attachment" in cd and cd.endswith("Rain thunder loop.wav"), cd)

        for bad in ("..%2F..%2Fetc%2Fpasswd", "x.mp3", "20261015-101010-abcdef.exe"):
            r = await c.get(f"/api/sounds/{bad}")
            check(f"name {bad!r} is not served", r.status_code == 404, str(r.status_code))
            r = await c.delete(f"/api/sounds/{bad}")
            # 405 = '..' đã đưa đường ra khỏi route này, sang static — cũng là không xoá gì.
            check(f"name {bad!r} is not deleted", r.status_code in (404, 405), str(r.status_code))

        before = set(os.listdir(so.SOUNDS_DIR))
        r = await c.post("/api/sounds", json={"url": "https://youtu.be/long-one"})
        check("a video skipped for length says so",
              r.status_code == 400 and "longer than 30 minutes" in r.json()["error"], r.text[:120])
        so._ytdlp_versions["/fake/bin/yt-dlp"] = "2025.02.19"
        r = await c.post("/api/sounds", json={"url": "https://youtu.be/broken"})
        err = r.json().get("error", "")
        check("a yt-dlp failure comes back with its own message",
              r.status_code == 502 and "page needs to be reloaded" in err, err)
        check("an old yt-dlp is named, with how to upgrade it", "days old" in err
              and "upgrade" in err, err)
        so._ytdlp_versions["/fake/bin/yt-dlp"] = "2099.01.01"
        check("failed extractions leave nothing behind",
              set(os.listdir(so.SOUNDS_DIR)) == before, str(set(os.listdir(so.SOUNDS_DIR)) - before))

        r = await c.delete(f"/api/sounds/{s['name']}")
        check("delete removes the sound and its notes", r.status_code == 200
              and not (so.SOUNDS_DIR / s["name"]).exists()
              and not (so.SOUNDS_DIR / s["name"]).with_suffix(".json").exists(), r.text[:90])

        present["yt-dlp"] = None
        so._ytdlp = real_ytdlp                        # đường thật: phải dừng ở chỗ thiếu binary
        r = await c.post("/api/sounds/search", json={"query": "rain"})
        check("without yt-dlp search says how to install it",
              r.status_code == 409 and "not installed" in r.json()["error"], r.text[:120])
        r = await c.get("/api/sounds")
        check("and the panel knows it is missing", r.json()["status"]["ytdlp"] in ("", None),
              str(r.json()["status"]))
        present["yt-dlp"] = "/fake/bin/yt-dlp"
        so._ytdlp = fake_ytdlp

    # ── MCP: cùng lõi, cùng thư viện ─────────────────────────────────────────
    res = json.loads(await sound_mcp.search_videos("rain", 5))
    check("search_videos returns the results as JSON", res["results"][0]["title"] == "Rain loop",
          str(res)[:120])
    made = json.loads(await sound_mcp.extract_audio("https://youtu.be/a1", "mp3", "sound-designer"))
    lib = json.loads(await sound_mcp.list_sounds())
    check("what an agent extracts lands in the user's library, signed with its name",
          any(i["name"] == made["name"] and i["by"] == "sound-designer" for i in lib), str(lib)[:160])
    check("the agent gets the file path to copy from", Path(made["path"]).is_file(), made["path"])
    msg = await sound_mcp.extract_audio("file:///etc/passwd")
    check("tool errors read as instructions, not as crashes", msg.startswith("⛔"), msg)

    # ── Nối dây ──────────────────────────────────────────────────────────────
    html = (HERE / "static/orchestrator/index.html").read_text(encoding="utf-8")
    js = (HERE / "static/orchestrator/app.js").read_text(encoding="utf-8")
    py = (HERE / "session_orchestrator.py").read_text(encoding="utf-8")
    spec = (HERE / "build.spec").read_text(encoding="utf-8")
    for want, where, blob in (('id="snd-modal"', "index.html", html),
                              ('onclick="sndOpen()"', "index.html", html),
                              ("window.sndOpen", "app.js", js),
                              ('Mount("/sounds", app=sound_app)', "session_orchestrator.py", py),
                              ('"sound_mcp"', "build.spec", spec)):
        check(f"{where} wires up {want}", want in blob, "not found")


asyncio.run(main())
so.shutil.which = real_which
(so.DB_DIR / "check_sounds.db").unlink(missing_ok=True)
print()
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("all sounds checks passed")
