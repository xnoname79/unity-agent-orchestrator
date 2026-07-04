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

- **Phase A — Orchestrator core (no UI)**
  - SQLite schema; session registry (spawn headless → lưu session_id).
  - Signal store + poller; per-session lock; executor `claude -p --resume` với allowlist.
  - Verify: phát 1 signal → inject → session chạy → log kết quả. Audit log baseline.
- **Phase B — Control API**
  - REST: list sessions, session detail + transcript, list signals, pause/resume/stop,
    approve/deny, stop-all. SSE/WebSocket cho transcript live.
- **Phase C — Monitoring UI**
  - Bảng sessions + status; hàng đợi signal; session detail + transcript live;
    nút pause/kill/approve/deny + kill switch; trang audit log.
- **Phase D — Safety hardening**
  - Human-in-the-loop mode; budget/rate/concurrent caps; dry-run; retry/error handling; health.
- **Phase E — MCP signal integration**
  - `send_signal` MCP tool (hoặc tái dùng sync-bridge) để agent phát signal tự nhiên;
    map role → session_id.

## Quyết định cần chốt

1. **Approval mode mặc định**: mọi signal cần human-approve trên UI [an toàn nhất], hay
   auto-run với allowlist + chỉ approve khi flagged nhạy cảm?
2. **UI**: dashboard nhẹ HTML+JS+SSE do Starlette phục vụ [khuyến nghị], hay SPA (React)?
3. **Signal bus**: tái dùng **sync-bridge** đã có, hay tạo bảng `signals` riêng trong orchestrator?
4. **Session driver**: headless CLI `claude -p --resume` trước [khuyến nghị], hay Agent SDK ngay?
