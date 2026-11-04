#!/usr/bin/env python3
"""Guards for the MCP servers panel.

The panel takes a secret off the user and writes it into ~/.claude.json, so the ways it can go
wrong quietly are all about that secret and that file:

  - a server that refuses the token must leave the config file BYTE-IDENTICAL. Writing first and
    verifying later is how someone ends up with a config that looks right and a tool call that
    fails on every turn;
  - claude is not the only CLI here. An agent spawned on agy reads a different file in a
    different syntax, so a server written only to ~/.claude.json is a tool that silently does not
    exist for half the engines;
  - Codex support was removed, but users still have ~/.codex/config.toml (their model, effort and
    trust levels live there). Nothing here may write to it any more — not a byte;
  - nothing that leaves this process may carry the token. It belongs on the wire to the MCP
    server and nowhere else — not in a listing, not in an error string;
  - the file holds every other MCP server the user has, in every project. Rewriting it must not
    drop them;
  - tools/list answers over plain JSON on some servers and SSE frames on others. Counting only
    one shape reports "0 tools" for a server that actually works;
  - a stdio server has no URL to probe. Reporting it as unreachable would be a lie;
  - the library and voice servers are gone, so what the orchestrator once registered must leave
    the CLI configs (every agent would show them as failed), and a user's own `library` must not.

Runs the real app in-process over ASGI against a throwaway config file and a fake MCP server.

    python3 check_mcp.py
"""
import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
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
# ~/.codex/config.toml còn nằm trên máy người dùng sau khi Codex bị bỏ: phải giữ nguyên từng byte.
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
# BẮT BUỘC trỏ đi chỗ khác: máy chạy test có thể đã cài agy thật, và endpoint ghi cho MỌI
# CLI đang cài — không override là test sửa thẳng cấu hình thật của người chạy nó.
# HOME cũng vào thư mục tạm: một ~/.codex/config.toml giả nằm ĐÚNG chỗ thật của nó, để chứng minh
# không đường nào còn ghi vào đó.
os.environ["HOME"] = os.environ["USERPROFILE"] = str(TMP)
CODEX = TMP / ".codex" / "config.toml"
CODEX.parent.mkdir(parents=True, exist_ok=True)
AGY = TMP / "agy-mcp.json"
CODEX.write_text(CODEX_BEFORE, encoding="utf-8")
AGY.write_text(json.dumps(AGY_BEFORE, indent=2), encoding="utf-8")
os.environ["ORCH_AGY_MCP_CONFIG"] = str(AGY)
os.environ["ORCH_MCP_TIMEOUT"] = "3"
# Connect only writes for CLIs that are INSTALLED (shutil.which). Fake them all (codex too: an old
# install must not bring its config back into play), so the checks
# below mean the same thing on a dev box with real CLIs and on a CI runner with none.
_BIN = TMP / "bin"
_BIN.mkdir(exist_ok=True)
for _env, _name in (("CLAUDE_BIN", "claude"), ("ORCH_CODEX_BIN", "codex"), ("ORCH_AGY_BIN", "agy")):
    _f = _BIN / (_name + (".exe" if os.name == "nt" else ""))
    _f.write_bytes(b"")
    _f.chmod(0o755)
    os.environ[_env] = str(_f)

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
    """Ảnh chụp mọi file — "không ghi gì khi chưa chứng minh được" phải đúng cho tất cả."""
    return (cfg_now(), CODEX.read_text(encoding="utf-8"), AGY.read_text(encoding="utf-8"))


