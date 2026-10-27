#!/usr/bin/env python3
"""Guards for reader_server.py — the PDF reader and Speak text hosted for several people.

It sits on the open internet, so the quiet ways it goes wrong are about who can do what:

  - nothing of the orchestrator's agent side may be reachable: no sessions, terminals, /v1, MCP,
    image generation or voice download — only documents, speech and Explain, and only when signed in;
  - one person must never see, change or delete another's documents or speech, whatever name
    they put in the URL;
  - passwords are only ever stored as scrypt hashes, guessing is capped, a wrong name and a
    wrong password look the same, and the session cookie cannot be read by page scripts;
  - another website must not be able to post as a signed-in person (CSRF), and a request for a
    domain the site does not serve is refused;
  - storage is capped per person, and one person cannot queue up speech or explanations for
    everyone else.

The voice is faked (_tts_generate), so this runs offline and without the 350 MB model.

    python3 check_reader.py
"""
import asyncio
import json
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
DATA = tempfile.mkdtemp()
os.environ.update(READER_DATA=DATA, READER_QUOTA_MB="1", READER_HOSTS="reader.example.com,127.0.0.1",
                  ORCH_DB="check_reader")

import httpx                          # noqa: E402
import reader_server as rs            # noqa: E402
import session_orchestrator as so     # noqa: E402

PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
BASE = "https://reader.example.com"
fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("ok   " if ok else "FAIL ") + name + (f" — {detail}" if not ok else ""))


def fake_generate(text, sid, speed):
    import time
    time.sleep(0.2)
    return [0.0, 0.1] * 2400, 24000


