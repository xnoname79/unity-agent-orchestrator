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

# 2 · Mọi hàm gọi trong onclick="" phải được gán vào window ở app.js.
# KHÔNG neo ^: app.js gán nhiều hàm trên cùng một dòng.
exported = set(re.findall(r"\bwindow\.(\w+)\s*=", JS))
called = set()
for src in (HTML, JS):
    for body in re.findall(r'onclick="([^"]*)"', src):
        # ${...} chạy lúc render trong scope JS, không phải trong onclick → bỏ đi.
        # (?<![.\w$]) bỏ lời gọi phương thức (this.value.trim()) — chỉ còn hàm toàn cục.
        called |= set(re.findall(r"(?<![.\w$])([a-zA-Z_$][\w$]*)\s*\(",
                                 re.sub(r"\$\{[^}]*\}", "", body)))
check("every onclick handler is exported", called - exported - BUILTIN,
      "called from an onclick but never assigned to window.*")

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

# 5 · Không được đập sạch #world. Card VS Code nhúng iframe, mà chuyển iframe sang cha mới là
# trình duyệt TẢI LẠI (đo được: 5 render rời nhịp = 5 lần load) — nên node của nó phải sống sót
# qua re-render. `world.innerHTML = ...` xoá cả nó, và triệu chứng chỉ lộ ra khi agent bàn giao
# việc (một tràng SSE = một tràng reload), tức là lúc khó ngờ tới nhất.
check("#world is never wiped wholesale",
      set(re.findall(r"\bworld\.innerHTML\s*=", JS)),
      "would drop the persistent VS Code nodes and reload their iframes — remove all children "
      "EXCEPT [data-vsc] and insertAdjacentHTML instead")

if fails:
    print("\n" + "\n\n".join(fails))
    sys.exit(1)
print("\nall checks passed")
