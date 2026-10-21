#!/usr/bin/env python3
"""Guards for Documents — PDFs in the Library and the reading state the PDF reader keeps for each.

The quiet ways it goes wrong:

  - an upload is a stranger's file on our disk: only PDF, under a size cap checked WHILE it
    streams, and a refused file must leave nothing behind;
  - the reader saves its scroll position every second; that save must never wipe the
    highlights or bookmarks, and one bad field must leave the whole state as it was;
  - the reader reopens at the bookmark added or gone to last — the Library row must agree;
  - files saved when there was only ONE bookmark must come back as a list, not lose it;
  - a quote longer than text to speech takes could be saved but never read aloud;
  - highlight colour, ids and boxes go back into the page as inline style and data attributes,
    so the server only keeps values of the exact shape the reader makes (a colour like
    "red;background:url(…)" is CSS injection);
  - document names come from the URL: only the names we make are served or deleted;
  - pdf.js is an ES module: a wrong MIME type for .mjs/.wasm (Windows reads them from the
    registry) and the reader never opens, with nothing on the server side to show why;
  - pdf.js below 6.2.108 runs JavaScript inside a PDF when scripting is on — the vendored copy
    must stay at or above it, without the scripting sandbox.

    python3 check_docs.py
"""
import asyncio
import json
import logging
import os
import re
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
os.environ["ORCH_DB"] = "check_docs"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
so.DOCS_DIR = Path(tempfile.mkdtemp())
fails = []
PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("ok   " if ok else "FAIL ") + name + (f" — {detail}" if not ok else ""))


def on_disk():
    return sorted(p.name for p in so.DOCS_DIR.iterdir())


HL = {"id": "kx1abc", "page": 1, "text": "Learning English takes patience.",
      "rects": [[1, 71.9, 721.5, 534.6, 735.0]], "color": "#ffd54f", "created": "2026-10-20T08:00:00Z"}
BM1 = {"id": "bm1", "name": "Chapter one", "page": 1, "y1": 605.86, "y2": 593.86,
       "text": "Good habits are built slowly", "created": "2026-10-20T08:00:00Z", "used": "2026-10-20T08:00:00Z"}
BM2 = {"id": "bm2", "name": "Chapter three", "page": 3, "y1": 700.0, "y2": 688.0,
       "text": "She opened the window", "created": "2026-10-20T09:00:00Z", "used": "2026-10-20T09:00:00Z"}


