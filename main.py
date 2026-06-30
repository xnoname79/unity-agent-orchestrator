"""
Agent-Sync-Bridge MCP Server

Shared HTTP server (streamable-http) that synchronizes API specs between
multiple Claude sessions (BE, FE, etc.).

Architecture:
  - One server process, one port, unlimited projects
  - Each project gets its own SQLite DB at ~/.sync_bridge_db/<project>.db
  - Tag-based filtering for multi-app projects

Start:  python3 main.py
Setup:  ./setup.sh --project <name> [--tag <tag>]
"""

import json
import os
import sqlite3
import asyncio
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

DB_DIR = Path.home() / ".sync_bridge_db"
SYNC_HOST = os.environ.get("SYNC_HOST", "0.0.0.0")
SYNC_PORT = int(os.environ.get("SYNC_PORT", "8989"))

_db_lock = asyncio.Lock()


def _db_path(project: str) -> str:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    return str(DB_DIR / f"{project}.db")


def _get_conn(project: str):
    conn = sqlite3.connect(_db_path(project))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_project_db(project: str):
    conn = _get_conn(project)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS api_requirements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT NOT NULL,
            method TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            tag TEXT NOT NULL DEFAULT '',
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
    cols = [r[1] for r in conn.execute("PRAGMA table_info(api_requirements)").fetchall()]
    if "tag" not in cols:
        conn.execute("ALTER TABLE api_requirements ADD COLUMN tag TEXT NOT NULL DEFAULT ''")
    conn.commit()
    conn.close()


def _ensure_db(project: str):
    db_path = _db_path(project)
    if not os.path.exists(db_path):
        _init_project_db(project)


def _log_change(conn, action, detail, requirement=None):
    ts = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO change_log (timestamp, action, detail, requirement_json) VALUES (?, ?, ?, ?)",
        (ts, action, detail, json.dumps(requirement, ensure_ascii=False) if requirement else None),
    )


def _row_to_dict(row):
    return dict(row)


def _backup_db(project: str):
    db_path = _db_path(project)
    if not os.path.exists(db_path):
        return ""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak_path = f"{db_path}.{timestamp}.bak"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(bak_path)
    src.backup(dst)
    src.close()
    dst.close()
    return bak_path


@asynccontextmanager
async def lifespan(server):
    yield {}


mcp = FastMCP(
    "Agent-Sync-Bridge",
    lifespan=lifespan,
    host=SYNC_HOST,
    port=SYNC_PORT,
)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request):
    projects = [f.stem for f in DB_DIR.glob("*.db")] if DB_DIR.exists() else []
    return JSONResponse({"status": "ok", "server": "Agent-Sync-Bridge", "projects": projects})


# ─── Tools ────────────────────────────────────────────────────────────────────


@mcp.tool()
async def add_api_requirement(project: str, endpoint: str, method: str, description: str, tag: str = ""):
    """Ghi lại yêu cầu API mới từ Agent FE hoặc BE.

    Args:
        project: Tên project (vd: "my-app", "ecommerce"). DB tự tạo tại ~/.sync_bridge_db/<project>.db
        endpoint: API endpoint (vd: "/api/users")
        method: HTTP method (GET, POST, PUT, DELETE, ...)
        description: Mô tả chi tiết bao gồm request/response format
        tag: Label để phân loại spec cho app cụ thể (vd: "user-app", "admin-app"). Để trống = dùng chung.
    """
    async with _db_lock:
        _ensure_db(project)
        now = datetime.now().isoformat()
        conn = _get_conn(project)
        conn.execute(
            "INSERT INTO api_requirements (endpoint, method, description, status, tag, created_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?, ?)",
            (endpoint, method, description, tag, now, now),
        )
        req = {"endpoint": endpoint, "method": method, "description": description, "status": "pending", "tag": tag}
        _log_change(conn, "add", f"Added {method} {endpoint}" + (f" [{tag}]" if tag else ""), req)
        conn.commit()
        conn.close()

    return f"[{project}] Đã ghi nhận yêu cầu API: {method} {endpoint}" + (f" (tag: {tag})" if tag else "")


