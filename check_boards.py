#!/usr/bin/env python3
"""Guard: a board must nudge the user like a friend, not like an alarm clock.

Boards are the one feature that talks to the user when nobody asked it to, so the failure modes
are social, not technical — and none of them raise an exception:

  - two nudges inside a few minutes reads as spam, and people mute spam;
  - a nudge at 03:00 is worse than no nudge at all;
  - nudging on a board whose work is finished, or that the user paused, is noise with no excuse;
  - nudges that keep coming at the same pace after four unanswered ones are a notification
    system, not a friend. The pace has to decay and then recover when the user engages again;
  - a reminder that never arrives because the clock was recomputed on every poll (that is what
    killed the first draft: random jitter moved the target forward each tick).

board_next_ping() takes `now` instead of reading the clock, so every case above is a plain
assertion against a fake Thursday rather than a test that has to sleep.

    python3 check_boards.py
"""
import asyncio
import json
import logging
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["ORCH_DB"] = "check_boards"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()
os.environ["ORCH_DRY_RUN"] = "1"

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
db = so.DB_DIR / "check_boards.db"
db.unlink(missing_ok=True)
so._ensure_db()

MON = datetime(2026, 10, 12, 9, 0)    # một thứ Hai 09:00, xa giờ im lặng
fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


def board(**kw):
    """Một dòng boards tối thiểu, đủ cho board_next_ping — không cần DB."""
    return {"id": "brd_test", "status": "active", "created_at": MON.isoformat(),
            "last_ping_at": "", "strikes": 0, "quiet_from": 22, "quiet_to": 8, **kw}


def task(status="doing", est=60, prio="normal", started=MON, tid=1):
    return {"id": tid, "status": status, "estimate_min": est, "priority": prio,
            "title": f"task {tid}", "detail": "",
            "started_at": started.isoformat() if started else ""}


def mins(at, frm=MON):
    return (at - frm).total_seconds() / 60


# ── Nhịp nhắc ────────────────────────────────────────────────────────────────

def test_checkpoints():
    """Giữa việc, gần hết giờ, rồi đúng hạn — theo tỷ lệ ước lượng, không phải mốc cố định."""
    at = so.board_next_ping(board(), [task(est=60)], MON)
    check("a 60 min task gets its first check-in around halfway", 30 <= mins(at) <= 32,
          f"{mins(at):.1f} min in")
    at = so.board_next_ping(board(), [task(est=20)], MON)
    check("a 20 min task scales down, not the same clock", 10 <= mins(at) <= 12,
          f"{mins(at):.1f} min in")
    # Đã qua mốc giữa (now = +40) → mốc kế tiếp là 0.85 × 60 = 51 phút.
    at = so.board_next_ping(board(), [task(est=60)], MON + timedelta(minutes=40))
    check("past halfway it moves to the wrap-up checkpoint", 51 <= mins(at) <= 53,
          f"{mins(at):.1f} min in")


def test_priority():
    hi = so.board_next_ping(board(), [task(est=60, prio="high")], MON)
    lo = so.board_next_ping(board(), [task(est=60, prio="low")], MON)
    check("a high priority task is checked on sooner", mins(hi) < mins(lo),
          f"high {mins(hi):.0f} vs low {mins(lo):.0f}")
    check("a low priority task is left alone until its deadline", 60 <= mins(lo) <= 62,
          f"{mins(lo):.1f} min in")


def test_overdue_backs_off():
    """Quá hạn thì giãn dần. Mốc 1.25/1.5/2/3× ước lượng, không phải mỗi vòng quét một lần."""
    t = task(est=60)
    seen = []
    now = MON + timedelta(minutes=61)
    for _ in range(3):
        at = so.board_next_ping(board(), [t], now)
        seen.append(round(mins(at)))
        now = at + timedelta(seconds=1)
    steps = [b - a for a, b in zip(seen, seen[1:])]
    check("overdue nudges spread out instead of repeating",
          seen == sorted(seen) and steps == sorted(steps) and seen[-1] > 2 * 60,
          f"minutes in: {seen}")


def test_min_gap():
    """Hai lời nhắc sát nhau là thứ biến bạn thành app spam — sàn phải cắn ngay cả khi mốc
    checkpoint vừa nhảy về quá khứ."""
    b = board(last_ping_at=(MON - timedelta(minutes=2)).isoformat())
    at = so.board_next_ping(b, [task(est=60, started=MON - timedelta(minutes=59))], MON)
    check("never two nudges inside the minimum gap",
          mins(at, MON) >= so.BOARD_MIN_GAP - 2 - 0.01, f"{mins(at, MON):.1f} min from now")


