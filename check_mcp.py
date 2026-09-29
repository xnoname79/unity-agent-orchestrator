#!/usr/bin/env python3
"""Guards for the MCP servers panel.

The panel takes a secret off the user and writes it into ~/.claude.json, so the ways it can go
wrong quietly are all about that secret and that file:

  - a server that refuses the token must leave the config file BYTE-IDENTICAL. Writing first and
    verifying later is how someone ends up with a config that looks right and a tool call that
    fails on every turn;
  - claude is not the only CLI here. An agent spawned on codex or agy reads a different file in a
    different syntax, so a server written only to ~/.claude.json is a tool that silently does not
    exist for two engines out of three;
  - codex keeps its MCP servers in the same config.toml as the user's model, effort and per-project
    trust levels. Adding one server must not disturb a byte of the rest;
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
# codex: MCP nằm chung file với model/effort/trust-level. Mọi dòng dưới đây phải sống sót.
CODEX_BEFORE = """model = "gpt-5.6-luna"
model_reasoning_effort = "medium"

[mcp_servers.signal]
url = "http://localhost:8992/signal/mcp"

[projects."/home/someone/repo"]
trust_level = "trusted"
"""
AGY_BEFORE = {"mcpServers": {"signal": {"disabled": False,
                                        "serverUrl": "http://localhost:8992/signal/mcp"}}}

os.environ["ORCH_DB"] = "check_mcp"
os.environ["ORCH_WORKSPACES_ROOT"] = str(TMP / "ws")
os.environ["ORCH_DRY_RUN"] = "1"
os.environ["ORCH_CLAUDE_CONFIG"] = str(CFG)
# BẮT BUỘC trỏ đi chỗ khác: máy chạy test có thể đã cài codex/agy thật, và endpoint ghi cho MỌI
# CLI đang cài — không override là test sửa thẳng cấu hình thật của người chạy nó.
CODEX = TMP / "codex-config.toml"
AGY = TMP / "agy-mcp.json"
CODEX.write_text(CODEX_BEFORE, encoding="utf-8")
AGY.write_text(json.dumps(AGY_BEFORE, indent=2), encoding="utf-8")
os.environ["ORCH_CODEX_CONFIG"] = str(CODEX)
os.environ["ORCH_AGY_MCP_CONFIG"] = str(AGY)
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
SESSION = "sess-abc123"


def sse(result):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result})
    return f"event: message\ndata: {payload}\n\n".encode()


class Server(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, sid=None):
        self.send_response(code)
        if sid:
            self.send_header("mcp-session-id", sid)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        method = (json.loads(raw or b"{}") or {}).get("method")
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self.reply(401, json.dumps({"error": "bad token"}).encode())
        # Server CÓ PHIÊN, đúng mặc định của mcp python SDK: initialize phát mcp-session-id, mọi
        # lượt sau thiếu header đó ăn 400. Không bắt tay thì không đời nào thấy được tools/list —
        # đây chính là cái làm /signal/mcp hiện ERROR trong modal.
        if method == "initialize":
            return self.reply(200, sse({"protocolVersion": "2024-11-05"}), sid=SESSION)
        if self.headers.get("Mcp-Session-Id") != SESSION:
            return self.reply(400, json.dumps(
                {"error": {"code": -32600, "message": "Bad Request: Missing session ID"}}).encode())
        if method == "notifications/initialized":
            return self.reply(202, b"")
        return self.reply(200, sse({"tools": TOOLS}))


srv = HTTPServer(("127.0.0.1", 0), Server)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_port}/mcp"
NAME = "story-studio"


def cfg_now():
    return CFG.read_text(encoding="utf-8")


def all_cfgs():
    """Ảnh chụp CẢ BA file — "không ghi gì khi chưa chứng minh được" phải đúng cho cả ba."""
    return (cfg_now(), CODEX.read_text(encoding="utf-8"), AGY.read_text(encoding="utf-8"))


async def main():
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        top = (await c.get("/api/mcp")).json()
        check("the reply says which CLIs are installed here",
              set(top["clis"]) <= {"claude", "codex", "agy"} and "claude" in top["clis"],
              str(top.get("clis")))
        listed = top["servers"]
        names = {s["name"]: s for s in listed}
        check("the servers already in the file are listed",
              set(names) == {"local-tool"}, str(sorted(names)))
        # Server orchestrator tự mount bị GIẤU khỏi modal, nhưng vẫn nguyên trong file — giấu
        # không phải xoá, agent vẫn signal được.
        check("the orchestrator's own bundled server is hidden",
              "signal" not in names, str(sorted(names)))
        check("but it is still registered in the file",
              "signal" in json.loads(cfg_now())["mcpServers"], cfg_now())
        check("a stdio server is listed but not checkable",
              names["local-tool"]["checkable"] is False
              and names["local-tool"]["type"] == "stdio", str(names.get("local-tool")))
        r = (await c.post("/api/mcp/check", json={"name": "local-tool"})).json()
        check("checking a stdio server says unsupported, not unreachable",
              r["state"] == "unsupported", str(r))

        # ── không ghi gì khi chưa chứng minh được ────────────────────────────
        before = all_cfgs()
        r = await c.post("/api/mcp/connect", json={"name": NAME, "url": URL, "token": "wrong"})
        check("a rejected token is refused",
              r.status_code == 400 and r.json().get("state") == "rejected",
              f"{r.status_code} {r.text[:110]}")
        check("and no CLI config was touched at all", all_cfgs() == before)

        for bad, why in ((({"name": "bad name!", "url": URL, "token": TOKEN}), "a bad name"),
                         (({"name": NAME, "url": "ftp://nope", "token": TOKEN}), "a non-http url"),
                         (({"name": NAME, "url": "http://127.0.0.1:1/mcp", "token": TOKEN}),
                          "an unreachable server")):
            r = await c.post("/api/mcp/connect", json=bad)
            check(f"{why} is refused", r.status_code == 400, f"{r.status_code} {r.text[:90]}")
        check("and none of those wrote anything", all_cfgs() == before)

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

        # ── cùng một server, hai CLI còn lại ─────────────────────────────────
        check("connect says where it wrote", set(body.get("clis") or []) == set(top["clis"]),
              str(body.get("clis")))
        toml = CODEX.read_text(encoding="utf-8")
        check("codex got the server in its own syntax",
              f'[mcp_servers."{NAME}"]' in toml and f'url = "{URL}"' in toml, toml)
        check("with the token as an http header, not a command-line flag",
              f'http_headers = {{ Authorization = "Bearer {TOKEN}" }}' in toml, toml)
        check("and codex's model, effort and trust levels are untouched",
              all(line in toml for line in ('model = "gpt-5.6-luna"',
                                            'model_reasoning_effort = "medium"',
                                            '[projects."/home/someone/repo"]',
                                            'trust_level = "trusted"')), toml)
        check("the codex server that was already there survived",
              "[mcp_servers.signal]" in toml, toml)
        agy = json.loads(AGY.read_text(encoding="utf-8"))["mcpServers"]
        check("agy got it under ITS key name (serverUrl, not url)",
              agy[NAME] == {"disabled": False, "serverUrl": URL,
                            "headers": {"Authorization": "Bearer " + TOKEN}}, str(agy.get(NAME)))
        check("and agy's own servers survived", "signal" in agy, str(sorted(agy)))
        # Ghi lại cùng tên = THAY, không phải khai hai lần. Codex đọc bảng trùng tên là lỗi parse.
        await c.post("/api/mcp/connect", json={"name": NAME, "url": URL, "token": TOKEN})
        check("re-adding replaces the codex table instead of duplicating it",
              CODEX.read_text(encoding="utf-8").count(f'[mcp_servers."{NAME}"]') == 1,
              CODEX.read_text(encoding="utf-8"))
        row = {s["name"]: s for s in (await c.get("/api/mcp")).json()["servers"]}[NAME]
        check("the listing says which CLIs have it",
              set(row["clis"]) == set(top["clis"]), str(row.get("clis")))

        # ── token không được rò ra bất kỳ response nào ───────────────────────
        leaks = []
        for path, kw in (("/api/mcp", {}), ("/api/mcp/check", {"json": {"name": NAME}})):
            resp = await (c.get(path) if not kw else c.post(path, **kw))
            if TOKEN in resp.text:
                leaks.append(path)
        check("no endpoint hands the token back out", not leaks, str(leaks))
        hint = {s["name"]: s for s in (await c.get("/api/mcp")).json()["servers"]}[NAME]["token_hint"]
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
        check("and it is gone from every other CLI too",
              NAME not in CODEX.read_text(encoding="utf-8")
              and NAME not in json.loads(AGY.read_text(encoding="utf-8"))["mcpServers"],
              CODEX.read_text(encoding="utf-8"))
        check("removing one codex server leaves the rest of config.toml alone",
              all(line in CODEX.read_text(encoding="utf-8")
                  for line in ('model = "gpt-5.6-luna"', "[mcp_servers.signal]",
                               '[projects."/home/someone/repo"]')),
              CODEX.read_text(encoding="utf-8"))
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
