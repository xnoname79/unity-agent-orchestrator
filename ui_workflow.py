"""
UI Workflow MCP Server

MCP server for managing browser UI test cases, reusable workflows, and run
history. Pairs with Playwright MCP (browser execution) — this is the
planning/tracking "brain", Playwright MCP is the "hands".

  - Test cases: named steps + expected result, grouped by tag
  - Workflows: reusable multi-step flows (login, checkout, ...)
  - Runs: pass/fail history with screenshots and notes
  - Export: dump a project's suite as JSON

Architecture:
  - Standalone HTTP server (streamable-http)
  - Each project gets its own SQLite DB at ~/.ui_workflow_db/<project>.db

Start:  python3 ui_workflow.py
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

DB_DIR = Path.home() / ".ui_workflow_db"
HOST = os.environ.get("UI_WORKFLOW_HOST", "0.0.0.0")
PORT = int(os.environ.get("UI_WORKFLOW_PORT", "8991"))

_db_lock = asyncio.Lock()


def _db_path(project: str) -> str:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    return str(DB_DIR / f"{project}.db")


def _get_conn(project: str):
    conn = sqlite3.connect(_db_path(project))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_db(project: str):
    conn = _get_conn(project)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS test_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            steps TEXT NOT NULL DEFAULT '[]',
            expected TEXT NOT NULL DEFAULT '',
            tag TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workflows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            steps TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_case_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            screenshots TEXT NOT NULL DEFAULT '[]',
            duration_ms INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (test_case_id) REFERENCES test_cases(id) ON DELETE CASCADE
        );
    """)
    conn.commit()
    conn.close()


def _ensure_db(project: str):
    if not os.path.exists(_db_path(project)):
        _init_db(project)


def _row_to_dict(row):
    return dict(row)


def _parse_json_field(value, fallback):
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return fallback


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
    "UI-Workflow",
    lifespan=lifespan,
    host=HOST,
    port=PORT,
)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request):
    projects = sorted(f.stem for f in DB_DIR.glob("*.db")) if DB_DIR.exists() else []
    return JSONResponse({"status": "ok", "server": "UI-Workflow", "projects": projects})


# ─── Test Cases ───────────────────────────────────────────────────────────────


@mcp.tool()
async def add_test_case(
    project: str,
    name: str,
    description: str = "",
    steps: str = "[]",
    expected: str = "",
    tag: str = "",
):
    """Tạo test case UI mới.

    Args:
        project: Tên project (vd: "shop-web"). DB tự tạo tại ~/.ui_workflow_db/<project>.db
        name: Tên test case (vd: "Đăng nhập thành công")
        description: Mô tả ngắn mục tiêu test
        steps: JSON array các bước, vd: '["mở /login","nhập email","nhập password","click Đăng nhập"]'
        expected: Kết quả mong đợi (vd: "chuyển tới /dashboard, hiện tên user")
        tag: Nhóm/scope (vd: "auth", "checkout") để lọc
    """
    async with _db_lock:
        _ensure_db(project)
        now = datetime.now().isoformat()
        conn = _get_conn(project)
        conn.execute(
            "INSERT INTO test_cases (name, description, steps, expected, tag, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
            (name, description, steps, expected, tag, now, now),
        )
        conn.commit()
        conn.close()
    return f"[{project}] Đã tạo test case: '{name}'" + (f" (tag: {tag})" if tag else "")


@mcp.tool()
async def list_test_cases(project: str, tag: str = "", status: str = ""):
    """Liệt kê test cases.

    Args:
        project: Tên project
        tag: Lọc theo tag. Để trống = tất cả.
        status: Lọc theo status (active, draft, deprecated). Để trống = tất cả.
    """
    _ensure_db(project)
    conn = _get_conn(project)
    query = "SELECT id, name, description, tag, status FROM test_cases WHERE 1=1"
    params = []
    if tag:
        query += " AND tag = ?"
        params.append(tag)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY tag, id"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    if not rows:
        return f"[{project}] Không có test case nào."
    return json.dumps([_row_to_dict(r) for r in rows], ensure_ascii=False)


@mcp.tool()
async def get_test_case(project: str, id: int):
    """Xem chi tiết một test case (bao gồm steps và expected).

    Args:
        project: Tên project
        id: ID của test case
    """
    _ensure_db(project)
    conn = _get_conn(project)
    row = conn.execute("SELECT * FROM test_cases WHERE id = ?", (id,)).fetchone()
    conn.close()
    if not row:
        return f"[{project}] Lỗi: test case id {id} không tồn tại."
    tc = _row_to_dict(row)
    tc["steps"] = _parse_json_field(tc["steps"], [])
    return json.dumps(tc, ensure_ascii=False)


