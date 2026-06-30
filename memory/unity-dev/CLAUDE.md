# The Last Signal — Tín Hiệu Cuối

> A psychological space-survival walking simulator about isolation, hope, and the
> price of knowing the truth. Working title — rename `project` slug as needed.

When using **unity-dev MCP**, always use `project="last-signal"` for all tool calls.

---

## Game Pitch

Bạn là người sống sót cuối cùng, trôi dạt trong vũ trụ trên một con tàu nhỏ. Từ
cabin an toàn, bạn dò những tín hiệu le lói trong khoảng không — mỗi tín hiệu là một
canh bạc giữa tài nguyên cạn kiệt và hy vọng tìm thấy sự sống. Người bạn đồng hành
duy nhất là AI của con tàu, giọng nói duy nhất trong sự tĩnh lặng vô tận.

## Design Pillars (4 trụ cột — luôn bám theo khi thiết kế)

1. **Psychological Isolation** — Sự cô độc là kẻ thù thật sự. Âm thanh (tiếng thở,
   vỏ tàu cọt kẹt, tĩnh lặng) tạo áp lực. Stress cao → ảo giác trên radar, âm thanh
   không thật, bóng người ngoài cửa kính. Tham chiếu tông: *Silent Hill 2*.
2. **Push-Your-Luck** — Quản lý đa tài nguyên: **Fuel** (di chuyển), **Oxygen**
   (sinh tồn), **Hull Integrity** (độ bền). Tín hiệu càng xa = rủi ro càng cao =
   reward càng lớn (manh mối, linh kiện nâng cấp). Mỗi quyết định là một giằng xé.
3. **The Ship's AI (Unreliable Narrator)** — NPC chính, nguồn hội thoại duy nhất.
   Báo distance/danger/reward của tín hiệu nhưng độ chính xác giảm dần hoặc giấu sự
   thật để "bảo vệ" người chơi. AI tiến hóa cảm xúc theo thời gian.
4. **Environmental Storytelling** — Mỗi xác tàu là một mảnh cốt truyện: hộp đen ghi
   cãi vã cuối cùng, vết cào trên vách, dòng chữ tuyệt vọng. Người chơi tự chắp ghép
   bức tranh toàn cảnh.

## Core Loop

```
CABIN (safe) → quét radar → AI báo cáo tín hiệu → ĐÁNH CƯỢC: đi hay ở?
   → travel (trừ fuel, cutscene ngắn) → khám phá xác tàu (environmental story)
   → loot linh kiện/manh mối → về CABIN (checkpoint + AI dialogue) → lặp lại
```

> **Scope rule (solo dev):** Travel là **menu-based** (chọn tín hiệu → trừ fuel →
> cutscene), KHÔNG phải flight sim 6DOF. Dồn sức cho narrative + atmosphere.

---

## unity-dev MCP — Cách dùng tools cho game này

### Game Design Document (`update_gdd` / `get_gdd`)
Bắt đầu mọi session bằng `get_gdd(project="last-signal")` để nắm context.
Sections nên có: `overview`, `pillars`, `core_loop`, `mechanics`, `resources`,
`ai_companion`, `story`, `art_style`, `audio`, `controls`.

### Scenes (`add_scene` / `list_scenes` / `update_scene`)
- `Cabin_Interior` — hub trung tâm, nơi an toàn (mood: ấm áp giả tạo, cô độc)
- `Travel_Cutscene` — chuyển cảnh giữa các tín hiệu
- `DeadShip_*` — các xác tàu khám phá (vd: `DeadShip_Medical`, `DeadShip_Cargo`)
- Đặt `order_index` theo thứ tự câu chuyện; `mood` mô tả atmosphere rõ ràng.

### Story Elements (`add_story_element` / `export_narrative_json`)
Quy ước `type`:
- `dialogue` — hội thoại AI (metadata: `{"speaker":"AI","emotion":"...","choices":[...]}`)
- `note` — log, hộp đen, nhật ký tìm thấy trên xác tàu
- `environment` — chi tiết kể chuyện qua môi trường (vết cào, dòng chữ máu)
- `event` — sự kiện kịch bản (ảo giác, mất điện, cảnh báo)
- `collectible` — linh kiện nâng cấp, manh mối vật phẩm

Quy ước `trigger_type`: `interact`, `proximity`, `auto`, `pickup`, `cutscene`.
Với ảo giác, dùng `event` + metadata `{"stress_threshold": 70}`.

Khi narrative của một scene xong → `export_narrative_json(scene=...)` để lấy JSON
đưa vào `Assets/Resources/` cho Unity đọc runtime.

### Assets (`add_asset` / `list_assets` / `update_asset`)
Track theo `type`: model, texture, sound, music, animation, prefab.
**Ưu tiên audio** (game này âm thanh là chủ đạo): tiếng thở, ambient vỏ tàu, radar,
giọng AI. Đặt `source` để nhớ nguồn (Freesound, Asset Store, tự tạo).
Status: `needed` → `found` → `done`.

### C# Scripts (`generate_script` / `list_templates`)
Templates phù hợp game này:
- `FirstPersonController` — di chuyển trong cabin/xác tàu
- `InteractionSystem` + `Interactable` — tương tác vật thể
- `DialogueUI` — UI hội thoại AI (có typing effect)
- `NotePickup` — đọc log/hộp đen
- `AudioZone` — vùng âm thanh (ambient theo khu vực)
- `CutsceneTrigger` — trigger sự kiện/ảo giác/travel

Generate xong → lưu vào `Assets/Scripts/` trong Unity project.

---

## Workflow Conventions

1. **Đầu session**: `get_gdd` để nắm vision, `list_scenes` để biết tiến độ.
2. **Khi thêm nội dung mới**: tạo scene trước → thêm story elements vào scene →
   track assets cần thiết.
3. **Story status**: `draft` (đang viết) → `final` (chốt, sẵn sàng export).
4. **Scene status**: `planned` → `in_progress` → `done`.
5. **Asset status**: `needed` → `found` → `done`.
6. Trước khi code C#, kiểm tra `list_templates` để tái sử dụng template thay vì viết
   từ đầu.

## North Star (mục tiêu cảm xúc)

> Quyết định: người chơi đang tìm gì? (một người thân cụ thể / số phận Trái Đất / lời
> hứa chưa trọn). Ghi vào GDD section `story`. Mọi tín hiệu nên dẫn dần tới câu trả
> lời này để push-your-luck có stakes cảm xúc, không chỉ là sinh tồn.