async def main():
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8992") as c:
        # ── Tải lên ──────────────────────────────────────────────────────────
        up = "/api/documents/upload?name=" + urllib.parse.quote("English Reader.v2.pdf")
        r = await c.post(up, content=PDF)
        d = r.json()
        check("a PDF upload lands in Documents, titled after the file",
              r.status_code == 200 and (so.DOCS_DIR / d["name"]).is_file() and d["title"] == "English Reader.v2"
              and d["highlights"] == 0, r.text[:160])
        name = d["name"]
        r = await c.post(up, content=b"\xef\xbb\xbf junk first\n" + PDF)
        check("a PDF with bytes before %PDF- (allowed by the spec) is taken", r.status_code == 200, r.text[:120])
        await c.delete(f"/api/documents/{r.json()['name']}")
        before = on_disk()
        for what, body in (("an HTML page", b"<html><script>alert(1)</script>"), ("an empty file", b""),
                           ("an mp3", b"ID3\x04" + b"\0" * 64)):
            r = await c.post(up, content=body)
            check(f"{what} is refused and nothing stays on disk",
                  r.status_code == 400 and "PDF" in r.text and on_disk() == before, f"{r.status_code} {r.text[:80]}")
        real_max = so.DOC_UPLOAD_MAX
        so.DOC_UPLOAD_MAX = 16
        r = await c.post(up, content=PDF)
        so.DOC_UPLOAD_MAX = real_max
        check("a file over the cap is refused while it streams, nothing left behind",
              r.status_code == 400 and "MB" in r.text and on_disk() == before, f"{r.status_code} {r.text[:80]}")

        r = await c.get(f"/api/documents/{name}")
        check("the reader gets the PDF as a PDF, never sniffed into something else",
              r.status_code == 200 and r.headers["content-type"] == "application/pdf"
              and r.headers.get("x-content-type-options") == "nosniff" and r.content == PDF,
              f"{r.status_code} {r.headers.get('content-type')}")
        (so.DOCS_DIR.parent / "secret.pdf").write_bytes(PDF)
        (so.DOCS_DIR / "secret.pdf").write_bytes(PDF)   # nằm đúng chỗ nhưng tên không do ta đặt
        for bad in ("..%2Fsecret.pdf", "secret.pdf", name[:-4] + ".json", name + "x"):
            got = [(await c.get(f"/api/documents/{bad}")).status_code,
                   (await c.get(f"/api/documents/{bad}/state")).status_code,
                   (await c.post(f"/api/documents/{bad}/state", json={"pages": 3})).status_code,
                   (await c.delete(f"/api/documents/{bad}")).status_code]
            check(f"name {bad!r} is neither served, changed nor deleted",
                  all(s in (404, 405) for s in got) and (so.DOCS_DIR / "secret.pdf").is_file(), str(got))
        (so.DOCS_DIR / "secret.pdf").unlink()

        # ── Trạng thái đọc ───────────────────────────────────────────────────
        st = f"/api/documents/{name}/state"
        r = await c.post(st, json={"pages": 3, "highlights": [HL], "bookmarks": [BM1, BM2]})
        check("highlights and several named bookmarks are saved",
              r.status_code == 200 and r.json()["highlights"] == [HL] and r.json()["bookmarks"] == [BM1, BM2],
              r.text[:200])
        r = await c.post(st, json={"pos": {"page": 2, "top": 400.123, "left": 0}, "scale": "1.5"})
        s = (await c.get(st)).json()
        check("saving the scroll position keeps every highlight and bookmark",
              r.status_code == 200 and s["highlights"] == [HL] and s["bookmarks"] == [BM1, BM2]
              and s["pos"] == {"page": 2, "top": 400.12, "left": 0.0}
              and s["title"] == "English Reader.v2", str(s)[:240])
        lib = (await c.get("/api/documents")).json()
        check("the Library row opens at the bookmark used last, not where you scrolled",
              [(i["page"], i["pages"], i["highlights"], i["bookmarks"]) for i in lib] == [(3, 3, 1, 2)], str(lib))
        renamed = {**BM1, "name": "Start here", "used": "2026-10-20T10:00:00Z"}
        await c.post(st, json={"bookmarks": [renamed, BM2]})
        lib = (await c.get("/api/documents")).json()
        check("rename and Go are saved; going to a bookmark makes it the one that opens",
              (await c.get(st)).json()["bookmarks"][0]["name"] == "Start here" and lib[0]["page"] == 1, str(lib))
        await c.post(st, json={"bookmarks": []})
        lib = (await c.get("/api/documents")).json()
        check("with no bookmarks left it opens where you stopped", (lib[0]["page"], lib[0]["bookmarks"]) == (2, 0),
              str(lib))
        side = so.DOCS_DIR / name.replace(".pdf", ".json")
        old = json.loads(side.read_text(encoding="utf-8"))
        old.pop("bookmarks")
        old["bookmark"] = {"page": 2, "y1": 500.0, "y2": 488.0, "text": "Reading aloud helps you hear the rhythm",
                           "created": "2026-10-19T08:00:00Z"}
        side.write_text(json.dumps(old), encoding="utf-8")
        s = (await c.get(st)).json()
        check("a file saved with the old single bookmark comes back as a one-item list",
              "bookmark" not in s and len(s["bookmarks"]) == 1 and s["bookmarks"][0]["page"] == 2
              and s["bookmarks"][0]["name"] == "Reading aloud helps you hear the", str(s.get("bookmarks"))[:200])
        await c.post(st, json={"pos": {"page": 1, "top": 1, "left": 0}})
        disk = json.loads(side.read_text(encoding="utf-8"))
        check("the next save writes it in the new shape", "bookmark" not in disk and len(disk["bookmarks"]) == 1,
              str(disk)[:200])
        r = await c.post(st, json={"bookmarks": (await c.get(st)).json()["bookmarks"]})
        check("the converted bookmark passes the same checks as new ones", r.status_code == 200, r.text[:120])
        check("any quote fits one text to speech call", so.DOC_TEXT_MAX <= so.TTS_MAX_CHARS,
              f"{so.DOC_TEXT_MAX} > {so.TTS_MAX_CHARS}")

        bad_bodies = (
            ("a page of 0", {"pos": {"page": 0, "top": 1, "left": 0}}),
            ("a page as text", {"pages": "3"}),
            ("a page as true", {"pages": True}),
            # json.loads của Python nhận chữ Infinity (JSON chuẩn thì không) — gửi thô.
            ("an infinite position", '"pos": {"page": 1, "top": Infinity, "left": 0}'),
            ("a zoom that is not a zoom", {"scale": "page-width; x"}),
            ("a colour that smuggles CSS", {"highlights": [{**HL, "color": "red;}*{"}]}),
            ("an id that breaks out of an attribute", {"highlights": [{**HL, "id": '"><img src=x>'}]}),
            ("a box with 4 numbers, no page", {"highlights": [{**HL, "rects": [[71.9, 721.5, 534.6, 735.0]]}]}),
            ("a highlight with no box", {"highlights": [{**HL, "rects": []}]}),
            ("a quote too long", {"highlights": [{**HL, "text": "x" * (so.DOC_TEXT_MAX + 1)}]}),
            ("a bookmark name too long", {"bookmarks": [{**BM1, "name": "x" * 121}]}),
            ("a bookmark id that breaks out of an attribute", {"bookmarks": [{**BM1, "id": '"><b>'}]}),
            ("a bookmark with no line", {"bookmarks": [{"id": "b", "page": 1, "text": "x"}]}),
            ("too many bookmarks", {"bookmarks": [BM1] * (so.DOC_MAX_BOOKMARKS + 1)}),
            ("an object where the list belongs", {"bookmarks": {"page": 1}}),
        )
        good = (await c.get(st)).json()
        for why, body in bad_bodies:
            # Một trường đúng đi kèm: sai một là không ghi gì cả.
            if isinstance(body, str):
                r = await c.post(st, content='{"pages": 99, ' + body + "}",
                                 headers={"content-type": "application/json"})
            else:
                r = await c.post(st, json={"pages": 99, **body})
            check(f"{why} is refused and nothing is written",
                  r.status_code == 400 and (await c.get(st)).json() == good, f"{r.status_code} {r.text[:100]}")
        r = await c.post(st, json={"title": "renamed", "evil": 1, "pages": 4})
        s = (await c.get(st)).json()
        check("keys the reader does not own are ignored", r.status_code == 200 and s["title"] == "English Reader.v2"
              and "evil" not in s and s["pages"] == 4, str(s)[:160])
        check("saves go through a temp file and leave none behind",
              not [n for n in on_disk() if n.endswith(".tmp")], str(on_disk()))
        kept = (await c.get(st)).json()
        r = await c.post(st, content=b"not json", headers={"content-type": "application/json"})
        check("a body that is not JSON changes nothing", (await c.get(st)).json() == kept,
              f"{r.status_code} {r.text[:80]}")

        r = await c.delete(f"/api/documents/{name}")
        check("delete removes the PDF and its reading state", r.status_code == 200 and on_disk() == [],
              str(on_disk()))

        # ── pdf.js được phục vụ đúng kiểu ─────────────────────────────────────
        for path, want in (("/vendor/pdfjs/build/pdf.min.mjs", "text/javascript"),
                           ("/vendor/pdfjs/build/pdf.worker.min.mjs", "text/javascript"),
                           ("/vendor/pdfjs/web/pdf_viewer.mjs", "text/javascript"),
                           ("/vendor/pdfjs/wasm/openjpeg.wasm", "application/wasm")):
            r = await c.get(path)
            check(f"{path.rsplit('/', 1)[1]} is served as {want}",
                  r.status_code == 200 and r.headers["content-type"].startswith(want),
                  f"{r.status_code} {r.headers.get('content-type')}")

    # ── Bản pdf.js vendor ────────────────────────────────────────────────────
    vend = HERE / "static/orchestrator/vendor/pdfjs"
    viewer = (vend / "web/pdf_viewer.mjs").read_text(encoding="utf-8")
    m = re.search(r'viewerVersion = "(\d+)\.(\d+)\.(\d+)"', viewer)
    ver = tuple(map(int, m.groups())) if m else (0,)
    check("vendored pdf.js is 6.2.108 or newer (PDF scripts fixed)", ver >= (6, 2, 108), str(ver))
    check("the library and viewer are the same version (the viewer refuses otherwise)",
          m and f'"{".".join(m.groups())}"' in (vend / "build/pdf.min.mjs").read_text(encoding="utf-8"), "")
    check("no scripting sandbox is vendored", not list(vend.rglob("*sandbox*")) and not list(vend.rglob("quickjs*")),
          str(list(vend.rglob("*sandbox*"))))

    # ── Nối dây ──────────────────────────────────────────────────────────────
    html = (HERE / "static/orchestrator/index.html").read_text(encoding="utf-8")
    js = (HERE / "static/orchestrator/app.js").read_text(encoding="utf-8")
    for want, where, blob in (('id="pdf-reader"', "index.html", html),
                              ('onclick="docPick()"', "index.html", html),
                              (".pdfViewer, .pdfViewer * { box-sizing: content-box; }", "index.html", html),
                              ('"/api/documents/upload?name="', "app.js", js),
                              ("annotationMode: lib.AnnotationMode.ENABLE", "app.js", js)):
        check(f"{where} wires up {want}", want in blob, "not found")
    check("the reader never hands PDFs a scripting manager", "scriptingManager:" not in js, "")


asyncio.run(main())
(so.DB_DIR / "check_docs.db").unlink(missing_ok=True)
print()
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("all document checks passed")