def test_backoff_and_recovery():
    """Bị bỏ qua thì lùi dần; user chạm vào board (strikes=0) thì nhịp trở lại bình thường."""
    gaps = []
    for s in (0, 1, 2, 5):
        b = board(strikes=s, last_ping_at=MON.isoformat())
        # Việc đã quá hạn từ lâu: mọi mốc checkpoint nằm ở quá khứ nên SÀN là thứ quyết định
        # — đúng chỗ backoff phải cắn. Việc còn hạn thì checkpoint luôn xa hơn sàn, backoff
        # không nhìn thấy được (và cũng không cần: nhịp đã do ước lượng quyết định).
        at = so.board_next_ping(b, [task(est=10, started=MON - timedelta(minutes=120))], MON)
        gaps.append(round(mins(at)))
    check("each ignored nudge pushes the next one further out", gaps[0] < gaps[1] < gaps[2],
          f"gaps {gaps}")
    check("the back-off is capped, it does not go silent for the day",
          gaps[3] <= so.BOARD_MIN_GAP * so.BOARD_BACKOFF_CAP + 3, f"gaps {gaps}")
    check("engaging with the board brings the pace back",
          gaps[0] < gaps[3] and gaps[0] <= so.BOARD_MIN_GAP + 3, f"gaps {gaps}")


def test_quiet_hours():
    night = datetime(2026, 10, 12, 23, 30)
    at = so.board_next_ping(board(last_ping_at=""), [task(est=60, started=night)], night)
    check("a nudge due at night waits for morning", at.hour == 8 and at.day == 13,
          at.isoformat())
    at = so.board_next_ping(board(quiet_from=0, quiet_to=0),
                            [task(est=60, started=night)], night)
    check("quiet hours can be switched off", at.day == 13 and at.hour == 0, at.isoformat())


def test_silence_when_there_is_nothing_to_say():
    check("a finished board says nothing",
          so.board_next_ping(board(), [task(status="done")], MON) is None)
    check("a paused board says nothing",
          so.board_next_ping(board(status="paused"), [task()], MON) is None)
    check("an empty board says nothing", so.board_next_ping(board(), [], MON) is None)


def test_idle_nudge():
    """Chưa kéo việc nào sang doing → nhắc BẮT ĐẦU, và mốc đếm từ board/lần nhắc trước chứ không
    từ `now`: đếm từ now thì mỗi vòng quét lại đẩy mốc đi thêm, lời nhắc không bao giờ tới."""
    b = board()
    first = so.board_next_ping(b, [task(status="todo", started=None)], MON)
    later = so.board_next_ping(b, [task(status="todo", started=None)], MON + timedelta(minutes=30))
    check("an untouched board nudges you to start", so.BOARD_IDLE_GAP <= mins(first) <= so.BOARD_IDLE_GAP + 3,
          f"{mins(first):.1f} min in")
    check("that deadline does not run away as the clock moves", first == later,
          f"{first} vs {later}")


def test_jitter_is_stable_but_not_round():
    """Nhịp tròn trịa đọc ra là máy; nhưng mốc phải ổn định giữa các lần tính lại."""
    a = so.board_next_ping(board(), [task(est=60)], MON)
    b = so.board_next_ping(board(), [task(est=60)], MON + timedelta(seconds=30))
    check("the same board recomputes to the same minute", a == b, f"{a} vs {b}")
    spread = {so.board_next_ping(board(id=f"brd_{i}"), [task(est=60)], MON).minute
              for i in range(8)}
    check("two boards do not nudge on the same tick", len(spread) > 1, f"minutes {spread}")


# ── Đọc câu trả lời của agent ────────────────────────────────────────────────

def test_json_from_agent_text():
    body = {"nudge": "hi", "research": {"needed": True}}
    check("plain json", so._json_obj(json.dumps(body)) == body)
    check("fenced json", so._json_obj("Sure!\n```json\n" + json.dumps(body) + "\n```\n") == body)
    check("json with chatter after it",
          so._json_obj(json.dumps(body) + "\n\nLet me know if you want changes.") == body)
    check("a brace inside a string does not break it",
          so._json_obj('talk {not json} then {"nudge": "use } carefully"}'
                       )["nudge"] == "use } carefully")
    check("no json at all is empty, not an exception", so._json_obj("I could not do that") == {})
    check("None is empty too", so._json_obj(None) == {})


def test_research_is_not_every_time():
    """Research chỉ chạy khi agent tự xin, và không sớm hơn recheck_in_min — nếu không thì một
    board mở cả ngày là vài chục lượt tra cho cùng một thông tin."""
    bid = so.create_board("default", "sess-x", "b")
    so._board_apply(bid, {"research": {"needed": False}}, MON)
    check("no research asked, none scheduled", not so.get_board(bid)["research_due"])
    so._board_apply(bid, {"research": {"needed": True, "why": "prices move", "recheck_in_min": 120}},
                    MON)
    b = so.get_board(bid)
    due = so._iso(b["research_due"])
    check("research is scheduled, not immediate", due and 119 <= mins(due) <= 121,
          b["research_due"])
    check("and it carries the agent's reason", b["research_why"] == "prices move", b["research_why"])
    so.delete_board(bid)


def test_apply_retimes_and_adds():
    bid = so.create_board("default", "sess-x", "b")
    tid = so.add_task(bid, "write the draft", "", 30)
    so._board_apply(bid, {"retime": [{"id": tid, "estimate_min": 90}],
                          "new_tasks": [{"title": "proofread", "estimate_min": 15}]}, MON)
    rows = {t["title"]: t for t in so.board_tasks(bid)}
    check("the agent can retime a task it underestimated",
          rows["write the draft"]["estimate_min"] == 90, str(rows["write the draft"]))
    check("and add one the user did not think of", "proofread" in rows, list(rows))
    so.delete_board(bid)


