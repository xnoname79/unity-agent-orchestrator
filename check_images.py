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
    orchestrator's default mode is bypass.

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
agy = {"cid": CID, "draw": True, "said": "done", "cmd": []}


async def fake_agy(cmd, cwd, session_id="", on_event=None):
    """Bày file đúng như agy 1.2.14: ảnh ở brain/<cid>/, kèm log và ghi chú KHÔNG phải ảnh."""
    agy["cmd"] = cmd
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

        r = await c.delete(f"/api/images/{name}")
        check("deleting takes the image and its prompt",
              r.status_code == 200 and not any(so.IMAGES_DIR.iterdir()),
              f"{r.status_code} {list(so.IMAGES_DIR.iterdir())}")


asyncio.run(main())
db.unlink(missing_ok=True)
if fails:
    print("\n" + "\n".join(fails))
    sys.exit(1)
print("\nall image guards pass")
