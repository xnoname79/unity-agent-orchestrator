#!/usr/bin/env python3
r"""Guard for the argv that starts the VS Code CLI — the one thing POSIX and Windows disagree on.

Opening a folder spawns the VS Code CLI. On Windows that CLI is `bin\code.cmd`, and CreateProcess
neither looks it up through PATHEXT (so a bare `code` is not found) nor runs a batch file at all
(WinError 193). Nobody here develops on Windows, so this only ever surfaces when a user clicks the
button. This check runs on the CI Windows runner and fails loudly if the argv ever goes back to
spawning the raw name.

    python3 check_vscode_argv.py
"""
import os
import sys
import tempfile
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
print("\nall checks passed")
