import json
import os
import sys
import sqlite3
import asyncio
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

DB_DIR = Path.home() / ".sync_bridge_db"

def _resolve_db_path():
    if os.environ.get("DB_FILE"):
        return os.environ["DB_FILE"]
    name = sys.argv[1] if len(sys.argv) > 1 else "default"
    DB_DIR.mkdir(parents=True, exist_ok=True)
    return str(DB_DIR / f"{name}.db")

DB_FILE = _resolve_db_path()
SYNC_HOST = os.environ.get("SYNC_HOST", "0.0.0.0")
SYNC_PORT = int(os.environ.get("SYNC_PORT", "8989"))

_db_lock = asyncio.Lock()
_change_event = anyio.Event()


def _signal_change():
    global _change_event
    _change_event.set()
    _change_event = anyio.Event()


def _get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db():
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS api_requirements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT NOT NULL,
            method TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS change_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT NOT NULL,
            requirement_json TEXT
        );
    """)
    conn.commit()
    conn.close()


def _log_change(conn, action, detail, requirement=None):
    ts = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO change_log (timestamp, action, detail, requirement_json) VALUES (?, ?, ?, ?)",
        (ts, action, detail, json.dumps(requirement, ensure_ascii=False) if requirement else None),
    )


def _row_to_dict(row):
    return dict(row)


def _backup_db():
    if not os.path.exists(DB_FILE):
        return ""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak_path = f"{DB_FILE}.{timestamp}.bak"
    src = sqlite3.connect(DB_FILE)
    dst = sqlite3.connect(bak_path)
    src.backup(dst)
    src.close()
    dst.close()
    return bak_path


@asynccontextmanager
async def lifespan(server):
    _init_db()
    yield {}


mcp = FastMCP(
    "Agent-Sync-Bridge",
    lifespan=lifespan,
    host=SYNC_HOST,
    port=SYNC_PORT,
)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request):
    return JSONResponse({"status": "ok", "server": "Agent-Sync-Bridge"})


# ─── Tools ────────────────────────────────────────────────────────────────────


@mcp.tool()
async def add_api_requirement(endpoint: str, method: str, description: str):
    """Ghi lại yêu cầu API mới từ Agent FE hoặc BE."""
    async with _db_lock:
        now = datetime.now().isoformat()
        conn = _get_conn()
        conn.execute(
            "INSERT INTO api_requirements (endpoint, method, description, status, created_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?)",
            (endpoint, method, description, now, now),
        )
        req = {"endpoint": endpoint, "method": method, "description": description, "status": "pending"}
        _log_change(conn, "add", f"Added {method} {endpoint}", req)
        conn.commit()
        conn.close()
    _signal_change()
    return f"Đã ghi nhận yêu cầu API: {method} {endpoint}"


@mcp.tool()
async def get_pending_requirements(status: str = ""):
    """Lấy danh sách các API specs đang hoạt động (chưa done).

    Args:
        status: Lọc theo trạng thái cụ thể (vd: "pending", "discuss", "confirm").
                Để trống = trả về tất cả specs chưa done.
    """
    conn = _get_conn()
    if status:
        rows = conn.execute(
            "SELECT id, endpoint, method, description, status FROM api_requirements WHERE status = ?", (status,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, endpoint, method, description, status FROM api_requirements WHERE status != 'done'"
        ).fetchall()
    conn.close()

    if not rows:
        msg = f"Không có specs nào với status '{status}'." if status else "Không có specs nào đang hoạt động."
        return msg
    return json.dumps([_row_to_dict(r) for r in rows], ensure_ascii=False)


@mcp.tool()
async def list_api_requirements():
    """Lấy toàn bộ danh sách API specs hiện có trong DB."""
    conn = _get_conn()
    rows = conn.execute("SELECT id, endpoint, method, description, status FROM api_requirements").fetchall()
    conn.close()
    if not rows:
        return "DB hiện đang trống."
    return json.dumps([_row_to_dict(r) for r in rows], ensure_ascii=False)


@mcp.tool()
async def update_api_requirement(
    id: int,
    endpoint: str = "",
    method: str = "",
    description: str = "",
    status: str = "",
):
    """Chỉnh sửa một API spec có sẵn theo id.

    Args:
        id: ID của API spec (lấy từ list_api_requirements hoặc get_pending_requirements)
        endpoint: Endpoint mới (để trống nếu không đổi)
        method: Method mới (để trống nếu không đổi)
        description: Mô tả mới (để trống nếu không đổi)
        status: Trạng thái mới (để trống nếu không đổi)
    """
    async with _db_lock:
        conn = _get_conn()
        row = conn.execute("SELECT * FROM api_requirements WHERE id = ?", (id,)).fetchone()
        if not row:
            conn.close()
            return f"Lỗi: id {id} không tồn tại."

        updates = []
        params = []
        changes = []
        if endpoint:
            updates.append("endpoint = ?")
            params.append(endpoint)
            changes.append(f"endpoint={endpoint}")
        if method:
            updates.append("method = ?")
            params.append(method)
            changes.append(f"method={method}")
        if description:
            updates.append("description = ?")
            params.append(description)
            changes.append("description updated")
        if status:
            updates.append("status = ?")
            params.append(status)
            changes.append(f"status={status}")

        if updates:
            updates.append("updated_at = ?")
            params.append(datetime.now().isoformat())
            params.append(id)
            conn.execute(f"UPDATE api_requirements SET {', '.join(updates)} WHERE id = ?", params)

        updated = conn.execute("SELECT * FROM api_requirements WHERE id = ?", (id,)).fetchone()
        _log_change(conn, "update", f"Updated #{id} {updated['method']} {updated['endpoint']}: {', '.join(changes)}", _row_to_dict(updated))
        conn.commit()
        conn.close()
    _signal_change()
    return f"Đã cập nhật API spec #{id}: {updated['method']} {updated['endpoint']}"


@mcp.tool()
async def reset_api_requirements():
    """Xóa toàn bộ API specs trong DB để bắt đầu mới. Tự động backup trước khi reset."""
    async with _db_lock:
        bak = _backup_db()
        conn = _get_conn()
        count = conn.execute("SELECT COUNT(*) FROM api_requirements").fetchone()[0]
        conn.execute("DELETE FROM api_requirements")
        _log_change(conn, "reset", f"Reset DB ({count} specs cleared). Backup: {bak}")
        conn.commit()
        conn.close()
    _signal_change()
    return f"Đã xóa toàn bộ API specs. DB đã được reset. Backup: {bak}"


@mcp.tool()
async def watch_for_changes(since: str = "", timeout: int = 30):
    """Chờ đợi thay đổi mới từ phía đối tác (BE hoặc FE). Tool sẽ block cho đến khi
    có thay đổi mới hoặc hết timeout.

    Args:
        since: ISO timestamp - chỉ trả về changes sau thời điểm này. Để trống = lấy tất cả.
        timeout: Số giây tối đa chờ thay đổi (mặc định 30, tối đa 120).
    """
    timeout = max(1, min(timeout, 120))

    conn = _get_conn()
    if since:
        rows = conn.execute("SELECT * FROM change_log WHERE timestamp > ? ORDER BY id", (since,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM change_log ORDER BY id").fetchall()
    conn.close()

    if rows:
        return json.dumps([_row_to_dict(r) for r in rows], ensure_ascii=False)

    current_event = _change_event
    try:
        with anyio.fail_after(timeout):
            await current_event.wait()
    except TimeoutError:
        return json.dumps({"status": "timeout", "message": f"Không có thay đổi nào trong {timeout}s."})

    conn = _get_conn()
    if since:
        rows = conn.execute("SELECT * FROM change_log WHERE timestamp > ? ORDER BY id", (since,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM change_log ORDER BY id").fetchall()
    conn.close()
    return json.dumps([_row_to_dict(r) for r in rows], ensure_ascii=False) if rows else json.dumps({"status": "no_changes"})


@mcp.tool()
async def get_change_log(limit: int = 20):
    """Lấy lịch sử thay đổi gần nhất.

    Args:
        limit: Số lượng entries tối đa trả về (mặc định 20).
    """
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM change_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    if not rows:
        return "Chưa có thay đổi nào."
    return json.dumps([_row_to_dict(r) for r in reversed(rows)], ensure_ascii=False)


# ─── Resources ────────────────────────────────────────────────────────────────


@mcp.resource("sync-bridge://requirements")
def requirements_resource():
    """Toàn bộ API requirements hiện tại."""
    conn = _get_conn()
    rows = conn.execute("SELECT id, endpoint, method, description, status FROM api_requirements").fetchall()
    conn.close()
    return json.dumps([_row_to_dict(r) for r in rows], indent=2, ensure_ascii=False)


@mcp.resource("sync-bridge://changelog")
def changelog_resource():
    """50 thay đổi gần nhất."""
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM change_log ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return json.dumps([_row_to_dict(r) for r in reversed(rows)], indent=2, ensure_ascii=False)


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _init_db()
    mcp.run(transport="streamable-http")
