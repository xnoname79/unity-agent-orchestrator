---
name: ui-testing
description: Develop and test UI + function workflows on a real browser (Firefox/Chrome) via the Playwright MCP. Use when building UI iteratively, writing/running E2E tests, reproducing a user workflow, verifying a form/flow works, debugging a frontend issue in the browser, or checking visual appearance. Triggers: "test the UI", "check the login flow", "does this button work", "run the E2E test", "verify the page", "reproduce in the browser", "screenshot the app".
---

# UI Testing & Dev (Playwright MCP)

Playbook điều khiển browser thật qua [Playwright MCP](https://github.com/microsoft/playwright-mcp)
để phát triển UI và test workflow. Setup: `docs/browser-ui.md`.

## Nguyên tắc tối thượng: LOOK → ACT → VERIFY

**KHÔNG BAO GIỜ tương tác mù.** Mỗi bước:
1. **LOOK** — `browser_snapshot` để lấy accessibility tree + element refs (tìm/nhắm
   element), `browser_take_screenshot` khi cần nhìn giao diện.
2. **ACT** — `browser_click`/`browser_type`/... trên element đã xác định từ snapshot.
3. **VERIFY** — assert kết quả: snapshot lại, `browser_evaluate`, đọc
   `browser_console_messages`/`browser_network_requests`, + screenshot đối chiếu.

## Selector strategy (chống flaky — quan trọng nhất)

Ưu tiên theo thứ tự (bền → dễ vỡ):
1. **Role + accessible name** (từ snapshot) — vd button "Đăng nhập". Bền nhất.
2. **Test id** — `data-testid`. Khuyến khích team thêm cho element hay test.
3. **Text hiển thị** — cho nội dung ổn định.
4. **CSS/XPath** — CHỈ khi bất đắc dĩ. Dễ vỡ khi UI đổi.

Luôn lấy element ref từ `browser_snapshot` thay vì đoán selector.

## Chống flaky test

- **KHÔNG dùng sleep cứng.** Dùng `browser_wait_for` (chờ text/element/điều kiện).
- Playwright auto-wait sẵn — tin vào nó, đừng thêm delay tùy tiện.
- Chờ **network idle** hoặc element cụ thể trước khi assert, không chờ theo thời gian.
- Test phải **độc lập & idempotent** — chạy lại nhiều lần vẫn pass, không phụ thuộc state trước.

## Arrange – Act – Assert

Cấu trúc mỗi test/workflow rõ 3 phần:
- **Arrange** — điều hướng, đăng nhập, set state ban đầu.
- **Act** — thực hiện đúng 1 hành động/luồng đang test.
- **Assert** — kiểm tra kết quả mong đợi (element, text, url, network, console không lỗi).

## Verify cả 2 tầng

- **Functional** — logic đúng chưa? (data hiện đúng, url chuyển đúng, API gọi đúng, không lỗi console).
- **Visual** — trông đúng chưa? (screenshot: layout, responsive, không vỡ giao diện).
Một workflow "pass" phải đạt cả hai.

## Phát triển UI (screenshot-driven)

Khi dev UI (không chỉ test):
1. Sửa code → reload trang (`browser_navigate` lại hoặc hot reload).
2. `browser_take_screenshot` → **nhìn** kết quả.
3. Tự phê bình: layout, spacing, responsive, trạng thái (hover/focus/error) đúng chưa?
4. Chỉnh code, lặp lại. Chụp nhiều viewport (`--viewport-size` / `--device`) để check responsive.

## Debug khi fail

- Đọc `browser_console_messages` (lỗi JS) và `browser_network_requests` (API fail, 4xx/5xx).
- `browser_take_screenshot` tại thời điểm fail để xem trạng thái thật.
- `browser_evaluate` để inspect DOM/state cụ thể.
- Bật `--save-trace` khi cần trace chi tiết.

## An toàn

- Test trên môi trường dev/staging, KHÔNG chạy trên production trừ khi được yêu cầu rõ.
- Hành động phá hủy (xóa data, submit thanh toán thật) → xác nhận với user trước.
- Không commit screenshot/trace chứa thông tin nhạy cảm.

## Vòng lặp với ui-workflow

`ui-workflow` MCP (port 8991) lưu test case, workflow tái sử dụng, và run history.

- Đầu việc: `list_test_cases` / `get_test_case` để lấy steps + expected.
- Thực thi steps qua Playwright MCP (LOOK→ACT→VERIFY).
- Kết thúc: `record_run(status, notes, screenshots)` để lưu kết quả.
- `get_test_summary` để xem sức khỏe toàn suite; `get_run_history` để soi regression.
- Flow lặp lại (login, checkout) → lưu bằng `add_workflow` để tái sử dụng.