@mcp.tool()
async def update_test_case(
    project: str,
    id: int,
    name: str = "",
    description: str = "",
    steps: str = "",
    expected: str = "",
    tag: str = "",
    status: str = "",
):
    """Cập nhật test case theo id.

    Args:
        project: Tên project
        id: ID của test case
        name: Tên mới (để trống = không đổi)
        description: Mô tả mới (để trống = không đổi)
        steps: JSON array bước mới (để trống = không đổi)
        expected: Kết quả mong đợi mới (để trống = không đổi)
        tag: Tag mới (để trống = không đổi)
        status: Status mới: active, draft, deprecated (để trống = không đổi)
    """
    async with _db_lock:
        _ensure_db(project)
        conn = _get_conn(project)
        row = conn.execute("SELECT * FROM test_cases WHERE id = ?", (id,)).fetchone()
        if not row:
            conn.close()
            return f"[{project}] Lỗi: test case id {id} không tồn tại."
        updates, params = [], []
        if name:
            updates.append("name = ?"); params.append(name)
        if description:
            updates.append("description = ?"); params.append(description)
        if steps:
            updates.append("steps = ?"); params.append(steps)
        if expected:
            updates.append("expected = ?"); params.append(expected)
        if tag:
            updates.append("tag = ?"); params.append(tag)
        if status:
            updates.append("status = ?"); params.append(status)
        if updates:
            updates.append("updated_at = ?"); params.append(datetime.now().isoformat())
            params.append(id)
            conn.execute(f"UPDATE test_cases SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        updated = conn.execute("SELECT * FROM test_cases WHERE id = ?", (id,)).fetchone()
        conn.close()
    return f"[{project}] Đã cập nhật test case: '{updated['name']}'"


@mcp.tool()
async def delete_test_case(project: str, id: int):
    """Xóa test case theo id (kèm toàn bộ run history của nó).

    Args:
        project: Tên project
        id: ID của test case cần xóa
    """
    async with _db_lock:
        _ensure_db(project)
        conn = _get_conn(project)
        row = conn.execute("SELECT * FROM test_cases WHERE id = ?", (id,)).fetchone()
        if not row:
            conn.close()
            return f"[{project}] Lỗi: test case id {id} không tồn tại."
        conn.execute("DELETE FROM test_cases WHERE id = ?", (id,))
        conn.commit()
        conn.close()
    return f"[{project}] Đã xóa test case: '{row['name']}' (và run history)."


# ─── Workflows (reusable flows) ───────────────────────────────────────────────


@mcp.tool()
async def add_workflow(project: str, name: str, description: str = "", steps: str = "[]"):
    """Tạo workflow tái sử dụng (vd: đăng nhập, thêm giỏ hàng) để nhiều test case dùng chung.

    Args:
        project: Tên project
        name: Tên workflow (vd: "login", "add-to-cart")
        description: Mô tả
        steps: JSON array các bước
    """
    async with _db_lock:
        _ensure_db(project)
        now = datetime.now().isoformat()
        conn = _get_conn(project)
        conn.execute(
            "INSERT INTO workflows (name, description, steps, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (name, description, steps, now, now),
        )
        conn.commit()
        conn.close()
    return f"[{project}] Đã tạo workflow: '{name}'"


@mcp.tool()
async def list_workflows(project: str):
    """Liệt kê tất cả workflows tái sử dụng.

    Args:
        project: Tên project
    """
    _ensure_db(project)
    conn = _get_conn(project)
    rows = conn.execute("SELECT * FROM workflows ORDER BY name").fetchall()
    conn.close()
    if not rows:
        return f"[{project}] Chưa có workflow nào."
    result = []
    for r in rows:
        wf = _row_to_dict(r)
        wf["steps"] = _parse_json_field(wf["steps"], [])
        result.append(wf)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def update_workflow(project: str, id: int, name: str = "", description: str = "", steps: str = ""):
    """Cập nhật workflow theo id.

    Args:
        project: Tên project
        id: ID của workflow
        name: Tên mới (để trống = không đổi)
        description: Mô tả mới (để trống = không đổi)
        steps: JSON array bước mới (để trống = không đổi)
    """
    async with _db_lock:
        _ensure_db(project)
        conn = _get_conn(project)
        row = conn.execute("SELECT * FROM workflows WHERE id = ?", (id,)).fetchone()
        if not row:
            conn.close()
            return f"[{project}] Lỗi: workflow id {id} không tồn tại."
        updates, params = [], []
        if name:
            updates.append("name = ?"); params.append(name)
        if description:
            updates.append("description = ?"); params.append(description)
        if steps:
            updates.append("steps = ?"); params.append(steps)
        if updates:
            updates.append("updated_at = ?"); params.append(datetime.now().isoformat())
            params.append(id)
            conn.execute(f"UPDATE workflows SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        updated = conn.execute("SELECT * FROM workflows WHERE id = ?", (id,)).fetchone()
        conn.close()
    return f"[{project}] Đã cập nhật workflow: '{updated['name']}'"


# ─── Runs (test result history) ───────────────────────────────────────────────


@mcp.tool()
async def record_run(
    project: str,
    test_case_id: int,
    status: str,
    notes: str = "",
    screenshots: str = "[]",
    duration_ms: int = 0,
):
    """Ghi lại kết quả một lần chạy test case.

    Args:
        project: Tên project
        test_case_id: ID test case đã chạy
        status: Kết quả: pass, fail, skipped, blocked
        notes: Ghi chú (lý do fail, quan sát, ...)
        screenshots: JSON array đường dẫn screenshot, vd: '["/tmp/login_fail.png"]'
        duration_ms: Thời gian chạy (ms)
    """
    async with _db_lock:
        _ensure_db(project)
        conn = _get_conn(project)
        tc = conn.execute("SELECT * FROM test_cases WHERE id = ?", (test_case_id,)).fetchone()
        if not tc:
            conn.close()
            return f"[{project}] Lỗi: test case id {test_case_id} không tồn tại."
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO runs (test_case_id, status, notes, screenshots, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (test_case_id, status, notes, screenshots, duration_ms, now),
        )
        conn.commit()
        conn.close()
    return f"[{project}] Ghi nhận run '{tc['name']}': {status.upper()}"


@mcp.tool()
async def get_run_history(project: str, test_case_id: int = 0, limit: int = 20):
    """Xem lịch sử run.

    Args:
        project: Tên project
        test_case_id: Lọc theo test case cụ thể. 0 = tất cả test cases.
        limit: Số run tối đa trả về (mặc định 20).
    """
    _ensure_db(project)
    conn = _get_conn(project)
    if test_case_id:
        rows = conn.execute(
            "SELECT * FROM runs WHERE test_case_id = ? ORDER BY id DESC LIMIT ?", (test_case_id, limit)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    if not rows:
        return f"[{project}] Chưa có run nào."
    result = []
    for r in rows:
        run = _row_to_dict(r)
        run["screenshots"] = _parse_json_field(run["screenshots"], [])
        result.append(run)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def get_test_summary(project: str):
    """Tổng quan trạng thái test suite: kết quả run mới nhất của mỗi test case + đếm pass/fail.

    Args:
        project: Tên project
    """
    _ensure_db(project)
    conn = _get_conn(project)
    cases = conn.execute("SELECT id, name, tag, status FROM test_cases ORDER BY tag, id").fetchall()
    summary = []
    counts = {"pass": 0, "fail": 0, "skipped": 0, "blocked": 0, "not_run": 0}
    for c in cases:
        latest = conn.execute(
            "SELECT status, created_at FROM runs WHERE test_case_id = ? ORDER BY id DESC LIMIT 1", (c["id"],)
        ).fetchone()
        last_status = latest["status"] if latest else "not_run"
        counts[last_status] = counts.get(last_status, 0) + 1
        summary.append({
            "id": c["id"],
            "name": c["name"],
            "tag": c["tag"],
            "last_status": last_status,
            "last_run": latest["created_at"] if latest else None,
        })
    conn.close()
    if not cases:
        return f"[{project}] Chưa có test case nào."
    return json.dumps({"counts": counts, "total": len(cases), "cases": summary}, ensure_ascii=False)


# ─── Export ───────────────────────────────────────────────────────────────────


@mcp.tool()
async def export_suite(project: str):
    """Export toàn bộ test suite (test cases + workflows) thành JSON.

    Args:
        project: Tên project
    """
    _ensure_db(project)
    conn = _get_conn(project)
    tc_rows = conn.execute("SELECT * FROM test_cases ORDER BY tag, id").fetchall()
    wf_rows = conn.execute("SELECT * FROM workflows ORDER BY name").fetchall()
    conn.close()
    test_cases = []
    for r in tc_rows:
        tc = _row_to_dict(r)
        tc["steps"] = _parse_json_field(tc["steps"], [])
        test_cases.append(tc)
    workflows = []
    for r in wf_rows:
        wf = _row_to_dict(r)
        wf["steps"] = _parse_json_field(wf["steps"], [])
        workflows.append(wf)
    if not test_cases and not workflows:
        return f"[{project}] Suite trống, không có gì để export."
    return json.dumps({"project": project, "test_cases": test_cases, "workflows": workflows}, indent=2, ensure_ascii=False)


@mcp.tool()
async def list_ui_projects():
    """Liệt kê tất cả UI projects hiện có."""
    if not DB_DIR.exists():
        return "Chưa có project nào."
    projects = sorted(f.stem for f in DB_DIR.glob("*.db"))
    if not projects:
        return "Chưa có project nào."
    return json.dumps(projects, ensure_ascii=False)


# ─── Resources ────────────────────────────────────────────────────────────────


@mcp.resource("ui-workflow://projects")
def projects_resource():
    """Danh sách tất cả UI projects."""
    if not DB_DIR.exists():
        return "[]"
    projects = sorted(f.stem for f in DB_DIR.glob("*.db"))
    return json.dumps(projects, indent=2, ensure_ascii=False)


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
