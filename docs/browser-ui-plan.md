# Plan: Browser UI Dev & Testing MCP

> Trạng thái: **DRAFT — đang thảo luận**. Chưa code. Dùng để chốt hướng trước khi triển khai.

Mục tiêu: MCP server hỗ trợ **phát triển và test UI + function workflow trên browser thật**
(Chrome/Firefox) — click, nhập liệu, kiểm tra kết quả, screenshot, và tự đánh giá.

## Kiến trúc đề xuất: mô hình "hai bộ não" (giống unity)

Lặp lại pattern đã dùng cho Unity — tách **thực thi** và **kế hoạch/metadata**:

```
┌──────────────────────────┐        ┌──────────────────────────┐
│  playwright-mcp           │        │  ui-workflow (repo này)   │
│  = ĐÔI TAY (browser)      │        │  = BỘ NÃO (test mgmt)     │
│                          │        │                          │
│  • launch Chrome/Firefox │  ◄──►  │  • test cases + steps    │
│  • click/type/navigate   │        │  • workflow tái sử dụng  │
│  • screenshot, DOM snap  │        │  • run history/results   │
│  • network, console      │        │  • visual baselines      │
│  (Playwright execution)  │        │  (metadata, SQLite)      │
└──────────────────────────┘        └──────────────────────────┘
              └──────── Skill: ui-testing (phương pháp) ────────┘
```

- **playwright-mcp** — điều khiển browser thật. Dùng [official Playwright MCP](https://github.com/microsoft/playwright-mcp)
  (Microsoft) hoặc wrapper Playwright-Python. KHÔNG viết lại driver.
- **ui-workflow** — MCP Python của mình (giống `unity-dev`): quản lý test case,
  workflow, lưu kết quả run, baseline ảnh.
- **Skill `ui-testing`** — playbook "test UI thế nào cho đúng" (giống `unity-environment-art`).

Vì sao Playwright: môi trường đã cài sẵn Chromium + Playwright (`PLAYWRIGHT_BROWSERS_PATH`),
hỗ trợ Chromium/Firefox/WebKit, auto-wait (chống flaky), selector semantic, screenshot,
network interception, tracing — chuẩn công nghiệp hiện tại.

## Vòng lặp cốt lõi: LOOK → ACT → VERIFY

Giống art-direction loop của Unity:
1. **LOOK** — screenshot + accessibility/DOM snapshot để "nhìn" trạng thái UI.
2. **ACT** — click/type/navigate theo workflow.
3. **VERIFY** — assert (element hiện, text đúng, url khớp) + screenshot đối chiếu.
4. Lặp đến khi workflow pass.

## Bộ tools dự kiến

### A. Browser control (playwright-mcp — dùng sẵn, không tự viết)
- `launch_browser(engine=chromium|firefox, headless)`
- `navigate(url)`, `click`, `type`, `hover`, `select`, `scroll`, `press_key`
- `screenshot(full_page | element)` — con mắt
- `snapshot_dom` / `accessibility_tree` — ảnh chụp ngữ nghĩa để model suy luận
- `wait_for(selector | text | network_idle)`
- `evaluate_js(script)`
- `get_console_logs`, `get_network_requests`
- `assert(visible | text | url | count)`

### B. Test/workflow management (ui-workflow — MCP Python mới, giống unity-dev)
- `add_test_case(project, name, steps, expected)`
- `list_test_cases` / `update_test_case` / `delete_test_case`
- `add_workflow(name, steps)` — flow tái sử dụng (login, checkout, ...)
- `record_run(test_case_id, status, screenshots, notes)`
- `get_run_history(test_case_id)`
- `export_suite(project)` — xuất JSON (hoặc Playwright test spec)

### C. Nâng cao (giai đoạn sau)
- **Visual regression** — so sánh screenshot với baseline, báo diff.
- **Responsive testing** — viewport presets (mobile/tablet/desktop).
- **Network mocking** — stub API để test edge case (lỗi 500, mạng chậm).
- **Accessibility audit** — chạy axe-core, báo vi phạm a11y.
- **Trace/video** — ghi lại để debug khi fail.

### D. Skill `ui-testing` (phương pháp, nạp theo yêu cầu)
- Selector strategy: ưu tiên role/text/test-id thay vì CSS/XPath dễ vỡ.
- Auto-wait, tránh `sleep`, chống flaky test.
- Arrange–Act–Assert.
- Verify cả **functional** (logic) lẫn **visual** (screenshot).
- Screenshot cái gì và khi nào.

## Ràng buộc & lưu ý môi trường

- **Headless mặc định** trên container remote. Muốn xem UI chạy live → chạy local.
  Chế độ thực tế cho remote: headless + screenshot (model "nhìn" qua ảnh).
- **Chromium** chạy ngay (pre-installed). **Firefox** cần `playwright install firefox`
  hoặc trỏ `executablePath` — bổ sung sau khi Chromium ổn.
- Port: ui-workflow sẽ chạy cổng riêng (vd 8991) — không đụng sync-bridge (8989)/unity-dev (8990).

## Các quyết định cần chốt (xem câu hỏi kèm theo)

1. **Cách tiếp cận driver**: dùng official Playwright MCP + ui-workflow mỏng [khuyến nghị],
   hay tự viết full Playwright-Python MCP?
2. **Trọng tâm use case**: thiên về *phát triển UI* (screenshot-driven), *E2E test automation*
   (test case + run), hay cả hai ngang nhau?
3. **Browser**: bắt đầu Chromium-only rồi mở rộng Firefox, hay cross-browser ngay?

## Lộ trình triển khai (sau khi chốt)

- **Phase 1** — Setup playwright-mcp + doc + skill `ui-testing`. Verify loop LOOK→ACT→VERIFY.
- **Phase 2** — `ui-workflow` MCP: test cases, workflows, run history (SQLite, giống unity-dev).
- **Phase 3** — Nâng cao: visual regression + responsive + network mocking.
