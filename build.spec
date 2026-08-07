# PyInstaller spec — build 1 file thực thi cho Linux và Windows.
#   pyinstaller build.spec --noconfirm
#
# Vì sao dùng file .spec thay vì cờ dòng lệnh: `--add-data` ngăn cách nguồn/đích bằng ':' trên
# Linux và ';' trên Windows → viết cờ là phải tách 2 lệnh cho 2 OS rồi lệch nhau. Ở đây khai
# bằng tuple, PyInstaller tự lo.
import sys
from PyInstaller.utils.hooks import collect_all

datas = [
    ("static", "static"),                    # dashboard (index.html, app.js, vendor xterm)
    (".claude/skills", ".claude/skills"),    # template vai cho dropdown spawn
]
binaries, hiddenimports = [], [
    # 3 module MCP được import BÊN TRONG build_app() — PyInstaller vẫn thấy, khai thêm cho chắc.
    "signal_mcp", "unity_dev", "asset_fetch",
]
# uvicorn nạp protocol/loop/lifespan bằng chuỗi lúc chạy → phân tích tĩnh không thấy, thiếu là
# binary chạy được tới lúc serve rồi chết "ModuleNotFoundError: uvicorn.protocols...".
# mcp cũng nạp động theo transport.
for pkg in ("uvicorn", "mcp"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["session_orchestrator.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "numpy", "PIL", "torch", "sentence_transformers"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="agent-orch",
    console=True,           # công cụ CLI: cần thấy log; console=False là chạy xong không biết gì
    upx=False,              # UPX hay bị Windows Defender báo nhầm — không nén cho yên
    strip=sys.platform != "win32",
)
