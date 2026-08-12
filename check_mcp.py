#!/usr/bin/env python3
"""Guards for the MCP servers panel.

The panel takes a secret off the user and writes it into ~/.claude.json, so the ways it can go
wrong quietly are all about that secret and that file:

  - a server that refuses the token must leave the config file BYTE-IDENTICAL. Writing first and
    verifying later is how someone ends up with a config that looks right and a tool call that
    fails on every turn;
  - nothing that leaves this process may carry the token. It belongs on the wire to the MCP
    server and nowhere else — not in a listing, not in an error string;
  - the file holds every other MCP server the user has, in every project. Rewriting it must not
    drop them;
  - tools/list answers over plain JSON on some servers and SSE frames on others. Counting only
    one shape reports "0 tools" for a server that actually works;
  - a stdio server has no URL to probe. Reporting it as unreachable would be a lie.

Runs the real app in-process over ASGI against a throwaway config file and a fake MCP server.

    python3 check_mcp.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

TOKEN = "tok-secret-value-9876"          # tìm chuỗi này trong mọi response: không được có
TMP = Path(tempfile.mkdtemp())
CFG = TMP / "claude.json"
# Cấu hình có sẵn của người dùng. Phải còn nguyên sau connect VÀ sau disconnect.
OTHER = {"mcpServers": {"signal": {"type": "http", "url": "http://localhost:8992/signal/mcp"},
                        "local-tool": {"command": "npx", "args": ["some-mcp"]}},
         "someOtherKey": {"keep": "me"}}
CFG.write_text(json.dumps(OTHER, indent=2), encoding="utf-8")

os.environ["ORCH_DB"] = "check_mcp"
os.environ["ORCH_WORKSPACES_ROOT"] = str(TMP / "ws")
os.environ["ORCH_DRY_RUN"] = "1"
os.environ["ORCH_CLAUDE_CONFIG"] = str(CFG)
os.environ["ORCH_MCP_TIMEOUT"] = "3"

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)

db = so.DB_DIR / "check_mcp.db"
db.unlink(missing_ok=True)
so._ensure_db()

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


# ── MCP server giả ───────────────────────────────────────────────────────────
# Trả tools/list bằng KHUNG SSE, đúng kiểu streamable-http hay dùng — nhánh khó của _count_tools.
TOOLS = [{"name": f"tool_{i}"} for i in range(12)]


class Server(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            body = json.dumps({"error": "bad token"}).encode()
            self.send_response(401)
        else:
            payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": TOOLS}})
            body = f"event: message\ndata: {payload}\n\n".encode()
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = HTTPServer(("127.0.0.1", 0), Server)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_port}/mcp"
NAME = "story-studio"


def cfg_now():
    return CFG.read_text(encoding="utf-8")


async def main():
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        listed = (await c.get("/api/mcp")).json()
        names = {s["name"]: s for s in listed}
        check("the servers already in the file are listed",
              set(names) == {"signal", "local-tool"}, str(sorted(names)))
        check("a stdio server is listed but not checkable",
              names["local-tool"]["checkable"] is False
              and names["local-tool"]["type"] == "stdio", str(names.get("local-tool")))
        r = (await c.post("/api/mcp/check", json={"name": "local-tool"})).json()
        check("checking a stdio server says unsupported, not unreachable",
              r["state"] == "unsupported", str(r))

        # ── không ghi gì khi chưa chứng minh được ────────────────────────────
        before = cfg_now()
        r = await c.post("/api/mcp/connect", json={"name": NAME, "url": URL, "token": "wrong"})
        check("a rejected token is refused",
              r.status_code == 400 and r.json().get("state") == "rejected",
              f"{r.status_code} {r.text[:110]}")
        check("and the config file was not touched at all", cfg_now() == before)

        for bad, why in ((({"name": "bad name!", "url": URL, "token": TOKEN}), "a bad name"),
                         (({"name": NAME, "url": "ftp://nope", "token": TOKEN}), "a non-http url"),
                         (({"name": NAME, "url": "http://127.0.0.1:1/mcp", "token": TOKEN}),
                          "an unreachable server")):
            r = await c.post("/api/mcp/connect", json=bad)
            check(f"{why} is refused", r.status_code == 400, f"{r.status_code} {r.text[:90]}")
        check("and none of those wrote anything", cfg_now() == before)

        # ── đường thành công ─────────────────────────────────────────────────
        r = await c.post("/api/mcp/connect", json={"name": NAME, "url": URL, "token": TOKEN})
        body = r.json()
        check("a good token connects", r.status_code == 200 and body.get("state") == "connected",
              f"{r.status_code} {r.text[:110]}")
        check("the SSE-framed tools/list is counted, not read as zero",
              body.get("tools") == len(TOOLS), str(body.get("tools")))

        saved = json.loads(cfg_now())
        check("the entry matches what `claude mcp add` writes",
              saved["mcpServers"][NAME] == {"type": "http", "url": URL,
                                            "headers": {"Authorization": "Bearer " + TOKEN}},
              str(saved["mcpServers"].get(NAME)))
        check("the user's other MCP servers survived the rewrite",
              saved["mcpServers"]["signal"] == OTHER["mcpServers"]["signal"]
              and saved["mcpServers"]["local-tool"] == OTHER["mcpServers"]["local-tool"],
              str(saved["mcpServers"]))
        check("and so did the rest of the file",
              saved.get("someOtherKey") == {"keep": "me"}, str(saved.get("someOtherKey")))

        # ── token không được rò ra bất kỳ response nào ───────────────────────
        leaks = []
        for path, kw in (("/api/mcp", {}), ("/api/mcp/check", {"json": {"name": NAME}})):
            resp = await (c.get(path) if not kw else c.post(path, **kw))
            if TOKEN in resp.text:
                leaks.append(path)
        check("no endpoint hands the token back out", not leaks, str(leaks))
        hint = {s["name"]: s for s in (await c.get("/api/mcp")).json()}[NAME]["token_hint"]
        check("the saved token shows as a hint only",
              hint.endswith(TOKEN[-4:]) and TOKEN not in hint, hint)

        r = (await c.post("/api/mcp/check", json={"name": NAME})).json()
        check("checking a stored server reports it live",
              r["state"] == "connected" and r["tools"] == len(TOOLS), str(r))
        check("and stamps when it was checked", bool(r.get("checked_at")), str(r))
        r = await c.post("/api/mcp/check", json={"name": "ghost"})
        check("checking an unknown name is 404", r.status_code == 404, str(r.status_code))

        # ── disconnect ───────────────────────────────────────────────────────
        r = (await c.post("/api/mcp/disconnect", json={"name": NAME})).json()
        check("disconnect removes it", r["removed"] is True, str(r))
        left = json.loads(cfg_now())["mcpServers"]
        check("the key is gone", NAME not in left, str(sorted(left)))
        check("the other servers are still there",
              set(left) == {"signal", "local-tool"}, str(sorted(left)))
        r = (await c.post("/api/mcp/disconnect", json={"name": NAME})).json()
        check("disconnecting twice is harmless", r["removed"] is False, str(r))

    # Nhánh JSON thường của _count_tools (server giả chỉ nói SSE).
    check("a plain-JSON tools/list is counted too",
          so._count_tools(json.dumps({"result": {"tools": TOOLS[:3]}})) == 3)
    check("a body with no tools counts zero", so._count_tools("not json at all") == 0)


asyncio.run(main())
srv.shutdown()
db.unlink(missing_ok=True)

if fails:
    print("\n" + "\n".join(fails))
    sys.exit(1)
print("\nall MCP checks passed")