def test_user_work_is_never_deleted_under_them():
    """board_plan chỉ thay task còn 'todo'. Việc đang làm / đã xong là của người dùng."""
    src = Path(__file__).with_name("session_orchestrator.py").read_text(encoding="utf-8")
    body = src.split("async def board_plan(")[1].split("\nasync def ")[0]
    check("re-planning only clears todo tasks",
          'if t["status"] == "todo":' in body and "delete_task" in body,
          "board_plan deletes without checking status")


def test_started_at_is_set_when_work_begins():
    """Toàn bộ nhịp nhắc đo từ started_at. Thiếu nó thì mỗi vòng quét lại coi như việc vừa bắt
    đầu và lời nhắc không bao giờ tới — nên nó phải được đóng ở store, không ở route."""
    bid = so.create_board("default", "sess-x", "b")
    tid = so.add_task(bid, "t", "", 30)
    so.update_task(tid, status="doing")
    first = so.board_tasks(bid)[0]["started_at"]
    check("moving a task into doing stamps the clock", bool(first), "started_at stayed empty")
    so.update_task(tid, status="todo")
    so.update_task(tid, status="doing")
    check("moving it back and forth keeps the original start",
          so.board_tasks(bid)[0]["started_at"] == first, "the clock restarted")
    so.delete_board(bid)


def test_board_signals_skip_the_ping_pong_cap():
    """Lời nhắc của board là việc theo giờ, không phải hai agent nhắn qua lại: nó không được
    tính vào trần id=94 (nếu không, board im sau 2 lần nhắc) và cũng không được RESET trần đó
    (nếu không, mỗi tick lại cấp ngân sách cho một vòng lặp agent không ai trông)."""
    so.register_session("sess-cap", "capbot", workspace_id="default")
    for i in range(so.PAIR_SIGNAL_CAP + 2):
        try:
            so.enqueue_signal("sess-cap", f"nudge {i}", so.BOARD_SENDER, 0, 0, "default")
        except so.SignalPairCapExceeded as e:
            check("board nudges are never capped", False, str(e)[:60])
            break
    else:
        check("board nudges are never capped", True)
    check("a board nudge does not reset the agent ping-pong budget",
          so._round_at.get(("default", "capbot")) is None, str(so._round_at))


def test_board_never_writes_the_chat_transcript():
    """Lời nhắc KHÔNG được chạy trên session người dùng đang chat.

    Khoá _lock_for chỉ xếp hàng các run headless với nhau — terminal nhúng là một tiến trình
    claude KHÁC mở đúng transcript ấy và không đi qua khoá nào, nên dùng chung session là hai
    tiến trình cùng nối vào một hội thoại. Board phải có thread riêng, cùng thư mục.
    """
    so.register_session("sess-owner", "writer", cwd="/tmp/proj", model="opus",
                        permission_mode="bypassPermissions", workspace_id="default")
    owner = so.get_session("sess-owner")
    bid = so.create_board("default", so.board_agent_for(owner, "b"), "b", owner_id="sess-owner")
    b = so.get_board(bid)
    check("the board runs on its own session, not the one you chat with",
          b["session_id"] != "sess-owner", b["session_id"])
    thread = so.get_session(b["session_id"])
    check("that session sits in the same folder", thread["cwd"] == "/tmp/proj", thread["cwd"])
    check("and carries the same model and permissions",
          thread["model"] == "opus" and thread["permission_mode"] == "bypassPermissions",
          f"{thread['model']} {thread['permission_mode']}")
    check("the board still shows the agent you picked",
          so.board_view(b)["agent"] == "writer", so.board_view(b)["agent"])
    so.delete_board(bid)
    check("deleting the board takes its thread with it",
          so.get_session(b["session_id"]) is None, "thread left behind")
    check("but never the agent you chat with", so.get_session("sess-owner") is not None,
          "the chat agent was deleted")


def test_old_board_upgrades_itself():
    """Board tạo trước khi thread được tách ra (owner_id rỗng) phải tự chuyển sang thread riêng,
    chứ không im lặng ghi tiếp vào transcript của người dùng."""
    so.register_session("sess-old", "oldbot", cwd="/tmp/proj2", workspace_id="default")
    bid = so.create_board("default", "sess-old", "legacy board")
    before = so.get_board(bid)
    check("an old board starts out sharing the chat session",
          before["session_id"] == "sess-old" and not before["owner_id"], str(dict(before))[:80])
    so.board_thread(before)
    after = so.get_board(bid)
    check("the next run moves it onto its own thread",
          after["session_id"] != "sess-old" and after["owner_id"] == "sess-old",
          f"{after['session_id']} / {after['owner_id']}")
    so.delete_board(bid)


