# Unity MCP — 3D Environment Design (Coplay)

Hướng dẫn cài đặt và tích hợp [Coplay unity-mcp](https://github.com/CoplayDev/unity-mcp)
để Claude thao tác trực tiếp scene Unity: đặt object, chỉnh lighting, materials,
chụp screenshot và tự đánh giá — vòng lặp art direction thật sự.

## Hai "bộ não" bổ trợ nhau

```
┌─────────────────────────┐        ┌──────────────────────────┐
│  unity-dev (repo này)    │        │  unity-mcp (Coplay)      │
│  = BỘ NÃO KẾ HOẠCH       │        │  = ĐÔI TAY THỰC THI      │
│                          │        │                          │
│  • GDD, story, scenes    │  ───►  │  • Tạo/di chuyển object  │
│  • asset tracking        │  plan  │  • Lighting, fog, PP     │
│  • C# script templates   │        │  • Materials, prefab     │
│                          │  ◄───  │  • Screenshot scene view │
│  (metadata, SQLite)      │ verify │  (thao tác Unity thật)   │
└─────────────────────────┘        └──────────────────────────┘
```

- **unity-dev** trả lời "cần dựng gì" (kế hoạch, câu chuyện, danh sách asset).
- **unity-mcp** thực hiện "dựng nó" trong Unity Editor và **chụp lại để Claude nhìn**.

## Yêu cầu

| Thành phần | Phiên bản |
|-----------|-----------|
| Unity | 2021.3 LTS → 6.x |
| Python | 3.10+ (qua [`uv`](https://docs.astral.sh/uv/)) |
| MCP client | Claude Code |

## Cài đặt (làm trên máy có Unity)

### 1. Cài `uv` (package manager cho Python server)

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 2. Cài Unity package (bridge chạy trong Editor)

Trong Unity Editor: **Window → Package Manager → + → Add package from git URL**, dán:

```
https://github.com/CoplayDev/unity-mcp.git?path=/MCPForUnity#main
```

> Pin bản ổn định: thay `#main` bằng `#v10.0.0`.
> Hoặc dùng OpenUPM: `openupm add com.coplaydev.unity-mcp`

### 3. Auto-register với Claude Code

Trong Unity Editor: **Window → MCP for Unity → Configure All Detected Clients**.

Lệnh này tự động ghi cấu hình MCP server vào Claude Code (bridge tự tải Python
server qua `uv` và chạy stdio transport — không cần start tay).

### 4. Kiểm tra

Mở Claude Code trong thư mục Unity project và hỏi: *"list the current scene hierarchy"*.
Nếu Claude đọc được cây object trong scene → kết nối OK.

## Bộ công cụ unity-mcp (47 tool entrypoints)

Nhóm năng lực chính (xem [tool catalog](https://coplaydev.github.io/unity-mcp/reference/tools/)):

| Nhóm | Dùng để |
|------|---------|
| Scene & GameObject | Tạo/xóa/di chuyển object, set transform, parent, instantiate prefab |
| Components | Add/config component (Light, AudioSource, Collider, ...) |
| C# Scripts | Tạo/sửa script trực tiếp trong project |
| Assets | Quản lý/import asset, materials |
| Menu & Console | Chạy menu item, đọc console log |
| Screenshot | **Chụp scene/game view** — mắt của Claude để art-direct |
| Test & Build | Chạy test, build game |

## Workflow đề xuất

1. **Plan** (unity-dev): `get_gdd`, `list_scenes` → biết cần dựng scene nào, mood gì.
2. **Blockout** (unity-mcp): tạo geometry thô, đặt object theo layout.
3. **Screenshot** (unity-mcp): chụp lại.
4. **Critique & iterate**: Claude nhìn ảnh → chỉnh lighting/fog/bố cục → chụp lại.
5. **Track** (unity-dev): `update_scene` status `in_progress`→`done`, cập nhật asset.

Art-direction playbook nằm trong skill **`unity-environment-art`**
(`.claude/skills/unity-environment-art/SKILL.md`) — nạp theo yêu cầu khi dựng cảnh.
Project binding + safety rules ở `memory/unity-mcp/CLAUDE.md`.
