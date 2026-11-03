#!/usr/bin/env python3
"""Guards for the director's team — sub-agents, the verifier gate, the handoff graph, reflection.

Runs the real state machine against a throwaway database, a real git repo and scripted fake CLIs.
Every way it can go wrong is quiet, which is why each one is checked here:
  - a sub-agent's work counted as done because its CLI exited 0, with no verifier ever looking;
  - the builder grading itself: the verifier must be a FRESH session every attempt;
  - a verifier that edits the worktree and then says PASS — the edit must be undone and the
    verdict thrown away, or "verified" means nothing;
  - a PASS with no evidence, or a verdict from someone who is not the current verifier;
  - work left uncommitted: the verdict must be tied to a commit, so the branch that gets merged is
    the one that was checked;
  - the sub-agent's work leaking into the user's main checkout, or its SKILL folder into a commit;
  - a sub-agent sending signals around the orchestrator, or an agent routing around the graph;
  - a loop that never ends: FAIL → fix → FAIL must stop at the attempt cap and tell the director;
  - reflection writing a playbook without a human approving it, or over a newer version.

    python3 check_team.py
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

TMP = Path(tempfile.mkdtemp(prefix="check_team_"))
BIN = TMP / "bin"
BIN.mkdir()
for name in ("claude", "agy"):            # both installed: the team must still pick Claude on its own
    f = BIN / (name + (".exe" if os.name == "nt" else ""))
    f.write_bytes(b"")
    f.chmod(0o755)
os.environ.update({
    "ORCH_DB": "check_team", "ORCH_WORKSPACES_ROOT": str(TMP / "ws"), "ORCH_DRY_RUN": "1",
    "CLAUDE_BIN": str(BIN / "claude"), "ORCH_AGY_BIN": str(BIN / ("agy" + (".exe" if os.name == "nt" else ""))),
    "ORCH_SUBTASK_MAX_ATTEMPTS": "3",
})
os.environ["CLAUDE_BIN"] = str(BIN / ("claude" + (".exe" if os.name == "nt" else "")))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import session_orchestrator as so   # noqa: E402
import signal_mcp                   # noqa: E402
import team                         # noqa: E402

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


db = so.DB_DIR / "check_team.db"
for ext in ("", "-wal", "-shm"):
    Path(str(db) + ext).unlink(missing_ok=True)
so.init_db()
team._worktrees_root = lambda: TMP / "worktrees"     # never under the real ~/.session_orch_db
signal_mcp._INPROC = True


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()


REPO = TMP / "repo"
REPO.mkdir()
git("init", "-q", "-b", "main", cwd=REPO)
git("config", "user.email", "dev@example.com", cwd=REPO)
git("config", "user.name", "Dev", cwd=REPO)
(REPO / "app.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
git("add", "-A", cwd=REPO)
git("commit", "-q", "-m", "init", cwd=REPO)
BASE = git("rev-parse", "HEAD", cwd=REPO)

WS = so.DEFAULT_WORKSPACE
so.register_session("dir-1", "lead", cwd=str(REPO), model="", workspace_id=WS)
so.register_session("peer-1", "peer", cwd=str(REPO), model="", workspace_id=WS)
so.register_session("peer-2", "other", cwd=str(REPO), model="", workspace_id=WS)

# ── Scripted fake CLIs ───────────────────────────────────────────────────────
# Each verifier session looks up what to do from VERIFY_PLAN[subtask] (one entry per attempt).
VERIFY_PLAN, BUILD_PLAN, SEEN = {}, {}, []


def _key_in(prompt):
    i = prompt.find('key="')
    return prompt[i + 5:prompt.index('"', i + 5)] if i >= 0 else ""


def _st_in(prompt, tag):
    i = prompt.find(f"[{tag} ")
    return prompt[i + len(tag) + 2:].split()[0].rstrip("]") if i >= 0 else ""


async def fake_run(session, prompt, on_event=None, dry_run=False):
    SEEN.append((session["name"], prompt))
    cwd = Path(session.get("cwd") or ".")
    name = session["name"]
    # A fake that writes must never touch anything outside this check's temp folder — not even
    # when the code under test hands it an empty cwd (that once committed into the real repo).
    if TMP.resolve() not in cwd.resolve().parents:     # both resolved: Windows temp is 8.3-short
        return {"ok": False, "result": f"refusing to write outside the check's temp folder: {cwd}",
                "session_id": session["id"], "tokens": 0, "raw": {}}
    if "[SUB-AGENT TASK " in prompt or "[VERIFIER FINDINGS " in prompt:
        st = _st_in(prompt, "SUB-AGENT TASK") or _st_in(prompt, "VERIFIER FINDINGS")
        step = BUILD_PLAN.get(st, ["commit"])
        act = step.pop(0) if len(step) > 1 else step[0]
        if act in ("commit", "dirty"):
            (cwd / "feature.py").write_text(f"VALUE = {len(SEEN)}\n", encoding="utf-8")
            if act == "commit":
                git("add", "feature.py", cwd=cwd)
                git("commit", "-q", "-m", "feature", cwd=cwd)
        if act == "crash":
            return {"ok": False, "result": "CLI died", "session_id": session["id"], "tokens": 0, "raw": {}}
        return {"ok": True, "result": f"Changed feature.py. Evidence: ran it ({act}).",
                "session_id": session["id"], "tokens": 7, "raw": {}}
    if "[VERIFY " in prompt:
        st = _st_in(prompt, "VERIFY")
        plan = VERIFY_PLAN.get(st, ["PASS"])
        act = plan.pop(0) if len(plan) > 1 else plan[0]
        if act == "tamper":
            (cwd / "feature.py").write_text("VALUE = 'patched by the verifier'\n", encoding="utf-8")
            team.submit_verdict(name, st, "PASS", "looked fine", key=_key_in(prompt))
        elif act == "silent":
            pass
        elif act == "PASS":
            team.submit_verdict(name, st, "PASS", "ran python -c 'import feature' → ok", key=_key_in(prompt))
        else:
            team.submit_verdict(name, st, "FAIL", "", "feature.py has the wrong VALUE", key=_key_in(prompt))
        return {"ok": True, "result": "verified", "session_id": session["id"], "tokens": 3, "raw": {}}
    if "[REFLECT " in prompt:
        st = _st_in(prompt, "REFLECT")
        res = team.propose_skill_change(name, st, PROPOSAL_TEXT, "pattern → sharper rule",
                                        key=_key_in(prompt))
        SEEN.append(("reflect-result", res))
        return {"ok": True, "result": "proposed", "session_id": session["id"], "tokens": 2, "raw": {}}
    return {"ok": True, "result": "noted", "session_id": session["id"], "tokens": 1, "raw": {}}


for eng in so.ENGINES.values():
    eng.run = fake_run
PROPOSAL_TEXT = "---\nname: <ROLE_NAME>\ndescription: x\n---\n# better feature playbook\n"


async def drain(rounds=200):
    for _ in range(rounds):
        sigs = so.eligible_signals()
        if sigs:
            for s in sigs:
                await so.process_signal(s)
            continue
        if team._bg:
            await asyncio.gather(*list(team._bg), return_exceptions=True)
            continue
        return


def to_lead():
    return [p for n, p in SEEN if n == "lead"]


def session_of(sid):
    return so.get_session(sid) or {}


async def main():
    check("team.TEAM_SENDER matches the orchestrator's", team.TEAM_SENDER == so.TEAM_SENDER)

    # ── Validation ───────────────────────────────────────────────────────────
    r = team.spawn_subagent("dir-1", "feature", "do it", "")
    check("a sub-agent needs a done_when contract", "done_when" in r.get("error", ""), str(r))
    r = team.spawn_subagent("dir-1", "deploy", "do it", "x")
    check("an unknown playbook is refused", "playbook" in r.get("error", ""), str(r))

    # ── Happy path with one fix loop ─────────────────────────────────────────
    r = team.spawn_subagent("dir-1", "feature", "Add feature.py with VALUE", "python -c 'import feature' prints nothing",
                            title="add feature")
    st_id = r.get("id", "")
    check("spawn returns at once, queued", r.get("status") == "queued", str(r))
    VERIFY_PLAN[st_id] = ["FAIL", "PASS"]
    BUILD_PLAN[st_id] = ["dirty", "commit"]
    await drain()
    st = team.get_subtask(st_id)
    check("the feature subtask passed after one fix", st["status"] == "passed",
          f"{st['status']} {st['reason']}")
    check("one failed attempt was counted", st["attempts"] == 1, str(st["attempts"]))
    wt = Path(st["worktree"])
    check("it worked in its own worktree, outside the repo",
          wt.is_dir() and REPO not in wt.parents and st["branch"] == f"orch/{st_id}", st["worktree"])
    check("uncommitted work was committed before verification",
          st["head_sha"] and st["head_sha"] != BASE and not team.is_dirty(str(wt)), st["head_sha"])
    check("the user's main checkout is untouched",
          not (REPO / "feature.py").exists() and git("rev-parse", "HEAD", cwd=REPO) == BASE)
    check("the branch holds the verified commit",
          git("rev-parse", st["branch"], cwd=REPO) == st["head_sha"])
    committed = git("show", "--name-only", "--format=", st["head_sha"], cwd=REPO)
    committed += "\n" + git("log", "--name-only", "--format=", f"{BASE}..{st['branch']}", cwd=REPO)
    check("no SKILL folder of the team got committed", ".claude" not in committed and "skills" not in committed,
          committed)
    verifiers = [s for s in so.list_sessions() if s.get("team_role") == "verifier"
                 and s["name"].startswith(f"sub-{st_id[3:]}")]
    check("each verification ran in a fresh verifier session", len(verifiers) == 2,
          str([v["name"] for v in verifiers]))
    check("builder and verifiers run on Claude even with agy installed",
          all(so.engine_name_of_session(v) == "claude" for v in verifiers)
          and so.engine_name_of_session(session_of(st["session_id"])) == "claude",
          str([(v["name"], v["model"]) for v in verifiers]))
    fix = [p for n, p in SEEN if n == session_of(st["session_id"]).get("name") and "VERIFIER FINDINGS" in p]
    check("the builder got the verifier's findings", fix and "wrong VALUE" in fix[0])
    passed = [p for p in to_lead() if f"[SUB-AGENT PASSED] {st_id}" in p]
    check("the director was told it passed, with the branch to merge",
          passed and f"git merge --no-ff orch/{st_id}" in passed[0], str(to_lead())[:300])
    check("team sessions stay off the canvas and out of list_agents",
          "sub-" not in await signal_mcp.list_agents(from_role="lead"))

    # ── PASS rules ───────────────────────────────────────────────────────────
    r = team.submit_verdict(verifiers[-1]["name"], st_id, "PASS", "late")
    check("a verdict outside verification is refused", r.startswith("⛔"), r)

    r = team.spawn_subagent("dir-1", "bugfix", "fix it", "feature.py exists", title="tamper")
    st2 = r["id"]
    VERIFY_PLAN[st2] = ["tamper", "PASS"]
    await drain()
    st = team.get_subtask(st2)
    check("a verifier that edits the worktree does not get its PASS",
          st["status"] == "passed" and st["attempts"] == 1, f"{st['status']} {st['attempts']}")
    check("and its edit was undone",
          "patched by the verifier" not in (Path(st["worktree"]) / "feature.py").read_text(encoding="utf-8"))
    builder_name = session_of(st["session_id"]).get("name")
    check("the builder was not blamed for the verifier's tampering",
          not any(n == builder_name and "VERIFIER FINDINGS" in p for n, p in SEEN))

    # Fake a live verification to check the verdict gate directly.
    team._update(st2, status="verifying", run_key="k3y")
    vname = session_of(st["verifier_id"]).get("name")
    r = team.submit_verdict(vname, st2, "PASS", "", key="k3y")
    check("a PASS without evidence is refused", r.startswith("⛔") and "evidence" in r, r)
    r = team.submit_verdict("someone-else", st2, "PASS", "ok", key="k3y")
    check("only the current verifier may submit", r.startswith("⛔"), r)
    r = team.submit_verdict(vname, st2, "PASS", "ok", key="guess")
    check("knowing the verifier's (predictable) name is not enough without its key",
          r.startswith("⛔") and "key" in r, r)
    team._update(st2, status="passed", run_key="")

    # ── The loop ends ────────────────────────────────────────────────────────
    r = team.spawn_subagent("dir-1", "feature", "never good enough", "impossible", title="doomed")
    st3 = r["id"]
    VERIFY_PLAN[st3] = ["FAIL"]
    await drain()
    st = team.get_subtask(st3)
    check("FAIL → fix → FAIL stops at the attempt cap",
          st["status"] == "failed" and st["attempts"] == team.MAX_ATTEMPTS, f"{st['status']} {st['attempts']}")
    check("the director was told it failed, with the findings",
          any(f"[SUB-AGENT FAILED] {st3}" in p and "wrong VALUE" in p for p in to_lead()))

    r = team.spawn_subagent("dir-1", "feature", "x", "y", title="silent")
    st4 = r["id"]
    VERIFY_PLAN[st4] = ["silent", "silent", "silent"]
    await drain()
    st = team.get_subtask(st4)
    check("a verifier that never gives a verdict is replaced, then the subtask fails",
          st["status"] == "failed" and "submit_verdict" in st["reason"], f"{st['status']} {st['reason']}")

    r = team.spawn_subagent("dir-1", "feature", "x", "y", title="crash")
    BUILD_PLAN[r["id"]] = ["crash"]
    await drain()
    st = team.get_subtask(r["id"])
    check("a builder whose run fails ends the subtask (no verifier for nothing)",
          st["status"] == "failed" and "run failed" in st["reason"] and not st["verifier_id"],
          f"{st['status']} {st['reason']}")

    # ── Read-only investigation ──────────────────────────────────────────────
    r = team.spawn_subagent("dir-1", "investigate", "how does greet work?", "names the file and function")
    st5 = r["id"]
    BUILD_PLAN[st5] = ["read"]
    await drain()
    st = team.get_subtask(st5)
    check("an investigation works in the director's folder, no worktree",
          st["status"] == "passed" and not st["worktree"], f"{st['status']} {st['worktree']}")
    left = [p for p in (REPO / ".claude" / "skills").glob("sub-*")] if (REPO / ".claude" / "skills").exists() else []
    check("it leaves no SKILL folder behind in the user's repo", not left, str(left))
    check("and the user's repo is still clean", not git("status", "--porcelain", cwd=REPO),
          git("status", "--porcelain", cwd=REPO))

    # ── Nesting, signals, the graph ──────────────────────────────────────────
    sub = so.get_session(team.get_subtask(st_id)["session_id"])
    r = team.spawn_subagent(sub["id"], "feature", "x", "y")
    check("a sub-agent cannot spawn sub-agents", "cannot spawn" in r.get("error", ""), str(r))
    try:
        so.enqueue_signal("dir-1", "hello", sub["name"], workspace_id=WS)
        check("a sub-agent cannot send signals", False, "enqueued")
    except so.SignalRefused as e:
        check("a sub-agent cannot send signals", "Sub-agents do not send" in str(e), str(e))

    so.enqueue_signal("peer-2", "open graph", "peer", workspace_id=WS)
    check("an open graph lets agents signal anyone", True)
    team.set_graph_mode(WS, "enforced")
    try:
        so.enqueue_signal("peer-2", "blocked?", "lead", workspace_id=WS)
        check("an enforced graph refuses a missing edge", False, "enqueued")
    except so.SignalRefused as e:
        check("an enforced graph refuses a missing edge", "HANDOFF NOT ALLOWED" in str(e), str(e))
    team.add_edge(WS, "lead", "other")
    so.enqueue_signal("peer-2", "allowed now", "lead", workspace_id=WS)
    check("an edge lets it through", True)
    so.enqueue_signal("peer-2", "from a person", "user", workspace_id=WS)
    check("people are never stopped by the graph", True)
    ok, err = await signal_mcp._enqueue("other", "via mcp", "lead")
    check("the MCP send_signal goes through the same gate", ok, str(err))
    ok, err = await signal_mcp._enqueue("lead", "back?", "peer")
    check("…and returns the refusal text to the agent", not ok and "HANDOFF NOT ALLOWED" in str(err), str(err))
    learned = team.learn_edges(WS)
    check("learn_edges adds edges seen in real traffic", learned["added"] >= 1
          and any(e["from_role"] == "peer" and e["to_role"] == "other" for e in team.edges(WS)), str(learned))
    team.set_graph_mode(WS, "open")
    await drain()

    # ── Handoff with done_when ───────────────────────────────────────────────
    SEEN.clear()
    ok, data = await signal_mcp._enqueue("other", "write notes.txt", "peer", done_when="notes.txt exists")
    sig = so._conn().execute("SELECT * FROM signals WHERE id = ?", (data["id"],)).fetchone()
    check("a handoff tells the receiver a verifier will check, and not to report back",
          ok and "Done means" in sig["message"] and sig["expects_reply"] == 0, str(dict(sig)))
    hst = next(s for s in team._rows("SELECT * FROM subtasks WHERE kind = 'handoff'"))
    VERIFY_PLAN[hst["id"]] = ["PASS"]
    await drain()
    hst = team.get_subtask(hst["id"])
    check("the handoff was verified", hst["status"] == "passed", f"{hst['status']} {hst['reason']}")
    check("the sender got [HANDOFF VERIFIED]",
          any(n == "peer" and "[HANDOFF VERIFIED]" in p for n, p in SEEN), str([n for n, _ in SEEN]))

    # ── Cancel & discard ─────────────────────────────────────────────────────
    r = team.spawn_subagent("dir-1", "refactor", "x", "y", title="cancel me")
    st6 = r["id"]
    await asyncio.gather(*list(team._bg))           # spawned, brief queued, not run yet
    pending = team.get_subtask(st6)["signal_id"]
    team.cancel(st6, by_user=True)
    await drain()
    st = team.get_subtask(st6)
    status = so._conn().execute("SELECT status FROM signals WHERE id = ?", (pending,)).fetchone()["status"]
    check("cancel stops it before its brief runs", st["status"] == "cancelled" and status == "denied",
          f"{st['status']} {status}")
    check("the director hears about a cancel from the dashboard",
          any(f"[SUB-AGENT CANCELLED] {st6}" in p for p in to_lead()))
    wt = st["worktree"]
    team.discard(st6)
    check("discard removes the worktree and its branch",
          not Path(wt).exists() and not git("branch", "--list", f"orch/{st6}", cwd=REPO))

    # ── Reflection ───────────────────────────────────────────────────────────
    r = team.start_reflect("playbook:feature", "dir-1")
    rid = r.get("id", "")
    await drain()
    rst = team.get_subtask(rid) if rid else {}
    props = team._rows("SELECT * FROM skill_proposals WHERE subtask_id = ?", (rid,))
    check("reflection on a playbook with failures produces one proposal",
          rst.get("status") == "passed" and len(props) == 1, f"{r} {rst.get('status')} {rst.get('reason')}")
    override = team.override_path(str(REPO), "feature")
    check("nothing is written before a human approves", not override.exists())
    res = team.decide_proposal(props[0]["id"], True) if props else {"error": "no proposal"}
    check("approving writes the repo's playbook override",
          res.get("ok") and override.read_text(encoding="utf-8") == PROPOSAL_TEXT, str(res))
    check("new sub-agents use the approved playbook",
          team.playbook_text("feature", str(REPO)) == PROPOSAL_TEXT)
    r2 = team.start_reflect("playbook:feature", "dir-1")
    await drain()
    p2 = team._rows("SELECT * FROM skill_proposals WHERE subtask_id = ?", (r2.get("id", ""),))
    note = [res for n, res in SEEN if n == "reflect-result"][-1]
    check("a proposal identical to the current text is refused", not p2 and "identical" in note, note)
    override.write_text("changed by hand\n", encoding="utf-8")
    pid = team._exec("INSERT INTO skill_proposals (workspace_id, subtask_id, target, path, old_text, "
                     "new_text, created_at) VALUES (?, '', 'playbook:feature', ?, ?, 'x', ?)",
                     (WS, str(override), PROPOSAL_TEXT, so._now()))
    res = team.decide_proposal(pid, True)
    check("a proposal over a newer version is refused", "changed since" in res.get("error", ""), str(res))
    r = team.start_reflect("role:peer-2", "peer-2")
    check("reflection with no evidence of failure is refused", "nothing has gone wrong" in r.get("error", "")
          or "no SKILL" in r.get("error", ""), str(r))


    # ── Review findings: no stuck subtasks, no overwritten cancels, read-only means read-only ──
    def clean_repo():
        git("checkout", "-q", "--", ".", cwd=REPO)
        git("clean", "-qfd", cwd=REPO)

    r = team.spawn_subagent("dir-1", "investigate", "look only", "names a file")
    BUILD_PLAN[r["id"]] = ["dirty"]
    await drain()
    st = team.get_subtask(r["id"])
    check("a read-only sub-agent that writes in your folder is failed, with the file named",
          st["status"] == "failed" and "read-only" in st["reason"] and "feature.py" in st["reason"],
          f"{st['status']} {st['reason']}")
    clean_repo()

    SEEN.clear()
    ok, data = await signal_mcp._enqueue("other", "tidy notes", "peer", done_when="notes are tidy")
    hid = team._row("SELECT id FROM subtasks WHERE signal_id = ?", (data["id"],))["id"]
    VERIFY_PLAN[hid] = ["tamper", "PASS"]
    await drain()
    st = team.get_subtask(hid)
    check("a verifier that edits the user's real folder (no worktree) loses its verdict",
          st["status"] == "passed" and st["attempts"] == 1, f"{st['status']} {st['attempts']}")
    check("…and is not silently undone in the user's folder (it is reported instead)",
          (REPO / "feature.py").exists())
    clean_repo()

    gate = asyncio.Event()
    real_spawn = so.ENGINES["claude"].spawn

    async def slow_spawn(*a, **kw):
        await gate.wait()
        return await real_spawn(*a, **kw)
    so.ENGINES["claude"].spawn = slow_spawn
    SEEN.clear()
    r = team.spawn_subagent("dir-1", "feature", "x", "y", title="cancel mid-spawn")
    for _ in range(50):
        await asyncio.sleep(0.02)
        if team.get_subtask(r["id"])["worktree"]:
            break
    team.cancel(r["id"], by_user=True)
    gate.set()
    await drain()
    so.ENGINES["claude"].spawn = real_spawn
    st = team.get_subtask(r["id"])
    check("a cancel during spawning stays cancelled", st["status"] == "cancelled", st["status"])
    check("…the brief never runs and the director hears only 'cancelled'",
          not any(f"[SUB-AGENT TASK {r['id']}" in p for _, p in SEEN)
          and not any(f"[SUB-AGENT FAILED] {r['id']}" in p for p in to_lead()))

    real_mk = team.make_worktree
    team.make_worktree = lambda repo, st_id: ("", "", "")
    r = team.spawn_subagent("dir-1", "feature", "x", "y")
    await drain()
    st = team.get_subtask(r["id"])
    check("no worktree in a git repo → fail, never fall back to your checkout",
          st["status"] == "failed" and "worktree" in st["reason"] and not st["session_id"],
          f"{st['status']} {st['reason']} {st['session_id']}")

    def boom(repo, st_id):
        raise RuntimeError("disk full")
    team.make_worktree = boom
    r = team.spawn_subagent("dir-1", "feature", "x", "y")
    await drain()
    team.make_worktree = real_mk
    st = team.get_subtask(r["id"])
    check("an exception in a background step fails the subtask instead of leaving it queued",
          st["status"] == "failed" and "disk full" in st["reason"], f"{st['status']} {st['reason']}")

    so.note_human_touch("peer", WS)        # a person hands out new work: the pair cap resets
    sid = so.enqueue_signal("peer-2", team.handoff_message("needs a yes", "x"), "peer", 1, 0, WS,
                            expects_reply=0)
    hst = team.create_handoff("peer-1", "peer-2", sid, "x", "needs a yes")
    import httpx
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        await c.post(f"/api/signals/{sid}/deny")
        st = team.get_subtask(hst)
        check("denying a subtask's waiting signal ends the subtask", st["status"] == "failed"
              and "denied" in st["reason"], f"{st['status']} {st['reason']}")

        dir_name = so.get_session("dir-1")["name"]
        r = await c.post("/api/sessions/peer-2/name", json={"name": "team"})
        check("an agent cannot be renamed to a reserved name", r.status_code == 400, r.text)
        r = await c.post("/api/signals", json={"to_role": dir_name, "from_role": "team",
                                               "message": "pretend", "workspace_id": WS})
        check("the API refuses a caller posing as the orchestrator", r.status_code == 400, r.text)
        so.register_session("old-team", "team", cwd=str(REPO), workspace_id=WS)
        r = await c.post("/api/sessions", json={"id": "old-team", "name": "team", "cwd": str(REPO),
                                                "workspace_id": WS})
        check("an agent that already had a reserved name can still re-register", r.status_code == 200,
              r.text[:200])
        team.set_graph_mode(WS, "enforced")
        try:
            so.enqueue_signal("peer-2", "hi", "team", workspace_id=WS)
            check("a real agent named 'team' gets no system exemption", False, "enqueued")
        except so.SignalRefused as e:
            check("a real agent named 'team' gets no system exemption", "HANDOFF NOT ALLOWED" in str(e), str(e))
        team.set_graph_mode(WS, "open")
        so.unregister_session("old-team")

    # Restart: a queued subtask has lost its task; a verifying one with no live verifier is re-checked.
    now = so._now()
    for sid_, stat in (("st_aaaaaaa1", "queued"), ("st_aaaaaaa2", "verifying")):
        team._exec("INSERT INTO subtasks (id, workspace_id, director_id, session_id, kind, playbook, "
                   "brief, done_when, status, created_at, updated_at) VALUES (?, ?, 'dir-1', 'peer-2', "
                   "'handoff', 'handoff', 'x', 'y', ?, ?, ?)", (sid_, WS, stat, now, now))
    VERIFY_PLAN["st_aaaaaaa2"] = ["PASS"]
    team.reconcile()
    await drain()
    check("after a restart a queued subtask is ended, not left hanging",
          team.get_subtask("st_aaaaaaa1")["status"] == "failed")
    check("after a restart an unfinished verification is run again",
          team.get_subtask("st_aaaaaaa2")["status"] == "passed",
          team.get_subtask("st_aaaaaaa2")["status"])

    before = team.get_subtask(st3)["attempts"]
    VERIFY_PLAN[st3] = ["PASS"]
    r1 = await team.reverify(st3)
    r2 = await team.reverify(st3)
    await drain()
    st = team.get_subtask(st3)
    check("Verify again runs one extra check and keeps the real failure count",
          r1.get("ok") and st["status"] == "passed" and st["attempts"] == before, f"{st['status']} {st['attempts']}")
    check("clicking Verify again twice does not start two verifiers", "error" in r2, str(r2))

    # ── Decisions, overview, API ─────────────────────────────────────────────
    res = await signal_mcp.record_decision(from_role="lead", decision="split into two units",
                                           reason="they touch different files")
    check("decisions are recorded", res == "Decision recorded.", res)
    ov = team.overview(WS)
    check("the overview lists directors, subtasks, decisions, proposals and the graph",
          any(d["name"] == "lead" for d in ov["directors"]) and len(ov["subtasks"]) >= 6
          and ov["decisions"] and ov["proposals"] and "graph_mode" in ov,
          json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in ov.items()})[:300])
    check("team sessions are not listed as directors",
          not any(d["name"].startswith("sub-") for d in ov["directors"]))

    import httpx
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/team", params={"workspace_id": WS})
        check("GET /api/team answers", r.status_code == 200 and r.json()["subtasks"], r.text[:200])
        r = await c.get(f"/api/team/subtasks/{st_id}/diff")
        check("the diff of a passed subtask is served", r.status_code == 200 and "feature.py" in r.json()["diff"],
              r.text[:200])
        r = await c.get("/api/sessions", params={"workspace_id": WS})
        flags = {s["name"]: bool(s.get("team")) for s in r.json()}
        check("/api/sessions flags team sessions for the dashboard",
              not flags["lead"] and all(v for k, v in flags.items() if k.startswith("sub-")), str(flags))
        # Panel của director trên canvas: đội của ĐÚNG agent đó, không phải cả workspace.
        lead = next(x for x in r.json() if x["name"] == "lead")
        mine = team._rows("SELECT id, status FROM subtasks WHERE director_id = 'dir-1' AND kind != 'reflect'")
        check("/api/sessions tells the dashboard which agent has a team",
              (lead.get("subtasks") or {}).get("total") == len(mine)
              and not next(x for x in r.json() if x["name"] == "other").get("subtasks"), str(lead.get("subtasks")))
        check("the team's own messages are not shown as a ping-pong pair",
              not any("team" in pair for pair in so.pair_counts(WS)), str(so.pair_counts(WS)))
        r = await c.get("/api/team/director/dir-1")
        got = r.json() if r.status_code == 200 else {}
        ids = {x["id"] for x in got.get("subtasks", [])}
        check("a director's panel lists its own sub-agents, and only those",
              r.status_code == 200 and ids and all(x["director_id"] == "dir-1" for x in got["subtasks"])
              and ids <= {m["id"] for m in mine} and got["total"] == len(mine), r.text[:200])
        active = {m["id"] for m in mine if m["status"] in team.ACTIVE}
        check("…every live one is there, however many finished ones there are", active <= ids,
              f"missing {active - ids}")
        few = team.director_view("dir-1", limit=1)
        check("…and only the most recent finished ones",
              len([x for x in few["subtasks"] if x["status"] not in team.ACTIVE]) <= 1, str(len(few["subtasks"])))
        check("an unknown director is a 404",
              (await c.get("/api/team/director/nobody")).status_code == 404)
        r = await c.post("/api/sessions/spawn", json={"name": "sub-sneaky", "workspace_id": WS})
        check("agents cannot take a team name", r.status_code == 400, r.text)
        r = await c.post("/api/team/models", json={"workspace_id": WS, "roles": {"verify": "opus"}})
        check("model roles can be changed", r.json().get("verify") == "opus", r.text)
        # Claude trừ khi NGƯỜI DÙNG chọn Gemini cho vai đó; director chạy agy không kéo đội theo.
        gem = {"model": "agy:gemini-3.1-pro-high"}
        check("a director on Gemini still gets Claude sub-agents",
              team.resolve_model(WS, "implement", gem) == "", team.resolve_model(WS, "implement", gem))
        check("a director on Claude passes its model down",
              team.resolve_model(WS, "implement", {"model": "sonnet"}) == "sonnet")
        team.set_model_roles(WS, {"investigate": "agy"})
        check("Gemini runs when the user picks it for a role",
              team.resolve_model(WS, "investigate", {"model": "opus"}) == "agy")
        so.set_setting(f"team_models:{WS}", json.dumps({"verify": "@other"}))
        check("an old '@other' setting falls back to Claude", team.model_roles(WS)["verify"] == "", team.model_roles(WS))
        so.set_setting(f"team_models:{WS}", "{}")
        res = await signal_mcp.spawn_subagent(from_role="lead", playbook="investigate", brief="b",
                                              done_when="d", model="agy:gemini-3.1-pro-high")
        check("a director cannot pick Gemini for a sub-agent", res.startswith("⛔") and "Claude" in res, res)
        r = await c.post("/api/signals", json={"to_role": "other", "from_role": "lead",
                                               "message": "x", "done_when": "y", "workspace_id": WS})
        check("POST /api/signals accepts done_when", r.status_code == 200, r.text)
        for path in ("/api/stats", "/api/available-tools"):
            r = await c.get(path)
            check(f"{path} is gone", r.status_code != 200 or "<title>" in r.text, str(r.status_code))
    await drain()

    team.on_session_removed("dir-1")
    check("removing a director cancels its live work",
          not team._rows("SELECT 1 FROM subtasks WHERE director_id = 'dir-1' AND status IN "
                         "('queued','running','verifying','fixing')"))


try:
    asyncio.run(main())
finally:
    for st in team._rows("SELECT * FROM subtasks"):
        team.remove_worktree(st)
    shutil.rmtree(TMP, ignore_errors=True)
    for ext in ("", "-wal", "-shm"):
        Path(str(db) + ext).unlink(missing_ok=True)

if fails:
    print(f"\n{len(fails)} FAILED:\n  " + "\n  ".join(fails))
    sys.exit(1)
print("\nall team checks passed")