async def test_board_reads_what_you_said_in_chat():
    """Board phải biết người dùng đã làm gì với agent của họ — đọc transcript, không ghi.

    Chỉ lời người dùng và lời agent; output công cụ, chữ hệ thống, subagent thì không. Mốc
    chat_seen chỉ tiến khi agent của board thật sự trả lời, nếu không đoạn chat đó mất luôn.
    """
    keep = so.CLAUDE_PROJECTS_DIR, so.CODEX_SESSIONS_DIR, so.AGY_CONVERSATIONS_DIR
    so.CLAUDE_PROJECTS_DIR = Path(tempfile.mkdtemp())
    so.CODEX_SESSIONS_DIR = Path(tempfile.mkdtemp())
    so.AGY_CONVERSATIONS_DIR = Path(tempfile.mkdtemp())
    try:
        so.register_session("sess-chat", "chatbot", cwd="/tmp/p3", workspace_id="default")
        tr = so.CLAUDE_PROJECTS_DIR / "-tmp-p3" / "sess-chat.jsonl"
        tr.parent.mkdir()

        def say(*rows):
            with tr.open("a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")

        def msg(kind, ts, content, **kw):
            return {"type": kind, "timestamp": ts, "message": {"content": content}, **kw}

        say(msg("user", "2026-10-12T02:00:00.000Z", "login page is done, moving to billing"),
            msg("assistant", "2026-10-12T02:00:05.000Z", [{"type": "text", "text": "Nice. Billing next."}]),
            msg("assistant", "2026-10-12T02:00:06.000Z", [{"type": "tool_use", "name": "Bash"}]),
            msg("user", "2026-10-12T02:00:07.000Z", "SECRET_TOOL_OUTPUT", toolUseResult={"x": 1}),
            msg("user", "2026-10-12T02:00:08.000Z", "injected caveat", isMeta=True),
            msg("user", "2026-10-12T02:00:09.000Z", "<command-name>/model</command-name>"),
            msg("assistant", "2026-10-12T02:00:10.000Z", [{"type": "text", "text": "subagent"}],
                isSidechain=True))
        bid = so.create_board("default", so.board_agent_for(so.get_session("sess-chat"), "c"),
                              "c", owner_id="sess-chat")
        chat, seen = so.board_chat_since(so.get_board(bid))
        check("the board sees what you told your agent",
              "user: login page is done" in chat and "agent: Nice. Billing next." in chat, chat)
        check("but not tool output, system text or subagents",
              not any(x in chat for x in ("SECRET_TOOL_OUTPUT", "caveat", "command-name",
                                          "subagent")), chat)
        check("and names the agent you chat with", "chatbot" in chat, chat[:80])
        check("the cursor lands on the last real turn", seen == "2026-10-12T02:00:05.000Z", seen)

        await so.board_tick(bid)                 # dry-run: agent không trả lời
        check("a nudge the agent never answered does not swallow the chat",
              so.get_board(bid)["chat_seen"] == "", so.get_board(bid)["chat_seen"])

        so.update_board(bid, chat_seen=seen)
        check("nothing new, nothing sent", so.board_chat_since(so.get_board(bid))[0] == "",
              "chat repeated")
        say(msg("user", "2026-10-12T03:00:00.000Z", "billing blocked on the stripe key"))
        chat, _ = so.board_chat_since(so.get_board(bid))
        check("only what came after", "stripe key" in chat and "login page" not in chat, chat)
        so.delete_board(bid)

        so.register_session("sess-cx", "cxbot", workspace_id="default", model="codex")
        roll = so.CODEX_SESSIONS_DIR / "2026/10/12" / "rollout-2026-10-12T09-00-00-sess-cx.jsonl"
        roll.parent.mkdir(parents=True)
        roll.write_text("\n".join(json.dumps(r) for r in (
            {"type": "event_msg", "timestamp": "2026-10-12T02:00:00.000Z",
             "payload": {"type": "user_message", "message": "draft the pricing email"}},
            {"type": "response_item", "timestamp": "2026-10-12T02:00:00.000Z",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "draft the pricing email"}]}},
            {"type": "event_msg", "timestamp": "2026-10-12T02:00:09.000Z",
             "payload": {"type": "agent_message", "message": "Drafted, 120 words."}})) + "\n",
            encoding="utf-8")
        cid = so.create_board("default", so.board_agent_for(so.get_session("sess-cx"), "x"),
                              "x", owner_id="sess-cx")
        chat, _ = so.board_chat_since(so.get_board(cid))
        check("codex chats are read too, each turn once",
              chat.count("pricing email") == 1 and "agent: Drafted" in chat, chat)
        so.delete_board(cid)

        # agy: sqlite, mỗi bước một blob protobuf. Tự mã hoá đúng các field hàm đọc tìm tới.
        def var(n):
            out = b""
            while n > 0x7f:
                out, n = out + bytes([n & 0x7f | 0x80]), n >> 7
            return out + bytes([n])

        def pb(*fields):
            out = b""
            for f, v in fields:
                if isinstance(v, int):
                    out += var(f << 3) + var(v)
                else:
                    v = v.encode() if isinstance(v, str) else v
                    out += var(f << 3 | 2) + var(len(v)) + v
            return out

        def at(sec):
            return (5, pb((1, pb((1, sec), (2, 250_000_000)))))

        t0 = 1791770400                         # 2026-10-12T02:00:00Z
        so.register_session("sess-agy", "gembot", workspace_id="default", model="agy")
        conn = sqlite3.connect(so.AGY_CONVERSATIONS_DIR / "sess-agy.db")
        conn.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, step_payload BLOB)")
        conn.executemany("INSERT INTO steps VALUES (?, ?, ?)", [
            (0, 14, pb((1, 14), at(t0), (19, pb((2, "outline is done, starting slides"))))),
            (1, 15, pb((1, 15), at(t0 + 5), (20, pb((3, "SECRET_THINKING"))))),
            (2, 132, pb((1, 132), at(t0 + 6), (22, "TOOL_OUTPUT"))),
            (3, 15, pb((1, 15), at(t0 + 9), (20, pb((1, "Good. Slides next."),
                                                  (3, "SECRET_THINKING"))))),
            (4, 14, b"\xff\xff\xff")])        # blob hỏng: bỏ qua, không làm hỏng cả lượt
        conn.commit()
        conn.close()
        gid = so.create_board("default", so.board_agent_for(so.get_session("sess-agy"), "g"),
                              "g", owner_id="sess-agy")
        chat, seen = so.board_chat_since(so.get_board(gid))
        check("antigravity chats are read too",
              "user: outline is done" in chat and "agent: Good. Slides next." in chat, chat)
        check("but never the model's private thinking or tool output",
              "SECRET_THINKING" not in chat and "TOOL_OUTPUT" not in chat, chat)
        check("antigravity times line up with the other CLIs",
              seen == "2026-10-12T02:00:09.250Z", seen)
        so.delete_board(gid)
    finally:
        so.CLAUDE_PROJECTS_DIR, so.CODEX_SESSIONS_DIR, so.AGY_CONVERSATIONS_DIR = keep


