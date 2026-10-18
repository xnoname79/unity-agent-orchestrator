"""
Board MCP Server — the dashboard's Boards, as tools for agents.

A board is the user's plan: a goal, tasks in todo / doing / done, a reminder agent that nudges, and
optionally a research worker that leaves findings on task cards. These tools let an agent read a
board and keep it in step with the work — mark a task doing or done, fix an estimate, add a task it
discovered — without calling the HTTP API by hand.

Scoped like signal: an agent only sees boards in its own workspace, worked out from `from_role`
(or `workspace_id` when the role name is not unique). No tool deletes a board or a task.

Mounted by the orchestrator at /boards/mcp — no separate process, no extra port. Register it once
per CLI (the Boards tab has a button that does this for every installed CLI):

    claude mcp add --transport http --scope user boards http://127.0.0.1:8992/boards/mcp
"""

import json

from mcp.server.fastmcp import FastMCP

import signal_mcp

mcp = FastMCP("Boards")

_TASK_KEYS = ("id", "title", "detail", "status", "priority", "estimate_min", "started_at",
              "done_at", "worker_state", "findings")


def _orch():
    # Import muộn: module này được build_app() import, lúc đó session_orchestrator đã nạp xong
    # (và đã alias __main__ → xem check_single_module.py).
    import session_orchestrator as orch
    return orch


def _scope(orch, from_role, workspace_id):
    """(workspace, lỗi). Không biết agent thuộc workspace nào thì từ chối — trả hết là rò board
    của tenant khác, cùng lý do như list_agents."""
    if not from_role:
        return None, "⛔ Pass from_role=<your session name> so the board tools know your workspace."
    if signal_mcp._check_from_role(orch, from_role):
        return None, (f"⛔ from_role={from_role!r} is not a registered session. Pass your own "
                      f"session name, exactly as the orchestrator gave it to you.")
    ws = signal_mcp._sender_workspace(orch, from_role, workspace_id)
    if ws is None:
        return None, ("⛔ That role name exists in more than one workspace — pass workspace_id "
                      "too (the orchestrator gave it to you when you were spawned).")
    return ws, ""


def _board(orch, board_id, from_role, workspace_id):
    ws, err = _scope(orch, from_role, workspace_id)
    if err:
        return None, err
    b = orch.get_board(board_id)
    if not b or b["workspace_id"] != ws:
        return None, f"⛔ No board {board_id!r} in your workspace — list_boards shows them."
    return b, ""


def _changed(orch, b):
    # Agent chạm vào board = đang làm việc, y như người dùng kéo card: lời nhắc kế tiếp không
    # cần nặng giọng hơn.
    orch.update_board(b["id"], strikes=0)
    orch.publish({"type": "board", "board": b["id"], "workspace_id": b["workspace_id"]})


def _view(orch, b):
    v = orch.board_view(orch.get_board(b["id"]))
    return {"id": v["id"], "name": v["name"], "goal": v["goal"], "context": v["context"],
            "status": v["status"], "agent": v["agent"], "next_ping": v["next_ping"],
            "done": v["done"], "total": v["total"],
            "tasks": [{k: t.get(k) for k in _TASK_KEYS} for t in v["tasks"]]}


@mcp.tool()
async def list_boards(from_role: str, workspace_id: str = ""):
    """The boards in your workspace: id, name, goal, status, whose board it is, and progress.

    Args:
        from_role: Your session name.
        workspace_id: Your workspace, if your role name exists in more than one.
    """
    orch = _orch()
    ws, err = _scope(orch, from_role, workspace_id)
    if err:
        return err
    return json.dumps([{"id": v["id"], "name": v["name"], "goal": v["goal"],
                        "status": v["status"], "agent": v["agent"], "done": v["done"],
                        "total": v["total"]}
                       for v in (orch.board_view(b) for b in orch.list_boards(ws))],
                      ensure_ascii=False)


@mcp.tool()
async def get_board(board_id: str, from_role: str, workspace_id: str = ""):
    """One board with all its tasks.

    Each task has id, title, detail, status (todo | doing | done), priority, estimate_min, and
    what the research worker found: worker_state ('' = not looked at, researching, researched,
    failed) and findings (markdown). Read findings before starting a researched task.
    """
    orch = _orch()
    b, err = _board(orch, board_id, from_role, workspace_id)
    return err or json.dumps(_view(orch, b), ensure_ascii=False)


@mcp.tool()
async def add_task(board_id: str, title: str, from_role: str, detail: str = "",
                   estimate_min: int = 30, priority: str = "normal", workspace_id: str = ""):
    """Add a task to a board — something that came up while working that the user should see.

    Args:
        priority: high, normal or low.
        estimate_min: How long it should take, in minutes (5 or more).
    Returns the board with the new task.
    """
    orch = _orch()
    b, err = _board(orch, board_id, from_role, workspace_id)
    if err:
        return err
    if not str(title or "").strip():
        return "⛔ A task needs a title."
    if priority not in ("high", "normal", "low"):
        return "⛔ priority must be high, normal or low."
    orch.add_task(b["id"], title.strip(), detail or "", estimate_min, priority)
    _changed(orch, b)
    return json.dumps(_view(orch, b), ensure_ascii=False)


@mcp.tool()
async def update_task(board_id: str, task_id: int, from_role: str, status: str = "",
                      title: str = "", detail: str = "", estimate_min: int = 0,
                      priority: str = "", workspace_id: str = ""):
    """Change a task: move it (status todo | doing | done), rename it, rewrite its detail,
    re-estimate it or change its priority. Leave a field empty (or 0) to keep it.

    Mark a task doing when you start on it and done when it is finished: the board's reminders
    are timed from that, so a task left in todo while you work on it means badly-timed nudges.
    Returns the board.
    """
    orch = _orch()
    b, err = _board(orch, board_id, from_role, workspace_id)
    if err:
        return err
    if not any(t["id"] == task_id for t in orch.board_tasks(b["id"])):
        return f"⛔ No task {task_id} on board {board_id!r} — get_board lists them."
    if status not in ("", "todo", "doing", "done"):
        return "⛔ status must be todo, doing or done."
    if priority not in ("", "high", "normal", "low"):
        return "⛔ priority must be high, normal or low."
    patch = {k: v for k, v in (("status", status), ("title", title.strip()), ("detail", detail),
                               ("estimate_min", estimate_min), ("priority", priority)) if v}
    if not patch:
        return "⛔ Nothing to change — pass at least one field."
    orch.update_task(task_id, **patch)
    _changed(orch, b)
    return json.dumps(_view(orch, b), ensure_ascii=False)
