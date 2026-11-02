"""Đội của một director — sub-agent kiểu pstack (Lauren Tan): đi sâu trước, mỗi đơn vị việc có
một chủ, mỗi chủ một git worktree, và KHÔNG có gì được tính là xong trước khi một verifier độc lập
quan sát được hợp đồng "Done means".

Vòng đời một subtask (bảng subtasks):

    queued → running → verifying ─PASS→ passed
                ↑          │
                └─ fixing ←┘ FAIL (còn lượt)        FAIL (hết lượt) → failed
    (bất kỳ lúc nào) → cancelled

  - running   : sub-agent làm việc trong worktree của nó (hoặc thư mục director nếu chỉ đọc)
  - verifying : một phiên verifier MỚI (mỗi lần kiểm một phiên, không ai chấm bài của chính mình)
                kiểm hợp đồng và gọi submit_verdict. Verifier sửa file → phán quyết bị huỷ.
  - fixing    : phát hiện của verifier quay về đúng sub-agent đó, rồi kiểm lại bằng verifier mới.

Ba loại subtask cùng một máy trạng thái:
  - subagent : director gọi spawn_subagent → orchestrator dựng sub-agent riêng.
  - handoff  : agent thường gửi send_signal kèm done_when → đích làm, verifier kiểm, rồi mới báo.
  - reflect  : đọc lỗi cũ của một playbook, đề xuất bản SKILL mới; NGƯỜI duyệt mới ghi.

Orchestrator là người đưa tin duy nhất của đội: sub-agent không send_signal, nó kết thúc lượt
bằng một báo cáo; orchestrator đọc, chạy verifier, rồi báo director. Signal hệ thống mang
from_session = TEAM_SENDER — miễn trần ping-pong như board (xem enqueue_signal).

Module này KHÔNG import session_orchestrator ở đầu file: file đó chạy dưới tên '__main__' và tự
đăng ký bí danh; import vòng ở mức module là nạp nó lần hai (xem check_single_module).
"""

import asyncio
import difflib
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

TEAM_SENDER = "team"
# playbook → (vai model, có worktree riêng không). Chỉ bốn cái này director tự gọi được.
PLAYBOOKS = {
    "feature": ("implement", True),
    "bugfix": ("implement", True),
    "refactor": ("implement", True),
    "investigate": ("investigate", False),
}
SYSTEM_PLAYBOOKS = ("verify", "reflect")
MODEL_ROLES = ("implement", "investigate", "verify")
# "@other" = verifier chạy bằng CLI KHÁC người làm nếu máy có — pstack trộn model ở khâu duyệt
# để cái mù của model này không phải cái mù của người chấm.
DEFAULT_MODELS = {"implement": "", "investigate": "", "verify": "@other"}
MAX_ATTEMPTS = max(1, int(os.environ.get("ORCH_SUBTASK_MAX_ATTEMPTS", "3")))
MAX_ACTIVE = max(1, int(os.environ.get("ORCH_SUBAGENTS_MAX", "4")))
ACTIVE = ("queued", "running", "verifying", "fixing")
REPORT_MAX = 6000      # báo cáo cuối của sub-agent giữ lại bấy nhiêu ký tự
DIFF_MAX = 200_000

_bg: set = set()       # giữ tham chiếu task nền, kẻo bị GC giữa chừng


def _so():
    import session_orchestrator as so   # noqa: PLC0415 — xem docstring module
    return so


def _spawn_bg(coro):
    t = asyncio.get_running_loop().create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)
    return t


# ─── Store ───────────────────────────────────────────────────────────────────

def migrate(conn):
    """Gọi từ init_db. Idempotent."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS subtasks (
            id TEXT PRIMARY KEY,                      -- st_<hex>
            workspace_id TEXT NOT NULL DEFAULT 'default',
            director_id TEXT NOT NULL,                -- người giao: director, hoặc agent gửi handoff
            session_id TEXT NOT NULL DEFAULT '',      -- người làm
            kind TEXT NOT NULL DEFAULT 'subagent',    -- subagent | handoff | reflect
            playbook TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            brief TEXT NOT NULL DEFAULT '',
            done_when TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,      -- số lần verifier đã đánh FAIL
            repo TEXT NOT NULL DEFAULT '',
            worktree TEXT NOT NULL DEFAULT '',
            branch TEXT NOT NULL DEFAULT '',
            base_sha TEXT NOT NULL DEFAULT '',
            head_sha TEXT NOT NULL DEFAULT '',        -- commit đem đi kiểm
            verifier_id TEXT NOT NULL DEFAULT '',     -- phiên verifier của LẦN KIỂM HIỆN TẠI
            verdict TEXT NOT NULL DEFAULT '{}',
            report TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            signal_id INTEGER,                        -- signal đang chạy cho subtask này
            target TEXT NOT NULL DEFAULT '',          -- reflect: role:<sid> | playbook:<tên>
            discarded INTEGER NOT NULL DEFAULT 0,     -- worktree + nhánh đã dọn
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            ended_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_subtasks_signal ON subtasks(signal_id);
        CREATE INDEX IF NOT EXISTS idx_subtasks_ws ON subtasks(workspace_id, created_at);
        CREATE TABLE IF NOT EXISTS team_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL DEFAULT 'default',
            session_id TEXT NOT NULL,
            subtask_id TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            evidence TEXT NOT NULL DEFAULT '',
            ts TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_team_decisions ON team_decisions(workspace_id, id);
        -- Đồ thị bàn giao: ai được signal cho ai. Chỉ CẮN khi graph_mode của workspace = enforced.
        CREATE TABLE IF NOT EXISTS team_edges (
            workspace_id TEXT NOT NULL,
            from_role TEXT NOT NULL,
            to_role TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (workspace_id, from_role, to_role)
        );
        CREATE TABLE IF NOT EXISTS skill_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL DEFAULT 'default',
            subtask_id TEXT NOT NULL DEFAULT '',
            target TEXT NOT NULL,                     -- role:<sid> | playbook:<tên>
            path TEXT NOT NULL,                       -- file sẽ bị ghi khi duyệt
            old_text TEXT NOT NULL,
            new_text TEXT NOT NULL,
            rationale TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
            created_at TEXT NOT NULL,
            decided_at TEXT NOT NULL DEFAULT ''
        );
    """)
    scols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
    # parent_id: director sở hữu phiên này. team_role: subagent | verifier | reflect — phiên của đội
    # không lên canvas, không hiện trong list_agents, và không được tự send_signal.
    for col in ("parent_id", "team_role"):
        if scols and col not in scols:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")


def _row(sql, args=()):
    so = _so()
    conn = so._conn()
    r = conn.execute(sql, args).fetchone()
    conn.close()
    return dict(r) if r else None


def _rows(sql, args=()):
    so = _so()
    conn = so._conn()
    rs = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(r) for r in rs]


def _exec(sql, args=()):
    so = _so()
    conn = so._conn()
    cur = conn.execute(sql, args)
    conn.commit()
    conn.close()
    return cur.lastrowid


def get_subtask(st_id):
    return _row("SELECT * FROM subtasks WHERE id = ?", (st_id,))