async def main():
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        top = (await c.get("/api/mcp")).json()
        check("the reply says which CLIs are installed here",
              set(top["clis"]) == {"claude", "agy"},
              str(top.get("clis")))
        listed = top["servers"]
        names = {s["name"]: s for s in listed}
        check("the servers already in the file are listed, the built-in signal too",
              set(names) == {"local-tool", "signal"}, str(sorted(names)))
        # Server orchestrator tự mount: hiện như mọi server (Check/Remove), có cờ builtin để modal
        # xếp vào mục riêng — nhận theo ĐƯỜNG DẪN, người dùng có thể đã đặt tên khác.
        check("the signal entry is recognised as built-in, by its path",
              names["signal"]["builtin"] == "signal" and names["local-tool"]["builtin"] == "",
              str(names["signal"]))
        built = {b["name"]: b for b in top["builtins"]}
        check("every server the orchestrator mounts is offered, connected or not",
              set(built) == {"signal"}, str(sorted(built)))
        check("with a URL the CLIs on this machine can reach, not the browser's address",
              built["signal"]["url"] == f"http://127.0.0.1:{so.ORCH_PORT}/signal/mcp",
              built["signal"]["url"])
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
        check("a leftover ~/.codex/config.toml is not written, even with codex installed",
              CODEX.read_text(encoding="utf-8") == CODEX_BEFORE, CODEX.read_text(encoding="utf-8"))
        agy = json.loads(AGY.read_text(encoding="utf-8"))["mcpServers"]
        check("agy got it under ITS key name (serverUrl, not url)",
              agy[NAME] == {"disabled": False, "serverUrl": URL,
                            "headers": {"Authorization": "Bearer " + TOKEN}}, str(agy.get(NAME)))
        check("and agy's own servers survived", "signal" in agy, str(sorted(agy)))
        # ── server nội bộ: một nút Connect ───────────────────────────────────
        probed, real_probe = [], so._mcp_probe
        kept = all_cfgs()

        async def fake_probe(url, token):
            probed.append(url)
            return "connected", 4, ""
        so._mcp_probe = fake_probe
        try:
            r = await c.post("/api/mcp/connect", json={"builtin": "signal",
                                                       "url": "http://evil.example/mcp"})
            bad = await c.post("/api/mcp/connect", json={"builtin": "nope"})
        finally:
            so._mcp_probe = real_probe
        want = f"http://127.0.0.1:{so.ORCH_PORT}/signal/mcp"
        check("Connect on a built-in writes the server's own URL, whatever the request says",
              r.status_code == 200 and probed == [want]
              and json.loads(cfg_now())["mcpServers"]["signal"]["url"] == want, r.text[:160])
        check("an unknown built-in is refused", bad.status_code == 400, bad.text[:90])
        r = await c.post("/api/mcp/disconnect", json={"name": "signal"})
        check("and Remove takes it out like any other server",
              r.json()["removed"] and "signal" not in json.loads(cfg_now())["mcpServers"], r.text)
        CFG.write_text(kept[0], encoding="utf-8")   # signal là "server có sẵn" của các bước sau
        AGY.write_text(kept[2], encoding="utf-8")

        await c.post("/api/mcp/connect", json={"name": NAME, "url": URL, "token": TOKEN})
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
        check("and it is gone from agy too",
              NAME not in json.loads(AGY.read_text(encoding="utf-8"))["mcpServers"],
              AGY.read_text(encoding="utf-8"))
        check("~/.codex/config.toml is byte-identical after connect, re-add and remove",
              CODEX.read_text(encoding="utf-8") == CODEX_BEFORE, CODEX.read_text(encoding="utf-8"))
        r = (await c.post("/api/mcp/disconnect", json={"name": NAME})).json()
        check("disconnecting twice is harmless", r["removed"] is False, str(r))

    # Nhánh JSON thường của _count_tools (server giả chỉ nói SSE).
    check("a plain-JSON tools/list is counted too",
          so._count_tools(json.dumps({"result": {"tools": TOOLS[:3]}})) == 3)
    check("a body with no tools counts zero", so._count_tools("not json at all") == 0)


def servers(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))["mcpServers"]


def test_stale_registrations():
    """library/voice đã bỏ: `serve` phải gỡ mục cũ của CHÍNH nó, không đụng server trùng tên của người
    dùng. Chạy `serve` thật — hàm dọn mà không được gọi lúc start thì cũng như không có."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    home = {"library": f"http://localhost:{port}/library/mcp",
            "voice": f"http://127.0.0.1:{port}/voice/mcp",
            "library-x": f"http://localhost:{port}/library/mcp"}
    CFG.write_text(json.dumps({"mcpServers": {n: {"type": "http", "url": u} for n, u in home.items()},
                               "someOtherKey": {"keep": "me"}}), encoding="utf-8")
    AGY.write_text(json.dumps({"mcpServers": {
        "library": {"disabled": False, "serverUrl": "https://example.com/mcp"},
        "voice": {"disabled": False, "serverUrl": f"http://127.0.0.1:{port}/voice/mcp"}}}),
        encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(HERE / "session_orchestrator.py"), "serve"], cwd=str(HERE),
                            env={**os.environ, "ORCH_PORT": str(port), "ORCH_HOST": "127.0.0.1"},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.25)
        else:
            check("the server came up for the stale-registration test", False, "no /health")
    finally:
        proc.terminate()
        proc.wait(timeout=15)
    left = servers(CFG)
    check("serve removes the library and voice it registered itself in claude's config",
          "library" not in left and "voice" not in left, str(sorted(left)))
    check("a server with another name stays, whatever its URL",
          left.get("library-x", {}).get("url") == home["library-x"], str(sorted(left)))
    check("and so does the rest of the file",
          json.loads(cfg_now()).get("someOtherKey") == {"keep": "me"}, cfg_now()[:120])
    left = servers(AGY)
    check("a foreign `library` survives in agy's config while its own `voice` goes",
          left.get("library", {}).get("serverUrl") == "https://example.com/mcp" and "voice" not in left,
          str(left))

    # Những mục gần giống: sai port, sai host, sai path, URL hỏng. Cái nào cũng không phải của mình.
    near = {"library": f"http://localhost:{so.ORCH_PORT + 1}/library/mcp",
            "voice": "http://localhost:notaport/voice/mcp"}
    near2 = {"library": f"http://127.0.0.1:{so.ORCH_PORT}/voice/mcp",
             "voice": f"http://example.com:{so.ORCH_PORT}/voice/mcp"}
    CFG.write_text(json.dumps({"mcpServers": {n: {"type": "http", "url": u} for n, u in near.items()}}),
                   encoding="utf-8")
    AGY.write_text(json.dumps({"mcpServers": {n: {"serverUrl": u} for n, u in near2.items()}}), encoding="utf-8")
    before = all_cfgs()
    so._forget_removed_builtins()
    check("wrong port, wrong path, wrong host and a broken URL are all left alone, not a byte rewritten",
          all_cfgs() == before, str(all_cfgs()[0])[:200])


asyncio.run(main())
test_stale_registrations()
srv.shutdown()
db.unlink(missing_ok=True)

if fails:
    print("\n" + "\n".join(fails))
    sys.exit(1)
print("\nall MCP checks passed")
