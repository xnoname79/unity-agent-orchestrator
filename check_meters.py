#!/usr/bin/env python3
"""Guards for the context / prompt-cache / plan-usage meters on the dashboard.

Two sources fill them, and each can break without a sound:
  - interactive terminals: Claude Code runs our statusLine command after every message and pipes
    its status JSON to it. The command is a bare `curl -T -` that PUTs that JSON to /api/meters.
    It has to survive bash, cmd AND PowerShell (where `curl` is Invoke-WebRequest and `@` is
    syntax), so the check runs the exact string from the generated settings file through the real
    shell against the real server. A user who already has a statusLine keeps theirs;
  - background runs: every `assistant` event of `claude -p --output-format stream-json` carries
    the usage of that API call. A scripted fake CLI drives the real stream parser.
Bad JSON must get a 400 and change nothing.

Everything lives under a throwaway HOME: DB, settings file, Claude config, tmux socket dir.

    python3 check_meters.py
"""
import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
NT = os.name == "nt"
TMP = Path(tempfile.mkdtemp(prefix="check_meters_"))
with socket.socket() as _sock:
    _sock.bind(("127.0.0.1", 0))
    PORT = _sock.getsockname()[1]
(TMP / "tmux").mkdir()
(TMP / "claude-config").mkdir()
ENV = {"HOME": str(TMP), "USERPROFILE": str(TMP), "ORCH_PORT": str(PORT), "ORCH_HOST": "127.0.0.1",
       "ORCH_DB": "check_meters", "ORCH_DRY_RUN": "1", "ORCH_WORKSPACES_ROOT": str(TMP / "ws"),
       "TMUX_TMPDIR": str(TMP / "tmux"), "CLAUDE_CONFIG_DIR": str(TMP / "claude-config")}
os.environ.update(ENV)
sys.path.insert(0, str(HERE))

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
assert so.DB_DIR.is_relative_to(TMP), so.DB_DIR    # never the real ~/.session_orch_db
so._ensure_db()

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


WS = so.DEFAULT_WORKSPACE
so.register_session("card-a", "alpha", cwd=str(TMP), model="", workspace_id=WS)
so.register_session("card-b", "beta", cwd=str(TMP), model="", workspace_id=WS)
so.register_session("card-g", "gamma", cwd=str(TMP), model="agy-default", workspace_id=WS)
so.set_session_resume("card-b", "transcript-b")       # beta's terminal runs a pinned transcript

PAYLOAD = {"session_id": "card-a",
           "context_window": {"used_percentage": 14, "context_window_size": 1000000,
                              "current_usage": {"input_tokens": 1000, "cache_creation_input_tokens": 0,
                                                "cache_read_input_tokens": 9000, "output_tokens": 50}},
           "cost": {"total_cost_usd": 0.25},
           "rate_limits": {"five_hour": {"used_percentage": 11, "resets_at": 1762600000},
                           "seven_day": {"used_percentage": 32, "resets_at": 1763000000}}}
LINE = "ctx 14% · cache 90% · 5h 11% · wk 32%"

# ── (a) the terminal gets our statusline, only for claude, only when the user has none ──
argv = so.terminal_launch_argv(so.get_session("card-a"), "claude")
settings = Path(argv[2]) if argv[1:2] == ["--settings"] else None
check("a claude terminal starts with --settings <file>",
      argv[0] == so.CLAUDE_BIN and settings is not None and settings.is_file(), argv)
cmd = json.loads(settings.read_text(encoding="utf-8"))["statusLine"]["command"] if settings else ""
curl = shutil.which("curl")
check("the file's statusLine is the bare curl PUT to this server",
      cmd == f"{Path(curl).as_posix()} -s -m 2 -T - http://127.0.0.1:{PORT}/api/meters"
      and not any(c in cmd for c in "\"'@|"), cmd)
check("the rest of the claude argv is untouched",
      argv[:1] + argv[3:] == so.terminal_argv(so.get_session("card-a"), "claude"), argv)
check("the terminal websocket launches through terminal_launch_argv",
      "lambda s: terminal_launch_argv(s, websocket.query_params" in (HERE / "session_orchestrator.py")
      .read_text(encoding="utf-8"))
check("an agy terminal is unchanged",
      so.terminal_launch_argv(so.get_session("card-g"), "agy")
      == so.terminal_argv(so.get_session("card-g"), "agy"))
user_settings = TMP / "claude-config" / "settings.json"
user_settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo mine"}}),
                         encoding="utf-8")
check("a user with their own statusLine keeps it (no --settings)",
      "--settings" not in so.terminal_launch_argv(so.get_session("card-a"), "claude"))
check("and their settings file is never written",
      json.loads(user_settings.read_text(encoding="utf-8"))["statusLine"]["command"] == "echo mine")
user_settings.unlink()


# ── (b) the exact command, through the real shell, against the real server ──
def shells():
    if NT:
        return [["cmd", "/c", cmd], ["powershell", "-NoProfile", "-Command", cmd]]
    return [["sh", "-c", cmd]]


def run_cmd(shell, payload):
    r = subprocess.run(shell, input=json.dumps(payload).encode("utf-8"), capture_output=True,
                       timeout=30)
    return r.stdout.decode("utf-8", "replace").strip()


