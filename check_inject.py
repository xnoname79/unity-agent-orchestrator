#!/usr/bin/env python3
"""Guards for the prompt a signal actually becomes.

Every signal is wrapped: role header, the role's SKILL, the peer/timing/reply rules, then the
message. That wrapper is what keeps a long-running agent in character, so it must stay on.

The exception is the whole point of this file. A CLI runs a slash command only when the prompt IS
that command. Wrap `/compact` and it lands at the bottom of a kilobyte of text, where it is just
text: the CLI reads it, answers it, and the transcript GROWS instead of shrinking.

Nothing shows it. The run still ends `ok` — the CLI answered a question, no error happened — and
`runs.prompt` stores the RAW `signal["message"]`, not the wrapped prompt (see start_run in the run
loop). What the CLI actually received is recorded nowhere, so there is no audit trail to compare
against and the guard has to look straight at inject_prompt.

So two mistakes have to stay impossible, and they pull in opposite directions:

  - wrapping a real slash command (the regression that started at 608d8db);
  - treating an ordinary message as a command and dropping the role with it. `/home/me/project`
    and `run /compact when done` are messages, not commands — an agent that silently loses its
    playbook for a turn is the harder failure to notice of the two.

Only claude is let through. What codex and agy do with `/compact` has not been measured here, so
they keep the old path exactly: trading a known bug for an unmeasured one is not a fix.

    python3 check_inject.py
"""
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
os.environ["ORCH_DB"] = "check_inject"
os.environ["ORCH_DRY_RUN"] = "1"

import session_orchestrator as so     # noqa: E402

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


tmp = Path(tempfile.mkdtemp())
ROLE = "backend-dev"
SKILL_MARK = "PLAYBOOK-MARKER-9f2a"
# Viết SKILL qua đường dẫn THẬT của app: test tự dựng layout thì nó lệch lúc layout đổi, và lệch
# theo kiểu vẫn xanh.
skill = Path(so._skill_path(str(tmp), ROLE))
skill.parent.mkdir(parents=True, exist_ok=True)
skill.write_text(f"# {ROLE}\n\n{SKILL_MARK}\n", encoding="utf-8")

CLAUDE = {"id": "s1", "name": ROLE, "cwd": str(tmp), "model": ""}
CODEX = {"id": "s2", "name": ROLE, "cwd": str(tmp), "model": "codex:gpt-5.6-terra"}
AGY = {"id": "s3", "name": ROLE, "cwd": str(tmp), "model": "agy:gemini-3.1-pro-high"}

check("the three fixtures really are the engines this test thinks they are",
      (so.engine_name_of_session(CLAUDE), so.engine_name_of_session(CODEX),
       so.engine_name_of_session(AGY)) == ("claude", "codex", "agy"),
      str([so.engine_name_of_session(s) for s in (CLAUDE, CODEX, AGY)]))


# ── a slash command reaches the CLI untouched ────────────────────────────────
out = so.inject_prompt(CLAUDE, "/compact", "orch")
check("/compact arrives as exactly that and nothing else", out == "/compact", repr(out)[:120])

out = so.inject_prompt(CLAUDE, "/compact keep the API contract, drop debug logs", "orch")
check("its argument rides along, still unwrapped",
      out == "/compact keep the API contract, drop debug logs", repr(out)[:120])

out = so.inject_prompt(CLAUDE, "  /compact\n", "orch")
check("stray whitespace is trimmed — the CLI wants the bare command",
      out == "/compact", repr(out)[:80])

check("a wrapped command would not even start with the command any more",
      not so._prepend_role(str(tmp), ROLE, "/compact", "orch").startswith("/compact"),
      "that is the shape that made it plain text")


# ── an ordinary message keeps the whole wrapper ──────────────────────────────
out = so.inject_prompt(CLAUDE, "please review the auth middleware", "frontend-dev")
check("a normal signal still opens with the role header",
      out.startswith(f"[Role: {ROLE}]"), repr(out[:60]))
