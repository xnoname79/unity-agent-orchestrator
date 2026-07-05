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


@asynccontextmanager
async def lifespan(server):
    yield {}


mcp = FastMCP("Agent-Signal", lifespan=lifespan, host=HOST, port=PORT)


@mcp.tool()
async def send_signal(to_role: str, message: str, from_role: str = "", requires_approval: bool = False):
    """Gửi signal tới một agent khác. Orchestrator sẽ inject message vào session đó.

    Dùng khi agent hiện tại cần bàn giao/thông báo cho agent khác (vd Artist Director
    → Developer để code, Developer → Artist Director để review).

    Args:
        to_role: Role/tên agent đích (vd "developer", "artist-director"). Phải khớp
                 name đã register với orchestrator.
        message: Nội dung yêu cầu/thông báo — sẽ trở thành user message cho agent đích.
        from_role: Role của agent gửi (để audit, tùy chọn).
        requires_approval: True nếu là thao tác nhạy cảm cần con người duyệt trên dashboard.
    """
    payload = {
        "to_role": to_role,
        "message": message,
        "from_role": from_role,
        "requires_approval": 1 if requires_approval else 0,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{ORCH_URL}/api/signals", json=payload)
    except Exception as e:  # noqa: BLE001
        return f"Lỗi kết nối orchestrator tại {ORCH_URL}: {e}"
    if r.status_code >= 400:
        return f"Lỗi gửi signal ({r.status_code}): {r.text}"
    data = r.json()
    return f"Đã gửi signal #{data.get('id')} tới '{to_role}' (target: {data.get('to_session')})."


@mcp.tool()
async def list_agents():
    """Liệt kê các agent (session) đang được orchestrator quản lý + trạng thái.

    Dùng để biết có thể gửi signal cho ai (to_role nào hợp lệ).
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{ORCH_URL}/api/sessions")
    except Exception as e:  # noqa: BLE001
        return f"Lỗi kết nối orchestrator tại {ORCH_URL}: {e}"
    if r.status_code >= 400:
        return f"Lỗi ({r.status_code}): {r.text}"
    agents = [{"role": s["name"], "status": s["status"], "project": s.get("project", "")} for s in r.json()]
    if not agents:
        return "Chưa có agent nào được register."
    return json.dumps(agents, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
