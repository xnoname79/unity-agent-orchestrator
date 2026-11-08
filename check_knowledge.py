#!/usr/bin/env python3
"""Guards for search_knowledge — an agent reading what its own workspace already knows.

The tool reads Claude Code memory folders and the team decision log. What can go wrong quietly:
  - another workspace's memory showing up, because two workspaces use the same words;
  - a forged or ambiguous from_role getting any data (or agent names) back;
  - a query matching on one word of several, or the result list flooding the caller's context.

Runs against a throwaway DB and a temp CLAUDE_CONFIG_DIR — never the real ~/.claude.

    python3 check_knowledge.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TMP = Path(tempfile.mkdtemp(prefix="check_knowledge_"))
os.environ.update({"ORCH_DB": "check_knowledge", "ORCH_WORKSPACES_ROOT": str(TMP / "ws"),
                   "ORCH_DRY_RUN": "1", "CLAUDE_CONFIG_DIR": str(TMP / "claude")})
sys.path.insert(0, str(Path(__file__).resolve().parent))

import session_orchestrator as so   # noqa: E402
import signal_mcp                   # noqa: E402
import team                         # noqa: E402

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


db = so.DB_DIR / "check_knowledge.db"
for ext in ("", "-wal", "-shm"):
    Path(str(db) + ext).unlink(missing_ok=True)
so.init_db()
assert str(so.CLAUDE_PROJECTS_DIR).startswith(str(TMP)), so.CLAUDE_PROJECTS_DIR


def agent(sid, name, ws, cwd):
    so.register_session(sid, name, cwd=str(cwd), workspace_id=ws)
    folder = so.CLAUDE_PROJECTS_DIR / "".join(c if c.isascii() and c.isalnum() else "-"
                                              for c in str(cwd)) / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def memory(folder, stem, name, desc, body):
    (folder / f"{stem}.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  type: project\n---\n\n{body}\n",
        encoding="utf-8")


X, Y, Z = TMP / "proj_x", TMP / "proj.y", TMP / "proj_z"
mx = agent("s-alpha", "alpha", "ws_a", X)
my = agent("s-beta", "beta", "ws_a", Y)
mz = agent("s-gamma", "gamma", "ws_b", Z)
agent("s-dup-a", "dupname", "ws_a", X)
agent("s-dup-b", "dupname", "ws_b", Z)

memory(mx, "alpha_note", "alpha-note", "alpha's own note", "Nothing about the others.")
memory(my, "beta_docker", "docker-secrets", "image bakes secrets",
       "The orchestrator image bakes the zephyrine token; keep the registry private.")
(my / "MEMORY.md").write_text("- [docker](beta_docker.md) — zephyrine index line\n", encoding="utf-8")
memory(mz, "gamma_docker", "gamma-docker", "same words elsewhere",
       "Another tenant also bakes the zephyrine token, plus quokkaword.")
for i in range(15):
    memory(my, f"bulk_{i}", f"bulk-{i}", "bulk", f"marmalade item {i}")
team.record_decision("alpha", "Ship the tangerine cache behind a flag", "rollback is one toggle",
                     "bench: 40ms → 9ms", "ws_a")
team._exec("INSERT INTO team_decisions (workspace_id, session_id, decision, reason, ts) "
           "VALUES ('ws_b', 's-gamma', 'tangerine in tenant B', '', '2026-01-01')")


async def search(role, query):
    content = await signal_mcp.mcp.call_tool("search_knowledge", {"from_role": role, "query": query})
    if isinstance(content, tuple):    # newer mcp returns (content, structured)
        content = content[0]
    return "\n".join(c.text for c in content)


async def main():
    tool = next((t for t in await signal_mcp.mcp.list_tools() if t.name == "search_knowledge"), None)
    check("search_knowledge is registered", tool is not None)
    if tool is None:
        return
    check("the schema takes only from_role and query",
          set(tool.inputSchema["properties"]) == {"from_role", "query"}, str(tool.inputSchema))

    r = await search("alpha", "registry")
    print("--- alpha / registry\n" + r + "\n---")
    check("a word only beta's memory has: owner beta and the file path",
          "owner: beta" in r and str(my / "beta_docker.md") in r, r)

    r = await search("alpha", "zephyrine")
    print("--- alpha / zephyrine\n" + r + "\n---")
    check("alpha finds beta's memory, owned by beta",
          "owner: beta" in r and str(my / "beta_docker.md") in r and "docker-secrets" in r, r)
    check("gamma's memory in the other workspace never appears",
          "gamma" not in r and "Another tenant" not in r, r)
    check("MEMORY.md (the index) is skipped", "index line" not in r, r)

    r = await search("alpha", "quokkaword")
    print("--- alpha / quokkaword\n" + r + "\n---")
    check("a word only the other workspace has finds nothing", "No memory or decision" in r
          and "send_signal" in r and "gamma" not in r, r)

    r = await search("alpha", "TANGERINE flag")
    print("--- alpha / tangerine flag\n" + r + "\n---")
    check("the recorded decision is found, with its owner",
          "[decision] owner: alpha" in r and "tangerine cache" in r and "rollback" in r, r)
    check("the other workspace's decision is not", "tenant B" not in r, r)

    r = await search("alpha", "zephyrine flag")
    check("every query word must match", "No memory or decision" in r, r)

    r = await search("alpha", "marmalade")
    check("15 matching files give at most 10 results", r.count("[memory]") == 10, str(r.count("[memory]")))
    check("snippets stay short", all(len(line) <= 310 for line in r.splitlines()), max(map(len, r.splitlines())))

    names = ("alpha", "beta", "gamma", "dupname")
    for role, why in (("nobody", "an unknown role"), ("dupname", "a name in two workspaces")):
        r = await search(role, "zephyrine")
        print(f"--- {role} / zephyrine\n" + r + "\n---")
        check(f"{why} is refused with no data", r == signal_mcp._NO_WORKSPACE, r)
        check(f"{why} leaks no agent name", not any(n in r for n in names), r)

    r = await search("alpha", "   ")
    check("an empty query is refused", r.startswith("⛔"), r)

    signal_mcp._INPROC = False
    r = await search("alpha", "zephyrine")
    check("outside the orchestrator process it refuses like the team tools",
          "built-in signal server" in r, r)


signal_mcp._INPROC = True
asyncio.run(main())
for ext in ("", "-wal", "-shm"):
    Path(str(db) + ext).unlink(missing_ok=True)
shutil.rmtree(TMP, ignore_errors=True)

if fails:
    print("\n" + "\n".join(fails))
    sys.exit(1)
print("\nall knowledge checks passed")