check("and still carries the role's SKILL", SKILL_MARK in out,
      "the playbook is what keeps a long session in character")
check("and still names who sent it", "[Signal from: frontend-dev]" in out, repr(out[:90]))
check("and still ends with the message itself",
      out.rstrip().endswith("please review the auth middleware"), repr(out[-60:]))


# ── things that only LOOK like commands must keep the wrapper ────────────────
for msg, why in (
    ("/home/thanh/my-mcp", "an absolute path"),
    ("/home/thanh/my-mcp please read the readme there", "a path with a sentence after it"),
    ("run /compact when done", "a command mentioned mid-sentence"),
    ("/", "a lone slash"),
    ("//comment", "a double slash"),
    ("", "an empty message"),
):
    out = so.inject_prompt(CLAUDE, msg, "orch")
    check(f"{why} is a message, not a command — it keeps its role block",
          out.startswith(f"[Role: {ROLE}]") and SKILL_MARK in out, repr(out[:70]))


# ── codex and agy are deliberately left on the old path ──────────────────────
for sess, cli in ((CODEX, "codex"), (AGY, "agy")):
    out = so.inject_prompt(sess, "/compact", "orch")
    check(f"{cli} keeps the wrapper — its behaviour here was never measured",
          out.startswith(f"[Role: {ROLE}]"),
          f"do not let {cli} through until someone has actually run /compact on it")


# ── the discriminator on its own ─────────────────────────────────────────────
for msg, want in (("/compact", True), ("/compact x", True), ("/help", True),
                  ("/a-b_c", True), ("/home/x", False), ("run /compact", False),
                  ("/", False), ("//x", False), ("hello", False), ("", False)):
    got = so._is_slash_cmd(msg)
    check(f"_is_slash_cmd({msg!r}) is {want}", got == want, f"got {got}")


# ── the run loop must actually GO THROUGH inject_prompt ──────────────────────
# Testing the function alone is not enough: the bug this file exists for WAS the call site, which
# wrapped everything it was handed. So capture what the engine really receives. Nothing else
# records it — runs.prompt keeps the raw message, not this.
import asyncio                        # noqa: E402

db = so.DB_DIR / "check_inject.db"
db.unlink(missing_ok=True)
so._ensure_db()
so.register_session("inject-1", ROLE, cwd=str(tmp), model="")

seen = []
_real_run = so.ENGINES["claude"].run


async def _spy(session, prompt, on_event=None, dry_run=False):
    seen.append(prompt)
    return {"ok": True, "result": "", "session_id": session["id"], "tokens": 0, "raw": {}}


so.ENGINES["claude"].run = _spy
try:
    # to_session là ID, không phải tên vai (get_session tra theo id) — gửi tên vai thì signal
    # 'failed: no session', mà đường thất bại ĐÓ vẫn ghi một run với prompt thô, nên một test bất
    # cẩn vẫn thấy "/compact" trong bảng runs và xanh trong khi engine chưa hề được gọi.
    outcome = [so.enqueue_signal("inject-1", "/compact", from_session="orch"),
               so.enqueue_signal("inject-1", "please review the auth middleware",
                                 from_session="orch")]
    done = asyncio.run(so.process_pending())
finally:
    so.ENGINES["claude"].run = _real_run

check("both signals actually reached a session",
      len(done) == 2 and all(d.get("status") != "failed" for d in done), str(done)[:160])
check("the run loop hands a slash command to the CLI bare", seen[:1] == ["/compact"],
      f"engine was given {seen[:1]!r} — the call site is wrapping it again")
check("and wraps an ordinary message on that very same path",
      len(seen) > 1 and seen[1].startswith(f"[Role: {ROLE}]") and SKILL_MARK in seen[1],
      repr(seen[1:2])[:120])

import shutil                         # noqa: E402
db.unlink(missing_ok=True)
shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print("\n" + "\n".join(fails))
    sys.exit(1)
print("\nall inject checks passed")
