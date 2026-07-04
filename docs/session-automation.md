# Research: Automation qua Claude Code sessions

> Trạng thái: **Nghiên cứu / thảo luận**. Chưa code. Trả lời câu hỏi "có inject message
> vào Claude panel trong VSCode được không" + đề xuất kiến trúc automation.

## Câu hỏi

Xây hệ thống polling: phát hiện message mới từ các agent session khác → gửi signal tới
session target → session target "listen" và nhận message như **user message** để tự động hóa.
Có gửi thẳng vào **Claude panel trong VSCode** được không?

## Trả lời ngắn

**Không** — không thể inject message vào Claude panel (webview) trong VSCode qua API công khai.
**Nhưng** mục tiêu automation **vẫn đạt được** — chỉ khác: session target là **session do
Agent SDK / headless CLI điều khiển**, KHÔNG phải panel tương tác của con người.

## Vì sao không inject được vào panel VSCode

- Webview trong VSCode bị **sandbox**. Chỉ extension sở hữu mới postMessage vào webview của nó.
  Không có API công khai cho process ngoài "gõ"/"submit" vào panel của extension khác.
- Claude Code VSCode extension **không** expose command/API để gửi chat message vào session đang mở.
- Thứ duy nhất có: URI handler `vscode://anthropic.claude-code/open?prompt=...&session=...`
  — chỉ **mở/focus tab và điền sẵn prompt** (có thể resume session), **không tự submit**.
  → Bán tự động: con người vẫn phải bấm Enter.

## Các cơ chế inject vào session (chính thức)

| Cơ chế | Inject vào session có sẵn? | Lập trình được? | Ghi chú |
|--------|:--:|:--:|--------|
| VSCode extension API | ❌ | — | Không có API; webview sandbox |
| VSCode URI handler | ⚠️ pre-fill | một phần | Điền sẵn prompt, người bấm Enter |
| **Agent SDK** (Py/TS) | ✅ | ✅ | `query(prompt, options={resume: session_id})` |
| **Headless CLI** | ✅ | ✅ | `claude -p --resume <id> "<prompt>"` |
| Hooks | ❌ | reactive | Chỉ phản ứng event trong session, không nhận trigger ngoài |
| Routines / scheduled | ❌ | tạo session MỚI | Không inject vào session đang chạy |
| Managed Agents API (cloud) | ✅ | ✅ REST | POST `/v1/sessions/<id>/events` — session hosted trên cloud |

**Kết luận**: muốn push message từ ngoài vào một session → dùng **Agent SDK** hoặc
**headless CLI `--resume`**. Session automation phải là session do controller điều khiển,
không phải panel VSCode tương tác.

## Kiến trúc đề xuất

Tái sử dụng **sync-bridge** (đã có) làm signal bus + một **orchestrator** nhỏ dùng SDK/CLI:

```
 Agent sessions (SDK/CLI)                        ┌───────────────────────┐
   │  ghi signal (message mới)                   │  session registry     │
   ▼                                             │  role → session_id    │
 ┌─────────────┐   poll    ┌────────────────────┴──┐                     │
 │ sync-bridge  │ ────────► │  orchestrator (Python) │◄────────────────────┘
 │ (signal bus) │           │  - poll signal mới     │
 └─────────────┘           │  - lock theo session   │
                            │  - gọi resume target   │
                            └───────────┬────────────┘
                                        │ claude -p --resume <target-id> "<msg>"
                                        │      (hoặc SDK query resume=id)
                                        ▼
                              Target session chạy turn mới (headless)
```

- **Signal bus**: sync-bridge (hoặc bảng SQLite/queue nhẹ) — nơi các session ghi "có message mới".
- **Orchestrator**: process Python poll signal → resolve session_id target → inject prompt qua
  `claude -p --resume` hoặc Agent SDK.
- **Session registry**: map logic (project/role) → `session_id` (SDK trả về session_id khi tạo).

## Ràng buộc & lưu ý (quan trọng)

1. **Concurrency/interleaving**: nếu resume một session đang chạy interactive song song, message
   sẽ **trộn lẫn** vào cùng transcript (không có lock sẵn). ⇒ target phải là session do controller
   độc quyền điều khiển; cần **lock/queue per session** để serialize.
2. **Session identity**: phải lưu và quản lý `session_id`. SDK/CLI (`--output-format json`) trả về id.
3. **Permissions/safety**: headless auto-approve tool (`--allowedTools`/`--permission-mode`) —
   phải allowlist cẩn thận, tránh cho phép hành động phá hủy. Cân nhắc dry-run.
4. **Isolation**: dùng `--worktree` nếu chạy nhiều session song song trên cùng repo.
5. **Panel VSCode**: nếu bắt buộc dùng panel người thật, chỉ "assist" được bằng URI handler
   (điền sẵn, người bấm Enter) — không full automation.

## Lộ trình đề xuất (nếu làm)

- **Phase A** — Session registry + orchestrator PoC: 1 controller poll sync-bridge, gọi
  `claude -p --resume` với message cố định. Verify inject được 1 turn.
- **Phase B** — Lock/queue per session, map role→session_id, xử lý nhiều signal.
- **Phase C** — Safety: allowlist tool, dry-run, retry/error handling, logging.

## Lựa chọn cloud (nếu muốn "push" sạch)

**Managed Agents API** (Anthropic-hosted): POST event vào `/v1/sessions/<id>/events` — đúng mô hình
"push message vào session" qua REST, không vướng sandbox VSCode. Đổi lại session chạy trên cloud,
không phải local VSCode.
