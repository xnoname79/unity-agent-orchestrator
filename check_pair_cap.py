#!/usr/bin/env python3
"""Guard for the ping-pong cap: what it blocks, what it lets through, what resets it.

Runs against a throwaway database, never the real one. The cap is enforced inside
enqueue_signal, so every path into the signal table goes through what is checked here.

    python3 check_pair_cap.py
"""
import asyncio
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

# ── xterm TỰ đáp truy vấn của TUI: không phải người gõ ──────────────────────
# Mỗi chuỗi dưới đây đã ĐO trên bản xterm.js đang vendor — thư viện tự gửi, không ai chạm bàn
# phím. Tính chúng là người gõ = mở vòng việc mới = trần ping-pong tụt về 1 giữa chừng.
for seq, label in [("\x1b[1;1R", "CPR (ESC[6n)"), ("\x1b[?1;2c", "DA1 (ESC[c)"),
                   ("\x1b[>0;276;0c", "DA2 (ESC[>c)"), ("\x1b[0n", "DSR (ESC[5n)"),
                   ("\x1b[I", "focus vào (ESC[?1004h)"), ("\x1b[O", "focus ra"),
                   ("\x1b[A", "mũi tên lên"), ("\x1b", "Esc"), ("\x03", "Ctrl-C")]:
    check(f"{label} không mở vòng việc", not so.is_user_typing(seq))
check("Enter mở vòng việc", so.is_user_typing("\r"))
check("gõ xong bấm Enter mở vòng việc", so.is_user_typing("sửa giúp tôi chỗ này\r"))
check("gõ dở chưa Enter thì chưa mở", not so.is_user_typing("sửa giúp tôi chỗ này"))

# ── Một chiều: báo cáo kết thúc cuộc trao đổi ──────────────────────────────
# Lỗi gốc: luật "xong việc thì báo cáo về người gửi" không phân biệt báo cáo với việc mới, nên
# A nhận báo cáo của B lại "báo cáo" ngược cho B… qua lại tới trần. Báo cáo phải tự thành một chiều.
fresh()


def reply_flag(sig_id):
    conn = so._conn()
    try:
        return conn.execute("SELECT expects_reply FROM signals WHERE id = ?", (sig_id,)).fetchone()[0]
    finally:
        conn.close()


task = so.enqueue_signal("id-b", "làm X", from_session="alpha", workspace_id=WS)
check("việc giao mới thì chờ báo cáo", reply_flag(task) == 1)
so.set_signal_status(task, "processing")              # beta đang làm việc alpha giao
report = so.enqueue_signal("id-a", "[REPORT] xong X", from_session="beta", workspace_id=WS)
check("báo cáo gửi trong lúc làm việc được giao thì tự thành một chiều", reply_flag(report) == 0)
handoff = so.enqueue_signal("id-c", "bước kế của X", from_session="beta", workspace_id=WS)
check("bàn giao cho vai THỨ BA vẫn chờ báo cáo", reply_flag(handoff) == 1)
so.set_signal_status(task, "done")
later = so.enqueue_signal("id-a", "việc mới", from_session="beta", workspace_id=WS)
check("xong việc rồi mới giao việc mới thì lại chờ báo cáo", reply_flag(later) == 1)
oneway = so.enqueue_signal("id-c", "cập nhật docs", from_session="alpha", workspace_id=WS,
                           expects_reply=0)
check("người gửi tự chọn một chiều", reply_flag(oneway) == 0)

alpha = so.get_session("id-a")
asks = so.inject_prompt(alpha, "x", "beta", 1)
quiet = so.inject_prompt(alpha, "x", "beta", 0)
check("signal chờ trả lời dặn báo cáo về người gửi", 'send_signal(to_role="beta"' in asks)
check("signal một chiều dặn KHÔNG gửi gì về",
      'send_signal(to_role="beta"' not in quiet and 'KHÔNG send_signal về "beta"' in quiet)


async def _oneway_paths():
    import logging
    import httpx
    import signal_mcp
    logging.getLogger("httpx").setLevel(logging.WARNING)
    signal_mcp._INPROC = True
    try:
        await signal_mcp.send_signal(to_role="gamma", message="fyi", from_role="alpha",
                                     workspace_id=WS, expect_reply=False)
    finally:
        signal_mcp._INPROC = False
    mcp_row = max(r["id"] for r in so.list_signals(50, 0, WS)[0])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=so.build_app()),
                                 base_url="http://test") as c:
        r = await c.post("/api/signals", json={"to_role": "gamma", "message": "fyi", "from_role": "beta",
                                               "workspace_id": WS, "expects_reply": False})
    return mcp_row, r.json().get("id")


mcp_id, http_id = asyncio.run(_oneway_paths())
check("send_signal(expect_reply=False) lưu một chiều", reply_flag(mcp_id) == 0)
check("POST /api/signals expects_reply=false lưu một chiều", http_id and reply_flag(http_id) == 0)

os.remove(so._db_path())
print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all pair-cap checks passed")
