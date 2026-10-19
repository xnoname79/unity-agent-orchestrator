#!/usr/bin/env python3
"""Guards for text to speech (Kokoro through sherpa-onnx) — the Speak text dialog and voice_mcp.

The quiet ways it goes wrong:

  - the voice model comes off the internet as a tar archive: a member named ../something must
    never land outside the voice folder, and a broken or partial download must leave nothing
    behind that looks installed;
  - two downloads at once (a double click, an agent and a person) must not race on one file;
  - a voice name maps to a speaker NUMBER inside the model; off by one is a different voice and
    nothing fails;
  - samples come back as floats and can overshoot 1.0 — written as 16-bit without clamping, they
    wrap round to a loud click;
  - each generation fills two CPU cores for longer than the speech lasts, so two must never run
    at once;
  - what is spoken lands in the Library like any other sound, findable by the words it says.

Neither sherpa-onnx nor the 350 MB model is used: _tts_generate and _tts_fetch are replaced by
fakes, so this runs offline and on Windows.

    python3 check_tts.py
"""
import asyncio
import io
import json
import logging
import os
import sys
import tarfile
import tempfile
import wave
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["ORCH_DB"] = "check_tts"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402
import voice_mcp                      # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
so.SOUNDS_DIR = Path(tempfile.mkdtemp())
so.TTS_DIR = Path(tempfile.mkdtemp())
fails = []
calls = []
live = {"now": 0, "max": 0}


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("ok   " if ok else "FAIL ") + name + (f" — {detail}" if not ok else ""))


def fake_generate(text, sid, speed):
    """Như sherpa-onnx: list float ở 24 kHz. 1.5 và -1.5 là mẫu vượt biên phải bị kẹp lại."""
    live["now"] += 1
    live["max"] = max(live["max"], live["now"])
    import time
    time.sleep(0.05)
    live["now"] -= 1
    calls.append((text, sid, speed))
    return [0.0, 0.5, -0.5, 1.5, -1.5] * 4800, 24000


