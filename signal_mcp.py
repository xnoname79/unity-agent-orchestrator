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
        matches = {s.get("workspace_id") for s in orch.list_sessions() if s.get("name") == from_role}
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
    if from_role in names:
        return ""
    return (f"⛔ from_role='{from_role}' không phải vai nào đang đăng ký. Điền ĐÚNG tên session "
            f"của bạn (gọi list_agents để xem danh sách), đừng tự đặt tên — sai tên thì "
            f"orchestrator không vẽ được luồng việc và không đếm đúng số lượt trao đổi.")


async def _enqueue(to_role: str, message: str, from_role: str = "", requires_approval: int = 0,
                   workspace_id: str = ""):
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
        try:
            sid = orch.enqueue_signal(target, message, from_role, int(requires_approval), 0, target_ws)
        except orch.SignalPairCapExceeded as e:
            # id=94 trần ping-pong: trả NGUYÊN VĂN lời hướng dẫn cho agent (nó phải đọc được là
            # cần báo cáo cho người dùng, không phải thử lại).
            return False, str(e)
        orch.publish({"type": "signal", "id": sid, "status": "pending",
                      "to_session": target, "workspace_id": target_ws})
        return True, {"id": sid, "to_session": target, "workspace_id": target_ws}

    payload = {
        "to_role": to_role,
        "message": message,
        "from_role": from_role,
        "requires_approval": int(requires_approval),
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
                      workspace_id: str = ""):
    """Gửi signal tới một agent khác. Orchestrator sẽ inject message vào session đó.

    Dùng khi agent hiện tại cần bàn giao/thông báo cho agent khác (vd Artist Director
    → Developer để code, Developer → Artist Director để review).

    Args:
        to_role: Role/tên agent đích (vd "developer", "artist-director"). Phải khớp
                 name đã register với orchestrator.
        message: Nội dung yêu cầu/thông báo — sẽ trở thành user message cho agent đích.
        from_role: TÊN SESSION CỦA CHÍNH BẠN, đúng như orchestrator đăng ký (gọi list_agents
                 nếu không chắc). ĐỪNG tự đặt tên mô tả nghề nghiệp — tên không có thật sẽ bị
                 từ chối, vì orchestrator dựa vào nó để vẽ luồng việc và đếm số lượt trao đổi.
        requires_approval: True nếu là thao tác nhạy cảm cần con người duyệt trên dashboard.
        workspace_id: (đa tenant) workspace của agent gửi — orchestrator cấp cho bạn khi
                 spawn. Truyền vào để signal chỉ resolve trong đúng workspace này (bắt buộc
                 khi role bị trùng giữa nhiều workspace). Bỏ trống nếu chạy đơn tenant.
    """
    ok, data = await _enqueue(to_role, message, from_role, 1 if requires_approval else 0, workspace_id)
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
    ok, data = await _enqueue(target, message, from_role, 0, workspace_id)
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
        sessions = [s for s in orch.list_sessions() if s.get("workspace_id") == sender_ws]
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
        sessions = r.json()
    agents = [{"role": s["name"], "status": s["status"], "project": s.get("project", "")} for s in sessions]
    if not agents:
        return f"Chưa có agent nào trong workspace '{sender_ws}'."
    return json.dumps(agents, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