# ── Worker: tự chọn task, tìm hiểu, ghi kết quả — chỉ đọc ─────────────────────

def test_worker_scheduling():
    """Khi nào chạy worker. Chạy thừa là đốt quota hỏi đi hỏi lại một danh sách không đổi."""
    so._worker_skip.clear()
    open_ = [task("todo", tid=1) | {"worker_state": ""}, task("done", tid=2) | {"worker_state": ""}]
    on = board(id="b1", worker=1, planning=0)
    check("a board with the worker off never starts one",
          not so.worker_should_start(board(id="b1", worker=0, planning=0), open_, set(), "global"), "")
    check("a board with the worker on and an open task starts one",
          so.worker_should_start(on, open_, set(), "global"), "")
    check("a paused or planning board does not",
          not so.worker_should_start(on | {"status": "paused"}, open_, set(), "global")
          and not so.worker_should_start(on | {"planning": 1}, open_, set(), "global"), "")
    check("one at a time across boards: another board's worker blocks it",
          not so.worker_should_start(on, open_, {"b2"}, "global"), "")
    check("one per board: another board's worker does not",
          so.worker_should_start(on, open_, {"b2"}, "board"), "")
    check("never two workers on one board",
          not so.worker_should_start(on, open_, {"b1"}, "board"), "")
    done = [t | {"worker_state": "researched"} if t["id"] == 1 else t for t in open_]
    check("nothing left to research → no worker", not so.worker_should_start(on, done, set(), "board"), "")
    so._worker_skip["b1"] = so._worker_key(so._worker_open(open_))
    check("after 'nothing needs research', the same list is not asked about again",
          not so.worker_should_start(on, open_, set(), "board"), "")
    more = open_ + [task("todo", tid=3) | {"worker_state": ""}]
    check("a new task gets it looked at again", so.worker_should_start(on, more, set(), "board"), "")
    so._worker_skip.clear()


def test_worker_is_read_only():
    """Người dùng chọn: worker CHỈ ĐỌC, để agent chính và worker không bao giờ cùng sửa một file.
    Không được thừa hưởng quyền bypass của agent gốc."""
    so.register_session("sess-wo", "coder", cwd="/tmp/projw", model="opus",
                        permission_mode="bypassPermissions", allowed_tools=["Bash", "Edit"],
                        workspace_id="default")
    bid = so.create_board("default", so.board_agent_for(so.get_session("sess-wo"), "w"), "w",
                          owner_id="sess-wo")
    wid = so.board_worker(so.get_board(bid))
    w = so.get_session(wid)
    check("the worker is its own session, in the same folder, with the same model",
          wid not in ("sess-wo", so.get_board(bid)["session_id"]) and w["cwd"] == "/tmp/projw"
          and w["model"] == "opus" and w["name"] == "coder-worker", str(w)[:160])
    check("it runs read-only whatever the agent it copies is allowed",
          w["permission_mode"] == "plan"
          and json.loads(w["allowed_tools"]) == list(so.WORKER_TOOLS), str(w)[:200])
    check("asking again reuses it", so.board_worker(so.get_board(bid)) == wid, "made a second one")
    check("it is hidden like the board's own session",
          so.board_threads().get(wid, {}).get("role") == "worker", str(so.board_threads()))
    flags = so._codex_flags(permission_mode="plan")
    check("on codex, read-only is the read-only sandbox",
          'sandbox_mode="read-only"' in flags and "--dangerously-bypass-approvals-and-sandbox"
          not in flags and 'sandbox_mode="workspace-write"' not in flags, str(flags))
    so.delete_board(bid)
    check("deleting the board takes the worker, never the agent you chat with",
          so.get_session(wid) is None and so.get_session("sess-wo") is not None, "")


