#!/usr/bin/env python3
"""Static self-check for the dashboard.

The UI is vanilla JS wired together by string ids and inline onclick attributes, so a
rename breaks it silently: the page still loads, the button just does nothing. These four
checks catch exactly that class of breakage.

    python3 static/orchestrator/check_ui.py
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# encoding="utf-8" BẮT BUỘC: không truyền thì read_text() theo locale, và trên Windows locale
# là cp1252 — index.html/app.js có comment tiếng Việt, gạch dài, ký tự khung nên vỡ ngay
# ở CI (UnicodeDecodeError), dù chạy tốt trên Linux.
HTML = (HERE / "index.html").read_text(encoding="utf-8")
JS = (HERE / "app.js").read_text(encoding="utf-8")

# Cùng lý do cho chiều ra: tên rule CSS in ra khi FAIL có thể chứa non-ASCII, console
# Windows mặc định cp1252 sẽ ném UnicodeEncodeError và giấu mất chính cái lỗi cần đọc.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):   # stream bị thay bằng thứ không reconfigure được
        pass

# Tên có sẵn của trình duyệt/JS + từ khoá — xuất hiện trong onclick nhưng không phải hàm của app.
BUILTIN = {
    "confirm", "alert", "prompt", "getElementById", "stopPropagation",
    "parseInt", "parseFloat", "String", "Number", "Boolean", "JSON",
    "if", "for", "while", "return", "typeof",
}

fails = []


def check(name, bad, hint):
    if bad:
        fails.append(f"{name}: {hint}\n    " + "\n    ".join(sorted(bad)))
    print(("FAIL " if bad else "ok   ") + name)


# 1 · Mọi $("id") trong JS phải tồn tại trong HTML.
html_ids = set(re.findall(r'id="([^"]+)"', HTML))
js_ids = set(re.findall(r'\$\("([^"]+)"\)', JS))
# Các id do chính JS sinh ra rồi mới truy vấn lại.
js_made = set(re.findall(r'\bid\s*=\s*"([\w-]+)"', JS)) | set(
    re.findall(r'<(?:svg|div|textarea|span)\s+id="([\w-]+)"', JS)
)
check("every $(\"id\") exists in index.html", js_ids - html_ids - js_made,
      "referenced by app.js but no such element")

# 2 · Mọi hàm gọi trong handler inline (onclick/onchange/oninput/onfocus/onmousedown…) phải
# được gán vào window ở app.js. KHÔNG chỉ onclick: ô tìm card chạy bằng oninput/onfocus, ô chọn
# phiên bằng onchange/onmousedown — sót attribute nào là loại đó không được kiểm, và một cái tên
# gõ sai ở đó im lặng y hệt onclick (element vẫn hiện, gõ vào thì không có gì xảy ra).
# KHÔNG neo ^: app.js gán nhiều hàm trên cùng một dòng.
exported = set(re.findall(r"\bwindow\.(\w+)\s*=", JS))
called = set()
for src in (HTML, JS):
    for body in re.findall(r'\bon[a-z]+="([^"]*)"', src):
        # ${...} chạy lúc render trong scope JS, không phải trong onclick → bỏ đi.
        # (?<![.\w$]) bỏ lời gọi phương thức (this.value.trim()) — chỉ còn hàm toàn cục.
        called |= set(re.findall(r"(?<![.\w$])([a-zA-Z_$][\w$]*)\s*\(",
                                 re.sub(r"\$\{[^}]*\}", "", body)))
check("every inline handler is exported", called - exported - BUILTIN,
      "called from an inline on*= handler but never assigned to window.*")

# 3 · Mọi icon <use href="#i-x"> phải có <symbol> tương ứng trong sprite.
symbols = set(re.findall(r'<symbol id="(i-[\w-]+)"', HTML))
used = set(re.findall(r'href="#(i-[\w-]+)"', HTML)) | {
    "i-" + n for n in re.findall(r'ic\("([\w-]+)"', JS)
}
check("every icon exists in the sprite", used - symbols, "no <symbol> with this id")

# 4 · Không màu hardcode ngoài khối token — hardcode là hỏng dark mode âm thầm.
style = HTML.split("<style>", 1)[1].split("</style>", 1)[0]
tokens_end = style.rindex("--ring:")
body_css = style[style.index("}", tokens_end):]
stray = set(re.findall(r"#[0-9a-fA-F]{3,8}\b", body_css)) | set(
    re.findall(r"rgba?\([^)]*\)", body_css)
)
# VS Code tự sơn nền của nó; term-lock là overlay trên terminal, luôn tối ở cả 2 theme.
stray -= {"#1e1e1e", "rgba(8,10,14,.88)", "#e6e8eb"}
check("no hardcoded colors outside the token block", stray,
      "put it in :root / :root[data-theme=dark] instead")

# 5 · Mỗi .term-slot phải mang data-key riêng. Card agent và card editor của CÙNG một session
# dùng chung sổ cvTerms; trùng key là hai card cùng trỏ vào một xterm, và cái sau cướp host của
# cái trước — trên màn hình là một card bỗng trống trơn, không lỗi, không log.
slots = re.findall(r'<div class="term-slot"([^>]*)>', JS)
check("every terminal slot carries a data-key",
      {s.strip() for s in slots if "data-key" not in s},
      "attachTerms keys cvTerms by data-key; without it the agent terminal and the editor of the "
      "same session collide on one xterm")

# 6 · Bản xterm vendor phải còn miếng vá toạ độ chuột. Card nằm trong #world có transform:scale(k);
# bản gốc của xterm trộn pixel màn hình (getBoundingClientRect) với cell size dạng CSS, nên mọi
# mức zoom khác 100% là bôi chọn trúng nhầm dòng — lệch = hàng × |1-k|, càng xa mép trên càng sai.
# Nâng cấp xterm sẽ ghi đè im lặng: không có chốt này thì bug quay lại mà không ai biết.
XTERM = (HERE / "vendor" / "xterm.js").read_text(encoding="utf-8")
missing = set()
if "PATCHED-FOR-ORCHESTRATOR" not in XTERM:
    missing.add("marker comment")
if "/u-n,(t.clientY-s.top)/d-o]" not in XTERM:
    missing.add("the divide-by-scale expression in getCoordsRelativeToElement")
check("the vendored xterm keeps its mouse-coordinate patch", missing,
      "re-apply it after upgrading xterm, or selection breaks at every zoom level except 100%")

# 7 · Card editor bị KHOÁ vào cạnh phải card terminal: vị trí + chiều cao là suy ra, không lưu.
# Ba mảnh phải đi cùng nhau, thiếu một là hỏng im lặng — thiếu snapPairs thì card nằm ở (0,0);
# thiếu lời gọi trong layoutZones thì nó đứng yên trong lúc kéo rồi mới nhảy về chỗ; thiếu chốt
# trong saveNodeGeom thì store còn x/y cũ và card nhấp một cái ở khung hình đầu mỗi lần render.
lost = set()
if "function snapPairs(" not in JS:
    lost.add("snapPairs() — the function that glues the editor to its terminal")
if "function layoutZones() {\n  snapPairs();" not in JS:
    lost.add("layoutZones() must call snapPairs() first, or the editor lags every drag")
if "cvPairOf[el.dataset.nid]" not in JS:
    lost.add("saveNodeGeom must drop x/y/h for a paired editor (they are derived)")
if "cvPairOf[node.dataset.nid] || node" not in JS:
    lost.add("pointerdown must redirect a paired editor's drag to its terminal card")
check("the editor card is locked to its terminal", lost,
      "a paired editor has no geometry of its own; only the pair moves")

# 8 · Khung cặp (terminal + editor của cùng session) phải kéo giãn được. .group-zone là
# pointer-events:none, nên tay nắm .rz bên trong nó CHẾT nếu CSS không bật lại — trên màn hình
# khung vẫn vẽ ra, góc vẫn có tam giác, kéo thì không có gì xảy ra. Đúng kiểu hỏng im lặng.
gaps = set()
if ".pair-zone .rz" not in style or "pointer-events: auto" not in style.split(".pair-zone .rz", 1)[1][:80]:
    gaps.add(".pair-zone .rz needs pointer-events:auto (the zone itself is pointer-events:none)")
if 'class="node group-zone pair-zone"' not in JS:
    gaps.add("pairZoneHtml must keep both group-zone (layout/drag) and pair-zone (style) classes")
if '"gresize"' not in JS:
    gaps.add("the pointerdown/pointermove branch that scales every member of the group")
check("the pair frame can be dragged and resized", gaps,
      "resizing the frame is the whole point of grouping the two cards")

# 9 · Shift+wheel phải zoom được kể cả khi con trỏ nằm trên terminal — đó là đường DUY NHẤT để
# zoom khi một card phủ kín canvas. Hai mảnh dễ bị "dọn cho gọn" mà hỏng im lặng:
#   - bỏ capture:true → xterm nhận wheel trước, Shift+wheel với nó là cuộn nhanh, nên terminal
#     vừa cuộn vừa zoom một lúc;
#   - rút `e.deltaY || e.deltaX` về mỗi deltaY → Chrome/Safari đổi trục khi giữ Shift (deltaY=0),
#     bánh xe quay mà mức zoom đứng im.
wheel = JS[JS.index('cv.addEventListener("wheel"'):][:1400]
gaps = set()
if "capture: true" not in wheel:
    gaps.add("capture:true — without it xterm fast-scrolls under the zoom")
if "e.deltaY || e.deltaX" not in wheel:
    gaps.add("e.deltaY || e.deltaX — browsers move shift+wheel onto the X axis")
if "!e.shiftKey && e.target.closest" not in wheel:
    gaps.add("the shift bypass of the .term-slot guard")
if "stopPropagation" not in wheel:
    gaps.add("stopPropagation — the terminal would still scroll a notch")
check("shift+wheel zooms even over a terminal", gaps,
      "a card covering the canvas leaves no background to scroll on")

if fails:
    print("\n" + "\n\n".join(fails))
    sys.exit(1)
print("\nall checks passed")
