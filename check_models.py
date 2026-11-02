#!/usr/bin/env python3
"""Guards for the model lists the dashboard offers.

The Gemini tab is read live from `agy models`, because agy does not fail on a model it does not
know: it quietly runs its default model and exits 0. A hardcoded slug that agy has dropped
therefore runs the wrong model with no error anywhere. What can go wrong:

  - the parser misses slugs (colour codes, bullets, a header row) or picks up words that are
    not models;
  - every page load starting the agy CLI again (seconds each, and it may touch the network);
  - agy missing, logged out or failing, and the dashboard showing an empty Gemini tab instead of
    the built-in list;
  - a built-in slug that this agy no longer lists still being offered once the live list is known.

    python3 check_models.py
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TMP = Path(tempfile.mkdtemp(prefix="check_models_"))
os.environ.update({"ORCH_DB": "check_models", "ORCH_WORKSPACES_ROOT": str(TMP / "ws"), "ORCH_DRY_RUN": "1"})
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import session_orchestrator as so   # noqa: E402

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


OUTPUT = (
    "\x1b[1mAvailable models\x1b[0m\n"
    "  ID                         NAME\n"
    "  gemini-4-argon-high        Gemini 4 Argon (High)\n"
    "  gemini-3.8-flash-low       Gemini 3.8 Flash (Low)\n"
    "- claude-sonnet-4-6\n"
    "* gpt-oss-120b-medium\n"
    "Run agy --model <id> to pick one.\n"
    "  gemini-3.8-flash-low\n"
)
WANT = ["gemini-4-argon-high", "gemini-3.8-flash-low", "claude-sonnet-4-6", "gpt-oss-120b-medium"]

check("slugs are read through colour codes, bullets and a header row, once each",
      so.parse_agy_models(OUTPUT) == WANT, so.parse_agy_models(OUTPUT))
check("prose and empty output give no models",
      so.parse_agy_models("Not logged in. Run agy login.\n\n") == [], so.parse_agy_models("Not logged in."))

# Fake `agy models`: the real CLI is not on CI, and a fake .exe cannot be written on Windows, so
# the subprocess call itself is replaced. CALLS counts how often the CLI would have started.
CALLS = []
REPLY = {"out": OUTPUT, "err": "", "rc": 0, "missing": False}


class FakeProc:
    def __init__(self):
        self.returncode = REPLY["rc"]

    async def communicate(self):
        return REPLY["out"].encode(), REPLY["err"].encode()


async def fake_exec(*argv, **kw):
    CALLS.append(argv)
    if REPLY["missing"]:
        raise FileNotFoundError(argv[0])
    return FakeProc()


so.asyncio.create_subprocess_exec = fake_exec
so.AGY_BIN = "agy-test"


async def main():
    r = await so.agy_models()
    check("the live list comes from `agy models`",
          r["models"] == WANT and CALLS and CALLS[-1] == ("agy-test", "models"), r)
    await so.agy_models()
    await so.agy_models()
    check("it is cached: page loads do not start the CLI again", len(CALLS) == 1, len(CALLS))
    REPLY["out"] = "  gemini-4-argon-high\n"
    r = await so.agy_models(refresh=True)
    check("refresh asks agy again", len(CALLS) == 2 and r["models"] == ["gemini-4-argon-high"], r)

    REPLY.update(missing=True)
    r = await so.agy_models(refresh=True)
    check("agy not installed → no list, and says why", r["models"] is None and "not found" in r["error"], r)
    n = len(CALLS)
    await so.agy_models()
    check("a failure is cached too, so a broken agy is not retried on every load", len(CALLS) == n)
    so._agy_models_cache["at"] -= 61
    REPLY.update(missing=False, rc=1, out="", err="Error: not logged in")
    r = await so.agy_models()
    check("…but only briefly: a minute later it asks again", len(CALLS) == n + 1)
    check("a failing agy → no list, with its own message", r["models"] is None and "not logged in" in r["error"], r)

    import httpx
    REPLY.update(rc=0, out=OUTPUT, err="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=so.build_app()), base_url="http://test") as c:
        r = await c.get("/api/models", params={"refresh": "1"})
        check("GET /api/models serves the agy list", r.status_code == 200 and r.json()["agy"]["models"] == WANT,
              r.text[:200])


asyncio.run(main())

# The dashboard side: once the live list is known, the Gemini tab is exactly that list (plus Auto).
node = shutil.which("node")
if not node:
    print("skip the dashboard half — node is not installed")
else:
    js = (HERE / "static" / "orchestrator" / "app.js").read_text(encoding="utf-8")
    start = js.index("const MODEL_TABS = [")
    end = js.index("async function agyLiveLoad(")
    probe = js[start:end] + """
const tab = () => MODEL_TABS.find((t) => t.engine === "agy").models.map((m) => m.id);
const fallback = tab();
agyLiveApply({ models: %s, error: "" });
const live = tab(), names = MODEL_TABS.find((t) => t.engine === "agy").models.map((m) => m.name);
agyLiveApply({ models: null, error: "agy CLI not found" });
console.log(JSON.stringify({ fallback, live, names, back: tab(),
  claude: MODEL_TABS.find((t) => t.engine === "claude").models.map((m) => m.id) }));
""" % json.dumps(WANT)
    (TMP / "probe.js").write_text(probe, encoding="utf-8")
    p = subprocess.run([node, str(TMP / "probe.js")], capture_output=True, text=True, encoding="utf-8")
    try:
        got = json.loads(p.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        got = {}
        check("the model list script runs", False, (p.stderr or p.stdout)[-400:])
    if got:
        check("the live list replaces the built-in Gemini models, keeping Auto first",
              got["live"] == ["agy"] + [f"agy:{m}" for m in WANT], got["live"])
        check("a built-in slug this agy does not list is no longer offered",
              "agy:gemini-3.1-pro-high" in got["fallback"] and "agy:gemini-3.1-pro-high" not in got["live"])
        check("a model nobody named yet gets a readable name",
              "4 Argon · high" in got["names"] and "Claude Sonnet 4.6" in got["names"], got["names"])
        check("without a live list the built-in one comes back", got["back"] == got["fallback"], got["back"])
        check("every Claude family has an alias that follows the newest version",
              {"fable", "opus", "sonnet", "haiku"} <= set(got["claude"]), got["claude"])

shutil.rmtree(TMP, ignore_errors=True)
for ext in ("", "-wal", "-shm"):
    Path(str(so.DB_DIR / "check_models.db") + ext).unlink(missing_ok=True)

if fails:
    print(f"\n{len(fails)} FAILED:\n  " + "\n  ".join(fails))
    sys.exit(1)
print("\nall good")
