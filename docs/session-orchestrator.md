# Session Orchestrator — Setup & Usage

Điều phối **headless Claude sessions** cho automation: agent phát signal → orchestrator
inject vào session target qua `claude -p --resume` → **dashboard giám sát & điều khiển**.

Thiết kế: xem `docs/session-orchestrator-plan.md`. Nghiên cứu nền: `docs/session-automation.md`.

## Kiến trúc

```
 Agent sessions ──POST /api/signals──► signals (SQLite)
                                          │ poll loop
                                   orchestrator (lock/queue)
                                          │ claude -p --resume <id> "<msg>"
                                          ▼
                                 Target session (headless) chạy
                                          │ SSE /api/events
                                          ▼
                                 Dashboard: sessions · queue · audit · kill switch
```

## Chạy

```bash
# Khởi động API + dashboard + poll loop (một tiến trình)
python3 session_orchestrator.py serve
# → http://localhost:8992   (dashboard tại /, API tại /api/*, SSE tại /api/events)
```

**An toàn khi test**: đặt `ORCH_DRY_RUN=1` để chạy thử pipeline mà KHÔNG gọi claude thật.

```bash
ORCH_DRY_RUN=1 python3 session_orchestrator.py serve
```

## Env

| Biến | Mặc định | Ý nghĩa |
|------|----------|---------|
| `ORCH_HOST` / `ORCH_PORT` | `0.0.0.0` / `8992` | Địa chỉ bind |
| `ORCH_DB` | `orchestrator` | Tên DB → `~/.session_orch_db/<name>.db` |
| `ORCH_DRY_RUN` | `0` | `1` = không gọi claude thật (test) |
| `ORCH_POLL_INTERVAL` | `5` | Giây giữa các lần poll |
| `ORCH_MAX_CONCURRENT` | `3` | Số session chạy song song tối đa |
| `CLAUDE_BIN` | `claude` | Đường dẫn claude CLI |

## Quy trình dùng

1. **Đăng ký session target** (session_id thật lấy từ `claude ... --output-format json`):
   ```bash
   curl -X POST http://localhost:8992/api/sessions -H 'Content-Type: application/json' \
     -d '{"id":"<session_id>","name":"be-worker","cwd":"/path/repo","allowed_tools":["Read","Grep"]}'
   ```
2. **Phát signal** (agent hoặc tay):
   ```bash
   curl -X POST http://localhost:8992/api/signals -H 'Content-Type: application/json' \
     -d '{"to_session":"<session_id>","message":"kiểm tra API /login"}'
   ```
   Thêm `"requires_approval":1` cho thao tác nhạy cảm → chờ Approve trên dashboard.
3. **Theo dõi & điều khiển** trên dashboard `http://localhost:8992/`:
   pause/resume/stop session, approve/deny signal, kill switch toàn cục, xem audit log.

## API tóm tắt

| Method | Path | Chức năng |
|--------|------|-----------|
| GET | `/health` | trạng thái + dry_run + kill_switch |
| GET/POST | `/api/sessions` | list / register session |
| GET | `/api/sessions/{id}` · `/runs` | chi tiết · lịch sử run |
| POST | `/api/sessions/{id}/pause` `resume` `stop` | điều khiển session |
| GET/POST | `/api/signals` | list / enqueue signal |
| POST | `/api/signals/{id}/approve` `deny` | duyệt signal nhạy cảm |
| GET | `/api/runs` | audit log |
| POST | `/api/stop-all` `resume-all` | kill switch toàn cục |
| GET | `/api/events` | SSE live stream |

## An toàn (đã có)

- **Tool allowlist** per session (`--allowedTools`).
- **Per-session lock**: 1 prompt in-flight/session → không trộn transcript.
- **requires_approval**: signal nhạy cảm chờ human approve trên dashboard.
- **Kill switch**: dừng toàn bộ xử lý tức thì.
- **Audit log**: mọi injection ghi vào bảng `runs`.
- **DRY_RUN**: test toàn bộ pipeline không tốn token.

## Lưu ý concurrency (quan trọng)

Đừng resume một session đang mở **interactive** song song — message sẽ trộn vào cùng
transcript. Session target nên do orchestrator độc quyền điều khiển; dùng `--worktree`/cwd
riêng khi chạy nhiều session trên cùng repo.