async def test_worker_researches_one_task():
    so.register_session("sess-wr", "researcher-owner", cwd="/tmp/projr", workspace_id="default")
    bid = so.create_board("default", so.board_agent_for(so.get_session("sess-wr"), "r"), "r",
                          owner_id="sess-wr")
    so.update_board(bid, worker=1, goal="ship the auth rewrite")
    ta = so.add_task(bid, "write the migration")
    tb = so.add_task(bid, "pick a token library", detail="jwt or paseto?")
    tc = so.add_task(bid, "already done")
    so.update_task(tc, status="done")
    td = so.add_task(bid, "looked at before")
    so.update_task(td, worker_state="researched", findings="old notes")
    seen, real_ask = [], so.board_ask

    async def fake_ask(b, prompt, target=None):
        state = {t["id"]: t["worker_state"] for t in so.board_tasks(bid)}
        seen.append((prompt, target, state))
        return ('{"task_id": %d, "why": "the choice blocks everything"}' % tb if len(seen) == 1
                else "## Findings\n- paseto: no alg confusion\n- next: add pyseto")
    so.board_ask = fake_ask
    try:
        so._worker_busy.add(bid)
        await so.board_work(bid)
    finally:
        so.board_ask = real_ask
    pick_prompt, target, _ = seen[0]
    check("the worker is offered only tasks nobody has finished or researched",
          "write the migration" in pick_prompt and "pick a token library" in pick_prompt
          and "already done" not in pick_prompt and "looked at before" not in pick_prompt,
          pick_prompt[-400:])
    check("it runs on the worker's own session", target == so.get_board(bid)["worker_id"],
          str(target))
    check("the task it picked shows 'researching' while it works",
          seen[1][2][tb] == "researching" and seen[1][2][ta] == "", str(seen[1][2]))
    check("the research prompt is about that task only, and forbids changing files",
          "pick a token library" in seen[1][0] and "jwt or paseto?" in seen[1][0]
          and "Do not" in seen[1][0] and "write the migration" not in seen[1][0], seen[1][0])
    t = {x["id"]: x for x in so.board_tasks(bid)}
    check("the findings land on that task card",
          t[tb]["worker_state"] == "researched" and "paseto" in t[tb]["findings"]
          and t[tb]["status"] == "todo", str(t[tb]))
    check("no other task is touched", t[ta]["worker_state"] == "" and t[td]["findings"] == "old notes",
          str(t[ta]))
    check("the worker frees its slot", bid not in so._worker_busy, str(so._worker_busy))

    async def ask_none(b, prompt, target=None):
        return '{"task_id": null, "why": "nothing needs research"}'

    async def ask_bad_id(b, prompt, target=None):
        return '{"task_id": %d}' % tc   # task đã xong: không được chọn

    for fake, label in ((ask_none, "says none"), (ask_bad_id, "names a task it was not offered")):
        so._worker_skip.clear()
        before = {x["id"]: x["worker_state"] for x in so.board_tasks(bid)}
        so.board_ask = fake
        try:
            await so.board_work(bid)
        finally:
            so.board_ask = real_ask
        after = {x["id"]: x["worker_state"] for x in so.board_tasks(bid)}
        check(f"when the worker {label}, nothing is marked, and the list is not asked about again",
              after == before and so._worker_skip.get(bid) == str(ta),
              f"{before} → {after}, skip={so._worker_skip}")
    so._worker_skip.clear()

    calls = []

    async def ask_empty(b, prompt, target=None):
        calls.append(1)
        if len(calls) == 1:
            return '{"task_id": %d}' % ta
        return ""
    so.board_ask = ask_empty
    try:
        await so.board_work(bid)
    finally:
        so.board_ask = real_ask
    t = {x["id"]: x for x in so.board_tasks(bid)}
    check("an empty answer is a failure the card says, not silent 'researched'",
          t[ta]["worker_state"] == "failed" and t[ta]["findings"], str(t[ta]))

    so.update_task(ta, worker_state="")
    calls.clear()

    async def ask_crash(b, prompt, target=None):
        calls.append(1)
        if len(calls) == 1:
            return '{"task_id": %d}' % ta
        raise RuntimeError("daily run cap reached")
    so.board_ask = ask_crash
    try:
        await so.board_work(bid)
    finally:
        so.board_ask = real_ask
    t = {x["id"]: x for x in so.board_tasks(bid)}
    check("a crash mid-research marks the task failed with the reason",
          t[ta]["worker_state"] == "failed" and "daily run cap" in t[ta]["findings"]
          and bid not in so._worker_busy, str(t[ta]))

    so.update_task(ta, worker_state="researching")
    so.worker_reset()
    check("a restart frees a task left 'researching'",
          {x["id"]: x for x in so.board_tasks(bid)}[ta]["worker_state"] == "", "still stuck")
    so.delete_board(bid)


