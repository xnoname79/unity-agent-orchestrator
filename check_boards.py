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
            ("function renderBoards(", "app.js", js),
            ("window.boardNew", "app.js", js),
            ("board_ping", "app.js", js),
            ('Route("/api/boards"', "session_orchestrator.py", py),
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
