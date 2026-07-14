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
        sender_ws = workspace_id or None
        # Chỉ suy workspace từ tên khi tên đó KHÔNG trùng giữa các workspace (nếu trùng thì
        # không đoán bừa — để resolve toàn cục, tránh gửi nhầm tenant).
        if sender_ws is None and from_role:
            matches = {s.get("workspace_id") for s in orch.list_sessions() if s.get("name") == from_role}
            if len(matches) == 1:
                sender_ws = next(iter(matches))
        target = orch.resolve_session_id(to_role, sender_ws)
        if not target:
            scope = f" trong workspace '{sender_ws}'" if sender_ws else ""
            return False, f"không tìm thấy session cho '{to_role}'{scope}"
        target_ws = orch.get_session(target).get("workspace_id") or orch.DEFAULT_WORKSPACE
        sid = orch.enqueue_signal(target, message, from_role, int(requires_approval), 0, target_ws)
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
        from_role: Role của agent gửi (để audit, tùy chọn).
        requires_approval: True nếu là thao tác nhạy cảm cần con người duyệt trên dashboard.
        workspace_id: (đa tenant) workspace của agent gửi — orchestrator cấp cho bạn khi
                 spawn. Truyền vào để signal chỉ resolve trong đúng workspace này (bắt buộc
                 khi role bị trùng giữa nhiều workspace). Bỏ trống nếu chạy đơn tenant.
    """
    ok, data = await _enqueue(to_role, message, from_role, 1 if requires_approval else 0, workspace_id)
    if not ok:
        return f"Lỗi gửi signal {data}"
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
async def list_agents():
    """Liệt kê các agent (session) đang được orchestrator quản lý + trạng thái.

    Dùng để biết có thể gửi signal cho ai (to_role nào hợp lệ).
    """
    if _INPROC:
        import session_orchestrator as orch
        sessions = orch.list_sessions()
    else:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{ORCH_URL}/api/sessions")
        except Exception as e:  # noqa: BLE001
            return f"Lỗi kết nối orchestrator tại {ORCH_URL}: {e}"
        if r.status_code >= 400:
            return f"Lỗi ({r.status_code}): {r.text}"
        sessions = r.json()
    agents = [{"role": s["name"], "status": s["status"], "project": s.get("project", "")} for s in sessions]
    if not agents:
        return "Chưa có agent nào được register."
    return json.dumps(agents, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