async def main():
    for name in ("alice", "bob"):
        rs._q("INSERT INTO users (name, pw, created) VALUES (?, ?, 0)", (name, rs.hash_pw(name + "-password")))
    stored = rs._q("SELECT pw FROM users WHERE name = 'alice'", one=True)["pw"]
    check("passwords are stored as scrypt hashes, salted", stored.startswith("scrypt$") and "alice-password" not in stored
          and stored != rs._q("SELECT pw FROM users WHERE name = 'bob'", one=True)["pw"], stored[:40])

    (Path(rs.so.TTS_DIR) / so.TTS_PACKAGE).mkdir(parents=True, exist_ok=True)
    (Path(rs.so.TTS_DIR) / so.TTS_PACKAGE / "model.onnx").write_bytes(b"x")
    so._tts_generate = fake_generate
    app = rs.build_app()

    def client(**kw):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, **kw)

    async def sign_in(c, name, pw):
        return await c.post("/api/login", json={"name": name, "password": pw}, headers={"origin": BASE})

    async with client() as anon, client() as alice, client() as bob:
        # ── Trang + những gì KHÔNG có ở đây ──────────────────────────────────
        r = await anon.get("/")
        check("the page is the reader page, ready for Add to Home Screen",
              r.status_code == 200 and 'data-mode="reader"' in r.text and "/manifest.webmanifest" in r.text
              and "apple-mobile-web-app-capable" in r.text, r.text[:120])
        check("every response carries the safety headers", r.headers.get("x-frame-options") == "DENY"
              and r.headers.get("x-content-type-options") == "nosniff", str(dict(r.headers))[:200])
        for path in ("/api/documents", "/api/tts", "/api/me", "/api/explain"):
            r = await anon.get(path)
            check(f"{path} needs a session", r.status_code == 401, f"{r.status_code} {r.text[:80]}")
        r = await anon.post("/api/explain", json={"doc": "x", "id": "q1"}, headers={"origin": BASE})
        check("/api/explain needs a session", r.status_code == 401, f"{r.status_code} {r.text[:80]}")

        await sign_in(alice, "alice", "alice-password")
        for method, path in (("GET", "/api/sessions"), ("POST", "/api/spawn"), ("GET", "/api/workspaces"),
                             ("POST", "/v1/chat/completions"), ("POST", "/api/images"), ("GET", "/api/sounds"),
                             ("POST", "/api/tts/download"), ("POST", "/signal/mcp"), ("POST", "/library/mcp"),
                             ("POST", "/api/mcp/connect"), ("GET", "/docs")):
            r = await alice.request(method, path, headers={"origin": BASE})
            check(f"{method} {path} does not exist here, even signed in", r.status_code in (404, 405),
                  f"{r.status_code} {r.text[:80]}")
        r = await anon.get("/vendor/pdfjs/build/pdf.min.mjs")
        check("pdf.js is served as a module", r.headers.get("content-type", "").startswith("text/javascript"),
              r.headers.get("content-type"))

        # ── Đăng nhập ────────────────────────────────────────────────────────
        async with client() as c:
            wrong_pw = await sign_in(c, "alice", "nope-nope-nope")
            no_user = await sign_in(c, "mallory", "nope-nope-nope")
        check("a wrong password and an unknown name get the same answer",
              wrong_pw.status_code == no_user.status_code == 401 and wrong_pw.json() == no_user.json(),
              f"{wrong_pw.text} / {no_user.text}")
        async with client() as c:
            r = await sign_in(c, "bob", "bob-password")
            cookie = r.headers.get("set-cookie", "")
        check("the session cookie is HttpOnly, Secure and SameSite", r.status_code == 200 and "HttpOnly" in cookie
              and "Secure" in cookie and "samesite=lax" in cookie.lower(), cookie)
        stored_tokens = [t["token"] for t in rs._q("SELECT token FROM sessions")]
        raw = cookie.split("=", 1)[1].split(";", 1)[0]
        check("the database keeps only a hash of each session token", raw not in stored_tokens
              and rs._token_key(raw) in stored_tokens, "")
        r = await anon.post("/api/login", json={"name": "alice", "password": "alice-password"},
                            headers={"origin": "https://evil.example"})
        check("another website cannot sign someone in (login CSRF)", r.status_code == 403, f"{r.status_code}")
        r = await anon.get("/", headers={"host": "evil.example"})
        check("a request for a domain this site does not serve is refused", r.status_code == 400, f"{r.status_code}")

        rs._fails.clear()
        async with client() as c:
            codes = [(await sign_in(c, "alice", f"guess-{i}-xxxx")).status_code for i in range(11)]
            last = await sign_in(c, "alice", "alice-password")
        check("ten wrong guesses lock the name for a while, even with the right password after",
              codes[:10] == [401] * 10 and codes[10] == 429 and last.status_code == 429, str(codes + [last.status_code]))
        rs._fails.clear()

        # ── Tài liệu của ai người nấy ────────────────────────────────────────
        await sign_in(bob, "bob", "bob-password")
        r = await alice.post("/api/documents/upload?name=alice.pdf", content=PDF, headers={"origin": BASE})
        a_doc = r.json()["name"]
        r = await bob.post("/api/documents/upload?name=bob.pdf", content=PDF, headers={"origin": BASE})
        b_doc = r.json()["name"]
        a_list = [d["title"] for d in (await alice.get("/api/documents")).json()]
        b_list = [d["title"] for d in (await bob.get("/api/documents")).json()]
        check("each person sees only their own documents", a_list == ["alice"] and b_list == ["bob"], f"{a_list} {b_list}")
        got = [(await bob.get(f"/api/documents/{a_doc}")).status_code,
               (await bob.get(f"/api/documents/{a_doc}/state")).status_code,
               (await bob.post(f"/api/documents/{a_doc}/state", json={"pages": 2}, headers={"origin": BASE})).status_code,
               (await bob.delete(f"/api/documents/{a_doc}", headers={"origin": BASE})).status_code,
               (await bob.get(f"/api/documents/..%2F..%2F1%2Fdocuments%2F{a_doc}")).status_code]
        check("bob cannot read, change or delete alice's document, by name or by path",
              all(s == 404 for s in got) and (await alice.get(f"/api/documents/{a_doc}")).status_code == 200, str(got))
        r = await alice.post(f"/api/documents/{a_doc}/state", json={"pages": 3},
                             headers={"origin": "https://evil.example"})
        check("another website cannot change a document as alice (CSRF)", r.status_code == 403, f"{r.status_code}")

        big = PDF + b"\0" * (600 * 1024)
        r1 = await alice.post("/api/documents/upload?name=a.pdf", content=big, headers={"origin": BASE})
        r2 = await alice.post("/api/documents/upload?name=b.pdf", content=big, headers={"origin": BASE})
        me = (await alice.get("/api/me")).json()
        check("storage is capped per person, and the refusal says so",
              r1.status_code == 200 and r2.status_code == 400 and "space" in r2.text and me["used"] <= me["quota"],
              f"{r1.status_code} {r2.status_code} {r2.text[:80]} {me}")
        r = await bob.post("/api/documents/upload?name=c.pdf", content=big, headers={"origin": BASE})
        check("one person's full storage does not stop another", r.status_code == 200, f"{r.status_code} {r.text[:80]}")

        # ── Tiếng đọc ────────────────────────────────────────────────────────
        r = await alice.post("/api/tts/speak", json={"text": "Hello there."}, headers={"origin": BASE})
        s = r.json()
        check("speech is made and signed with the person's name", r.status_code == 200 and s["by"] == "alice",
              r.text[:120])
        check("alice can play her speech, bob cannot",
              (await alice.get(f"/api/tts/files/{s['name']}")).status_code == 200
              and (await bob.get(f"/api/tts/files/{s['name']}")).status_code == 404, "")
        first, second = await asyncio.gather(
            alice.post("/api/tts/speak", json={"text": "One."}, headers={"origin": BASE}),
            alice.post("/api/tts/speak", json={"text": "Two."}, headers={"origin": BASE}))
        check("one person gets one speech at a time", sorted([first.status_code, second.status_code]) == [200, 429],
              f"{first.status_code} {second.status_code}")

        async def slow_explain(text, language="", model=None):
            await asyncio.sleep(0.2)
            return {"model": "m", "explanation": text, "words": []}
        so.explain = slow_explain
        quote = {"highlights": [{"id": "q1", "page": 1, "text": "alice's secret quote", "rects": [[1, 0, 0, 9, 9]]}]}
        await alice.post(f"/api/documents/{a_doc}/state", json=quote, headers={"origin": BASE})
        r = await bob.post("/api/explain", json={"doc": a_doc, "id": "q1"}, headers={"origin": BASE})
        check("bob cannot explain alice's quote, or read it that way",
              r.status_code == 409 and "secret" not in r.text, f"{r.status_code} {r.text[:80]}")
        first, second, speech = await asyncio.gather(
            alice.post("/api/explain", json={"doc": a_doc, "id": "q1"}, headers={"origin": BASE}),
            alice.post("/api/explain", json={"doc": a_doc, "id": "q1"}, headers={"origin": BASE}),
            alice.post("/api/tts/speak", json={"text": "Three."}, headers={"origin": BASE}))
        check("one explanation at a time per person, not blocked by their speech",
              sorted([first.status_code, second.status_code]) == [200, 429] and speech.status_code == 200
              and "explanation" in (first.text + second.text), f"{first.status_code} {second.status_code} {speech.status_code}")
        old = Path(s["path"])
        b = (await bob.post("/api/tts/speak", json={"text": "Bye."}, headers={"origin": BASE})).json()
        fresh = Path(b["path"])
        os.utime(old, (0, 0))

        # ── Đăng xuất / đổi mật khẩu ─────────────────────────────────────────
        r = await alice.post("/api/logout", headers={"origin": BASE})
        check("signing out ends the session on the server", r.status_code == 200
              and (await alice.get("/api/documents")).status_code == 401, r.text[:80])
        await sign_in(bob, "bob", "bob-password")
        bob_id = rs._q("SELECT id FROM users WHERE name = 'bob'", one=True)["id"]
        rs._q("DELETE FROM sessions WHERE user_id = ?", (bob_id,))   # = passwd: đăng xuất mọi nơi
        check("revoked sessions stop working at once", (await bob.get("/api/me")).status_code == 401, "")

    # Dọn theo giờ, không chờ ai đọc tiếp: lifespan chạy lượt dọn đầu tiên ngay khi server lên.
    async with app.router.lifespan_context(app):
        for _ in range(50):
            if not old.exists():
                break
            await asyncio.sleep(0.05)
    check("speech older than an hour is cleared on a timer, even if nobody speaks again",
          not old.exists() and fresh.exists(), f"old={old.exists()} fresh={fresh.exists()}")

    # ── Image Docker: không có bí mật, không có agent ────────────────────────
    dockerfile = (HERE / "Dockerfile.reader").read_text(encoding="utf-8")
    ignore = (HERE / ".dockerignore").read_text(encoding="utf-8").split()
    copies = [ln for ln in dockerfile.splitlines() if ln.startswith("COPY")]
    check("the image copies named files only (never the whole folder, never .env)",
          copies and all(" . " not in ln and ".env" not in ln for ln in copies), str(copies))
    check(".dockerignore keeps .env and the data out of the build", ".env" in ignore and "reader-data" in ignore, str(ignore))
    check("the container does not run as root", "\nUSER " in dockerfile, "")


asyncio.run(main())
print()
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("all reader checks passed")
