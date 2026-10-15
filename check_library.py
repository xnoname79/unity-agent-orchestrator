#!/usr/bin/env python3
"""Guards for the Library MCP tools — search_media and inspect_media, read-only.

An agent picks media through these with no ears and no eyes, so every number has to be the right
number, and every way it goes wrong is quiet:

  - the measurements must say where a sound sits in the file: a silence-then-sound file must come
    back with the quiet stretch first and the loud one after, at the right seconds;
  - they are saved beside the file and reused — measured once, not on every call — and measured
    again when the file changes, or a search hands out numbers for a file that is not there;
  - a sound deleted while it is measured must not leave an orphan .json behind;
  - a sound kept from the editor is measured on its own, so search has numbers without asking;
  - image sizes come from the file header (no Pillow), for PNG, JPEG and WebP;
  - a name the library does not list is refused, never turned into a path;
  - the tools stay read-only: nothing here adds, changes or deletes media.

ffmpeg is not run: _ffmpeg is replaced by a fake that answers the way ffmpeg does, so this runs
offline and on Windows.

    python3 check_library.py
"""
import asyncio
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["ORCH_DB"] = "check_library"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()

import session_orchestrator as so     # noqa: E402
import library_mcp                    # noqa: E402
from mcp.server.fastmcp import Image  # noqa: E402

so.SOUNDS_DIR = Path(tempfile.mkdtemp())
so.IMAGES_DIR = Path(tempfile.mkdtemp())
fails = []
ff_calls = []
ff = {"fail": "", "delete": None}


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("ok   " if ok else "FAIL ") + name + (f" — {detail}" if not ok else ""))


async def fake_ffmpeg(args, timeout):
    """PCM: 1 giây im lặng rồi 1 giây ở nửa biên độ. astats: RMS từng giây, theo dải lọc."""
    ff_calls.append(list(args))
    if ff["delete"]:   # người dùng bấm Delete đúng lúc đang đo: mất cả file lẫn .json
        ff["delete"].unlink()
        ff["delete"].with_suffix(".json").unlink()
        ff["delete"] = None
    if "-af" in args:
        af = args[args.index("-af") + 1]
        if ff["fail"] == "astats":
            return 1, b"", "No such filter: 'astats'"
        # giây 0 im lặng; giây 1 có tiếng; giây 2 gần im (-60) nhưng toàn tiếng rít — phải bị bỏ,
        # không thì nó kéo độ sáng của cả bài lên.
        top, hiss = ((-30, -58) if "highpass=f=3000" in af else (-20, -55) if "highpass=f=1500" in af
                     else (-10, -60))
        return 0, "".join(f"frame:{i}\nlavfi.astats.Overall.RMS_level={v}\n"
                          for i, v in enumerate(("-inf", top, hiss))).encode(), ""
    if ff["fail"] == "all":
        return 1, b"", "Invalid data found when processing input"
    if "mjpeg" in args:
        return 0, b"\xff\xd8\xffsmall", ""
    return 0, b"\0\0" * 8000 + (16384).to_bytes(2, "little", signed=True) * 8000, ""


def sound(name, **meta):
    (so.SOUNDS_DIR / name).write_bytes(b"ID3audio")
    (so.SOUNDS_DIR / name).with_suffix(".json").write_text(json.dumps(meta), encoding="utf-8")


def image(name, data, prompt):
    (so.IMAGES_DIR / name).write_bytes(data)
    (so.IMAGES_DIR / name).with_suffix(".json").write_text(json.dumps({"prompt": prompt}),
                                                           encoding="utf-8")