def tarball(members):
    """tar.bz2 trong bộ nhớ: members = {tên: bytes}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as t:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


fetched = []


def fake_fetch_of(blob):
    async def fetch(url, dst, progress):
        fetched.append(url)
        await asyncio.sleep(0.05)
        Path(dst).write_bytes(blob)
        progress(len(blob))
    return fetch


async def main():
    so._tts_generate = fake_generate
    pkg = so.TTS_PACKAGE
    good = tarball({f"{pkg}/model.onnx": b"onnx", f"{pkg}/voices.bin": b"v",
                    f"{pkg}/tokens.txt": b"t", f"{pkg}/lexicon-us-en.txt": b"l"})

    # ── Giọng đọc: id → số speaker trong model ───────────────────────────────
    check("voice names map to the model's speaker numbers (read from its id2speaker)",
          so.TTS_VOICES.index("af_heart") == 3 and so.TTS_VOICES.index("bm_lewis") == 27
          and len(so.TTS_VOICES) == 28, str(so.TTS_VOICES[:4]))
    check("voices read as people, not ids", so._tts_label("bf_emma") == "Emma · UK female"
          and so._tts_label("am_adam") == "Adam · US male", so._tts_label("bf_emma"))

    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8992") as c:
        st = (await c.get("/api/tts")).json()
        check("before the download the dialog says the voice is not there",
              st["ready"] is False and st["default"] == "af_heart" and len(st["voices"]) == 28,
              str(st)[:160])
        r = await c.post("/api/tts/speak", json={"text": "hello"})
        check("speaking without the voice says how to get it",
              r.status_code == 409 and "Download" in r.json()["error"] and not calls, r.text[:120])

        # ── Tải giọng ────────────────────────────────────────────────────────
        for label, blob in (("one with a ../ path in it", tarball({f"{pkg}/model.onnx": b"x",
                                                                   "../escaped.txt": b"pwn"})),
                            ("one without the model", tarball({f"{pkg}/tokens.txt": b"t"}))):
            so._tts_fetch = fake_fetch_of(blob)
            await so.tts_download()
            check(f"a bad archive ({label}) fails, visibly, and nothing looks installed",
                  so._tts_dl["state"] == "failed" and so._tts_dl["error"] and not so._tts_ready()
                  and not (so.TTS_DIR / "escaped.txt").exists()   # ../ tính từ .unpacking/
                  and not any(so.TTS_DIR.glob("*.part")) and not (so.TTS_DIR / ".unpacking").exists(),
                  f"{so._tts_dl} {sorted(p.name for p in so.TTS_DIR.iterdir())}")
        so._tts_fetch = fake_fetch_of(good)
        fetched.clear()
        r = await c.post("/api/tts/download", json={})
        check("pressing Download answers 'downloading' at once, so the dialog shows progress",
              r.status_code == 202 and r.json()["download"]["state"] == "downloading", r.text[:120])
        await asyncio.gather(*so._tts_tasks, so.tts_download())
        check("two downloads at once fetch once", fetched == [so.TTS_URL], str(fetched))
        check("a good download installs the voice and cleans up after itself",
              so._tts_ready() and so._tts_dl["state"] == "" and not any(so.TTS_DIR.glob("*.part"))
              and not (so.TTS_DIR / ".unpacking").exists(), f"{so._tts_dl}")

        # ── Đọc ───────────────────────────────────────────────────────────────
        for body, why in (({"text": ""}, "empty text"), ({"text": "x" * 3001}, "too much text"),
                          ({"text": "hi", "voice": "darth_vader"}, "an unknown voice"),
                          ({"text": "hi", "speed": 3}, "a speed out of range"),
                          ({"text": "hi", "speed": "fast"}, "a speed that is not a number")):
            n = len(calls)
            r = await c.post("/api/tts/speak", json=body)
            check(f"{why} is refused before anything is generated",
                  r.status_code == 400 and len(calls) == n, f"{r.status_code} {r.text[:90]}")
        r = await c.post("/api/tts/speak", json={"text": "Could you tell me the way to the "
                                                         "station, please?", "voice": "bf_emma",
                                                 "speed": 0.85})
        s = r.json()
        check("text is spoken with the voice and speed asked for",
              r.status_code == 200 and calls[-1][1:] == (21, 0.85), f"{r.status_code} {r.text[:160]}")
        with wave.open(s["path"]) as w:
            frames = w.readframes(w.getnframes())
            fmt = (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes())
        check("it is a real 16-bit mono WAV at the model's rate", fmt == (1, 2, 24000, 24000), str(fmt))
        first = [int.from_bytes(frames[i:i + 2], "little", signed=True) for i in range(0, 10, 2)]
        check("samples past full scale are clamped, not wrapped into a click",
              first == [0, 16383, -16383, 32767, -32767], str(first))
        check("the library entry says what was said, by whom and how",
              s["text"].startswith("Could you") and s["voice"] == "bf_emma" and s["by"] == "user"
              and s["duration"] == 1.0 and s["license"] == "Kokoro-82M, Apache-2.0"
              and s["channel"] == "Kokoro · Emma · UK female", str(s))
        lib = (await c.get("/api/sounds")).json()["items"]
        check("it shows up in the Library", any(i["name"] == s["name"] for i in lib), str(lib)[:120])
        hits = so.library_search("station")["items"]
        check("and agents find it by the words it says", any(i["name"] == s["name"] for i in hits),
              str(hits)[:160])
        long = "word " * 40 + "\nsecond line"
        r = await c.post("/api/tts/speak", json={"text": long})
        check("a long text gets a short title", len(r.json()["title"]) <= 81
              and r.json()["title"].endswith("…"), r.json()["title"])

        live["max"] = 0
        await asyncio.gather(*(so.tts_speak(f"line {i}") for i in range(3)))
        check("only one generation runs at a time", live["max"] == 1, str(live))

    # ── Agent: voice_mcp ─────────────────────────────────────────────────────
    v = json.loads(await voice_mcp.list_voices())
    check("list_voices says it is ready and offers the voices", v["ready"] and len(v["voices"]) == 28,
          str(v)[:120])
    out = json.loads(await voice_mcp.speak("Nice to meet you.", "am_adam", 1.0, "tutor"))
    check("speak saves to the Library, signed by the agent, with a path to copy",
          out["by"] == "tutor" and Path(out["path"]).is_file() and calls[-1][1] == 11, str(out)[:160])
    msg = await voice_mcp.speak("hi", "nobody")
    check("a bad voice reads as an instruction", msg.startswith("⛔"), msg)
    tools = {t.name for t in await voice_mcp.mcp.list_tools()}
    check("the voice tools are speak and list_voices", tools == {"speak", "list_voices"}, str(tools))

    # Tiếng mới vào thư viện được đo ở nền (ffmpeg): chờ xong, kẻo asyncio.run treo lúc đóng.
    await asyncio.gather(*so._SOUND_BG, return_exceptions=True)

    # ── Nối dây ──────────────────────────────────────────────────────────────
    py = (HERE / "session_orchestrator.py").read_text(encoding="utf-8")
    spec = (HERE / "build.spec").read_text(encoding="utf-8")
    req = (HERE / "requirements.txt").read_text(encoding="utf-8")
    html = (HERE / "static/orchestrator/index.html").read_text(encoding="utf-8")
    js = (HERE / "static/orchestrator/app.js").read_text(encoding="utf-8")
    for want, where, blob in (('Mount("/voice", app=voice_app)', "session_orchestrator.py", py),
                              ('("voice", "/voice/mcp"', "session_orchestrator.py", py),
                              ('"voice_mcp"', "build.spec", spec),
                              ('"sherpa_onnx"', "build.spec", spec),
                              ("sherpa-onnx", "requirements.txt", req),
                              ('id="tts-modal"', "index.html", html),
                              ('onclick="ttsOpen()"', "index.html", html),
                              ("window.ttsSpeak", "app.js", js)):
        check(f"{where} wires up {want}", want in blob, "not found")


asyncio.run(main())
(so.DB_DIR / "check_tts.db").unlink(missing_ok=True)
print()
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("all tts checks passed")