@mcp.tool()
async def get_pending_requirements(project: str, status: str = "", tag: str = ""):
    """Lấy danh sách các API specs đang hoạt động (chưa done).

    Args:
        project: Tên project
        status: Lọc theo trạng thái cụ thể (vd: "pending", "discuss", "confirm").
                Để trống = trả về tất cả specs chưa done.
        tag: Lọc theo tag (vd: "user-app"). Để trống = trả về tất cả tags.
    """
    _ensure_db(project)
    conn = _get_conn(project)
    query = "SELECT id, endpoint, method, description, status, tag FROM api_requirements WHERE 1=1"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    else:
        query += " AND status != 'done'"
    if tag:
        query += " AND tag = ?"
        params.append(tag)
    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        filters = []
        if status:
            filters.append(f"status='{status}'")
        if tag:
            filters.append(f"tag='{tag}'")
        detail = f" ({', '.join(filters)})" if filters else ""
        return f"[{project}] Không có specs nào đang hoạt động{detail}."
    return json.dumps([_row_to_dict(r) for r in rows], ensure_ascii=False)


@mcp.tool()
async def list_api_requirements(project: str, tag: str = ""):
    """Lấy toàn bộ danh sách API specs hiện có trong DB.

    Args:
        project: Tên project
        tag: Lọc theo tag (vd: "user-app"). Để trống = trả về tất cả.
    """
    _ensure_db(project)
    conn = _get_conn(project)
    if tag:
        rows = conn.execute("SELECT id, endpoint, method, description, status, tag FROM api_requirements WHERE tag = ?", (tag,)).fetchall()
    else:
        rows = conn.execute("SELECT id, endpoint, method, description, status, tag FROM api_requirements").fetchall()
    conn.close()
    if not rows:
        return f"[{project}] DB hiện đang trống." + (f" (tag: {tag})" if tag else "")
    return json.dumps([_row_to_dict(r) for r in rows], ensure_ascii=False)


@mcp.tool()
async def update_api_requirement(
    project: str,
    id: int,
    endpoint: str = "",
    method: str = "",
    description: str = "",
    status: str = "",
    tag: str = "",
):
    """Chỉnh sửa một API spec có sẵn theo id.

    Args:
        project: Tên project
        id: ID của API spec (lấy từ list_api_requirements hoặc get_pending_requirements)
        endpoint: Endpoint mới (để trống nếu không đổi)
        method: Method mới (để trống nếu không đổi)
        description: Mô tả mới (để trống nếu không đổi)
        status: Trạng thái mới (để trống nếu không đổi)
        tag: Tag mới (để trống nếu không đổi)
    """
    async with _db_lock:
        _ensure_db(project)
        conn = _get_conn(project)
        row = conn.execute("SELECT * FROM api_requirements WHERE id = ?", (id,)).fetchone()
        if not row:
            conn.close()
            return f"[{project}] Lỗi: id {id} không tồn tại."

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
        if tag:
            updates.append("tag = ?")
            params.append(tag)
            changes.append(f"tag={tag}")

        if updates:
            updates.append("updated_at = ?")
            params.append(datetime.now().isoformat())
            params.append(id)
            conn.execute(f"UPDATE api_requirements SET {', '.join(updates)} WHERE id = ?", params)

        updated = conn.execute("SELECT * FROM api_requirements WHERE id = ?", (id,)).fetchone()
        _log_change(conn, "update", f"Updated #{id} {updated['method']} {updated['endpoint']}: {', '.join(changes)}", _row_to_dict(updated))
        conn.commit()
        conn.close()

    return f"[{project}] Đã cập nhật API spec #{id}: {updated['method']} {updated['endpoint']}"


@mcp.tool()
async def reset_api_requirements(project: str):
    """Xóa toàn bộ API specs trong DB để bắt đầu mới. Tự động backup trước khi reset.

    Args:
        project: Tên project
    """
    async with _db_lock:
        _ensure_db(project)
        bak = _backup_db(project)
        conn = _get_conn(project)
        count = conn.execute("SELECT COUNT(*) FROM api_requirements").fetchone()[0]
        conn.execute("DELETE FROM api_requirements")
        _log_change(conn, "reset", f"Reset DB ({count} specs cleared). Backup: {bak}")
        conn.commit()
        conn.close()

    return f"[{project}] Đã xóa toàn bộ API specs. DB đã được reset. Backup: {bak}"


@mcp.tool()
async def list_projects():
    """Liệt kê tất cả projects hiện có trong sync-bridge."""
    if not DB_DIR.exists():
        return "Chưa có project nào."
    projects = sorted(f.stem for f in DB_DIR.glob("*.db"))
    if not projects:
        return "Chưa có project nào."
    return json.dumps(projects, ensure_ascii=False)


# ─── Resources ────────────────────────────────────────────────────────────────


@mcp.resource("sync-bridge://projects")
def projects_resource():
    """Danh sách tất cả projects."""
    if not DB_DIR.exists():
        return "[]"
    projects = sorted(f.stem for f in DB_DIR.glob("*.db"))
    return json.dumps(projects, indent=2, ensure_ascii=False)


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
