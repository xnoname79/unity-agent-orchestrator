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
| `ORCH_MAX_RUNS_PER_SESSION` | `0` | Trần số run/session (0=tắt) — **chống lặp vô tận** |
| `ORCH_SESSION_TOKEN_BUDGET` | `0` | Trần token/session (0=tắt) |
| `ORCH_MAX_RETRIES` | `0` | Số lần retry khi executor lỗi |
| `ORCH_STREAM` | `1` | `1` = stream transcript (thinking/tool_use/text) real-time |
| `ORCH_STREAM_PARTIAL` | `0` | `1` = thêm `--include-partial-messages` (text chảy từng token) |
| `ORCH_EVENT_TRUNC` | `2000` | Số ký tự tối đa mỗi payload event (chống phình DB / lộ dữ liệu) |
| `CLAUDE_BIN` | `claude` | Đường dẫn claude CLI |

## Agent-to-agent (signal_mcp)

Để agent tự phát signal cho nhau (vd Artist Director → Developer), chạy thêm
**signal_mcp** và cho mỗi agent đăng ký nó:

```bash
python3 signal_mcp.py          # port 8993, POST tới orchestrator ORCH_URL (default :8992)
claude mcp add --transport http signal http://localhost:8993/mcp
```

Agent gọi tool `send_signal(to_role="developer", message="...")` → orchestrator resolve
role → inject vào session đó. `list_agents()` để xem role hợp lệ. Con người trigger 1 lần
(qua dashboard hoặc chính tool này), phần còn lại agent tự chuyền tay nhau.

Agent cũng có thể **tự nén context** bằng `compact_context(role="", focus="")` — bỏ trống
`role` để nén chính mình (chạy ngay sau khi lượt hiện tại kết thúc, không cắt ngang).
Dùng sau khi xong một subtask lớn để giữ transcript gọn.

> **Quan trọng**: session của con người (panel VSCode) chỉ nên **phát** signal, KHÔNG bao
> giờ là `to_role`/target → orchestrator không inject vào panel → không interleaving.

## Quản lý trên Dashboard (không cần curl)

Mở `http://localhost:8992/` — panel **Manage agents** làm được mọi thứ:

- **🚀 Spawn agent** — orchestrator chạy `claude -p` tạo session mới (nhập role, cwd,
  chọn allowed tools, init prompt). Session_id tự sinh & register.
- **🔧 Load tools từ cwd** — bấm để lấy **checklist** tools khả dụng (built-in + tools của
  các MCP server đã đăng ký cho project đó) → tick chọn, không cần gõ. Mỗi MCP server có
  thêm wildcard `mcp__<server>__*` (allow toàn bộ tool của server).
- **🔗 Register session có sẵn** — dán session_id (lấy từ `claude ... --output-format json`)
  + role/cwd/tools.
- **✉️ Send signal** — chọn role đích, nhập message, tick requires-approval / dry-run.
- Bảng **Sessions**: Pause / Resume / **🗜 Compact** / Stop / **Unregister** từng session.
  - **Compact**: nén context của session đó (hỏi *focus* tùy chọn) — hữu ích khi agent
    chạy dài, transcript phình. Đi qua per-session lock nên **không cắt ngang** prompt đang chạy.
- **Signal queue**: Approve / Deny signal cần duyệt.
- **Kill switch** (STOP ALL) ở header.
- Bảng **Audit log**: bấm vào một run → mở **drawer transcript** hiển thị từng bước
  (🧠 thinking · 🔧 tool_use · 📄 tool_result · 💬 text · ✅ result) **theo thời gian thực**.

Tất cả cũng có API tương ứng nếu muốn tự động hóa (xem bảng dưới).

## Transcript real-time (streaming)

Mặc định orchestrator chạy `claude -p` với `--output-format stream-json`, đọc **NDJSON**
từng dòng ngay khi agent phát ra, thay vì chờ kết quả cuối. Mỗi bước được:

1. Ghi vào bảng `run_events` (`run_id`, `seq`, `kind`, `summary`, `payload`) → **replay được** sau khi F5.
2. Đẩy qua **SSE `/api/events`** (`type: "run_event"`) → dashboard append vào drawer đang mở **live**.

