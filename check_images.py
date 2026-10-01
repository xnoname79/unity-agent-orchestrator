#!/usr/bin/env python3
"""Guards for the Images section — pictures drawn by agy's generate_image tool.

The tool cannot be told where to save, so the orchestrator picks the file up from agy's own
data folder once the run ends. Every way that goes wrong is a quiet one:

  - the conversation id comes from agy's output and becomes a path that is then deleted. An id
    that is not a real one ('..') must never reach rmtree;
  - an image name comes from the URL. Anything but a flat file name inside the images folder
    must be refused, or the endpoint serves — and deletes — files elsewhere on disk;
  - every drawing leaves an agy conversation behind. Left there, they bury the user's real
    sessions in the terminal card's session picker;
  - the drawing run needs no permissions, so it must not get them — even though the
    orchestrator's default mode is bypass. Picking a model to rewrite the prompt must not change
    that, and a "model" that is really a flag must never reach argv;
  - reference images go to agy as absolute paths. They may only come from the gallery;
  - an upload is judged by its bytes, not by its name, and is read with a ceiling.

agy itself is not called: _agy_exec is replaced by a fake that lays files out the way
agy 1.2.14 does.

    python3 check_images.py
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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# DB, workspace và thư mục agy riêng: check này tạo/xoá file, KHÔNG được đụng dữ liệu thật.
os.environ["ORCH_DB"] = "check_images"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()
os.environ["ORCH_AGY_HOME"] = tempfile.mkdtemp()

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)

so.IMAGES_DIR = Path(tempfile.mkdtemp()) / "images"
db = so.DB_DIR / "check_images.db"
db.unlink(missing_ok=True)            # chạy lại phải cho cùng kết quả
so._ensure_db()

CID = "0b7e6a52-1c3d-4e5f-8a9b-0c1d2e3f4a5b"
JPEG = b"\xff\xd8\xff\xe0 not really a jpeg"
PNG = b"\x89PNG\r\n\x1a\n not really a png"
agy = {"cid": CID, "draw": True, "said": "done", "cmd": [], "drawn": "a red apple"}


async def fake_agy(cmd, cwd, session_id="", on_event=None):
    """Bày file đúng như agy 1.2.14: ảnh ở brain/<cid>/, kèm log và ghi chú KHÔNG phải ảnh. Phát
    event tool_use như _iter_agy_events — đó là chỗ duy nhất biết Prompt thật đã vẽ."""
    agy["cmd"] = cmd
    if on_event:
        await on_event("tool_use", "generate_image(...)",
                       {"name": "generate_image", "input": {"Prompt": agy["drawn"]}})
    brain = so.AGY_HOME / "brain" / agy["cid"]
    (brain / ".system_generated" / "logs").mkdir(parents=True, exist_ok=True)
    (brain / ".system_generated" / "logs" / "transcript.jsonl").write_text("{}")
    (brain / "task.md").write_text("not an image")
    if agy["draw"]:
        (brain / "red_apple_1790782326134.jpg").write_bytes(JPEG)
    so.AGY_CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    (so.AGY_CONVERSATIONS_DIR / f"{agy['cid']}.db").write_bytes(b"")
    return {"ok": True, "result": agy["said"], "session_id": agy["cid"], "tokens": 1,
            "raw": {"engine": "agy", "conversation_id": agy["cid"]}}


so._agy_exec = fake_agy
fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


async def main():
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        r = await c.post("/api/images", json={"prompt": "a red apple"})
        made = r.json() if r.status_code == 200 else []
        check("a drawing comes back as one image", len(made) == 1, f"{r.status_code} {r.text[:120]}")
        name = made[0]["name"] if made else "missing.jpg"
        check("the run does not bypass permissions",
              "--dangerously-skip-permissions" not in agy["cmd"], str(agy["cmd"][:8]))
        check("the user's words reach agy", "a red apple" in agy["cmd"][-1], agy["cmd"][-1][-60:])

        files = sorted(p.name for p in so.IMAGES_DIR.iterdir())
        check("only the image is picked up, with its prompt beside it",
              files == sorted([name, Path(name).with_suffix(".json").name]), str(files))
        listed = (await c.get("/api/images")).json()
        check("the gallery lists it with its prompt",
              [i["prompt"] for i in listed] == ["a red apple"], str(listed))
        r = await c.get(f"/api/images/{name}")
        check("the image is served as what it is",
              r.status_code == 200 and r.content == JPEG
              and r.headers["content-type"] == "image/jpeg", f"{r.status_code} {r.headers}")

        check("agy's scratch conversation is gone",
              not (so.AGY_CONVERSATIONS_DIR / f"{CID}.db").exists())
        check("and so is its brain folder", not (so.AGY_HOME / "brain" / CID).exists())

        # Agent từ chối vẽ: lời nó nói phải tới được người dùng, không chỉ "lỗi".
        agy.update(cid="5f1c0e2a-9b8d-4c7e-a6f5-4e3d2c1b0a99", draw=False,
                   said="I can't draw that.")
        r = await c.post("/api/images", json={"prompt": "something refused"})
        check("a refusal is an error that says why",
              r.status_code == 502 and "I can't draw that." in r.json().get("error", ""),
              f"{r.status_code} {r.text[:120]}")

        # Id không phải UUID → không bao giờ được ghép thành đường dẫn để xoá.
        keep = so.AGY_HOME / "keep.txt"
        keep.write_text("agy's own data")
        agy.update(cid="..", draw=True, said="done")
        r = await c.post("/api/images", json={"prompt": "an evil id"})
        check("a conversation id that is not a UUID deletes nothing",
              keep.exists() and so.AGY_HOME.exists() and r.status_code == 502,
              f"{r.status_code} keep={keep.exists()}")
        agy.update(cid=CID)

        for bad in ("", "x" * (so.IMAGE_PROMPT_MAX + 1)):
            r = await c.post("/api/images", json={"prompt": bad})
            check(f"a prompt of {len(bad)} characters is refused", r.status_code == 400,
                  str(r.status_code))

        for bad in ("../x.jpg", "a/b.jpg", ".jpg", Path(name).with_suffix(".json").name,
                    "evil.jpg.exe"):
            check(f"'{bad}' is not an image name", so.image_path(bad) is None)
        r = await c.get(f"/api/images/{Path(name).with_suffix('.json').name}")
        check("the prompt file is not served as an image", r.status_code == 404,
              str(r.status_code))

        # ── hình dạng, ảnh mẫu, model viết lại ─────────────────────────────────
        ref = so.IMAGES_DIR / name
        agy.update(drawn="A glossy red apple, studio light, 35mm")
        r = await c.post("/api/images", json={"prompt": "an apple, wide", "ratio": "16:9",
                                              "refs": [name], "writer": "gemini-3.8-flash-low"})
        got = r.json()[0] if r.status_code == 200 else {}
        said = agy["cmd"][-1]
        check("the shape reaches the tool", '"16:9"' in said, said[:160])
        check("a reference reaches the tool as an absolute path",
              str(ref.resolve()) in said or str(ref) in said, said[:200])
        check("a chosen model rewrites, with --model and still no bypass",
              agy["cmd"][agy["cmd"].index("--model") + 1] == "gemini-3.8-flash-low"
              and "--dangerously-skip-permissions" not in agy["cmd"] and "Rewrite" in said,
              str(agy["cmd"][:10]))
        check("the gallery keeps both the user's words and what was drawn",
              got.get("prompt") == "an apple, wide" and got.get("drawn") == agy["drawn"]
              and got.get("ratio") == "16:9", str(got))
        r = await c.post("/api/images", json={"prompt": "exact words"})
        check("no model chosen = the user's words, unchanged",
              "unchanged" in agy["cmd"][-1] and "--model" not in agy["cmd"]
              and "drawn" not in r.json()[0], agy["cmd"][-1][:120])

        for bad in ({"ratio": "5:4"}, {"writer": "--dangerously-skip-permissions"},
                    {"refs": ["../check_images.db"]}, {"refs": "not-a-list"},
                    {"refs": [name] * (so.IMAGE_REFS_MAX + 1)}):
            r = await c.post("/api/images", json={"prompt": "x", **bad})
            check(f"{bad} is refused", r.status_code == 400, f"{r.status_code} {r.text[:80]}")

        # ── tải ảnh lên ─────────────────────────────────────────────────────────
        r = await c.post("/api/images/upload?name=cat.png", content=PNG)
        up = r.json() if r.status_code == 200 else {}
        check("an upload lands in the gallery, judged by its bytes",
              up.get("name", "").endswith(".png") and up.get("uploaded") == "cat.png",
              f"{r.status_code} {r.text[:120]}")
        r = await c.post("/api/images/upload?name=evil.png", content=b"<script>alert(1)</script>")
        check("a file that is not an image is refused, whatever its name", r.status_code == 400,
              str(r.status_code))
        so.IMAGE_UPLOAD_MAX = 64
        r = await c.post("/api/images/upload?name=big.png", content=PNG + b"x" * 100)
        check("an upload over the ceiling is refused", r.status_code == 413, str(r.status_code))

        for i in (await c.get("/api/images")).json():
            await c.delete(f"/api/images/{i['name']}")
        check("deleting takes every image and its prompt", not any(so.IMAGES_DIR.iterdir()),
              str(list(so.IMAGES_DIR.iterdir())))


asyncio.run(main())
db.unlink(missing_ok=True)
if fails:
    print("\n" + "\n".join(fails))
    sys.exit(1)
print("\nall image guards pass")
