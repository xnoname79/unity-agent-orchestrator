#!/usr/bin/env python3
"""Guards for the Sounds panel — search a video, pull its sound out with yt-dlp, cut it.

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
    it is and how to upgrade THAT copy — a generic `pip install --upgrade` lands in whichever python
    the user's shell has (conda), while the orchestrator keeps running the old release binary;
  - the version is cached for the 5-second poll, but an upgrade in place must show without a
    restart;
  - the length filter runs AFTER the search, so a tight one must fetch more, then trim to the
    limit — or "up to 5 min" comes back nearly empty;
  - the panel's extract is a DRAFT: not in the library until the editor keeps it, a failed cut
    keeps the draft, a bad range never reaches ffmpeg, keeping all of it is not re-encoded;
  - an upload is a stranger's file on our disk: only mp3, under a size cap checked WHILE it
    streams in, and nothing left behind when it is refused.

yt-dlp and ffmpeg are not run: _ytdlp and _ffmpeg are replaced by fakes that answer the way
yt-dlp 2026.08 and ffmpeg do (measured), so this runs offline and on Windows.

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
    home = Path(out_tmpl).parent
    if "broken" in target:
        (home / f"{stem}.webm.part").write_bytes(b"half")
        return 1, "", "ERROR: [youtube] x: The page needs to be reloaded.\n"
    fmt = args[args.index("--audio-format") + 1]
    dst = home / f"{stem}.{fmt}"
    dst.write_bytes(b"ID3fake-audio")
    info = {"id": "a1", "title": 'Rain / thunder: "loop"', "channel": "Renders", "duration": 42,
            "webpage_url": target, "filepath": str(dst)}
    return 0, "Deprecated Feature: something\n" + json.dumps(info) + "\n", ""


BIN = Path(tempfile.mkdtemp())
YT = BIN / "yt-dlp"
YT.write_text("#!/opt/conda/bin/python3.11\nfrom yt_dlp import main\n")
ff_calls = []                         # argv của mọi lần "chạy" ffmpeg
ff = {"fail": False}


async def fake_ffmpeg(args, timeout):
    """Đọc sóng → PCM s16le: 1 giây im lặng rồi 1 giây ở nửa biên độ. Cắt → ghi file đích."""
    ff_calls.append(list(args))
    if ff["fail"]:
        return 1, b"", "Error while decoding stream #0:0"
    if args[-1] == "-":
        return 0, b"\0\0" * 8000 + (16384).to_bytes(2, "little", signed=True) * 8000, ""
    Path(args[-1]).write_bytes(b"ID3cut")
    return 0, b"", ""


real_which = so.shutil.which
present = {"yt-dlp": str(YT), "ffmpeg": "/fake/bin/ffmpeg"}


def fake_which(cmd, *a, **kw):
    return present.get(cmd) if cmd in ("yt-dlp", "ffmpeg") else real_which(cmd, *a, **kw)


def set_version(v):
    so._ytdlp_versions[(str(YT), os.stat(YT).st_mtime_ns)] = v


so.shutil.which = fake_which
set_version("2099.01.01")                                # mới: không cảnh báo


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
        check("videos longer than the most we extract are left out",
              "10 hours of rain" not in titles, titles)
        r = await c.post("/api/sounds/search", json={"query": "rain", "limit": 12, "max_minutes": 5})
        check("a tight length filter fetches more, since it filters after the search",
              calls[-1][-1].startswith("ytsearch36:"), calls[-1][-1])
        check("and keeps only videos up to that length",
              [x["title"] for x in r.json()["results"]] == ["Rain loop"], r.text[:160])
        r = await c.post("/api/sounds/search", json={"query": "rain", "limit": 1, "max_minutes": 60})
        check("results are trimmed to the limit", len(r.json()["results"]) == 1, r.text[:160])
        r = await c.post("/api/sounds/search", json={"query": "rain", "max_minutes": "abc"})
        check("a bad length falls back to the most we extract",
              r.status_code == 200 and calls[-1][-1].startswith("ytsearch10:"), r.text[:120])
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
              "duration<=3600 & !is_live" in args, str(args))
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
              r.status_code == 400 and "longer than 60 minutes" in r.json()["error"], r.text[:120])
        set_version("2025.02.19")
        r = await c.post("/api/sounds", json={"url": "https://youtu.be/broken"})
        err = r.json().get("error", "")
        check("a yt-dlp failure comes back with its own message",
              r.status_code == 502 and "page needs to be reloaded" in err, err)
        check("an old yt-dlp is named, with how to upgrade it", "days old" in err
              and "upgrade" in err, err)
        check("a pip-installed yt-dlp is upgraded with the pip of ITS python",
              "/opt/conda/bin/python3.11 -m pip install --upgrade yt-dlp" in err, err)

        # Nâng cấp tại chỗ: file bị ghi đè → mtime đổi → đọc lại version, không cần restart.
        st = os.stat(YT)
        os.utime(YT, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        real_run = so.subprocess.run
        so.subprocess.run = lambda *a, **kw: so.subprocess.CompletedProcess(a, 0, "2099.02.02\n", "")
        try:
            r = await c.get("/api/sounds")
        finally:
            so.subprocess.run = real_run
        st = r.json()["status"]
        check("an upgrade in place shows without a restart",
              st["version"] == "2099.02.02" and not st["stale"], str(st))

        # Bản release (zipapp/exe, vài MB) tự nâng cấp được.
        YT.write_bytes(b"#!/usr/bin/env python3\nPK" + bytes(1_000_000))
        up = so._ytdlp_upgrade_cmd(str(YT))
        check("a release build of yt-dlp is upgraded with its own -U",
              up.endswith(" -U") and str(YT) in up, up)
        YT.write_text("#!/opt/conda/bin/python3.11\nfrom yt_dlp import main\n")
        set_version("2099.01.01")
        check("failed extractions leave nothing behind",
              set(os.listdir(so.SOUNDS_DIR)) == before, str(set(os.listdir(so.SOUNDS_DIR)) - before))

        r = await c.delete(f"/api/sounds/{s['name']}")
        check("delete removes the sound and its notes", r.status_code == 200
              and not (so.SOUNDS_DIR / s["name"]).exists()
              and not (so.SOUNDS_DIR / s["name"]).with_suffix(".json").exists(), r.text[:90])

        # ── Bản nháp → editor → thư viện ─────────────────────────────────────
        so._ffmpeg = fake_ffmpeg
        drafts = so.SOUNDS_DIR / "drafts"
        drafts.mkdir(exist_ok=True)
        old = drafts / "20200101-000000-abcdef.mp3"
        old.write_bytes(b"x")
        os.utime(old, (0, 0))
        r = await c.post("/api/sounds", json={"url": "https://youtu.be/a1", "draft": True})
        d = r.json()["name"]
        check("the panel's extract is a draft", r.status_code == 200 and (drafts / d).is_file(),
              r.text[:120])
        check("drafts left for over a day are swept", not old.exists(), str(list(drafts.iterdir())))
        lib = (await c.get("/api/sounds")).json()["items"]
        check("a draft is not in the library yet", all(i["name"] != d for i in lib), str(lib)[:120])
        r = await c.get(f"/api/sounds/drafts/{d}")
        check("the editor can play the draft", r.status_code == 200, str(r.status_code))
        r = await c.get(f"/api/sounds/{d}")
        check("the library route does not serve a draft", r.status_code == 404, str(r.status_code))
        r = await c.get(f"/api/sounds/drafts/{d}/peaks")
        pk = r.json()
        check("the wave comes back as peaks, with the length ffmpeg decoded",
              pk.get("duration") == 2.0 and len(pk.get("peaks", [])) == 1000
              and pk["peaks"][0] == 0 and pk["peaks"][-1] == 0.5, str(pk)[:120])
        r = await c.get("/api/sounds/drafts/x.mp3/peaks")
        check("a made-up draft name is not read", r.status_code != 200, str(r.status_code))

        keep = f"/api/sounds/drafts/{d}/keep"
        for bad in ({"start": -1, "end": 1}, {"start": 1, "end": 1.01}, {"start": "abc"},
                    {"start": "nan", "end": 1}, {"start": 0, "end": "inf"}):
            n = len(ff_calls)
            r = await c.post(keep, json=bad)
            check(f"range {bad} is refused before ffmpeg runs",
                  r.status_code == 400 and len(ff_calls) == n, f"{r.status_code} {r.text[:80]}")
        ff["fail"] = True
        r = await c.post(keep, json={"start": 0.5, "end": 1.5})
        ff["fail"] = False
        check("a failed cut keeps the draft and puts nothing in the library",
              r.status_code == 502 and (drafts / d).is_file() and not (so.SOUNDS_DIR / d).exists()
              and [f.name for f in drafts.iterdir() if "-cut" in f.name] == [], r.text[:120])
        r = await c.post(keep, json={"start": 0.5, "end": 1.5})
        k = r.json()
        args = next(a for a in reversed(ff_calls) if "-ss" in a)   # sau nó có thể là lượt đo nền
        check("the cut asks ffmpeg for exactly that part",
              args[args.index("-ss") + 1] == "0.500" and args[args.index("-t") + 1] == "1.000",
              str(args))
        check("the cut lands in the library with its length and where it came from",
              r.status_code == 200 and (so.SOUNDS_DIR / d).read_bytes() == b"ID3cut"
              and k["duration"] == 1.0 and k["clip"] == [0.5, 1.5] and k["title"].startswith("Rain"),
              r.text[:160])
        check("and the draft is gone", not list(drafts.glob(Path(d).stem + "*")),
              str(list(drafts.iterdir())))
        r = await c.post(keep, json={})
        check("a draft is kept only once", r.status_code == 409, str(r.status_code))

        r = await c.post("/api/sounds", json={"url": "https://youtu.be/a1", "draft": True})
        d2 = r.json()["name"]
        n = len(ff_calls)
        r = await c.post(f"/api/sounds/drafts/{d2}/keep", json={"start": 0, "end": None})
        check("keeping all of it moves the file as is, not re-encoded",
              r.status_code == 200 and not any("-ss" in a for a in ff_calls[n:])
              and (so.SOUNDS_DIR / d2).read_bytes() == b"ID3fake-audio", r.text[:120])

        r = await c.post("/api/sounds", json={"url": "https://youtu.be/a1", "draft": True})
        d3 = r.json()["name"]
        r = await c.delete(f"/api/sounds/drafts/{d3}")
        check("discard removes the draft", r.status_code == 200 and not list(drafts.iterdir()),
              str(list(drafts.iterdir())))

        # ── Tải mp3 lên → bản nháp ───────────────────────────────────────────
        up = "/api/sounds/upload?name=" + urllib.parse.quote("My clip.v2.mp3")
        r = await c.post(up, content=b"ID3\x04" + b"\0" * 64)
        u = r.json()
        check("an mp3 upload becomes a draft named after the file",
              r.status_code == 200 and (drafts / u["name"]).is_file() and u["title"] == "My clip.v2"
              and u["by"] == "user", r.text[:160])
        lib = (await c.get("/api/sounds")).json()["items"]
        check("an upload is not in the library until kept", all(i["name"] != u["name"] for i in lib),
              str(lib)[:120])
        await c.get(f"/api/sounds/drafts/{u['name']}/peaks")
        r = await c.post(f"/api/sounds/drafts/{u['name']}/keep", json={})
        check("its length comes from the decoded wave, since nobody stated it",
              r.status_code == 200 and r.json()["duration"] == 2.0, r.text[:160])
        r = await c.post(up, content=b"\xff\xfb\x90\x00" + b"\0" * 64)
        check("an mp3 without an ID3 tag is taken too", r.status_code == 200, r.text[:120])
        await c.delete(f"/api/sounds/drafts/{r.json()['name']}")
        for what, body in (("an HTML page", b"<html><script>alert(1)</script>"), ("an empty file", b"")):
            r = await c.post(up, content=body)
            check(f"{what} is refused and nothing stays on disk",
                  r.status_code == 400 and not list(drafts.iterdir()), f"{r.status_code} {r.text[:80]}")
        real_max = so.SOUND_UPLOAD_MAX
        so.SOUND_UPLOAD_MAX = 16
        r = await c.post(up, content=b"ID3" + b"\0" * 64)
        so.SOUND_UPLOAD_MAX = real_max
        check("a file over the size cap is refused and removed",
              r.status_code == 400 and "MB" in r.text and not list(drafts.iterdir()), r.text[:120])

        present["yt-dlp"] = None
        so._ytdlp = real_ytdlp                        # đường thật: phải dừng ở chỗ thiếu binary
        r = await c.post("/api/sounds/search", json={"query": "rain"})
        check("without yt-dlp search says how to install it",
              r.status_code == 409 and "not installed" in r.json()["error"], r.text[:120])
        r = await c.get("/api/sounds")
        check("and the panel knows it is missing", r.json()["status"]["ytdlp"] in ("", None),
              str(r.json()["status"]))
        present["yt-dlp"] = str(YT)
        so._ytdlp = fake_ytdlp

    # ── Nối dây ──────────────────────────────────────────────────────────────
    html = (HERE / "static/orchestrator/index.html").read_text(encoding="utf-8")
    js = (HERE / "static/orchestrator/app.js").read_text(encoding="utf-8")
    for want, where, blob in (('id="snd-modal"', "index.html", html),
                              ('onclick="toolsClose(); sndOpen()"', "app.js", js),
                              ("window.sndOpen", "app.js", js),
                              ('id="snd-len"', "index.html", html),
                              ('id="sed-modal"', "index.html", html),
                              ("draft: true", "app.js", js),
                              ('onclick="libOpen()"', "index.html", html),
                              ('id="lib-drawer"', "index.html", html),
                              ('"/api/sounds/upload?name="', "app.js", js),
                              ('max_minutes: +$("snd-len").value', "app.js", js),
                              ('onclick="sndRecheck()"', "app.js", js)):
        check(f"{where} wires up {want}", want in blob, "not found")


asyncio.run(main())
so.shutil.which = real_which
(so.DB_DIR / "check_sounds.db").unlink(missing_ok=True)
print()
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("all sounds checks passed")