async def test_board_mcp():
    """Agent đọc/sửa board qua MCP: chỉ trong workspace của nó, không xoá gì."""
    import board_mcp
    so.register_session("sess-m1", "planner", workspace_id="default")
    so.register_session("sess-m2", "stranger", workspace_id="ws-other")
    mine = so.create_board("default", so.board_agent_for(so.get_session("sess-m1"), "mine"), "mine",
                           owner_id="sess-m1")
    theirs = so.create_board("ws-other", so.board_agent_for(so.get_session("sess-m2"), "theirs"),
                             "theirs", owner_id="sess-m2")
    so.update_board(mine, strikes=3)
    out = await board_mcp.list_boards(from_role="planner")
    names = {x["name"] for x in json.loads(out)}
    check("list_boards shows your workspace's boards only", "mine" in names and "theirs" not in names,
          out[:200])
    for bad in ("", "ghost"):
        out = await board_mcp.list_boards(from_role=bad)
        check(f"from_role {bad!r} is refused", out.startswith("⛔"), out[:120])
    out = await board_mcp.get_board(theirs, from_role="planner")
    check("another workspace's board cannot be read", out.startswith("⛔"), out[:120])
    out = json.loads(await board_mcp.add_task(mine, "check the logs", from_role="planner",
                                              priority="high", estimate_min=20))
    tid = next(t["id"] for t in out["tasks"] if t["title"] == "check the logs")
    check("add_task puts the task on the board", out["total"] == 1, str(out)[:160])
    check("an agent touching the board counts as work, like a person dragging a card",
          so.get_board(mine)["strikes"] == 0, str(so.get_board(mine)["strikes"]))
    out = json.loads(await board_mcp.update_task(mine, tid, from_role="planner", status="doing"))
    t = next(x for x in out["tasks"] if x["id"] == tid)
    check("update_task moves it, and the reminder clock starts", t["status"] == "doing"
          and t["started_at"], str(t))
    for kw, label in (({"status": "sideways"}, "a bogus status"), ({"priority": "urgent"},
                      "a bogus priority"), ({}, "an empty change")):
        out = await board_mcp.update_task(mine, tid, from_role="planner", **kw)
        check(f"{label} is refused", out.startswith("⛔"), out[:120])
    other_task = so.add_task(theirs, "secret")
    out = await board_mcp.update_task(mine, other_task, from_role="planner", title="mine now")
    check("a task from another board cannot be edited through yours", out.startswith("⛔")
          and so.board_tasks(theirs)[0]["title"] == "secret", out[:120])
    out = await board_mcp.add_task(mine, "  ", from_role="planner")
    check("a task needs a title", out.startswith("⛔"), out[:120])
    so.update_task(tid, worker_state="researched", findings="use the audit log")
    out = json.loads(await board_mcp.get_board(mine, from_role="planner"))
    check("get_board carries the worker's findings", out["tasks"][0]["findings"] == "use the audit log",
          str(out)[:200])
    tools = {t.name for t in await board_mcp.mcp.list_tools()}
    check("the board tools read, add and update — nothing deletes",
          tools == {"list_boards", "get_board", "add_task", "update_task"}, str(tools))
    so.delete_board(mine)
    so.delete_board(theirs)


def test_wiring():
    """Nút và tab phải có thật — backend đúng mà UI không gọi tới thì tính năng không tồn tại."""
    here = Path(__file__).parent
    html = (here / "static/orchestrator/index.html").read_text(encoding="utf-8")
    js = (here / "static/orchestrator/app.js").read_text(encoding="utf-8")
    py = (here / "session_orchestrator.py").read_text(encoding="utf-8")
    for want, where, blob in (
            ('id="tab-btn-boards"', "index.html", html),
            ("switchTab('boards')", "index.html", html),
            ('id="boards-view"', "index.html", html),
            ('id="task-modal"', "index.html", html),
            ("window.taskOpen", "app.js", js),
            ("function renderTask(", "app.js", js),
            ("def board_agent_for(", "session_orchestrator.py", py),
            ("function renderBoards(", "app.js", js),
            ('id="bd-agents"', "index.html", html),
            ("function renderBoardAgents(", "app.js", js),
            ("renderCanvas(work,", "app.js", js),
            ("window.boardNew", "app.js", js),
            ("board_ping", "app.js", js),
            ('Route("/api/boards"', "session_orchestrator.py", py),
            ('Route("/api/board-worker"', "session_orchestrator.py", py),
            ('Mount("/boards", app=board_app)', "session_orchestrator.py", py),
            ('id="bd-scope"', "index.html", html),
            ("window.boardWorker", "app.js", js),
            ("window.taskResearchAgain", "app.js", js),
            ('url: location.origin + "/boards/mcp"', "app.js", js),
            ("asyncio.create_task(board_loop())", "session_orchestrator.py", py)):
        check(f"{where} wires up {want}", want in blob, "not found")


