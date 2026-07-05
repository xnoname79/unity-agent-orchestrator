# Plan: Session Orchestrator + Monitoring UI

> Trạng thái: **Plan — chờ chốt quyết định** (xem cuối). Chưa code.

Hệ thống điều phối **headless Claude sessions**: agent gửi signal → orchestrator inject
message vào session target qua `claude -p --resume` → **UI giám sát & quản lý** để an toàn.

## Mục tiêu

1. Agent sessions phát signal (message cần gửi tới session khác).
2. Orchestrator poll signal, route tới session target, inject như user turn (headless).
3. **UI dashboard** theo dõi: session đang chạy, transcript live, hàng đợi signal, và
   **điều khiển an toàn** (pause/kill/approve/deny, kill switch toàn cục).

## Thành phần

```
 Agent sessions (headless, CLI/SDK)
   │  send_signal(to, msg)  ── MCP tool
   ▼
 ┌──────────────────────────────────────────────┐
 │  orchestrator (Python daemon)                 │
 │  signal poller → per-session lock/queue →     │
 │  executor (claude -p --resume, safe)          │
 │        │                                       │
 │        ▼   SQLite: sessions / signals / runs  │
 │  Control API (HTTP + SSE/WebSocket)           │
 └───────────────┬───────────────────────────────┘
                 │
          Monitoring UI (web dashboard)
          list · transcript live · pause/kill · approve · audit
```

1. **Session store (SQLite)** — theo pattern các MCP server hiện có.
2. **Orchestrator daemon** — poll signal, khóa per-session, gọi executor, ghi log.
3. **Executor** — chạy `claude -p --resume <id> "<msg>" --output-format stream-json
   --allowedTools <allowlist>`, thu kết quả, cập nhật trạng thái.
4. **Control API** — backend cho UI (list/detail/pause/kill/approve/logs + live stream).
5. **Monitoring UI** — dashboard giám sát + điều khiển an toàn.
6. **Signal MCP tool** — `send_signal(to_role, message)` cho agent phát signal (tái dùng
   sync-bridge làm bus, hoặc bảng signals riêng).

## Data model (SQLite)

```
sessions
  id TEXT PK            -- claude session_id
  name TEXT             -- role/label (vd "be-worker", "fe-worker")
  project TEXT
  cwd TEXT              -- thư mục làm việc (hoặc worktree)
  status TEXT           -- idle | running | paused | stopped
  allowed_tools TEXT    -- JSON allowlist
  permission_mode TEXT
  created_at, last_active

signals
  id INTEGER PK
  from_session TEXT
  to_session TEXT       -- hoặc to_role, orchestrator resolve → session_id
  message TEXT
  requires_approval INT -- 0/1 (human-in-the-loop)
  status TEXT           -- pending | approved | processing | done | failed | denied
  created_at, delivered_at

runs
  id INTEGER PK
  session_id TEXT
  signal_id INTEGER
  prompt TEXT
  result_json TEXT      -- output-format json
  status TEXT           -- ok | error
  tokens INTEGER
  started_at, ended_at
```

## Thiết kế AN TOÀN (trọng tâm)

Đây là lý do cần UI — mọi hành động headless phải kiểm soát được:

| Cơ chế | Mô tả |
|--------|-------|
| **Human-in-the-loop** | Signal gắn `requires_approval` → phải bấm Approve trên UI mới inject. Bật mặc định cho thao tác nhạy cảm. |
| **Tool allowlist per session** | Mỗi session giới hạn `--allowedTools`. Không blanket auto-approve. |
| **Permission mode thận trọng** | Không dùng chế độ bỏ qua toàn bộ; mặc định an toàn. |
| **Per-session lock/queue** | Chỉ 1 prompt in-flight/session → tránh trộn transcript. Signal khác xếp hàng. |
| **Dry-run** | Xem trước message sẽ inject mà không chạy. |
| **Budget & limits** | Trần token/session, max concurrent sessions, rate limit. |
| **Kill switch** | Nút dừng-tất-cả + kill từng session trên UI. |
| **Audit log** | Mọi injection ghi lại (ai, gì, khi nào, kết quả). |
| **Isolation** | Mỗi session một cwd/worktree riêng. |

## Tech stack

