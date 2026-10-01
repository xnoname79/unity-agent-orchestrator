#!/usr/bin/env python3
"""Guard: a website you visit must not be able to drive this orchestrator.

There is no API key, so the only thing separating "the dashboard asked" from "a page in another
tab asked" is the Origin header. Three beliefs here are wrong and each one is quiet:

  - "CORS protects us." It does not. A browser SENDS a simple request (POST, text/plain) and only
    blocks the caller from READING the reply. MEASURED: with CORS fully off, a cross-site POST to
    /api/images still reached the handler. An attacker who spawns an agent does not care what the
    reply said;
  - "binding 127.0.0.1 protects us." It does not. The user's own browser runs on that machine, so
    localhost is reachable from any page they open;
  - "WebSockets are covered." They are not — WS ignores CORS entirely, and /ws/terminal is a real
    shell.

So the guard checks Origin on state-changing requests and on every websocket. Non-browser clients
(curl, the OpenAI SDK, n8n, agents) send no Origin and must keep working, or the whole API dies.

    python3 check_origin.py
"""
import asyncio
import logging
import os
import sys
import tempfile

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["ORCH_DB"] = "check_origin"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()
os.environ["ORCH_DRY_RUN"] = "1"

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
db = so.DB_DIR / "check_origin.db"
db.unlink(missing_ok=True)
so._ensure_db()

HOST = "127.0.0.1:8992"
EVIL = "https://evil.example"
fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


def test_defaults():
    check("it listens on loopback only out of the box",
          so.ORCH_HOST == "127.0.0.1" and so._LOOPBACK_ONLY, so.ORCH_HOST)
    check("CORS is off out of the box", so.CORS_ORIGINS == [], str(so.CORS_ORIGINS))


async def main():
    test_defaults()
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url=f"http://{HOST}") as c:
        # THE measured attack: a simple request needs no preflight, so CORS never sees it.
        r = await c.post("/api/images", content='{"prompt": "pwned"}',
                         headers={"content-type": "text/plain", "origin": EVIL})
        check("the cross-site simple POST that used to reach the handler is refused",
              r.status_code == 403 and r.json()["error"]["code"] == "cross_origin",
              f"{r.status_code} {r.text[:90]}")
        r = await c.post("/v1/images/generations", json={"prompt": "x"},
                         headers={"origin": EVIL})
        check("/v1 is refused the same way", r.status_code == 403, str(r.status_code))
        r = await c.post("/api/sessions/spawn", json={"name": "evil"}, headers={"origin": EVIL})
        check("spawning an agent cross-site is refused", r.status_code == 403, str(r.status_code))
        r = await c.delete("/api/images/x.jpg", headers={"origin": EVIL})
        check("deleting cross-site is refused", r.status_code == 403, str(r.status_code))

        # file:// and sandboxed iframes send 'null'. No legitimate page of this app does.
        r = await c.post("/api/images", json={"prompt": "x"}, headers={"origin": "null"})
        check("origin 'null' is refused", r.status_code == 403, str(r.status_code))
        # Prefix matching must not be fooled by a lookalike host.
        r = await c.post("/api/images", json={"prompt": "x"},
                         headers={"origin": f"http://{HOST}.evil.example"})
        check("a lookalike origin is refused", r.status_code == 403, str(r.status_code))

        # ...while everything legitimate still works.
        r = await c.post("/api/images", json={"prompt": "", "ratio": "5:4"},
                         headers={"origin": f"http://{HOST}"})
        check("the dashboard's own origin passes the guard",
              r.status_code == 400, f"{r.status_code} (400 = reached the handler)")
        r = await c.post("/api/images", json={"prompt": "", "ratio": "5:4"})
        check("a client with no Origin (curl, the OpenAI SDK, n8n) passes",
              r.status_code == 400, str(r.status_code))
        r = await c.get("/api/images", headers={"origin": EVIL})
        check("a cross-site GET is left to CORS, not blocked here",
              r.status_code == 200, str(r.status_code))
        r = await c.get("/health", headers={"origin": EVIL})
        check("/health is not guarded", r.status_code == 200, str(r.status_code))

        # WS ignores CORS, and /ws/terminal is a shell. The guard closes with 4403; anything
        # else means the request got THROUGH it (4404 = the handler's own "no such session").
        from starlette.testclient import TestClient
        tc = TestClient(app)

        def ws_close_code(origin):
            try:
                with tc.websocket_connect("/ws/terminal?session=nope",
                                          headers={"origin": origin} if origin else {}):
                    return "connected"
            except Exception as e:
                return getattr(e, "code", None)

        check("a cross-site websocket to the terminal is refused",
              ws_close_code(EVIL) == 4403, str(ws_close_code(EVIL)))
        check("the editor websocket is guarded too",
              ws_close_code(EVIL) == 4403, str(ws_close_code(EVIL)))
        for who, origin in (("the dashboard", "http://testserver"), ("a non-browser client", None)):
            code = ws_close_code(origin)
            check(f"{who} reaches the terminal handler, not the guard",
                  code != 4403, f"closed {code}")

asyncio.run(main())
db.unlink(missing_ok=True)
if fails:
    print("\n" + "\n".join(fails))
    sys.exit(1)
print("\nall origin guards pass")
