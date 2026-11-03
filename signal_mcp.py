"""
Signal MCP Server — agent-to-agent signaling for the Session Orchestrator

MCP tool để các headless agent tự phát signal cho nhau qua orchestrator.
Ví dụ: Artist Director gọi send_signal(to_role="developer", ...) → orchestrator
resume session developer với message đó.

Kiến trúc: MCP tool này chỉ là "mỏ phát" — nó POST tới Control API của orchestrator
(ORCH_URL/api/signals). Orchestrator là single-writer, resolve role → session_id.

Env:
  ORCH_URL           URL orchestrator Control API (default http://localhost:8992)
  SIGNAL_MCP_HOST    bind (default 0.0.0.0)
  SIGNAL_MCP_PORT    port (default 8993)

Start:  python3 signal_mcp.py
Agent:  claude mcp add --transport http signal http://localhost:8993/mcp
"""

import json
import os
from contextlib import asynccontextmanager

import httpx
from mcp.server.fastmcp import FastMCP

ORCH_URL = os.environ.get("ORCH_URL", "http://localhost:8992").rstrip("/")
HOST = os.environ.get("SIGNAL_MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("SIGNAL_MCP_PORT", "8993"))

# Khi được mount chung process với orchestrator, đặt cờ này = True để các tool gọi
# thẳng hàm orchestrator (không tự POST HTTP về chính mình). Standalone thì để False.
_INPROC = False


@asynccontextmanager
async def lifespan(server):
    yield {}


mcp = FastMCP("Agent-Signal", lifespan=lifespan, host=HOST, port=PORT)


def _sender_workspace(orch, from_role: str = "", workspace_id: str = ""):
    """Workspace của agent ĐANG GỌI, theo thứ tự tin cậy:
      1. workspace_id truyền thẳng (orchestrator cấp lúc spawn — chính xác nhất)
      2. suy từ from_role NẾU tên đó chỉ tồn tại ở đúng 1 workspace
    Không xác định được → None (caller tự quyết: _enqueue resolve toàn cục, list_agents từ chối).
    Trùng tên giữa nhiều workspace thì KHÔNG đoán bừa — đoán sai là rò sang tenant khác."""
    if workspace_id:
        return workspace_id
    if from_role:
        # Tên cũ của card đã đổi tên cũng tính: agent đó vẫn ký from_role bằng tên nó nhớ.
        matches = ({s.get("workspace_id") for s in orch.list_sessions() if s.get("name") == from_role}
                   or {s.get("workspace_id") for s in orch.alias_sessions(from_role)})
        if len(matches) == 1:
            return next(iter(matches))
    return None


def _check_from_role(orch, from_role: str):
    """from_role phải là TÊN VAI CÓ THẬT. Trả "" nếu hợp lệ, chuỗi lỗi nếu không.

    from_role là chuỗi tự do do model tự điền — bịa một tên không tồn tại thì signal VẪN vào DB,
    và hỏng âm thầm 2 chỗ:
      1. canvas không vẽ được mũi tên (không có node nguồn để nối);
      2. trần ping-pong (id=94) khoá theo TÊN cặp → mỗi tên bịa là một hạn mức mới, model chỉ cần
         đổi from_role là lách được trần.
    Tên rỗng/'human'/'user' là mốc NGƯỜI gửi (_HUMAN_SENDERS) — giữ nguyên, không kiểm.
    """
    if not from_role or from_role.strip().lower() in orch._HUMAN_SENDERS:
        return ""
    names = {s.get("name") for s in orch.list_sessions()}
    if from_role in names or orch.alias_sessions(from_role):   # tên cũ của card đã đổi tên
        return ""
    return (f"⛔ from_role='{from_role}' không phải vai nào đang đăng ký. Điền ĐÚNG tên session "
            f"của bạn (gọi list_agents để xem danh sách), đừng tự đặt tên — sai tên thì "
            f"orchestrator không vẽ được luồng việc và không đếm đúng số lượt trao đổi.")


async def _enqueue(to_role: str, message: str, from_role: str = "", requires_approval: int = 0,
                   workspace_id: str = "", expect_reply: bool = True, done_when: str = ""):
    """Đẩy 1 signal vào orchestrator. In-process nếu chạy chung, ngược lại POST HTTP.

    Đa tenant: resolve `to_role` TRONG CÙNG workspace với người gửi — để hai workspace trùng
    role không phát tín hiệu xuyên nhau. Xác định workspace người gửi theo thứ tự tin cậy:
      1. `workspace_id` truyền thẳng (orchestrator cấp cho agent khi spawn — CHÍNH XÁC nhất).
      2. suy từ `from_role` nếu tên đó chỉ tồn tại ở đúng 1 workspace (an toàn khi không trùng).
    Không xác định được → resolve toàn cục (tương thích single-tenant cũ).

    Trả về (ok, data_or_error_str). data là dict {id, to_session, workspace_id} khi ok.
    """
    if _INPROC:
        import session_orchestrator as orch
        # Chỉ suy workspace từ tên khi tên đó KHÔNG trùng giữa các workspace (nếu trùng thì
        # không đoán bừa — để resolve toàn cục, tránh gửi nhầm tenant).
        sender_ws = _sender_workspace(orch, from_role, workspace_id)
        err = _check_from_role(orch, from_role)
        if err:
            return False, err
        # from_role: alias 'orch' resolve theo người gửi (chọn orch cùng cwd khi workspace có nhiều project)
        target = orch.resolve_session_id(to_role, sender_ws, from_role)
        if not target:
            scope = f" trong workspace '{sender_ws}'" if sender_ws else ""
            return False, f"không tìm thấy session cho '{to_role}'{scope}"
        target_ws = orch.get_session(target).get("workspace_id") or orch.DEFAULT_WORKSPACE
        import team
        sender = None
        if done_when.strip():
            # Việc có hợp đồng: đích không tự báo cáo; verifier kiểm rồi orchestrator báo về đây.
            sender = orch.get_session_by_name(orch.current_name(from_role, target_ws), target_ws)
            if not sender:
                return False, "⛔ done_when cần from_role là tên session thật của bạn."
            message, expect_reply = team.handoff_message(message, done_when.strip()), False
        try:
            sid = orch.enqueue_signal(target, message, from_role, int(requires_approval), 0, target_ws,
                                      expects_reply=expect_reply)
        except orch.SignalRefused as e:
            # Trần ping-pong id=94, đồ thị bàn giao, sub-agent tự signal: trả NGUYÊN VĂN lời hướng
            # dẫn cho agent (nó phải đọc được là cần báo cáo cho người dùng, không phải thử lại).
            return False, str(e)
        if sender:
            team.create_handoff(sender["id"], target, sid, done_when.strip(), message)
        orch.publish({"type": "signal", "id": sid, "status": "pending",
                      "to_session": target, "workspace_id": target_ws})
        return True, {"id": sid, "to_session": target, "workspace_id": target_ws}

    payload = {
        "to_role": to_role,
        "message": message,
        "from_role": from_role,
        "requires_approval": int(requires_approval),
        "expects_reply": bool(expect_reply),
        "done_when": done_when,
    }
    if workspace_id:
        payload["workspace_id"] = workspace_id
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{ORCH_URL}/api/signals", json=payload)
    except Exception as e:  # noqa: BLE001
        return False, f"Lỗi kết nối orchestrator tại {ORCH_URL}: {e}"
    if r.status_code >= 400:
        return False, f"({r.status_code}): {r.text}"
    return True, r.json()


@mcp.tool()
async def send_signal(to_role: str, message: str, from_role: str = "", requires_approval: bool = False,
                      workspace_id: str = "", expect_reply: bool = True, done_when: str = ""):
    """Gửi signal tới một agent khác. Orchestrator sẽ inject message vào session đó.

    Dùng khi agent hiện tại cần bàn giao/thông báo cho agent khác (vd Artist Director
    → Developer để code, Developer → Artist Director để review).

    Args:
        to_role: Role/tên agent đích (vd "backend", "reviewer"). Phải khớp name đã register
                 với orchestrator — gọi list_agents nếu không chắc, roster đổi liên tục.
        message: Nội dung yêu cầu/thông báo — sẽ trở thành user message cho agent đích.
        from_role: TÊN SESSION CỦA CHÍNH BẠN, đúng như orchestrator đăng ký (gọi list_agents
                 nếu không chắc). ĐỪNG tự đặt tên mô tả nghề nghiệp — tên không có thật sẽ bị
                 từ chối, vì orchestrator dựa vào nó để vẽ luồng việc và đếm số lượt trao đổi.
        requires_approval: True nếu là thao tác nhạy cảm cần con người duyệt trên dashboard.
        workspace_id: (đa tenant) workspace của agent gửi — orchestrator cấp cho bạn khi
                 spawn. Truyền vào để signal chỉ resolve trong đúng workspace này (bắt buộc
                 khi role bị trùng giữa nhiều workspace). Bỏ trống nếu chạy đơn tenant.
        expect_reply: False = yêu cầu MỘT CHIỀU (cập nhật, thông báo): vai kia làm xong là dừng,
                 KHÔNG gửi báo cáo về. Để True khi bạn cần kết quả để làm tiếp. Báo cáo
                 [REPORT] cho việc người kia giao thì orchestrator tự đánh dấu, không cần đặt.
        done_when: (tuỳ chọn) điều kiện QUAN SÁT ĐƯỢC để coi là xong (lệnh + kết quả mong đợi, luồng
                 chạy trên app thật, giá trị đọc lại). Có nó thì một verifier độc lập kiểm sau khi
                 vai kia làm xong; bạn nhận [HANDOFF VERIFIED]/[HANDOFF FAILED] thay vì [REPORT].
    """
    ok, data = await _enqueue(to_role, message, from_role, 1 if requires_approval else 0, workspace_id,
                              expect_reply, done_when)
    if not ok:
        # Trần ping-pong (id=94) và from_role sai đều đã là câu chỉ dẫn hoàn chỉnh — trả nguyên
        # văn, đừng bọc thêm chữ "Lỗi" khiến agent tưởng là trục trặc kỹ thuật rồi thử lại.
        return str(data) if str(data).startswith("⛔") else f"Lỗi gửi signal {data}"
    return f"Đã gửi signal #{data.get('id')} tới '{to_role}' (target: {data.get('to_session')})."


@mcp.tool()
async def compact_context(role: str = "", focus: str = "", from_role: str = "", workspace_id: str = ""):
    """Nén (compact) context của một agent để tránh phình transcript khi làm việc dài.

    Gửi lệnh /compact tới session đích qua orchestrator. Vì đi qua per-session lock,
    nếu agent tự nén chính mình thì việc nén sẽ chạy NGAY SAU khi lượt hiện tại kết thúc
    (an toàn, không cắt ngang). Dùng sau khi hoàn tất một subtask lớn hoặc khi thấy nặng.

    Args:
        role: Role/tên agent cần nén. Bỏ trống = nén chính agent đang gọi (dùng from_role).
        focus: (tùy chọn) nội dung cần giữ lại khi nén, vd "giữ API contract, bỏ log debug".
        from_role: Role của agent gọi (để audit; cũng là đích nếu role trống).
        workspace_id: (đa tenant) workspace của agent — resolve role trong đúng workspace này.
    """
    target = role or from_role
    if not target:
        return "Cần 'role' (hoặc 'from_role') của agent cần nén."
    message = "/compact" + (f" {focus}" if focus.strip() else "")
    # Nén là việc vận hành, không có gì để báo cáo ngược.
    ok, data = await _enqueue(target, message, from_role, 0, workspace_id, expect_reply=False)
    if not ok:
        return f"Lỗi gửi lệnh compact {data}"
    return f"Đã lên lịch compact cho '{target}' (signal #{data.get('id')})."


@mcp.tool()
async def list_agents(from_role: str = "", workspace_id: str = ""):
    """Liệt kê các agent (session) TRONG CÙNG WORKSPACE với bạn + trạng thái.

    Dùng để biết có thể gửi signal cho ai (to_role nào hợp lệ). CHỈ thấy agent cùng workspace —
    agent của workspace khác không hiện ra và cũng không gửi signal tới được.

    Args:
        from_role: Role của chính bạn (vai đang gọi). Dùng để xác định workspace.
        workspace_id: Workspace của bạn — orchestrator cấp lúc spawn. Truyền vào là chắc nhất
                 (bắt buộc khi role bị trùng tên giữa nhiều workspace).
    """
    if _INPROC:
        import session_orchestrator as orch
        # id=94: KHÔNG suy được workspace thì TỪ CHỐI, không trả toàn bộ. Trả hết = rò danh sách
        # agent của tenant khác, và mời model gửi signal xuyên workspace.
        sender_ws = _sender_workspace(orch, from_role, workspace_id)
        if sender_ws is None:
            return ("Cần biết bạn thuộc workspace nào mới liệt kê được. Gọi lại kèm from_role="
                    "<vai của bạn> (hoặc workspace_id nếu vai bị trùng tên giữa nhiều workspace).")
        # Phiên của đội sub-agent không phải đồng nghiệp để nhắn: director theo dõi chúng bằng
        # list_subagents.
        sessions = [s for s in orch.list_sessions()
                    if s.get("workspace_id") == sender_ws and not s.get("team_role")]
    else:
        if not workspace_id:
            return ("Chế độ HTTP cần workspace_id tường minh để lọc đúng workspace của bạn "
                    "(orchestrator cấp lúc spawn).")
        sender_ws = workspace_id
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{ORCH_URL}/api/sessions", params={"workspace_id": workspace_id})
        except Exception as e:  # noqa: BLE001
            return f"Lỗi kết nối orchestrator tại {ORCH_URL}: {e}"
        if r.status_code >= 400:
            return f"Lỗi ({r.status_code}): {r.text}"
        sessions = [s for s in r.json() if not s.get("team")]
    agents = [{"role": s["name"], "status": s["status"], "project": s.get("project", "")} for s in sessions]
    if not agents:
        return f"Chưa có agent nào trong workspace '{sender_ws}'."
    return json.dumps(agents, ensure_ascii=False)


# ─── Đội sub-agent (director) — xem team.py ─────────────────────────────────
# Chỉ chạy in-process: cần đọc thẳng DB và dựng phiên. Bản signal_mcp chạy độc lập không có.

def _caller(from_role: str, workspace_id: str = ""):
    """(orch, session của agent đang gọi, lỗi). Gọi tool đội bằng tên giả là bị từ chối — tên đó
    quyết định ai được giao việc, ai được chấm."""
    if not _INPROC:
        return None, None, "Team tools need the orchestrator's built-in signal server (/signal/mcp)."
    import session_orchestrator as orch
    ws = _sender_workspace(orch, from_role, workspace_id)
    s = orch.get_session_by_name(orch.current_name(from_role, ws), ws) if ws else None
    if not s:
        return orch, None, ("⛔ from_role must be your exact session name (and workspace_id if the "
                            "name exists in several workspaces).")
    return orch, s, ""


@mcp.tool()
async def spawn_subagent(from_role: str, playbook: str, brief: str, done_when: str, title: str = "",
                         model: str = "", workspace_id: str = ""):
    """Delegate one unit of work to a new sub-agent (pstack style). Returns its subtask id at once;
    the orchestrator messages you [SUB-AGENT PASSED] or [SUB-AGENT FAILED] when it is done.

    Args:
        from_role: your exact session name.
        playbook: feature | bugfix | refactor (own git worktree + branch orch/<id>) or
                  investigate (read-only, answers a question in your folder).
        brief: the goal, the relevant files, constraints and what must not change. The sub-agent
               cannot see your conversation — make it stand on its own.
        done_when: the observable contract an independent verifier checks: a command and its
               expected output, a flow driven in the running app, a value read back. "The build
               passes" is not enough.
        title: a short label for the dashboard.
        model: optional Claude model (e.g. "opus", "sonnet"); default follows the workspace's
               model roles. Sub-agents run on Claude — only the user picks Gemini (agy).
        workspace_id: your workspace, if your name exists in several.
    """
    orch, me, err = _caller(from_role, workspace_id)
    if err:
        return err
    # agy chỉ khi người dùng chọn (tab Team), không phải director tự chọn.
    if model and orch.engine_from_model(model) != "claude":
        return ("⛔ Sub-agents run on Claude. Leave model empty or name a Claude model (opus, "
                "sonnet…) — only the user can pick Gemini, per role in the Team tab.")
    import team
    res = team.spawn_subagent(me["id"], playbook, brief, done_when, title, model)
    if res.get("error"):
        return f"⛔ {res['error']}"
    return (f"Sub-agent {res['id']} queued ({playbook}). End your turn when you have delegated "
            f"everything; you will be messaged when it passes or fails verification.")


@mcp.tool()
async def list_subagents(from_role: str, workspace_id: str = ""):
    """Status of the work you delegated: queued/running/verifying/fixing/passed/failed/cancelled,
    verification attempts, and the branch to merge for passed ones."""
    orch, me, err = _caller(from_role, workspace_id)
    if err:
        return err
    import team
    rows = team.list_for_director(me["id"])
    return json.dumps(rows, ensure_ascii=False) if rows else "You have not delegated anything yet."


@mcp.tool()
async def cancel_subagent(from_role: str, subtask_id: str, workspace_id: str = ""):
    """Stop a sub-agent you delegated (it is killed if running). Its worktree stays until discarded
    on the dashboard."""
    orch, me, err = _caller(from_role, workspace_id)
    if err:
        return err
    import team
    st = team.get_subtask(subtask_id)
    if not st or st["director_id"] != me["id"]:
        return "⛔ not one of your subtasks"
    res = team.cancel(subtask_id)
    return f"⛔ {res['error']}" if res.get("error") else f"Cancelled {subtask_id}."


@mcp.tool()
async def record_decision(from_role: str, decision: str, reason: str = "", evidence: str = "",
                          workspace_id: str = ""):
    """Log a decision that shapes the work (a design chosen, a unit cut, a cause ruled out, a
    finding dismissed) with the reason and the evidence. The user reviews this trail on the Team
    tab, so long work stays auditable."""
    import team
    if not _INPROC:
        return "Team tools need the orchestrator's built-in signal server (/signal/mcp)."
    return team.record_decision(from_role, decision, reason, evidence, workspace_id)


@mcp.tool()
async def submit_verdict(from_role: str, subtask_id: str, verdict: str, key: str = "",
                         evidence: str = "", findings: str = "", workspace_id: str = ""):
    """Verifiers only: record your verdict for the subtask you are checking, exactly once.

    Args:
        key: the key given in your [VERIFY] task message.
        verdict: PASS only if you observed every clause of Done means yourself; otherwise FAIL.
        evidence: the commands you ran and what they printed, per clause.
        findings: on FAIL — what is wrong, where, and how to reproduce it, so the builder can act.
    """
    if not _INPROC:
        return "Team tools need the orchestrator's built-in signal server (/signal/mcp)."
    import team
    return team.submit_verdict(from_role, subtask_id, verdict, evidence, findings, workspace_id,
                               key=key)


@mcp.tool()
async def propose_skill_change(from_role: str, subtask_id: str, new_text: str, rationale: str = "",
                               key: str = "", workspace_id: str = ""):
    """Reflection agents only: propose the full new SKILL.md for the playbook you were asked to
    improve. Nothing is written until a human approves it on the dashboard."""
    if not _INPROC:
        return "Team tools need the orchestrator's built-in signal server (/signal/mcp)."
    import team
    return team.propose_skill_change(from_role, subtask_id, new_text, rationale, key=key)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
