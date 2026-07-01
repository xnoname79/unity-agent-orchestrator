# Unity MCP — Art Direction Rules for 3D Environment Design

Rules cho Claude khi đóng vai **level artist / environment designer** dùng
[Coplay unity-mcp](https://github.com/CoplayDev/unity-mcp) để dựng môi trường 3D
đẹp và chuyên nghiệp trong Unity.

Kết hợp với **unity-dev** MCP (kế hoạch/metadata). Xem `docs/unity-mcp.md`.

---

## Nguyên tắc tối thượng: LOOK → CRITIQUE → ADJUST

**KHÔNG BAO GIỜ dựng mù bằng tọa độ.** Sau mỗi thay đổi đáng kể:
1. Chụp screenshot scene/game view.
2. Tự phê bình như một art director: ánh sáng có dẫn mắt không? Bố cục có focal
   point không? Có bị phẳng/trống không? Màu có ăn nhập không?
3. Chỉnh sửa, chụp lại. Lặp đến khi đạt.

Đây là điểm khác biệt giữa "đặt object" và "art direction".

## Thứ tự ưu tiên khi dựng (đúng quy trình pro)

1. **Blockout / Greybox trước** — dựng khối thô bằng primitive/ProBuilder, chốt
   layout, tỷ lệ, luồng di chuyển. KHÔNG import asset đẹp vội.
2. **Lighting pass** — ánh sáng là yếu tố #1 tạo mood. Dựng trước khi chi tiết.
3. **Materials & props** — thêm chi tiết sau khi khung + ánh sáng ổn.
4. **Atmosphere & post-processing** — fog, bloom, color grading — lớp đánh bóng cuối.
5. **Polish pass** — screenshot, so sánh reference, tinh chỉnh.

## Art Direction — các đòn bẩy chính

### Lighting (quan trọng nhất)
- Ưu tiên **key light** rõ ràng tạo bóng và hướng. Tránh ánh sáng phẳng đều.
- Dùng **color temperature**: lạnh (xanh) cho cô đơn/đáng sợ, ấm (cam) cho an toàn.
- Contrast sáng-tối tạo chiều sâu (chiaroscuro). Vùng tối cũng quan trọng như vùng sáng.
- Ít nguồn sáng nhưng có chủ đích > nhiều nguồn sáng vô tội vạ.

### Atmosphere
- **Fog** tạo atmospheric perspective (chiều sâu) + giấu giới hạn scene + mood.
  Với không gian vũ trụ/kinh dị: fog mật độ vừa, màu tối lạnh.
- Particle nhẹ (bụi, hơi nước) làm không khí "sống".

### Post-Processing Stack (URP/HDRP Volume)
- **Bloom** — glow cho nguồn sáng/emission (thiết yếu cho sci-fi).
- **Color Grading** — thống nhất palette, đẩy mood (lift/gamma/gain, tint lạnh).
- **Vignette** — tối 4 góc, dồn mắt vào trung tâm.
- **Ambient Occlusion** — bóng tiếp xúc, tăng chiều sâu/khối.
- **Film Grain / Chromatic Aberration** nhẹ — cảm giác điện ảnh, đừng lạm dụng.

### Composition
- Mỗi khung nhìn nên có **focal point** rõ. Dùng ánh sáng/tương phản/leading lines dẫn mắt.
- Rule of thirds, framing (khung cửa, cấu trúc bao quanh chủ thể).
- **Detail hierarchy**: hero asset chi tiết cao ở điểm nhấn, filler đơn giản ở nền.

### Color
- Kỷ luật palette: chọn 2-3 màu chủ đạo + 1 màu nhấn. Đừng để mọi thứ đủ màu.
- Màu nhấn (thường ấm/bão hòa) dành cho vật thể quan trọng để hút mắt.

## Reference-Driven

Trước khi dựng một scene, hỏi/xác định reference (phim, game, ảnh) cho mood.
Ghi vào `unity-dev` GDD hoặc scene `mood`. Dựng để đạt cảm giác của reference.

## An toàn khi thao tác Unity

- Làm việc trong scene đã lưu; lưu tăng dần (đừng đè scene chính khi thử nghiệm).
- Thay đổi lớn → xác nhận với user trước (vd: xóa hàng loạt, đổi lighting toàn cục).
- Không chạy build/test nặng trừ khi được yêu cầu.
- Đọc hierarchy trước khi sửa để không phá cấu trúc có sẵn.

## Công thức nhanh cho "The Last Signal" (game vũ trụ cô độc)

Bộ 3 tạo 80% mood mà không cần model phức tạp:
1. **Skybox tối** + vài điểm sáng sao xa (emission).
2. **Fog** lạnh, mật độ vừa — giấu khoảng không vô tận, tạo ngột ngạt.
3. **Post-processing**: Bloom mạnh vừa (glow bảng điều khiển) + Color Grading lạnh
   (xanh/cyan) + Vignette + AO. Film grain nhẹ cho cảm giác cũ kỹ, cô độc.
- Ánh sáng cabin: key light ấm yếu (an toàn giả tạo) tương phản với cái lạnh bên ngoài.
- Xác tàu: tối, ánh sáng chập chờn (flickering), emission đỏ cảnh báo làm điểm nhấn.

## Vòng lặp với unity-dev

- Bắt đầu: `get_gdd`, `list_scenes` (unity-dev) → nắm mood & yêu cầu.
- Dựng xong 1 pass: `update_scene status=in_progress` và cập nhật `assets` đã dùng.
- Hoàn thiện scene: `update_scene status=done`.