PNG = b"\x89PNG\r\n\x1a\n\0\0\0\x0dIHDR" + struct.pack(">II", 640, 360) + b"\x08\x02\0\0\0"
JPEG = (b"\xff\xd8" + b"\xff\xe0\x00\x10" + b"JFIF\0" + b"\0" * 9      # APP0
        + b"\xff\xc4\x00\x05" + b"\0\0\0"                              # DHT: cùng dải số, không phải SOF
        + b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", 600, 800) + b"\x03" + b"\0" * 9)
WEBP_X = b"RIFF\0\0\0\0WEBPVP8X\x0a\0\0\0\0\0\0\0" + (1023).to_bytes(3, "little") + (767).to_bytes(3, "little")
WEBP_L = b"RIFF\0\0\0\0WEBPVP8L\0\0\0\0\x2f" + ((300 - 1) | ((200 - 1) << 14)).to_bytes(4, "little")

sound("20261001-100000-aaaaaa.mp3", title="Heavy rain on a tin roof", channel="Ambience Lab",
      duration=120, license="cc-by", format="mp3")
sound("20261002-100000-bbbbbb.mp3", title="Sword clash", channel="SFX Forge", duration=2,
      format="mp3")
image("20261003-100000-cccccc.png", PNG, "a castle at dusk")
image("20261004-100000-dddddd.jpg", JPEG, "a sword stuck in a stone")
image("20261005-100000-eeeeee.webp", WEBP_X, "")
image("20261006-100000-ffffff.webp", WEBP_L, "")


async def search(**kw):
    out = await library_mcp.search_media(**kw)
    return out if out.startswith("⛔") else json.loads(out)


async def main():
    so._ffmpeg = fake_ffmpeg

    # ── search_media ─────────────────────────────────────────────────────────
    r = await search()
    check("an empty search lists everything, newest first",
          [i["name"][:8] for i in r["items"]] == ["20261006", "20261005", "20261004", "20261003",
                                                   "20261002", "20261001"], str(r)[:200])
    r = await search(query="sword")
    check("a word finds sounds and images alike",
          {i["kind"] for i in r["items"]} == {"sound", "image"} and r["total"] == 2, str(r)[:200])
    r = await search(query="rain roof clash")
    check("more matching words rank higher", r["items"][0]["title"].startswith("Heavy rain"),
          str(r)[:200])
    r = await search(kind="image")
    check("kind narrows to images", r["total"] == 4 and all(i["kind"] == "image" for i in r["items"]),
          str(r)[:200])
    r = await search(max_seconds=10)
    check("a length filter keeps only sounds that fit",
          [i["title"] for i in r["items"]] == ["Sword clash"], str(r)[:200])
    r = await search(kind="video")
    check("an unknown kind reads as an instruction", isinstance(r, str) and r.startswith("⛔"), r)
    sizes = {i["name"][:8]: (i["width"], i["height"]) for i in (await search(kind="image"))["items"]}
    check("image sizes come from the header: PNG, JPEG (past a DHT), WebP VP8X and VP8L",
          sizes == {"20261003": (640, 360), "20261004": (800, 600), "20261005": (1024, 768),
                    "20261006": (300, 200)}, str(sizes))
    rain = (await search(query="rain"))["items"][0]
    check("every item carries the path to copy from", Path(rain["path"]).is_file()
          and rain["inspected"] is False, str(rain))

    # ── inspect_media: tiếng ─────────────────────────────────────────────────
    n = len(ff_calls)
    r = json.loads(await library_mcp.inspect_media(rain["name"]))
    ins = r["inspect"]
    check("the sound is measured: silence first, sound after, at the right seconds",
          ins["seconds"] == 2.0 and ins["level"] == "0" * 30 + "8" * 30
          and ins["quiet"] == [[0.0, 1.0]] and ins["loud"] == [[1.0, 2.0]], str(ins))
    check("peak and average loudness are right",
          ins["peak_dbfs"] == -6.0 and ins["rms_dbfs"] == -9.0, str(ins))
    check("brightness counts only the seconds with sound",
          ins["highs"] == {"above_1_5k": {"median": -10.0, "p95": -10.0},
                           "above_3k": {"median": -20.0, "p95": -20.0}, "brightest_at": 1},
          str(ins.get("highs")))
    check("bookkeeping stays out of what the agent reads", "v" not in ins and "mtime" not in ins,
          str(ins))
    side = json.loads((so.SOUNDS_DIR / rain["name"]).with_suffix(".json").read_text("utf-8"))
    check("the measurements are saved beside the file", side.get("inspect", {}).get("level")
          == ins["level"] and side["title"].startswith("Heavy rain"), str(side)[:160])
    again = (await search(query="rain"))["items"][0]
    check("search returns them from then on", again["inspected"] and again["inspect"] == ins,
          str(again)[:200])
    n = len(ff_calls)
    await library_mcp.inspect_media(rain["name"])
    check("a second look is not measured again", len(ff_calls) == n, str(ff_calls[n:]))
    p = so.SOUNDS_DIR / rain["name"]
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    await library_mcp.inspect_media(rain["name"])
    check("a changed file is measured again", len(ff_calls) > n, "no ffmpeg call")

    ff["fail"] = "astats"
    r = json.loads(await library_mcp.inspect_media("20261002-100000-bbbbbb.mp3"))
    ff["fail"] = ""
    check("without the brightness filters the rest still comes back",
          r["inspect"]["highs"] is None and r["inspect"]["seconds"] == 2.0, str(r)[:200])
    ff["fail"] = "all"
    sound("20261007-100000-a1b2c3.mp3", title="Broken")
    msg = await library_mcp.inspect_media("20261007-100000-a1b2c3.mp3")
    ff["fail"] = ""
    check("a file ffmpeg cannot read reads as an instruction",
          msg.startswith("⛔") and "ffmpeg could not read" in msg, msg)

    ff["delete"] = so.SOUNDS_DIR / "20261007-100000-a1b2c3.mp3"
    await so.sound_inspect("20261007-100000-a1b2c3.mp3", refresh=True)
    check("a sound deleted while measured leaves no .json behind",
          not (so.SOUNDS_DIR / "20261007-100000-a1b2c3.json").exists(), "orphan .json")

    for bad in ("../../etc/passwd", "nope.mp3", ""):
        msg = await library_mcp.inspect_media(bad)
        check(f"name {bad!r} is refused", msg.startswith("⛔"), msg)

    # ── inspect_media: ảnh ───────────────────────────────────────────────────
    out = await library_mcp.inspect_media("20261003-100000-cccccc.png")
    meta = json.loads(out[0])
    check("an image comes back as the picture, with its prompt and size",
          isinstance(out[1], Image) and meta["prompt"] == "a castle at dusk"
          and (meta["width"], meta["height"]) == (640, 360), str(out)[:200])
    real_max, library_mcp.SHOW_MAX = library_mcp.SHOW_MAX, 10
    out = await library_mcp.inspect_media("20261003-100000-cccccc.png")
    library_mcp.SHOW_MAX = real_max
    check("a big image is shrunk with ffmpeg before it is shown",
          isinstance(out[1], Image) and out[1].data == b"\xff\xd8\xffsmall"
          and "mjpeg" in ff_calls[-1], str(out)[:200])

    # ── Giữ từ editor → tự đo ────────────────────────────────────────────────
    drafts = so.SOUNDS_DIR / "drafts"
    drafts.mkdir(exist_ok=True)
    (drafts / "20261008-100000-d4e5f6.mp3").write_bytes(b"ID3audio")
    (drafts / "20261008-100000-d4e5f6.json").write_text('{"title": "Kept"}', encoding="utf-8")
    await so.sound_keep("20261008-100000-d4e5f6.mp3")
    await asyncio.gather(*so._SOUND_BG)
    side = json.loads((so.SOUNDS_DIR / "20261008-100000-d4e5f6.json").read_text("utf-8"))
    check("a sound kept from the editor is measured on its own",
          side.get("inspect", {}).get("seconds") == 2.0 and side.get("duration") == 2.0,
          str(side)[:160])

    # ── Chỉ đọc, và đã nối dây ───────────────────────────────────────────────
    tools = {t.name for t in await library_mcp.mcp.list_tools()}
    check("the tools are search_media and inspect_media, nothing that writes",
          tools == {"search_media", "inspect_media"}, str(tools))
    py = (HERE / "session_orchestrator.py").read_text(encoding="utf-8")
    spec = (HERE / "build.spec").read_text(encoding="utf-8")
    js = (HERE / "static/orchestrator/app.js").read_text(encoding="utf-8")
    for want, where, blob in (('Mount("/library", app=library_app)', "session_orchestrator.py", py),
                              ('"library_mcp"', "build.spec", spec),
                              ('url: location.origin + "/library/mcp"', "app.js", js)):
        check(f"{where} wires up {want}", want in blob, "not found")


asyncio.run(main())
(so.DB_DIR / "check_library.db").unlink(missing_ok=True)
print()
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("all library checks passed")