- **Backend**: Python + Starlette + uvicorn (đã có sẵn qua `mcp`). Consistent với repo.
- **DB**: SQLite (giống sync-bridge/unity-dev/ui-workflow).
- **Session driver**: headless CLI `claude -p --resume --output-format stream-json` (đơn giản,
  script-friendly). Agent SDK bổ sung sau nếu cần streaming sâu.
- **Live update**: SSE hoặc WebSocket relay `stream-json` events → UI transcript real-time.
- **UI**: single-page dashboard nhẹ (HTML + JS thuần + SSE/WebSocket) do Starlette phục vụ.
  Không cần framework nặng cho v1.

## Lộ trình triển khai

- **Phase A ✅ — Orchestrator core (no UI)** — `session_orchestrator.py`
  - SQLite schema (sessions/signals/runs); session registry.
  - Signal store + poller; per-session lock + max-concurrent; executor `claude -p --resume`
    với allowlist; `requires_approval` chặn auto-run; audit log.
  - Verified (dry-run): auto signal chạy, signal nhạy cảm chờ approve, ghost session → failed,
    same-session serialize, audit đầy đủ.
  - Test thật (trên máy có claude CLI): bỏ `ORCH_DRY_RUN`, session_id thật.
- **Phase B ✅ — Control API** — `session_orchestrator.py serve` (Starlette, port 8992)
  - REST: `/health`, `/api/sessions` (GET/POST register), `/api/sessions/{id}` (+`/runs`,
    `/pause`, `/resume`, `/stop`), `/api/signals` (GET/POST), `/api/signals/{id}/approve|deny`,
    `/api/runs`, `/api/stop-all`, `/api/resume-all`.
  - **SSE** `/api/events` — live stream (signal/run/session/kill_switch) cho UI.
  - Background poll loop chạy trong lifespan. Verified: REST CRUD, approve flow, pause/resume
    giữ signal chờ, kill switch chặn xử lý, SSE nhận event real-time.
- **Phase C ✅ — Monitoring UI** — `static/orchestrator/` (index.html + app.js), serve tại `/`
  - Bảng sessions (status badge, tools, Pause/Resume/Stop); signal queue (Approve/Deny cho
    signal cần duyệt); audit log (runs); nút kill switch toàn cục (STOP/RESUME ALL).
  - Live update qua SSE `/api/events` (debounced refetch). HTML+JS thuần, không build step.
  - Verified: dashboard serve tại `/`, app.js load, browser kết nối SSE (log `GET /api/events 200`).
- **Phase E ✅ — MCP signal integration** — `signal_mcp.py` (port 8993)
  - Tool `send_signal(to_role, message, from_role, requires_approval)` + `list_agents()`.
  - POST tới orchestrator `/api/signals`; orchestrator resolve **role → session_id**
    (`to_role` hoặc `to_session`). Verified: agent gọi send_signal → inject đúng target.
- **Phase D ✅ — Safety hardening**
  - Circuit breaker chống lặp vô tận: `ORCH_MAX_RUNS_PER_SESSION`, `ORCH_SESSION_TOKEN_BUDGET`
    → signal vượt cap = `blocked`. Retry `ORCH_MAX_RETRIES` + backoff. Dry-run per-signal
    (`dry_run:1`). `/api/stats` + limits trong `/health`. Verified: cap chặn đúng, stats,
    dry-run stub, retry.

## Quyết định đã chốt

1. **Approval mode**: **auto-run + allowlist** — signal tự chạy nếu tool nằm trong allowlist;
   `requires_approval` chỉ bật cho thao tác nhạy cảm (human approve trên UI).
2. **UI**: **dashboard nhẹ HTML + JS thuần + SSE** do Starlette phục vụ. Không build step.
3. **Signal bus**: **bảng `signals` riêng** trong orchestrator (SQLite). Độc lập với sync-bridge.
4. **Session driver**: **headless CLI `claude -p --resume`** (`--output-format stream-json`).
   Agent SDK cân nhắc sau nếu cần.

## Cấu trúc file dự kiến

```
session_orchestrator.py      -- daemon: poller + lock/queue + executor + Control API (Starlette)
  ~/.session_orch_db/<name>.db   -- SQLite: sessions / signals / runs
static/orchestrator/          -- dashboard (index.html + app.js, SSE client)
docs/session-orchestrator.md  -- setup & usage (viết ở Phase C)
```