async def main():
    test_checkpoints()
    test_priority()
    test_overdue_backs_off()
    test_min_gap()
    test_backoff_and_recovery()
    test_quiet_hours()
    test_silence_when_there_is_nothing_to_say()
    test_idle_nudge()
    test_jitter_is_stable_but_not_round()
    test_json_from_agent_text()
    test_research_is_not_every_time()
    test_apply_retimes_and_adds()
    test_user_work_is_never_deleted_under_them()
    test_started_at_is_set_when_work_begins()
    test_board_signals_skip_the_ping_pong_cap()
    test_board_never_writes_the_chat_transcript()
    test_old_board_upgrades_itself()
    test_worker_scheduling()
    test_worker_is_read_only()
    await test_worker_researches_one_task()
    await test_board_mcp()
    await test_board_reads_what_you_said_in_chat()
    test_wiring()

    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8992") as c:
        so.register_session("sess-api", "boardbot", workspace_id="default")
        r = await c.post("/api/boards", json={"name": "Launch day", "session_id": "boardbot",
                                              "workspace_id": "default"})
        b = r.json()
        check("POST /api/boards creates a board for an agent by role name",
              r.status_code == 201 and b["agent"] == "boardbot", f"{r.status_code} {r.text[:90]}")
        bid = b["id"]
        r = await c.post(f"/api/boards/{bid}/tasks", json={"title": "ship it", "estimate_min": 45})
        check("POST a task", r.status_code == 201 and r.json()["total"] == 1,
              f"{r.status_code} {r.text[:90]}")
        tid = r.json()["tasks"][0]["id"]
        r = await c.post(f"/api/boards/{bid}/tasks/{tid}", json={"status": "doing"})
        v = r.json()
        check("moving a task returns the next nudge time", bool(v["next_ping"]), r.text[:120])
        r = await c.post(f"/api/boards/{bid}/tasks/{tid}", json={"status": "sideways"})
        check("a bogus status is refused, not stored", r.status_code == 400, str(r.status_code))
        other = so.create_board("default", "sess-api", "other board")
        r = await c.post(f"/api/boards/{other}/tasks/{tid}", json={"title": "stolen"})
        check("a task cannot be edited through another board's url", r.status_code == 404,
              str(r.status_code))
        r = await c.get("/api/boards?workspace_id=default")
        check("GET /api/boards lists them with their tasks",
              r.status_code == 200 and any(x["id"] == bid and x["total"] == 1 for x in r.json()),
              f"{r.status_code} {r.text[:90]}")
        r = await c.get("/api/sessions?workspace_id=default")
        rows = {x["name"]: x for x in r.json()}
        thread = rows.get("boardbot-board") or {}
        check("the board's own session is flagged, so the canvas leaves it out",
              (thread.get("board") or {}).get("id") == bid, str(thread.get("board")))
        check("the agent you chat with is not", rows["boardbot"]["board"] is None,
              str(rows["boardbot"]["board"]))
        import signal_mcp
        signal_mcp._INPROC = True
        try:
            peers = json.loads(await signal_mcp.list_agents(from_role="boardbot",
                                                            workspace_id="default"))
        finally:
            signal_mcp._INPROC = False
        check("other agents cannot see a board's session to message it",
              "boardbot-board" not in {a["role"] for a in peers}
              and "boardbot" in {a["role"] for a in peers}, str(peers))
        r = await c.post(f"/api/boards/{bid}", json={"worker": True})
        check("the worker is switched on per board", r.status_code == 200 and r.json()["worker"] == 1,
              r.text[:120])
        so.board_worker(so.get_board(bid))
        r = await c.get("/api/sessions?workspace_id=default")
        rows = {x["name"]: x for x in r.json()}
        check("the worker's session is flagged too, as a worker",
              ((rows.get("boardbot-worker") or {}).get("board") or {}).get("role") == "worker",
              str(rows.get("boardbot-worker"))[:160])
        so.update_task(tid, worker_state="researched", findings="notes")
        r = await c.post(f"/api/boards/{bid}/tasks/{tid}", json={"worker_state": "researching"})
        check("a person cannot fake the worker's state", r.status_code == 400, str(r.status_code))
        r = await c.post(f"/api/boards/{bid}/tasks/{tid}", json={"worker_state": ""})
        t = next(x for x in r.json()["tasks"] if x["id"] == tid)
        check("'Research again' clears the findings so the worker can pick it again",
              t["worker_state"] == "" and t["findings"] == "", str(t))
        r = await c.get("/api/board-worker")
        check("workers run one at a time across boards unless you say otherwise",
              r.json()["scope"] == "global", r.text)
        r = await c.post("/api/board-worker", json={"scope": "board"})
        check("you can switch to one per board", r.json()["scope"] == "board"
              and so.worker_scope() == "board", r.text)
        r = await c.post("/api/board-worker", json={"scope": "everywhere"})
        check("a bogus scope is refused", r.status_code == 400 and so.worker_scope() == "board",
              r.text)
        r = await c.delete(f"/api/boards/{bid}")
        check("DELETE takes the tasks with it",
              r.status_code == 200 and not so.board_tasks(bid), r.text[:90])

    db.unlink(missing_ok=True)
    print()
    if fails:
        print(f"{len(fails)} FAILED")
        sys.exit(1)
    print("all good")


asyncio.run(main())
