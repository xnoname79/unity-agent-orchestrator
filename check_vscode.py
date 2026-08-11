#!/usr/bin/env python3
r"""Guards for the VS Code card: how the CLI is started, and when it is actually serving.

1. The argv. On Windows the CLI is `bin\code.cmd`, and CreateProcess neither looks it up through
   PATHEXT (so a bare `code` is not found) nor runs a batch file at all (WinError 193). Nobody
   here develops on Windows, so this only surfaces when a user clicks the button — the CI Windows
   runner is the only place the branch is real.

2. What counts as ready. `code serve-web` opens its port in about a second and downloads the
   ~100MB server later, on the first request, answering 202 the whole time. A TCP check therefore
   passes immediately and means nothing; swap the HTTP check back for one and the card will point
   an iframe at a server that has nothing to serve.

    python3 check_vscode.py
"""
import asyncio
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Same reason as check_ui.py: a path printed on FAIL may hold non-ASCII, and a Windows console
# defaults to cp1252 — it would raise UnicodeEncodeError and hide the very error you need to read.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_orchestrator as so   # noqa: E402

NT = os.name == "nt"
tmp = Path(tempfile.mkdtemp())
# CLI giả, đặt đúng đuôi mà VS Code dùng trên từng hệ: Windows là batch, POSIX là script thường.
fake = tmp / ("fake-code.cmd" if NT else "fake-code")
fake.write_text("@echo off\n" if NT else "#!/bin/sh\n", encoding="utf-8")
fake.chmod(0o755)
os.environ["PATH"] = str(tmp) + os.pathsep + os.environ.get("PATH", "")

so.VSCODE_BIN = "fake-code"      # tên trần, đúng như người dùng để mặc định
argv = so._vscode_argv("serve-web", "--port", "8995")

# So sánh bỏ qua hoa/thường: trên Windows shutil.which() ghép đuôi lấy TỪ PATHEXT (viết hoa) chứ
# không lấy tên file thật, nên fake-code.cmd quay về thành fake-code.CMD. Chỉ cái test này phải
# biết chuyện đó — code sản phẩm đã .lower() trước khi xét đuôi rồi.
lowered = [a.lower() for a in argv]
target = str(fake).lower()

assert target in lowered, f"PATH lookup missed the CLI (Windows needs PATHEXT): {argv}"
assert argv[-3:] == ["serve-web", "--port", "8995"], f"arguments got dropped: {argv}"
if NT:
    assert argv[:2] == ["cmd.exe", "/c"], f"a batch file has to go through cmd.exe: {argv}"
else:
    assert lowered[0] == target, f"POSIX must not be wrapped in cmd.exe: {argv}"

# CLI không cài: vẫn trả tên trần, để thông báo lỗi nói đúng thứ vừa thử chứ không nói trống không.
so.VSCODE_BIN = "definitely-not-installed-xyz"
assert so._vscode_argv() == ["definitely-not-installed-xyz"], so._vscode_argv()

print("ok   argv resolves the CLI through PATH" + (" and runs .cmd via cmd.exe" if NT else ""))
print("ok   a missing CLI keeps its name so the error says what was tried")


def serving(code):
    """Một HTTP server dùng một lần, trả đúng mã này — đóng vai serve-web ở từng giai đoạn."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(code)
            self.send_header("Content-Length", "1")
            self.end_headers()
            self.wfile.write(b"x")

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class DeadProc:
    returncode = 0


class LiveProc:
    returncode = None


async def ready_when(code):
    srv = serving(code)
    try:
        return await so._vscode_ready({"proc": LiveProc(), "port": srv.server_port})
    finally:
        srv.shutdown()


async def probes():
    assert await ready_when(202) is False, "202 = đang tải server, CHƯA dùng được"
    # 403 vì probe không kèm token — nhưng trả lời được nghĩa là server đã lên.
    assert await ready_when(403) is True, "mã khác 202 = serve-web đã phục vụ"
    assert await ready_when(200) is True

    v = {"proc": LiveProc(), "port": 1}          # cổng không ai nghe
    assert await so._vscode_ready(v) is False, "chưa nghe thì chưa sẵn sàng"

    v = {"proc": LiveProc(), "port": 1, "ready": True}
    assert await so._vscode_ready(v) is True, "ready phải DÍNH, không probe lại khi đã lên"

    v = {"proc": DeadProc(), "port": 1}
    assert await so._vscode_ready(v) is False, "tiến trình chết thì thôi probe"

asyncio.run(probes())
print("ok   202 means still downloading, anything else means serving")
print("ok   ready is sticky and stops probing once up or dead")
print("\nall checks passed")