def _update(st_id, **fields):
    so = _so()
    fields["updated_at"] = so._now()
    if fields.get("status") in ("passed", "failed", "cancelled"):
        fields.setdefault("ended_at", so._now())
    cols = ", ".join(f"{k} = ?" for k in fields)
    _exec(f"UPDATE subtasks SET {cols} WHERE id = ?", (*fields.values(), st_id))
    st = get_subtask(st_id)
    if st:
        so.publish({"type": "team", "subtask": st_id, "status": st["status"],
                    "workspace_id": st["workspace_id"]})
    return st


def _set_team(session_id, parent_id, team_role):
    _exec("UPDATE sessions SET parent_id = ?, team_role = ? WHERE id = ?",
          (parent_id, team_role, session_id))


def team_sessions():
    """{session_id: {director, role, subtask}} cho mọi phiên của đội. Dashboard giấu chúng khỏi
    canvas (gom về tab Team), list_agents giấu chúng khỏi đồng nghiệp."""
    out = {}
    for s in _rows("SELECT id, parent_id, team_role FROM sessions WHERE team_role != ''"):
        out[s["id"]] = {"director": s["parent_id"], "role": s["team_role"]}
    return out


def is_team_session(session):
    return bool((session or {}).get("team_role"))


# ─── git ─────────────────────────────────────────────────────────────────────

def _git(args, cwd, timeout=60):
    """(returncode, stdout). Không có git trên máy → (127, '')."""
    if not shutil.which("git"):
        return 127, ""
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout or "") + ("" if p.returncode == 0 else (p.stderr or ""))


def repo_root(cwd):
    if not cwd:
        return ""
    rc, out = _git(["rev-parse", "--show-toplevel"], cwd)
    return out.strip() if rc == 0 else ""


def head_sha(cwd):
    rc, out = _git(["rev-parse", "HEAD"], cwd)
    return out.strip() if rc == 0 else ""


def is_dirty(cwd):
    rc, out = _git(["status", "--porcelain"], cwd)
    return rc == 0 and bool(out.strip())


def _worktrees_root():
    return _so().DB_DIR / "worktrees"


def _exclude_team_skills(repo):
    """SKILL của phiên đội (thư mục sub-*) không được lọt vào commit: ghi vào info/exclude CHUNG
    của repo (worktree dùng chung file này), không đụng .gitignore của người dùng."""
    rc, common = _git(["rev-parse", "--git-common-dir"], repo)
    if rc != 0:
        return
    p = Path(common.strip())
    if not p.is_absolute():
        p = Path(repo) / p
    f = p / "info" / "exclude"
    lines = [f"{r}/skills/sub-*/" for r in _so().CLI_SKILL_ROOTS]
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        have = f.read_text(encoding="utf-8") if f.exists() else ""
        add = [ln for ln in lines if ln not in have.splitlines()]
        if add:
            with f.open("a", encoding="utf-8") as fh:
                fh.write(("" if have.endswith("\n") or not have else "\n")
                         + "# agent orchestrator: team SKILL folders\n" + "\n".join(add) + "\n")
    except OSError:
        pass


def make_worktree(repo, st_id):
    """Worktree riêng NGOÀI repo (không lồng vào cây của người dùng), nhánh orch/<id> từ HEAD hiện
    tại của director. Trả (path, branch, base_sha) hoặc ('', '', '') nếu không dựng được."""
    base = head_sha(repo)
    if not base:
        return "", "", ""
    path = _worktrees_root() / st_id
    path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"orch/{st_id}"
    rc, out = _git(["worktree", "add", "-b", branch, str(path), base], repo)
    if rc != 0:
        print(f"[team] worktree for {st_id} failed: {out.strip()[:300]}", flush=True)
        return "", "", ""
    _exclude_team_skills(repo)
    return str(path), branch, base


def _identity_args(cwd):
    """Repo chưa cấu hình user.email thì commit hỏng; mượn danh tính tạm cho CHÍNH lệnh đó."""
    rc, out = _git(["config", "user.email"], cwd)
    if rc == 0 and out.strip():
        return []
    return ["-c", "user.name=Agent Orchestrator", "-c", "user.email=orchestrator@localhost"]


def commit_leftovers(worktree, msg):
    """Phán quyết phải gắn với MỘT commit: thứ sub-agent quên commit được commit hộ, để verifier
    kiểm đúng cái sẽ được merge. Trả sha HEAD sau đó."""
    if is_dirty(worktree):
        _git(["add", "-A"], worktree)
        _git([*_identity_args(worktree), "commit", "-q", "--no-verify", "-m", msg], worktree)
    return head_sha(worktree)


def restore(worktree, sha):
    """Đưa worktree về đúng commit đã đem kiểm. clean KHÔNG -x: thư mục SKILL bị exclude ở lại."""
    _git(["reset", "-q", "--hard", sha], worktree)
    _git(["clean", "-q", "-fd"], worktree)


def diff_stat(st):
    if not (st.get("worktree") and st.get("base_sha")):
        return ""
    rc, out = _git(["diff", "--stat", f"{st['base_sha']}..HEAD"], st["worktree"])
    return out.strip() if rc == 0 else ""


def diff_text(st):
    if not (st.get("worktree") and st.get("base_sha")) or st.get("discarded"):
        return ""
    rc, out = _git(["diff", f"{st['base_sha']}..{st.get('head_sha') or 'HEAD'}"], st["worktree"])
    return out[:DIFF_MAX] if rc == 0 else ""


def remove_worktree(st, delete_branch=True):
    repo, wt = st.get("repo"), st.get("worktree")
    if repo and wt:
        _git(["worktree", "remove", "--force", wt], repo)
        shutil.rmtree(wt, ignore_errors=True)
        _git(["worktree", "prune"], repo)
        if delete_branch and st.get("branch"):
            _git(["branch", "-D", st["branch"]], repo)


def _drop_skill(cwd, name):
    """Phiên đội chạy ngay trong thư mục director (chỉ đọc) để lại SKILL trong repo người dùng —
    xong việc là dọn. Chỉ đụng thư mục sub-*, không bao giờ đụng SKILL của vai thật."""
    if not (cwd and name and name.startswith("sub-")):
        return
    so = _so()
    for root in so.CLI_SKILL_ROOTS:
        shutil.rmtree(so._skills_dir(cwd, root) / name, ignore_errors=True)


# ─── Playbook & model ────────────────────────────────────────────────────────

def _bundled_playbook_dir():
    so = _so()
    for base in (so._app_dir() / "templates" / "playbooks", so._bundle_dir() / "templates" / "playbooks"):
        if base.is_dir():
            return base
    return so._bundle_dir() / "templates" / "playbooks"


def override_path(repo, name):
    """Bản playbook RIÊNG của một repo (do reflect đề xuất + người duyệt): <repo>/.orch/playbooks."""
    return Path(repo) / ".orch" / "playbooks" / name / "SKILL.md"


