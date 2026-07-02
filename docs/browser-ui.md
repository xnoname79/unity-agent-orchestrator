# Browser UI — Dev & Testing (Playwright MCP)

Setup [official Playwright MCP](https://github.com/microsoft/playwright-mcp) để Claude
điều khiển browser thật (Firefox) — phát triển UI và test workflow theo vòng lặp
**LOOK → ACT → VERIFY**.

Đây là **Phase 1**. Xem toàn cảnh ở `docs/browser-ui-plan.md`, phương pháp ở skill
`ui-testing`.

## Hai bộ não (giống Unity setup)

```
┌──────────────────────────┐        ┌──────────────────────────┐
│  playwright-mcp           │        │  ui-workflow (Python)     │
│  = ĐÔI TAY (browser)      │  ◄──►  │  = BỘ NÃO (test mgmt)     │
│  navigate/click/type      │        │  test cases, workflows,   │
│  snapshot, screenshot     │        │  run history (SQLite)     │
└──────────────────────────┘        └──────────────────────────┘
```

- **playwright-mcp** — điều khiển browser (bên dưới).
- **ui-workflow** — quản lý test case/workflow/run history (xem `## ui-workflow` cuối trang).

## Yêu cầu

| Thành phần | Ghi chú |
|-----------|---------|
| Node.js | 18+ (dùng `npx`) |
| Firefox | qua Playwright — `playwright install firefox` một lần |
| MCP client | Claude Code |

## Cài đặt

### 1. Cài Firefox cho Playwright (một lần)

```bash
npx playwright install firefox
```

### 2. Đăng ký Playwright MCP với Claude Code (Firefox)

```bash
claude mcp add playwright -- npx @playwright/mcp@latest --browser firefox
```

> Thêm `--headless` nếu chạy trên server không có màn hình:
> ```bash
> claude mcp add playwright -- npx @playwright/mcp@latest --browser firefox --headless
> ```

### 3. Verify

Mở Claude Code, hỏi: *"navigate to example.com and take a snapshot"*.
Nếu Claude mở được trang và trả về accessibility snapshot → OK.

## Các tool chính (Playwright MCP)

| Tool | Nhóm | Dùng để |
|------|------|---------|
| `browser_navigate` / `browser_navigate_back` | Điều hướng | Mở URL, quay lại |
| `browser_snapshot` | **LOOK** | Accessibility tree + element refs — cách chính để "nhìn" và **target element** |
| `browser_take_screenshot` | **LOOK** | Ảnh pixel — verify giao diện/visual |
| `browser_click` / `browser_type` / `browser_fill_form` | ACT | Tương tác |
| `browser_select_option` / `browser_drag` / `browser_press_key` | ACT | Tương tác nâng cao |
| `browser_evaluate` | ACT/VERIFY | Chạy JS trong trang |
| `browser_console_messages` / `browser_network_requests` | VERIFY | Đọc console/network để kiểm tra |
| `browser_wait_for` | Đồng bộ | Chờ text/element/điều kiện (chống flaky) |

> **Quan trọng:** dùng `browser_snapshot` (accessibility tree) để tìm & nhắm element —
> chính xác và ổn định hơn screenshot. Dùng `browser_take_screenshot` để verify **hình thức**.

## Cờ CLI hữu ích

| Flag | Công dụng |
|------|-----------|
| `--browser firefox\|chrome\|webkit\|msedge` | Chọn engine (ta dùng `firefox`) |
| `--headless` | Chạy ẩn (server không màn hình) |
| `--viewport-size "1280x720"` | Kích thước cửa sổ |
| `--device "iPhone 15"` | Giả lập thiết bị (responsive) |
| `--isolated` | Profile trong RAM, không lưu disk |
| `--save-trace` / `--output-dir` | Lưu trace/artifact để debug |

## Vòng lặp LOOK → ACT → VERIFY

1. **LOOK** — `browser_snapshot` (tìm element) + `browser_take_screenshot` (nhìn giao diện).
2. **ACT** — `browser_click` / `browser_type` / ... theo workflow.
3. **VERIFY** — `browser_snapshot`/`browser_evaluate`/`browser_console_messages` để assert
   kết quả + screenshot đối chiếu.
4. Lặp đến khi workflow đạt yêu cầu.

Phương pháp chi tiết (selector strategy, chống flaky, Arrange-Act-Assert): skill `ui-testing`.

## ui-workflow (test management)

MCP Python quản lý test case, workflow tái sử dụng, và lịch sử run — bộ nhớ bền cho
test suite (Playwright MCP chỉ điều khiển browser, không lưu gì).

```bash
python3 ui_workflow.py          # chạy ở port 8991
claude mcp add --transport http ui-workflow http://localhost:8991/mcp
```

DB tự tạo per project tại `~/.ui_workflow_db/<project>.db`. Health: `curl http://localhost:8991/health`.

### Tools

| Tool | Dùng để |
|------|---------|
| `add_test_case` / `list_test_cases` / `get_test_case` / `update_test_case` / `delete_test_case` | CRUD test case (name, steps, expected, tag) |
| `add_workflow` / `list_workflows` / `update_workflow` | Workflow tái sử dụng (login, checkout, ...) |
| `record_run` / `get_run_history` | Ghi & xem kết quả run (pass/fail, screenshot, notes) |
| `get_test_summary` | Tổng quan suite: run mới nhất mỗi case + đếm pass/fail |
| `export_suite` | Xuất toàn bộ test cases + workflows ra JSON |
| `list_ui_projects` | Liệt kê projects |

### Luồng kết hợp

1. Định nghĩa test case trong `ui-workflow` (`add_test_case` với steps + expected).
2. Thực thi qua Playwright MCP (LOOK→ACT→VERIFY).
3. Ghi kết quả về `ui-workflow` (`record_run` + screenshot) để track history/regression.
4. `get_test_summary` để xem sức khỏe toàn suite.
