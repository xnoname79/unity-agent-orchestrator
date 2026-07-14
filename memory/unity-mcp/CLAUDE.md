# Unity MCP — Project Rules

Rules cho project khi dùng [Coplay unity-mcp](https://github.com/CoplayDev/unity-mcp)
để thao tác trực tiếp scene Unity, kết hợp với **unity-dev** MCP (kế hoạch/metadata).
Setup: xem `docs/unity-mcp.md`.

## Hai bộ não bổ trợ

- **unity-dev** = kế hoạch: GDD, story, scenes, asset tracking, C# templates.
- **unity-mcp** = thực thi: tạo/di chuyển object, lighting, fog, materials, screenshot.

Luồng: plan (unity-dev) → build (unity-mcp) → screenshot → iterate → track (unity-dev).

## Khi dựng / thiết kế môi trường 3D

Invoke skill **`unity-environment-art`** — playbook art-direction đầy đủ (LOOK→
CRITIQUE→ADJUST, lighting/atmosphere/post-processing/composition, recipe theo mood).

## Safety

- Làm việc trong scene đã lưu; lưu tăng dần, đừng đè scene chính khi thử nghiệm.
- Thay đổi lớn (xóa hàng loạt, đổi lighting toàn cục) → xác nhận với user trước.
- Đọc scene hierarchy trước khi sửa để không phá cấu trúc có sẵn.
- Không chạy build/test nặng trừ khi được yêu cầu.
