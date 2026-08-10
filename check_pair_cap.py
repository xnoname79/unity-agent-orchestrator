#!/usr/bin/env python3
"""Guard for the ping-pong cap: what it blocks, what it lets through, what resets it.

Runs against a throwaway database, never the real one. The cap is enforced inside
enqueue_signal, so every path into the signal table goes through what is checked here.

    python3 check_pair_cap.py
"""
import os
import sys

# Console Windows mặc định cp1252: tên check có tiếng Việt sẽ ném UnicodeEncodeError và giấu
# mất chính kết quả cần đọc. Cùng lý do như trong check_ui.py.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

os.environ["ORCH_DB"] = "check_pair_cap"
os.environ.setdefault("ORCH_PAIR_SIGNAL_CAP", "4")

import session_orchestrator as so   # noqa: E402

WS = "ws_check"
FAILS = []


def check(name, ok, got=""):
    print(("ok   " if ok else "FAIL ") + name + (f"   [{got}]" if not ok else ""))
    if not ok:
        FAILS.append(name)


def blocked(frm, to, msg="turn"):
    try:
        so.enqueue_signal(to, msg, from_session=frm, workspace_id=WS)
        return False
    except so.SignalPairCapExceeded:
        return True


def fresh():
    """DB trắng + bộ nhớ mốc vòng việc trắng, cho mỗi kịch bản chạy độc lập."""
    path = so._db_path()
    if os.path.exists(path):
        os.remove(path)
    so._round_at.clear()
    so.init_db()
    for sid, name in (("id-a", "alpha"), ("id-b", "beta"), ("id-c", "gamma")):
        so.register_session(sid, name, "", "/tmp", [], "", "claude-sonnet-5", "", WS, "claude")


CAP = so.PAIR_SIGNAL_CAP

# ── Chặn đúng ở lượt thứ CAP ────────────────────────────────────────────────
fresh()
for i in range(CAP):
    so.enqueue_signal("id-b" if i % 2 == 0 else "id-a", f"turn {i}",
                      from_session="alpha" if i % 2 == 0 else "beta", workspace_id=WS)
check(f"{CAP} lượt đầu đi lọt", so.pair_signal_count("alpha", "id-b", WS)[0] == CAP,
      so.pair_signal_count("alpha", "id-b", WS)[0])
check("lượt kế bị chặn", blocked("alpha", "id-b"))

# ── Ngân sách theo TỪNG CẶP, không phải theo vai ────────────────────────────
check("cặp khác vẫn còn nguyên lượt", not blocked("alpha", "id-c"))

# ── Người dùng gõ terminal nhúng → mở lại (đường _round_at) ─────────────────
fresh()
for i in range(CAP):
    so.enqueue_signal("id-b" if i % 2 == 0 else "id-a", f"turn {i}",
                      from_session="alpha" if i % 2 == 0 else "beta", workspace_id=WS)
check("chạm trần trước khi gõ", blocked("alpha", "id-b"))
so.note_human_touch("alpha", WS)          # đúng lời gọi trong ws_terminal
check("gõ terminal mở lại ngân sách", not blocked("alpha", "id-b"))

# ── Người dùng gửi signal từ dashboard → mở lại (đường DB) ──────────────────
fresh()
for i in range(CAP):
    so.enqueue_signal("id-b" if i % 2 == 0 else "id-a", f"turn {i}",
                      from_session="alpha" if i % 2 == 0 else "beta", workspace_id=WS)
so._round_at.clear()                      # cô lập: chỉ còn mốc nằm trong DB
so.enqueue_signal("id-a", "việc mới", from_session="human", workspace_id=WS)
so._round_at.clear()                      # enqueue trên vừa đặt mốc RAM, xoá để test đúng DB
check("signal của người dùng mở lại ngân sách", not blocked("alpha", "id-b"))

# ── /compact là lệnh vận hành, không tiêu lượt ──────────────────────────────
fresh()
for _ in range(CAP + 2):
    so.enqueue_signal("id-b", "/compact giữ contract", from_session="alpha", workspace_id=WS)
check("/compact không tiêu lượt", so.pair_signal_count("alpha", "id-b", WS)[0] == 0,
      so.pair_signal_count("alpha", "id-b", WS)[0])

# ── pair_counts khớp với con số đem đi chặn ─────────────────────────────────
fresh()
for i in range(3):
    so.enqueue_signal("id-b" if i % 2 == 0 else "id-a", f"turn {i}",
                      from_session="alpha" if i % 2 == 0 else "beta", workspace_id=WS)
so.enqueue_signal("id-c", "khác cặp", from_session="alpha", workspace_id=WS)
counts = so.pair_counts(WS)
check("pair_counts khớp pair_signal_count",
      counts.get(("alpha", "beta")) == so.pair_signal_count("alpha", "id-b", WS)[0] == 3
      and counts.get(("alpha", "gamma")) == 1,
      dict(counts))

os.remove(so._db_path())
print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all pair-cap checks passed")
