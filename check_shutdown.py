#!/usr/bin/env python3
"""Guard: Ctrl-C phải tắt được server dù dashboard đang mở stream SSE.

uvicorn tắt kiểu graceful = CHỜ mọi response đang dở chạy xong. Stream `/api/events` thì không
bao giờ xong: vòng lặp của nó chỉ thoát khi CLIENT ngắt, mà lúc người dùng bấm Ctrl-C thì tab
dashboard vẫn đang mở. Triệu chứng đúng như báo cáo:

    INFO:     Waiting for connections to close. (CTRL+C to force quit)

…rồi đứng đó tới khi force quit, và force quit thì ném một tràng CancelledError/KeyboardInterrupt.

Test này dựng server thật, mở một kết nối SSE thật, gửi SIGTERM, và fail nếu tiến trình không
chết trong hạn.

    python3 check_shutdown.py
"""
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
DEADLINE = 15          # giây: graceful timeout là 5, cộng dư cho máy CI chậm

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]

tmp = tempfile.mkdtemp()
env = {**os.environ, "ORCH_PORT": str(PORT), "ORCH_HOST": "127.0.0.1",
       "ORCH_DB": "check_shutdown", "ORCH_DRY_RUN": "1",
       "ORCH_WORKSPACES_ROOT": os.path.join(tmp, "ws"),
       "ORCH_CLAUDE_CONFIG": os.path.join(tmp, "claude.json")}
proc = subprocess.Popen([sys.executable, "session_orchestrator.py", "serve"], env=env, cwd=str(HERE),
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
try:
    for _ in range(80):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    else:
        raise AssertionError("server never came up")

    # Kết nối SSE THẬT, giữ mở — đúng cái tab dashboard đang làm lúc người dùng bấm Ctrl-C.
    sse = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/events", timeout=10)
    assert b"ready" in sse.readline() + sse.readline(), "stream did not open"

    t0 = time.time()
    proc.send_signal(signal.SIGINT)      # y hệt Ctrl-C ở terminal
    try:
        err = proc.communicate(timeout=DEADLINE)[1].decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError(
            f"server still running {DEADLINE}s after Ctrl-C with an SSE client attached — "
            "this is the 'Waiting for connections to close' hang")
    took = time.time() - t0
    # PHẢI nhanh hơn hẳn timeout_graceful_shutdown=5s. Không có mốc này thì chốt chặn ép-đóng
    # cũng làm test xanh, và cái hook thật (handle_exit → _stop_streams) hỏng mà không ai biết.
    assert took < 3, (f"took {took:.1f}s — the stream was force-closed by the graceful timeout, "
                      "not ended by _stop_streams()")
    print(f"ok   Ctrl-C shuts down in {took:.1f}s with a live SSE stream attached")

    # Tắt sạch còn phải KHÔNG in traceback. asyncio.run() của 3.11 dựng lại KeyboardInterrupt sau
    # khi loop dừng; không nuốt thì mỗi lần Ctrl-C người dùng ăn một stack trace trông như crash,
    # dù shutdown đã chạy xong. -2 = chết theo SIGINT, đúng quy ước, cũng chấp nhận.
    assert "Traceback" not in err, ("Ctrl-C printed a traceback:\n"
                                    + "\n".join(err.strip().splitlines()[-8:]))
    assert proc.returncode in (0, -signal.SIGINT), f"unclean exit status {proc.returncode}"
    assert "Application shutdown complete" in err, "lifespan shutdown never ran:\n" + err[-400:]
    print("ok   and it exits without a traceback, after the lifespan shutdown ran")
finally:
    if proc.poll() is None:
        proc.kill()
    db = Path.home() / ".session_orch_db" / "check_shutdown.db"
    for sfx in ("", "-wal", "-shm"):
        Path(str(db) + sfx).unlink(missing_ok=True)

print("\nall checks passed")