def playbook_text(name, repo=""):
    if name not in PLAYBOOKS and name not in SYSTEM_PLAYBOOKS:
        return ""
    if repo:
        try:
            return override_path(repo, name).read_text(encoding="utf-8")
        except OSError:
            pass
    try:
        return (_bundled_playbook_dir() / name / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return ""


def installed_engines():
    so = _so()
    bins = {"claude": so.CLAUDE_BIN, "codex": so.CODEX_BIN, "agy": so.AGY_BIN}
    return [e for e, b in bins.items() if shutil.which(b)]


def model_roles(workspace_id):
    so = _so()
    try:
        saved = json.loads(so.get_setting(f"team_models:{workspace_id}", "{}") or "{}")
    except (json.JSONDecodeError, ValueError):
        saved = {}
    return {r: str(saved.get(r, DEFAULT_MODELS[r])) for r in MODEL_ROLES}


def set_model_roles(workspace_id, roles):
    cur = model_roles(workspace_id)
    for r in MODEL_ROLES:
        if r in roles:
            cur[r] = str(roles[r] or "").strip()
    _so().set_setting(f"team_models:{workspace_id}", json.dumps(cur))
    return cur


def resolve_model(workspace_id, role, director, builder_model=""):
    """Model cho một vai. '' = như director. '@other' = CLI khác người làm (nếu máy có)."""
    so = _so()
    want = model_roles(workspace_id).get(role, "")
    if want == "@other":
        builder = so.engine_from_model(builder_model or director.get("model", ""))
        for eng in installed_engines():
            if eng != builder:
                return {"claude": "", "codex": "codex", "agy": "agy"}[eng]
        return builder_model or director.get("model", "")
    return want or director.get("model", "")


# ─── Signal & thông điệp ─────────────────────────────────────────────────────

def _enqueue(session_id, message, workspace_id):
    so = _so()
    sid = so.enqueue_signal(session_id, message, TEAM_SENDER, 0, 0, workspace_id, expects_reply=0)
    so.publish({"type": "signal", "id": sid, "status": "pending", "to_session": session_id,
                "workspace_id": workspace_id})
    return sid


def _where(st):
    if st.get("worktree"):
        return (f"You work in {st['worktree']} — a git worktree of {st['repo']} on branch "
                f"{st['branch']}. Commit your work here. Never push, never switch branches, never "
                f"touch the main checkout.")
    return "You work in the director's folder. Do not create, edit, move or delete any file."


def _brief_msg(st, director_name):
    return (f"[SUB-AGENT TASK {st['id']} · playbook: {st['playbook']}]\n"
            f"Delegated by: {director_name}\n{_where(st)}\n\n"
            f"## Goal\n{st['brief']}\n\n"
            f"## Done means — an independent verifier checks exactly this\n{st['done_when']}\n\n"
            f"Follow your playbook. When you finish, end your turn with the report it describes "
            f"(changed / evidence / open). Do not call send_signal: the orchestrator reads your "
            f"final message and runs verification.")


def _fix_msg(st, findings):
    return (f"[VERIFIER FINDINGS {st['id']} — attempt {st['attempts']}/{MAX_ATTEMPTS} failed]\n"
            f"An independent verifier checked your work against the contract and it did not pass.\n\n"
            f"## Findings\n{findings}\n\n"
            f"## Done means (unchanged)\n{st['done_when']}\n\n"
            f"Fix the cause, re-check it yourself on the real interface, commit, then end your turn "
            f"with a fresh report. Do not call send_signal.")


def _verify_msg(st):
    report = st.get("report") or "(the builder gave no final report)"
    where = (f"Worktree: {st['worktree']} (branch {st['branch']}, commit {st['head_sha'][:12]}, "
             f"base {st['base_sha'][:12]}).\nChanged files:\n{diff_stat(st) or '(no changes)'}"
             if st.get("worktree") else "Folder: the builder's working folder (no isolated branch).")
    return (f"[VERIFY {st['id']}]\n"
            f"You are the verifier for subtask {st['id']}. You did not build it.\n{where}\n\n"
            f"## Done means — check every clause yourself\n{st['done_when']}\n\n"
            f"## The original goal (context)\n{st['brief']}\n\n"
            f"## The builder's report — a claim, not evidence\n{report}\n\n"
            f"Do not change any file or git state. When done call exactly once:\n"
            f"submit_verdict(from_role=\"<your session name>\", subtask_id=\"{st['id']}\", "
            f"verdict=\"PASS\" or \"FAIL\", evidence=..., findings=...)\nThen end your turn.")


def _notify(st, text):
    """Báo người giao việc (director / agent gửi handoff). Một chiều: không ai trả lời báo cáo."""
    so = _so()
    if so.get_session(st["director_id"]):
        _enqueue(st["director_id"], text, st["workspace_id"])


def _outcome_msg(st, passed):
    v = _verdict(st)
    head = "[SUB-AGENT PASSED]" if passed else "[SUB-AGENT FAILED]"
    if st["kind"] == "handoff":
        head = "[HANDOFF VERIFIED]" if passed else "[HANDOFF FAILED]"
    lines = [f"{head} {st['id']} · {st['title'] or st['playbook']}"]
    if passed and st.get("branch"):
        lines.append(f"Verified at commit {st['head_sha'][:12]} on branch {st['branch']}. "
                     f"Review: git diff HEAD...{st['branch']}  ·  take it: git merge --no-ff {st['branch']}")
    if not passed:
        lines.append(f"Reason: {st.get('reason') or 'verification failed'} "
                     f"(after {st['attempts']} verification attempt(s)).")
    if v.get("evidence"):
        lines.append(f"\nVerifier evidence:\n{v['evidence']}")
    if v.get("findings") and not passed:
        lines.append(f"\nVerifier findings:\n{v['findings']}")
    if st.get("report"):
        lines.append(f"\nBuilder's report:\n{st['report'][:3000]}")
    lines.append("\nThis is a status update — decide the next step; do not reply to it.")
    return "\n".join(lines)


def _verdict(st):
    try:
        return json.loads(st.get("verdict") or "{}") or {}
    except (json.JSONDecodeError, ValueError):
        return {}


# ─── Tạo việc ────────────────────────────────────────────────────────────────

def _new_id():
    return "st_" + secrets.token_hex(4)


def _active_count(director_id):
    r = _row(f"SELECT COUNT(*) n FROM subtasks WHERE director_id = ? AND kind != 'reflect' "
             f"AND status IN ({','.join('?' * len(ACTIVE))})", (director_id, *ACTIVE))
    return r["n"] if r else 0


def spawn_subagent(director_id, playbook, brief, done_when, title="", model=""):
    """Xếp hàng một sub-agent. Trả {id, status} ngay — dựng phiên (một lượt CLI đầy đủ) chạy nền,
    director không phải chờ. Lỗi lúc dựng: subtask 'failed' kèm lý do + báo director."""
    so = _so()
    director = so.get_session(director_id)
    if not director:
        return {"error": "director session not found"}
    if is_team_session(director):
        return {"error": "a sub-agent cannot spawn sub-agents — report back and let your director "
                         "split the work"}
    playbook = (playbook or "").strip().lower()
    if playbook not in PLAYBOOKS:
        return {"error": f"playbook must be one of: {', '.join(PLAYBOOKS)}"}
    if not str(brief or "").strip():
        return {"error": "brief is required: the sub-agent cannot see your conversation"}
    if not str(done_when or "").strip():
        return {"error": "done_when is required: write the observable check an independent "
                         "verifier will run (a command and its expected output, a flow in the "
                         "running app, a value read back)"}
    if _active_count(director_id) >= MAX_ACTIVE:
        return {"error": f"{MAX_ACTIVE} sub-agents are already active for you — wait for one to "
                         f"finish (or cancel one). Fewer, deeper units beat many shallow ones."}
    st_id, now = _new_id(), so._now()
    _exec("INSERT INTO subtasks (id, workspace_id, director_id, kind, playbook, title, brief, "
          "done_when, model, status, created_at, updated_at) "
          "VALUES (?, ?, ?, 'subagent', ?, ?, ?, ?, ?, 'queued', ?, ?)",
          (st_id, director.get("workspace_id") or so.DEFAULT_WORKSPACE, director_id, playbook,
           str(title or "")[:120], str(brief), str(done_when), str(model or ""), now, now))
    so.publish({"type": "team", "subtask": st_id, "status": "queued",
                "workspace_id": director.get("workspace_id")})
    _spawn_bg(_start_subagent(st_id))
    return {"id": st_id, "status": "queued"}


async def _spawn_session(name, cwd, playbook, model, director, workspace_id, repo):
    """Dựng một phiên đội. Trả (session, err)."""
    so = _so()
    eng = so.engine_from_model(model)
    res = await so.engine_for(eng).spawn(
        name, cwd=cwd, skill=playbook_text(playbook, repo), model=model,
        effort=director.get("effort", ""), permission_mode=director.get("permission_mode", ""),
        workspace_id=workspace_id)
    if not res or res.get("error"):
        return None, (res or {}).get("error") or "spawn failed"
    return res, ""


async def _start_subagent(st_id):
    so = _so()
    st = get_subtask(st_id)
    if not st or st["status"] != "queued":
        return
    director = so.get_session(st["director_id"])
    if not director:
        return _fail(st, "director was removed", notify=False)
    write = PLAYBOOKS[st["playbook"]][1]
    cwd = director.get("cwd") or ""
    repo = await asyncio.to_thread(repo_root, cwd)
    fields = {"repo": repo}
    if write and repo:
        wt, branch, base = await asyncio.to_thread(make_worktree, repo, st_id)
        if wt:
            cwd = wt
            fields.update(worktree=wt, branch=branch, base_sha=base, head_sha=base)
    elif repo:
        await asyncio.to_thread(_exclude_team_skills, repo)
    model = st["model"] or resolve_model(st["workspace_id"], PLAYBOOKS[st["playbook"]][0], director)
    fields["model"] = model
    st = _update(st_id, **fields)
    name = f"sub-{st_id[3:]}-{st['playbook']}"
    sess, err = await _spawn_session(name, cwd, st["playbook"], model, director,
                                     st["workspace_id"], repo)
    if err:
        return _fail(get_subtask(st_id), f"could not start the sub-agent: {err}")
    _set_team(sess["id"], director["id"], "subagent")
    st = get_subtask(st_id)
    if st["status"] == "cancelled":
        return
    sig = _enqueue(sess["id"], _brief_msg(st, director["name"]), st["workspace_id"])
    _update(st_id, session_id=sess["id"], status="running", signal_id=sig)


def create_handoff(sender_id, target_id, signal_id, done_when, message):
    """send_signal kèm done_when: đích làm như thường, nhưng 'xong' do verifier quyết."""
    so = _so()
    sender, target = so.get_session(sender_id), so.get_session(target_id)
    if not sender or not target:
        return None
    st_id, now = _new_id(), so._now()
    text = so._coerce_message(message)
    _exec("INSERT INTO subtasks (id, workspace_id, director_id, session_id, kind, playbook, title, "
          "brief, done_when, model, status, signal_id, repo, created_at, updated_at) "
          "VALUES (?, ?, ?, ?, 'handoff', 'handoff', ?, ?, ?, ?, 'running', ?, ?, ?, ?)",
          (st_id, target.get("workspace_id") or so.DEFAULT_WORKSPACE, sender_id, target_id,
           text.splitlines()[0][:120] if text else "", text, str(done_when),
           target.get("model", ""), signal_id, repo_root(target.get("cwd") or ""), now, now))
    so.publish({"type": "team", "subtask": st_id, "status": "running",
                "workspace_id": target.get("workspace_id")})
    return st_id


def handoff_message(message, done_when):
    """Lời giao việc gửi tới đích khi có done_when: nói trước là sẽ có người kiểm."""
    text = _so()._coerce_message(message)
    return (f"{text}\n\n## Done means — an independent verifier checks exactly this before your "
            f"work counts as done\n{done_when}\n\nWhen you finish, end your turn with a report: "
            f"what changed, the evidence you observed, what is still open. Do not send_signal a "
            f"report back — the orchestrator verifies, then reports for you.")


# ─── Sau mỗi run ─────────────────────────────────────────────────────────────

async def after_run(signal, result):
    """process_signal gọi sau MỖI run (đã nhả khoá session). Run không thuộc subtask nào → bỏ."""
    st = _row("SELECT * FROM subtasks WHERE signal_id = ?", (signal.get("id"),))
    if not st or st["status"] not in ACTIVE:
        return
    try:
        if st["session_id"] == signal.get("to_session") and st["status"] in ("running", "fixing"):
            await _after_work(st, result)
        elif st["verifier_id"] == signal.get("to_session") and st["status"] == "verifying":
            await _after_verify(st, result)
    except Exception as e:  # noqa: BLE001 — một subtask hỏng không được giết run_loop
        print(f"[team] {st['id']} error: {e}", flush=True)
        _fail(get_subtask(st["id"]) or st, f"orchestrator error: {e}")


async def _after_work(st, result):
    if result.get("status") not in ("done", "ok"):
        why = result.get("reason") or result.get("result") or "the run failed"
        return _fail(st, f"the sub-agent's run failed: {str(why)[:300]}")
    report = str(result.get("result") or "")[:REPORT_MAX]
    if st["kind"] == "reflect":
        ok = _row("SELECT 1 FROM skill_proposals WHERE subtask_id = ?", (st["id"],))
        _update(st["id"], report=report, status="passed" if ok else "failed",
                reason="" if ok else "no change was proposed")
        return _finish(get_subtask(st["id"]))
    fields = {"report": report}
    if st["worktree"]:
        fields["head_sha"] = await asyncio.to_thread(
            commit_leftovers, st["worktree"], f"{st['id']}: uncommitted work at end of turn")
    st = _update(st["id"], **fields)
    await _start_verifier(st)


async def _start_verifier(st):
    so = _so()
    st = _update(st["id"], status="verifying", verdict="{}", verifier_id="")
    builder = so.get_session(st["session_id"]) or {}
    director = so.get_session(st["director_id"]) or builder
    model = resolve_model(st["workspace_id"], "verify", director, builder.get("model", ""))
    cwd = st["worktree"] or builder.get("cwd") or director.get("cwd") or ""
    n = _row("SELECT COUNT(*) n FROM sessions WHERE team_role = 'verifier' AND name LIKE ?",
             (f"sub-{st['id'][3:]}-verify%",))["n"] + 1
    name = f"sub-{st['id'][3:]}-verify{n}"
    sess, err = await _spawn_session(name, cwd, "verify", model, director, st["workspace_id"],
                                     st["repo"])
    if err:
        return _fail(get_subtask(st["id"]), f"could not start a verifier: {err}")
    _set_team(sess["id"], st["director_id"], "verifier")
    if get_subtask(st["id"])["status"] != "verifying":     # bị huỷ lúc đang dựng
        return
    sig = _enqueue(sess["id"], _verify_msg(st), st["workspace_id"])
    _update(st["id"], verifier_id=sess["id"], signal_id=sig)


async def _after_verify(st, result):
    so = _so()
    verifier = so.get_session(st["verifier_id"]) or {}
    v = _verdict(st)
    tampered = ""
    if st["worktree"]:
        now = await asyncio.to_thread(head_sha, st["worktree"])
        dirty = await asyncio.to_thread(is_dirty, st["worktree"])
        if now != st["head_sha"] or dirty:
            tampered = "the verifier changed the worktree, so its verdict does not count"
            await asyncio.to_thread(restore, st["worktree"], st["head_sha"])
    if not tampered and v.get("sha") and st["worktree"] and v["sha"] != st["head_sha"]:
        tampered = "the verdict was for a different commit"
    _drop_skill(verifier.get("cwd") if not st["worktree"] else "", verifier.get("name", ""))
    if v.get("verdict") == "PASS" and not tampered:
        _update(st["id"], status="passed", reason="")
        return _finish(get_subtask(st["id"]))
    findings = tampered or v.get("findings") or (
        "the verifier ended its turn without calling submit_verdict" if not v else "no findings given")
    attempts = st["attempts"] + 1
    if attempts >= MAX_ATTEMPTS:
        _update(st["id"], attempts=attempts, status="failed",
                reason=f"did not pass verification: {findings[:300]}")
        return _finish(get_subtask(st["id"]))
    st = _update(st["id"], attempts=attempts, status="fixing")
    if tampered or not v:
        # Lỗi của NGƯỜI KIỂM, không phải của người làm: kiểm lại bằng một verifier mới.
        return await _start_verifier(st)
    sig = _enqueue(st["session_id"], _fix_msg(st, findings), st["workspace_id"])
    _update(st["id"], signal_id=sig)


def _fail(st, reason, notify=True):
    if not st:
        return
    _update(st["id"], status="failed", reason=reason[:500])
    if notify:
        _finish(get_subtask(st["id"]))


def _finish(st):
    """Đóng việc: báo người giao (trừ reflect — kết quả là đề xuất chờ người duyệt), dọn SKILL
    của phiên chạy trong thư mục director."""
    so = _so()
    if st["kind"] != "reflect":
        _notify(st, _outcome_msg(st, st["status"] == "passed"))
    if not st["worktree"] and st["kind"] in ("subagent", "reflect"):
        s = so.get_session(st["session_id"]) or {}
        _drop_skill(s.get("cwd"), s.get("name"))


# ─── Công cụ của agent (signal MCP gọi vào) ──────────────────────────────────

def submit_verdict(verifier_name, subtask_id, verdict, evidence="", findings="", workspace_id=""):
    so = _so()
    st = get_subtask(subtask_id)
    if not st:
        return "⛔ no such subtask"
    me = so.get_session(st["verifier_id"]) or {}
    if st["status"] != "verifying" or me.get("name") != verifier_name:
        return (f"⛔ only the current verifier of {subtask_id} can submit a verdict, while it is "
                f"being verified")
    verdict = str(verdict or "").strip().upper()
    if verdict not in ("PASS", "FAIL"):
        return "⛔ verdict must be PASS or FAIL"
    if verdict == "PASS" and not str(evidence or "").strip():
        return ("⛔ a PASS needs evidence: the commands you ran and what they printed, per clause "
                "of Done means. If you could not observe it, the verdict is FAIL.")
    _update(subtask_id, verdict=json.dumps({
        "verdict": verdict, "evidence": str(evidence)[:REPORT_MAX],
        "findings": str(findings)[:REPORT_MAX], "sha": st["head_sha"], "by": verifier_name,
        "at": so._now()}))
    return f"Verdict {verdict} recorded for {subtask_id}. End your turn now."


def record_decision(session_name, decision, reason="", evidence="", workspace_id=""):
    so = _so()
    s = so.get_session_by_name(session_name, workspace_id or None) if workspace_id else None
    if not s:
        cands = [x for x in so.list_sessions() if x.get("name") == session_name]
        s = cands[0] if len(cands) == 1 else None
    if not s:
        return "⛔ from_role must be your exact session name"
    if not str(decision or "").strip():
        return "⛔ decision is required"
    st = _row(f"SELECT id FROM subtasks WHERE (session_id = ? OR verifier_id = ?) "
              f"AND status IN ({','.join('?' * len(ACTIVE))}) ORDER BY created_at DESC LIMIT 1",
              (s["id"], s["id"], *ACTIVE))
    ws = s.get("workspace_id") or so.DEFAULT_WORKSPACE
    _exec("INSERT INTO team_decisions (workspace_id, session_id, subtask_id, decision, reason, "
          "evidence, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
          (ws, s["id"], st["id"] if st else "", str(decision)[:1000], str(reason)[:2000],
           str(evidence)[:4000], so._now()))
    so.publish({"type": "team", "decision": True, "workspace_id": ws})
    return "Decision recorded."


def list_for_director(director_id):
    out = []
    for st in _rows("SELECT * FROM subtasks WHERE director_id = ? AND kind != 'reflect' "
                    "ORDER BY created_at DESC LIMIT 30", (director_id,)):
        v = _verdict(st)
        out.append({"id": st["id"], "title": st["title"], "playbook": st["playbook"],
                    "status": st["status"], "attempts": st["attempts"], "branch": st["branch"],
                    "verdict": v.get("verdict", ""), "reason": st["reason"]})
    return out


# ─── Huỷ / kiểm lại / dọn ────────────────────────────────────────────────────

def _kill(session_id):
    so = _so()
    proc = so.ACTIVE_PROCS.get(session_id)
    if proc and proc.returncode is None:
        so.KILLED_SESSIONS.add(session_id)
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def cancel(st_id, by_user=False):
    so = _so()
    st = get_subtask(st_id)
    if not st:
        return {"error": "not found"}
    if st["status"] not in ACTIVE:
        return {"error": f"already {st['status']}"}
    # Signal còn chờ của subtask này không được chạy nữa.
    if st.get("signal_id"):
        _exec("UPDATE signals SET status = 'denied', reason = 'subtask cancelled' "
              "WHERE id = ? AND status IN ('pending', 'approved')", (st["signal_id"],))
    for sid in (st["session_id"], st["verifier_id"]):
        if sid:
            _kill(sid)
    st = _update(st_id, status="cancelled", reason="cancelled by the user" if by_user else "cancelled")
    if by_user and st["kind"] != "reflect":
        _notify(st, f"[SUB-AGENT CANCELLED] {st_id} · {st['title'] or st['playbook']} was cancelled "
                    f"by the user from the dashboard. Do not reply to this.")
    so.publish({"type": "team", "subtask": st_id, "status": "cancelled",
                "workspace_id": st["workspace_id"]})
    return {"ok": True}


async def reverify(st_id):
    st = get_subtask(st_id)
    if not st:
        return {"error": "not found"}
    if st["status"] not in ("failed", "passed") or st["kind"] == "reflect" or not st["session_id"]:
        return {"error": "only a finished subtask can be verified again"}
    if st["discarded"]:
        return {"error": "its worktree was discarded"}
    if st["worktree"]:
        sha = await asyncio.to_thread(commit_leftovers, st["worktree"],
                                      f"{st_id}: changes made before re-verification")
        st = _update(st_id, head_sha=sha)
    _update(st_id, attempts=max(0, MAX_ATTEMPTS - 1), ended_at="")
    _spawn_bg(_start_verifier(get_subtask(st_id)))
    return {"ok": True}


def discard(st_id):
    st = get_subtask(st_id)
    if not st:
        return {"error": "not found"}
    if st["status"] in ACTIVE:
        return {"error": "cancel it first"}
    remove_worktree(st, delete_branch=True)
    _update(st_id, discarded=1)
    return {"ok": True}


def on_session_removed(session_id):
    """Director bị gỡ → việc đang chạy của nó dừng; không ai còn để nhận kết quả."""
    for st in _rows(f"SELECT id FROM subtasks WHERE director_id = ? "
                    f"AND status IN ({','.join('?' * len(ACTIVE))})", (session_id, *ACTIVE)):
        cancel(st["id"])


# ─── Đồ thị bàn giao ─────────────────────────────────────────────────────────

def graph_mode(workspace_id):
    return _so().get_setting(f"graph_mode:{workspace_id}", "open") or "open"


def set_graph_mode(workspace_id, mode):
    mode = "enforced" if mode == "enforced" else "open"
    _so().set_setting(f"graph_mode:{workspace_id}", mode)
    return mode


def edges(workspace_id):
    return _rows("SELECT from_role, to_role FROM team_edges WHERE workspace_id = ? "
                 "ORDER BY from_role, to_role", (workspace_id,))


def add_edge(workspace_id, from_role, to_role):
    from_role, to_role = str(from_role or "").strip(), str(to_role or "").strip()
    if not from_role or not to_role or from_role == to_role:
        return {"error": "pick two different agents"}
    _exec("INSERT OR IGNORE INTO team_edges (workspace_id, from_role, to_role, created_at) "
          "VALUES (?, ?, ?, ?)", (workspace_id, from_role, to_role, _so()._now()))
    return {"ok": True}


def remove_edge(workspace_id, from_role, to_role):
    _exec("DELETE FROM team_edges WHERE workspace_id = ? AND from_role = ? AND to_role = ?",
          (workspace_id, from_role, to_role))
    return {"ok": True}


def learn_edges(workspace_id, limit=500):
    """Thêm cạnh cho mọi cặp agent→agent đã thật sự nhắn nhau gần đây — điểm xuất phát để chuyển
    sang enforced mà không chặn nhầm luồng đang chạy tốt."""
    so = _so()
    names = {s["name"] for s in so.list_sessions()
             if s.get("workspace_id") == workspace_id and not is_team_session(s)}
    added = 0
    for r in _rows("SELECT s.from_session f, sess.name t FROM signals s JOIN sessions sess "
                   "ON sess.id = s.to_session WHERE s.workspace_id = ? ORDER BY s.id DESC LIMIT ?",
                   (workspace_id, limit)):
        if r["f"] in names and r["t"] in names and r["f"] != r["t"]:
            before = _row("SELECT 1 FROM team_edges WHERE workspace_id = ? AND from_role = ? "
                          "AND to_role = ?", (workspace_id, r["f"], r["t"]))
            if not before:
                add_edge(workspace_id, r["f"], r["t"])
                added += 1
    return {"added": added}


def check_signal(from_name, to_session, workspace_id, message=""):
    """'' = cho đi; chuỗi = lý do từ chối (đã là câu chỉ dẫn hoàn chỉnh cho agent đọc).
    Chỉ gọi cho người gửi là AGENT (người, board, team đã được enqueue_signal cho qua)."""
    so = _so()
    if str(message or "").startswith("/compact"):
        return ""          # nén context là việc vận hành, kể cả sub-agent tự nén mình
    src = so.get_session_by_name(from_name, workspace_id)
    if src and is_team_session(src):
        return ("⛔ Sub-agents do not send signals. Finish the work and end your turn with your "
                "report — the orchestrator verifies it and tells your director.")
    if graph_mode(workspace_id) != "enforced":
        return ""
    to_name = so._session_name(to_session)
    allowed = [e["to_role"] for e in edges(workspace_id) if e["from_role"] == from_name]
    if to_name in allowed:
        return ""
    return (f"⛔ HANDOFF NOT ALLOWED: this workspace's handoff graph has no edge "
            f"'{from_name}' → '{to_name}'. You may hand work to: "
            f"{', '.join(allowed) if allowed else 'nobody'}. Do not route around it through "
            f"another agent — report to the user instead and let them decide.")


# ─── Reflect ─────────────────────────────────────────────────────────────────

def _evidence_for_role(session_id):
    rows = _rows("SELECT r.id, r.status, substr(r.prompt, 1, 300) p, r.started_at, s.reason "
                 "FROM runs r LEFT JOIN signals s ON s.id = r.signal_id "
                 "WHERE r.session_id = ? AND (r.status = 'error' OR s.status IN "
                 "('failed', 'blocked', 'denied')) ORDER BY r.id DESC LIMIT 15", (session_id,))
    out = [f"- run #{r['id']} {r['status']} at {r['started_at'][:16]}: {r['reason'] or ''}\n"
           f"  task: {r['p']!r}" for r in rows]
    for st in _rows("SELECT * FROM subtasks WHERE director_id = ? AND status = 'failed' "
                    "ORDER BY created_at DESC LIMIT 10", (session_id,)):
        out.append(f"- delegated {st['id']} ({st['playbook']}) failed: {st['reason']}\n"
                   f"  done_when was: {st['done_when'][:300]!r}")
    for d in _rows("SELECT * FROM team_decisions WHERE session_id = ? ORDER BY id DESC LIMIT 15",
                   (session_id,)):
        out.append(f"- decision: {d['decision']} — {d['reason']}")
    return "\n".join(out)


def _evidence_for_playbook(workspace_id, name):
    out = []
    for st in _rows("SELECT * FROM subtasks WHERE workspace_id = ? AND playbook = ? AND "
                    "(attempts > 0 OR status = 'failed') ORDER BY created_at DESC LIMIT 12",
                    (workspace_id, name)):
        v = _verdict(st)
        out.append(f"- {st['id']} {st['status']} after {st['attempts']} failed check(s). "
                   f"Goal: {st['brief'][:200]!r}\n  findings: {(v.get('findings') or st['reason'])[:600]!r}")
    return "\n".join(out)


def _reflect_target(target, workspace_id):
    """target 'role:<sid>' | 'playbook:<tên>' → (path, current_text, label, cwd) hoặc lỗi."""
    so = _so()
    kind, _, ref = str(target or "").partition(":")
    if kind == "role":
        s = so.get_session(ref)
        if not s or is_team_session(s):
            return None, "agent not found"
        return {"path": str(so._skill_path(s.get("cwd") or "", s["name"])), "cwd": s.get("cwd") or "",
                "text": so._role_skill(s.get("cwd") or "", s["name"]), "label": f"role '{s['name']}'",
                "session": s, "evidence": _evidence_for_role(s["id"])}, ""
    if kind == "playbook" and ref in PLAYBOOKS:
        return {"playbook": ref, "label": f"playbook '{ref}'",
                "evidence": _evidence_for_playbook(workspace_id, ref)}, ""
    return None, "unknown target"


def start_reflect(target, director_id):
    """Một phiên reflect CHỈ ĐỌC đề xuất bản SKILL mới. Không ghi gì cho tới khi người duyệt."""
    so = _so()
    director = so.get_session(director_id)
    if not director or is_team_session(director):
        return {"error": "pick a director (or another agent) to reflect for"}
    ws = director.get("workspace_id") or so.DEFAULT_WORKSPACE
    t, err = _reflect_target(target, ws)
    if err:
        return {"error": err}
    repo = repo_root(director.get("cwd") or "")
    if "playbook" in t:
        if not repo:
            return {"error": "playbook overrides live in a git repo; this agent's folder is not one"}
        t["path"] = str(override_path(repo, t["playbook"]))
        t["text"] = playbook_text(t["playbook"], repo)
        t["cwd"] = director.get("cwd") or ""
    if not t["text"].strip():
        return {"error": f"{t['label']} has no SKILL text to improve"}
    if not t["evidence"].strip():
        return {"error": f"nothing has gone wrong for {t['label']} yet — no evidence to learn from"}
    st_id, now = _new_id(), so._now()
    brief = (f"Improve {t['label']}.\nFile: {t['path']}\n\n## Current SKILL.md\n{t['text']}\n\n"
             f"## Evidence of what went wrong\n{t['evidence']}")
    _exec("INSERT INTO subtasks (id, workspace_id, director_id, kind, playbook, title, brief, "
          "done_when, status, target, repo, created_at, updated_at) "
          "VALUES (?, ?, ?, 'reflect', 'reflect', ?, ?, '', 'queued', ?, ?, ?, ?)",
          (st_id, ws, director_id, f"Reflect on {t['label']}", brief, target, repo, now, now))
    _spawn_bg(_start_reflect(st_id, t["cwd"]))
    return {"id": st_id, "status": "queued"}


async def _start_reflect(st_id, cwd):
    so = _so()
    st = get_subtask(st_id)
    director = so.get_session(st["director_id"])
    if not director:
        return _fail(st, "agent was removed", notify=False)
    model = resolve_model(st["workspace_id"], "investigate", director)
    name = f"sub-{st_id[3:]}-reflect"
    sess, err = await _spawn_session(name, cwd, "reflect", model, director, st["workspace_id"],
                                     st["repo"])
    if err:
        return _fail(get_subtask(st_id), f"could not start reflection: {err}", notify=False)
    _set_team(sess["id"], director["id"], "reflect")
    msg = (f"[REFLECT {st_id}]\n{st['brief']}\n\nWhen done call propose_skill_change("
           f"from_role=\"{name}\", subtask_id=\"{st_id}\", new_text=<full SKILL.md>, "
           f"rationale=...) once, then end your turn.")
    sig = _enqueue(sess["id"], msg, st["workspace_id"])
    _update(st_id, session_id=sess["id"], status="running", signal_id=sig, model=model)


def propose_skill_change(reflector_name, subtask_id, new_text, rationale=""):
    so = _so()
    st = get_subtask(subtask_id)
    if not st or st["kind"] != "reflect":
        return "⛔ no such reflection"
    me = so.get_session(st["session_id"]) or {}
    if st["status"] != "running" or me.get("name") != reflector_name:
        return "⛔ only the reflection agent of this subtask can propose, while it runs"
    if _row("SELECT 1 FROM skill_proposals WHERE subtask_id = ?", (subtask_id,)):
        return "⛔ already proposed — one proposal per reflection"
    if not str(new_text or "").strip():
        return "⛔ new_text must be the full SKILL.md"
    t, err = _reflect_target(st["target"], st["workspace_id"])
    if err:
        return f"⛔ {err}"
    if "playbook" in t:
        t["path"] = str(override_path(st["repo"], t["playbook"]))
        t["text"] = playbook_text(t["playbook"], st["repo"])
    if str(new_text).strip() == t["text"].strip():
        return "⛔ identical to the current text — propose nothing instead"
    _exec("INSERT INTO skill_proposals (workspace_id, subtask_id, target, path, old_text, new_text, "
          "rationale, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
          (st["workspace_id"], subtask_id, st["target"], t["path"], t["text"], str(new_text),
           str(rationale)[:4000], so._now()))
    so.publish({"type": "team", "proposal": True, "workspace_id": st["workspace_id"]})
    return "Proposal recorded. A human will approve or reject it. End your turn now."


def decide_proposal(pid, approve):
    so = _so()
    p = _row("SELECT * FROM skill_proposals WHERE id = ?", (pid,))
    if not p:
        return {"error": "not found"}
    if p["status"] != "pending":
        return {"error": f"already {p['status']}"}
    if approve:
        kind, _, ref = p["target"].partition(":")
        if kind == "role":
            s = so.get_session(ref)
            if not s:
                return {"error": "that agent no longer exists"}
            cur = so._role_skill(s.get("cwd") or "", s["name"])
            if cur.strip() != p["old_text"].strip():
                return {"error": "the SKILL changed since this was proposed — reflect again"}
            so._write_role_skill(s.get("cwd") or "", s["name"], p["new_text"])
        else:
            path = Path(p["path"])
            cur = path.read_text(encoding="utf-8") if path.exists() else \
                playbook_text(ref, str(path.parents[3]) if len(path.parents) > 3 else "")
            if cur.strip() != p["old_text"].strip():
                return {"error": "the playbook changed since this was proposed — reflect again"}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(p["new_text"], encoding="utf-8")
    _exec("UPDATE skill_proposals SET status = ?, decided_at = ? WHERE id = ?",
          ("approved" if approve else "rejected", so._now(), pid))
    so.publish({"type": "team", "proposal": True, "workspace_id": p["workspace_id"]})
    return {"ok": True}


def proposal_diff(p):
    return "".join(difflib.unified_diff(
        p["old_text"].splitlines(keepends=True), p["new_text"].splitlines(keepends=True),
        fromfile="current", tofile="proposed", n=2))


# ─── Bức tranh cho dashboard ─────────────────────────────────────────────────

def _run_totals(session_ids):
    ids = [s for s in session_ids if s]
    if not ids:
        return {"runs": 0, "tokens": 0, "running": False}
    q = ",".join("?" * len(ids))
    r = _row(f"SELECT COUNT(*) n, COALESCE(SUM(tokens), 0) t, "
             f"SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) live "
             f"FROM runs WHERE session_id IN ({q})", tuple(ids))
    return {"runs": r["n"], "tokens": r["t"], "running": bool(r["live"])}


def _last_event(session_ids):
    ids = [s for s in session_ids if s]
    if not ids:
        return None
    q = ",".join("?" * len(ids))
    r = _row(f"SELECT kind, summary, ts FROM run_events WHERE session_id IN ({q}) "
             f"ORDER BY id DESC LIMIT 1", tuple(ids))
    return r


def overview(workspace_id):
    so = _so()
    sessions = {s["id"]: s for s in so.list_sessions() if s.get("workspace_id") == workspace_id}
    subs = _rows("SELECT * FROM subtasks WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 200",
                 (workspace_id,))
    verifiers = {}
    for s in sessions.values():
        if s.get("team_role") == "verifier":
            verifiers.setdefault(s["name"].rsplit("-verify", 1)[0], []).append(s["id"])
    items = []
    for st in subs:
        sess_ids = [st["session_id"], *verifiers.get(f"sub-{st['id'][3:]}", [])]
        v = _verdict(st)
        items.append({
            **{k: st[k] for k in ("id", "director_id", "session_id", "verifier_id", "kind",
                                  "playbook", "title", "brief", "done_when", "model", "status",
                                  "attempts", "branch", "worktree", "head_sha", "base_sha", "reason",
                                  "report", "target", "discarded", "created_at", "updated_at",
                                  "ended_at")},
            "verdict": v, "session_name": (sessions.get(st["session_id"]) or {}).get("name", ""),
            "verifier_name": (sessions.get(st["verifier_id"]) or {}).get("name", ""),
            "director_name": (sessions.get(st["director_id"]) or {}).get("name", ""),
            "max_attempts": MAX_ATTEMPTS, **_run_totals(sess_ids),
            "last": _last_event(sess_ids) if st["status"] in ACTIVE else None,
            "isolated": bool(st["worktree"]),
        })
    threads = so.board_threads()
    directors = [{"id": s["id"], "name": s["name"], "status": s["status"], "model": s.get("model", ""),
                  "cwd": s.get("cwd", ""),
                  "active": sum(1 for i in items if i["director_id"] == s["id"] and i["status"] in ACTIVE)}
                 for s in sessions.values() if not is_team_session(s) and s["id"] not in threads]
    decisions = _rows("SELECT d.*, s.name AS session_name FROM team_decisions d LEFT JOIN sessions s "
                      "ON s.id = d.session_id WHERE d.workspace_id = ? ORDER BY d.id DESC LIMIT 60",
                      (workspace_id,))
    proposals = []
    for p in _rows("SELECT * FROM skill_proposals WHERE workspace_id = ? ORDER BY id DESC LIMIT 20",
                   (workspace_id,)):
        proposals.append({k: p[k] for k in ("id", "subtask_id", "target", "path", "rationale",
                                            "status", "created_at", "decided_at")}
                         | {"diff": proposal_diff(p)})
    return {
        "directors": sorted(directors, key=lambda d: (-d["active"], d["name"])),
        "subtasks": items, "decisions": decisions, "proposals": proposals,
        "edges": edges(workspace_id), "graph_mode": graph_mode(workspace_id),
        "models": model_roles(workspace_id), "engines": installed_engines(),
        "playbooks": list(PLAYBOOKS), "max_active": MAX_ACTIVE, "max_attempts": MAX_ATTEMPTS,
    }


# ─── HTTP routes (build_app gắn vào) ─────────────────────────────────────────

def routes():
    from starlette.requests import Request          # noqa: PLC0415
    from starlette.responses import JSONResponse    # noqa: PLC0415
    from starlette.routing import Route             # noqa: PLC0415
    so = _so()

    async def body(request: Request):
        try:
            b = await request.json()
        except Exception:  # noqa: BLE001
            b = {}
        return b if isinstance(b, dict) else {}

    def reply(res, created=False):
        if isinstance(res, dict) and res.get("error"):
            return JSONResponse(res, status_code=404 if res["error"] == "not found" else 400)
        return JSONResponse(res, status_code=201 if created else 200)

    def ws_of(request, b=None):
        return (b or {}).get("workspace_id") or request.query_params.get("workspace_id") \
            or so.DEFAULT_WORKSPACE

    async def api_team(request: Request):
        return JSONResponse(await asyncio.to_thread(overview, ws_of(request)))

    async def api_spawn(request: Request):
        b = await body(request)
        return reply(spawn_subagent(b.get("director_id", ""), b.get("playbook", ""), b.get("brief", ""),
                                    b.get("done_when", ""), b.get("title", ""), b.get("model", "")),
                     created=True)

    async def api_cancel(request: Request):
        return reply(cancel(request.path_params["st"], by_user=True))

    async def api_reverify(request: Request):
        return reply(await reverify(request.path_params["st"]))

    async def api_discard(request: Request):
        return reply(await asyncio.to_thread(discard, request.path_params["st"]))

    async def api_diff(request: Request):
        st = get_subtask(request.path_params["st"])
        if not st:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"stat": await asyncio.to_thread(diff_stat, st),
                             "diff": await asyncio.to_thread(diff_text, st)})

    async def api_edges(request: Request):
        b = await body(request)
        ws = ws_of(request, b)
        if b.get("learn"):
            return reply(learn_edges(ws))
        if b.get("remove"):
            return reply(remove_edge(ws, b.get("from_role"), b.get("to_role")))
        if b.get("mode"):
            return reply({"mode": set_graph_mode(ws, b["mode"])})
        return reply(add_edge(ws, b.get("from_role"), b.get("to_role")))

    async def api_models(request: Request):
        b = await body(request)
        return reply(set_model_roles(ws_of(request, b), b.get("roles") or {}))

    async def api_reflect(request: Request):
        b = await body(request)
        target = f"role:{b['session_id']}" if b.get("session_id") else f"playbook:{b.get('playbook', '')}"
        return reply(start_reflect(target, b.get("director_id") or b.get("session_id", "")),
                     created=True)

    async def api_decide(request: Request):
        b = await body(request)
        return reply(decide_proposal(int(request.path_params["pid"]), bool(b.get("approve"))))

    return [
        Route("/api/team", api_team),
        Route("/api/team/spawn", api_spawn, methods=["POST"]),
        Route("/api/team/subtasks/{st}/cancel", api_cancel, methods=["POST"]),
        Route("/api/team/subtasks/{st}/reverify", api_reverify, methods=["POST"]),
        Route("/api/team/subtasks/{st}/discard", api_discard, methods=["POST"]),
        Route("/api/team/subtasks/{st}/diff", api_diff),
        Route("/api/team/edges", api_edges, methods=["POST"]),
        Route("/api/team/models", api_models, methods=["POST"]),
        Route("/api/team/reflect", api_reflect, methods=["POST"]),
        Route("/api/team/proposals/{pid:int}", api_decide, methods=["POST"]),
    ]