proc = subprocess.Popen([sys.executable, str(HERE / "session_orchestrator.py"), "serve"],
                        cwd=str(HERE), env={**os.environ, **ENV},
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
try:
    base = f"http://127.0.0.1:{PORT}"
    for _ in range(120):
        try:
            httpx.get(base + "/health", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.25)
    else:
        raise AssertionError("server never came up: " + proc.stderr.read1().decode("utf-8", "replace"))

    def meters():
        return {s["id"]: s.get("meter") for s in httpx.get(base + "/api/sessions").json()}

    for shell in shells():
        out = run_cmd(shell, PAYLOAD)
        check(f"{shell[0]}: the statusline prints the short line", out == LINE, repr(out))
    m = meters()["card-a"] or {}
    check("the card's meter is stored, matched by card id",
          m.get("pct") == 14 and m.get("cache_hit") == 0.9 and m.get("tokens") == 10000
          and m.get("window") == 1000000 and m.get("cost") == 0.25 and m.get("cost_src") == "terminal", m)
    plan = httpx.get(base + "/health").json().get("plan_usage") or {}
    check("plan usage is stored for the account",
          plan.get("five_hour", {}).get("used_percentage") == 11
          and plan.get("seven_day", {}).get("resets_at") == 1763000000, plan)

    out = run_cmd(shells()[0], {**PAYLOAD, "session_id": "transcript-b", "rate_limits": None})
    m = meters()
    check("a pinned transcript's payload lands on its card (resume_id)",
          (m["card-b"] or {}).get("pct") == 14 and m["card-a"] is not None, m)
    check("a payload without rate_limits keeps the last plan usage", out == LINE, repr(out))
    check("a card with no data has no meter", m["card-g"] is None, m["card-g"])

    later = {**PAYLOAD, "session_id": "not-a-card",
             "rate_limits": {"seven_day": {"used_percentage": 40, "resets_at": 1763000000}}}
    out = run_cmd(shells()[0], later)
    plan = httpx.get(base + "/health").json().get("plan_usage") or {}
    check("an unknown session still updates plan usage and gets its line",
          out == "ctx 14% · cache 90% · wk 40%" and plan.get("seven_day", {}).get("used_percentage") == 40
          and "five_hour" not in plan and "not-a-card" not in meters(), f"{out!r} {plan}")

    before = (meters(), httpx.get(base + "/health").json().get("plan_usage"))
    r = httpx.put(base + "/api/meters", content=b"{not json")
    check("bad JSON on the real server is a 400 and stores nothing",
          r.status_code == 400 and (meters(), httpx.get(base + "/health").json().get("plan_usage")) == before,
          r.status_code)
finally:
    proc.kill()
    proc.wait()


# ── (c) background run: usage of the latest assistant event, cost from result ──
FAKE = TMP / "fake_claude.py"
FAKE.write_text("""import json, sys
sys.stdin.read()
for ev in [
    {"type": "system", "subtype": "init", "model": "fake", "tools": []},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "one"}],
     "usage": {"input_tokens": 3, "cache_creation_input_tokens": 50000, "cache_read_input_tokens": 0}}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "two"}],
     "usage": {"input_tokens": 5, "cache_creation_input_tokens": 995, "cache_read_input_tokens": 134000}}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "done", "num_turns": 2,
     "total_cost_usd": 0.4321, "usage": {"output_tokens": 20}},
]:
    print(json.dumps(ev), flush=True)
""", encoding="utf-8")
if NT:
    so.CLAUDE_BIN = str(TMP / "fake_claude.cmd")
    Path(so.CLAUDE_BIN).write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n', encoding="utf-8")
else:
    so.CLAUDE_BIN = str(TMP / "fake_claude")
    Path(so.CLAUDE_BIN).write_text(f"#!{sys.executable}\n" + FAKE.read_text(encoding="utf-8"),
                                   encoding="utf-8")
    os.chmod(so.CLAUDE_BIN, 0o755)
so.DRY_RUN = False
so.STREAM = True


async def noop(*_):
    pass


async def background():
    r = await so._run_claude(so.get_session("card-b"), "hello", on_event=noop)
    check("the fake run itself succeeds", r["ok"], r)
    m = so.METERS.get("card-b") or {}
    check("a run without a known window shows tokens, never a guessed percent",
          m.get("tokens") == 135000 and m.get("pct") is None and m.get("window") is None, m)
    check("cache hit comes from the latest API call",
          abs((m.get("cache_hit") or 0) - 134000 / 135000) < 1e-9, m)
    check("run cost comes from the result event", m.get("cost") == 0.4321 and m.get("cost_src") == "run", m)
    check("the line falls back to tokens", so._meter_line(m, {}) == "ctx 135k · cache 99%",
          so._meter_line(m, {}))

    so.METERS["card-a"] = {"window": 1000000}             # a terminal already told us the window
    await so._run_claude(so.get_session("card-a"), "hello", on_event=noop)
    m = so.METERS.get("card-a") or {}
    check("with a window from the statusline the run shows a percent",
          abs((m.get("pct") or 0) - 13.5) < 1e-9, m)

    # ── (d) bad payloads: 400, nothing stored ──
    so.PLAN_USAGE.clear()
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        snap = json.dumps(so.METERS, sort_keys=True)
        for body in (b"{bad", b"[1, 2]", b"\xff\xfe",
                     json.dumps({**PAYLOAD, "context_window": {"used_percentage": "lots"}}).encode(),
                     json.dumps({**PAYLOAD, "rate_limits": {"five_hour": {"used_percentage": "x"}}}).encode()):
            r = await c.put("/api/meters", content=body)
            check(f"bad payload {body[:40]!r} is a 400", r.status_code == 400, r.status_code)
        check("and none of them stored anything",
              json.dumps(so.METERS, sort_keys=True) == snap and not so.PLAN_USAGE)
        r = await c.put("/api/meters", content=json.dumps(PAYLOAD), headers={"Origin": "http://evil.example"})
        check("a cross-site browser PUT is still refused", r.status_code == 403, r.status_code)


asyncio.run(background())

shutil.rmtree(TMP, ignore_errors=True)
if fails:
    print(f"\n{len(fails)} check(s) failed")
    sys.exit(1)
print("\nall checks passed")