Loại bước (`kind`): `system` (init) · `thinking` · `text` · `tool_use` · `tool_result` · `result` · `error`.

- Payload mỗi bước bị **cắt** theo `ORCH_EVENT_TRUNC` (default 2000 ký tự) để tránh phình DB / lộ dữ liệu.
- Muốn text chảy **từng token** (hiệu ứng gõ chữ): đặt `ORCH_STREAM_PARTIAL=1`.
- Tắt hẳn streaming (về chế độ 1-cục-JSON cũ): `ORCH_STREAM=0`.

> Lưu ý bảo mật: transcript có thể chứa nội dung file/lệnh agent đọc. Cắt bớt đã bật sẵn,
> nhưng cân nhắc không cho tool nhạy cảm vào allowlist nếu dashboard được chia sẻ.

## Compact context (chống phình khi chạy dài)

Session chạy lâu → transcript phình → mỗi lượt tốn nhiều token. Claude Code có auto-compact
khi gần chạm giới hạn, nhưng bạn có thể **nén chủ động** bất cứ lúc nào:

- **Dashboard**: nút **🗜 Compact** ở dòng session (hỏi *focus* tùy chọn).
- **API**: `POST /api/sessions/{id}/compact` với body `{"focus": "giữ API contract"}` (focus tùy chọn).
- **Agent tự nén**: tool `compact_context(role="", focus="")` của signal_mcp.

Cả ba đều **enqueue một signal có message `/compact …`** → đi qua per-session lock, nên
việc nén luôn chạy **sau khi** lượt hiện tại của session kết thúc (an toàn, không cắt ngang).
Bản thân lần nén là một lượt gọi model (tốn ít token, tính vào `runs`); các lượt sau rẻ hơn.

> **Cần verify trên máy thật**: `/compact` trong chế độ `-p --resume` có thực thi đúng không —
> một số slash command hành xử khác giữa interactive và print mode. Nếu không nén được,
> phương án thay thế là *summarize + respawn* (tóm tắt trạng thái rồi tạo session mới) —
> chưa implement, báo nếu bạn cần.

## Quy trình dùng (bằng API/curl — tương đương dashboard)

1. **Spawn hoặc register session target**:
   ```bash
   # orchestrator tự spawn
   curl -X POST http://localhost:8992/api/sessions/spawn -H 'Content-Type: application/json' \
     -d '{"name":"be-worker","cwd":"/path/repo","allowed_tools":["Read","Grep"]}'
   # hoặc register session_id có sẵn
   curl -X POST http://localhost:8992/api/sessions -H 'Content-Type: application/json' \
     -d '{"id":"<session_id>","name":"be-worker","cwd":"/path/repo","allowed_tools":["Read","Grep"]}'
   ```
2. **Phát signal** (agent hoặc tay), resolve theo `to_role` hoặc `to_session`:
   ```bash
   curl -X POST http://localhost:8992/api/signals -H 'Content-Type: application/json' \
     -d '{"to_role":"be-worker","message":"kiểm tra API /login"}'
   ```
   Thêm `"requires_approval":1` cho thao tác nhạy cảm → chờ Approve trên dashboard.
3. **Theo dõi & điều khiển** trên dashboard.

## API tóm tắt

| Method | Path | Chức năng |
|--------|------|-----------|
| GET | `/health` | trạng thái + dry_run + kill_switch |
| GET/POST | `/api/sessions` | list / register session |
| POST | `/api/sessions/spawn` | orchestrator spawn session mới (`claude -p`) |
| GET | `/api/sessions/{id}` · `/runs` | chi tiết · lịch sử run |
| POST | `/api/sessions/{id}/pause` `resume` `stop` `unregister` | điều khiển / gỡ session |
| POST | `/api/sessions/{id}/compact` | nén context session (body `{focus?}`) — enqueue `/compact` |
| GET/POST | `/api/signals` | list / enqueue signal |
| POST | `/api/signals/{id}/approve` `deny` | duyệt signal nhạy cảm |
| GET | `/api/runs` | audit log |
| GET | `/api/runs/{id}/events` | transcript của 1 run (replay từng bước) |
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
