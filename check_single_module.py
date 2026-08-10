#!/usr/bin/env python3
"""Guard: session_orchestrator must never exist as two module objects.

The server starts as `python session_orchestrator.py serve`, so that file is `__main__`.
signal_mcp then does `import session_orchestrator`, and without an alias in sys.modules
Python loads the file a SECOND time into a separate module with its own globals.

Everything DB-backed keeps working, which is what makes it so quiet — only the in-memory
state splits. That is how the ping-pong cap lost its reset: typing in the embedded terminal
wrote the round marker into `__main__._round_at`, while send_signal over MCP read the
imported copy's, which stayed empty forever.

    python3 check_single_module.py
"""
import os
import pathlib
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
TARGET = HERE / "session_orchestrator.py"

# DB riêng: kịch bản này chạy tới nhánh CLI của module, đừng để nó đụng DB thật.
os.environ["ORCH_DB"] = "check_single_module"
sys.argv = [TARGET.name, "list-sessions"]          # lệnh chỉ đọc
sys.path.insert(0, str(HERE))

# Nạp y như production: file chạy như script, mang tên '__main__'.
main_mod = types.ModuleType("__main__")
main_mod.__file__ = str(TARGET)
sys.modules["__main__"] = main_mod
try:
    exec(compile(TARGET.read_text(encoding="utf-8"), str(TARGET), "exec"), main_mod.__dict__)
except SystemExit:
    pass

import session_orchestrator as imported            # đúng dòng signal_mcp chạy

fails = []
if imported is not main_mod:
    fails.append("`import session_orchestrator` trả về module KHÁC với bản '__main__' đang chạy")
elif imported._round_at is not main_mod._round_at:
    fails.append("_round_at bị chẻ làm hai bản")
else:
    # Đường thật: gõ terminal ghi ở bản '__main__', MCP đọc ở bản import.
    main_mod.note_human_touch("probe-role", "probe-ws")
    if not imported._round_at.get(("probe-ws", "probe-role")):
        fails.append("mốc do note_human_touch ghi ra không thấy được ở phía import")

print(("FAIL " if fails else "ok   ") + "session_orchestrator is a single module object")
if fails:
    print("\n" + "\n".join("  - " + f for f in fails))
    print("\n  Sửa: thêm lại vào đầu session_orchestrator.py, ngay sau khối import:\n"
          '      if __name__ == "__main__":\n'
          '          sys.modules.setdefault("session_orchestrator", sys.modules[__name__])')
    sys.exit(1)
