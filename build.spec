# PyInstaller spec — Linux ra 1 file thực thi, Windows ra 1 THƯ MỤC (xem ONEDIR cuối file).
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
# Bỏ qua khi quét submodule. `mcp.cli` import `typer` — dep TUỲ CHỌN (chỉ có nếu cài `mcp[cli]`).
# Thiếu typer thì module đó gọi sys.exit(1), mà SystemExit KHÔNG phải Exception nên tham số
# on_error của PyInstaller không đỡ được: cả build chết. Lọc từ đầu để nó không bị import.
# Ta cũng không dùng CLI của mcp — chỉ dùng server.
SKIP_SUBMODULES = ("mcp.cli",)


def _keep(name):
    return not name.startswith(SKIP_SUBMODULES)


# uvicorn nạp protocol/loop/lifespan bằng chuỗi lúc chạy → phân tích tĩnh không thấy, thiếu là
# binary chạy được tới lúc serve rồi chết "ModuleNotFoundError: uvicorn.protocols...".
# mcp cũng nạp động theo transport.
for pkg in ("uvicorn", "mcp"):
    d, b, h = collect_all(pkg, filter_submodules=_keep)
    datas += d
    binaries += b
    hiddenimports += h

# Terminal nhúng trên Windows chạy qua ConPTY (pywinpty). Gói này có extension biên dịch sẵn +
# DLL đi kèm, và session_orchestrator import nó BÊN TRONG hàm (chỉ khi thực sự mở terminal) nên
# phân tích tĩnh không thấy → phải gom tay. Thiếu bước này thì binary Windows vẫn build, vẫn
# chạy, và chỉ chết đúng lúc user bấm 💻.
if sys.platform == "win32":
    d, b, h = collect_all("winpty")
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["session_orchestrator.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # pkg_resources/setuptools: KHÔNG code nào ở đây dùng (đã kiểm cả uvicorn, starlette, httpx,
    # mcp, anyio, websockets — không cái nào import). Nhưng chỉ cần nó lọt vào bundle là
    # PyInstaller gắn runtime hook pyi_rth_pkgres, hook đó import pkg_resources → jaraco.text →
    # jaraco.context → `backports.tarfile`, gói TUỲ CHỌN thường không được cài → binary chết ngay
    # dòng đầu, trước cả main(). Loại hẳn: hết hook, hết jaraco, và nhẹ bớt vài MB.
    # (Chỉ dính setuptools ~71–80 — bản cũ chưa vendor jaraco, bản 81+ bỏ hẳn pkg_resources.
    #  Runner GitHub rơi đúng vào dải đó nên máy dev không thấy.)
    excludes=["tkinter", "matplotlib", "numpy", "PIL", "torch", "sentence_transformers",
              "pkg_resources", "setuptools"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# Windows đóng gói kiểu THƯ MỤC (onedir), Linux giữ 1 file. Vì sao lệch nhau: bootloader onefile
# giải nén cả bundle vào %TEMP% rồi chạy chính file nó vừa ghi ra — đúng hành vi của dropper, nên
# heuristic của Defender/SmartScreen hay báo nhầm và bảo người dùng XOÁ file. onedir không có bước
# ghi-rồi-chạy đó. Linux không dính chuyện này nên để nguyên 1 file cho gọn.
# Cả hai kiểu đều không đổi cách tìm đường dẫn: _bundle_dir() vẫn đọc sys._MEIPASS (onedir trỏ vào
# _internal/), _app_dir() vẫn là thư mục chứa file thực thi — chỗ người dùng đặt .env.
ONEDIR = sys.platform == "win32"

common = dict(
    name="agent-orch",
    console=True,           # công cụ CLI: cần thấy log; console=False là chạy xong không biết gì
    upx=False,              # UPX hay bị Windows Defender báo nhầm — không nén cho yên
)

if ONEDIR:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
    COLLECT(exe, a.binaries, a.datas, upx=False, strip=False, name="agent-orch")
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], strip=True, **common)
