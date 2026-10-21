#!/usr/bin/env python3
"""Guards for Explain — a small local model (Ollama) explains text selected in the PDF reader.

The quiet ways it goes wrong:

  - small models get the kind of text wrong (they call a proverb a "pronoun"), so the code decides
    word / phrase / sentence, and asks for a part of speech only for single words;
  - they copy the reader's own sentence back as an "example", or repeat one example three times —
    those are dropped, so the dialog never shows the reader their own text as something new;
  - Ollama's JSON schema only forces the SHAPE of the answer: a wrong type, a part of speech
    outside the list, a huge string or broken JSON must not reach the page;
  - Ollama not running, or the model not pulled, must say how to fix it (409), not a bare 500;
  - another website must not be able to make the model run (CSRF).

Ollama is faked, so this runs offline.

    python3 check_explain.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["ORCH_DB"] = "check_explain"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
fails = []
BANK = "She sat on the bank of the river and watched the boats."


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("ok   " if ok else "FAIL ") + name + (f" — {detail}" if not ok else ""))


class FakeOllama:
    """Trả lời /api/chat bằng reply (dict → JSON trong message.content, str → nguyên văn), nhớ
    request cuối để kiểm thứ gửi đi."""

    def __init__(self):
        self.status, self.reply, self.sent = 200, {}, None

    def __call__(self, request):
        self.sent = json.loads(request.content)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "model 'x' not found"})
        content = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})


async def main():
    fake = FakeOllama()
    real_client = httpx.AsyncClient

    class Client(real_client):
        def __init__(self, **kw):
            if "transport" not in kw:          # client của explain(); ASGITransport thì để yên
                kw["transport"] = httpx.MockTransport(fake)
            super().__init__(**kw)

    so.httpx.AsyncClient = Client
    app = so.build_app()
    async with real_client(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8992") as c:
        async def ask(text, context=BANK, **kw):
            return await c.post("/api/explain", json={"text": text, "context": context}, **kw)

        # ── Code quyết loại chữ ──────────────────────────────────────────────
        fake.reply = {"part_of_speech": "noun", "meaning": "the land along a river",
                      "meaning_here": "the edge of the river where she sat",
                      "examples": ["We had a picnic on the river bank.", "The bank was muddy after the rain."]}
        r = await ask("bank,")
        d = r.json()
        props = fake.sent["format"]["properties"]
        check("a word is explained with its part of speech, trailing comma dropped",
              r.status_code == 200 and d["term"] == "bank" and d["kind"] == "word" and d["part_of_speech"] == "noun"
              and len(d["examples"]) == 2 and d["model"] == so.LLM_MODEL, r.text[:200])
        check("the model is asked for a part of speech from a fixed list, as JSON, without randomness",
              props["part_of_speech"]["enum"] == list(so.EXPLAIN_POS) and fake.sent["stream"] is False
              and fake.sent["options"]["temperature"] == 0, json.dumps(fake.sent)[:200])
        check("the sentence around the word goes to the model (river bank, not money bank)",
              BANK in fake.sent["messages"][-1]["content"], fake.sent["messages"][-1]["content"])

        fake.reply = {"part_of_speech": "adjective", "meaning": "to use all of something",
                      "meaning_here": "they may finish the milk", "examples": ["Don't run out of gas.", "We ran out of time."]}
        d = (await ask("run out of", "We might run out of milk before the weekend.")).json()
        check("a phrase gets no part of speech, even if the model gives one",
              d["kind"] == "phrase" and d["part_of_speech"] == "" and "part_of_speech" not in fake.sent["format"]["properties"],
              json.dumps(d))
        for text, why in (("The early bird catches the worm.", "ends with a full stop"),
                          ("if you get up before everyone else you will win", "is eight words or more")):
            d = (await ask(text, "")).json()
            check(f"a selection that {why} is a sentence, with other ways to say it",
                  d["kind"] == "sentence" and "other ways to say" in fake.sent["messages"][-1]["content"], json.dumps(d))

        # ── Bỏ những gì model chép lại ───────────────────────────────────────
        fake.reply = {"part_of_speech": "verb", "meaning": "a place to keep money",
                      "meaning_here": "She sat on the bank of the river and watched the boats.",
                      "examples": ["She sat on the bank of the river and watched the boats.",
                                   "The bank opens at nine.", "the bank opens at nine!"]}
        d = (await ask("bank")).json()
        check("the reader's own sentence is never shown back as an example or as the meaning here",
              d["examples"] == ["The bank opens at nine."] and d["meaning_here"] == "", json.dumps(d))

        # ── Kiểm lại câu trả lời ─────────────────────────────────────────────
        fake.reply = {"part_of_speech": "<b>noun</b>", "meaning": "x " * 2000, "meaning_here": 7,
                      "examples": [None, 3, "Fine example here."] * 3}
        d = (await ask("bank")).json()
        check("a part of speech outside the list, wrong types and huge strings are cleaned",
              d["part_of_speech"] == "" and len(d["meaning"]) <= 600 and d["meaning_here"] == ""
              and d["examples"] == ["Fine example here."], json.dumps(d)[:200])
        for reply, what in (('{"meaning": "cut off', "broken JSON"), ({"examples": []}, "no meaning"),
                            ("[1, 2]", "not an object")):
            fake.reply = reply
            r = await ask("bank")
            check(f"an answer with {what} is a 502 that says to try again",
                  r.status_code == 502 and "try again" in r.json()["error"], f"{r.status_code} {r.text[:100]}")

        # ── Chữ người dùng gửi lên ───────────────────────────────────────────
        for text, why in ((" ", "nothing"), ("x" * (so.EXPLAIN_TERM_MAX + 1), "too much"), ("…", "only punctuation")):
            r = await ask(text)
            check(f"selecting {why} is refused", r.status_code == 400, f"{r.status_code} {r.text[:80]}")
        r = await c.post("/api/explain", json=["bank"])
        check("a body that is not an object is refused, not a crash", r.status_code == 400, f"{r.status_code}")
        fake.reply = {"part_of_speech": "noun", "meaning": "m", "meaning_here": "h", "examples": ["a b c d"]}
        await ask("bank", "y" * 5000)
        check("the context sent to the model is capped", "y" * 1001 not in fake.sent["messages"][-1]["content"]
              and "y" * 1000 in fake.sent["messages"][-1]["content"], "")
        fake.sent = None
        r = await ask("bank", headers={"origin": "https://evil.example"})
        check("another website cannot make the model run (CSRF)", r.status_code == 403 and fake.sent is None,
              f"{r.status_code}")

        # ── Ollama vắng / thiếu model ────────────────────────────────────────
        fake.status = 404
        r = await ask("bank")
        check("a model that is not pulled says how to pull it",
              r.status_code == 409 and f"ollama pull {so.LLM_MODEL}" in r.json()["error"], r.text[:120])
        so.httpx.AsyncClient = real_client
        url, so.LLM_URL = so.LLM_URL, "http://127.0.0.1:9"
        r = await ask("bank")
        so.LLM_URL = url
        check("Ollama not running says what is needed", r.status_code == 409 and "Ollama" in r.json()["error"],
              f"{r.status_code} {r.text[:120]}")

    # ── Nối dây ──────────────────────────────────────────────────────────────
    html = (HERE / "static/orchestrator/index.html").read_text(encoding="utf-8")
    js = (HERE / "static/orchestrator/app.js").read_text(encoding="utf-8")
    for want, where, blob in (('onclick="pdfExplainSel()"', "index.html", html), ('id="exp-modal"', "index.html", html),
                              ('"/api/explain"', "app.js", js), ("ttsOpen(exp.examples[", "app.js", js)):
        check(f"{where} wires up {want}", want in blob, "not found")
    check("the Explain dialog sits before Speak text, so Speak opens on top of it",
          html.index('id="exp-modal"') < html.index('id="tts-modal"'), "")
    check("every model string is escaped before it reaches the page",
          all(f"esc({k})" in js for k in ("r.term", "r.model", "x", "text")) and "esc(r.part_of_speech || r.kind)" in js, "")


asyncio.run(main())
(so.DB_DIR / "check_explain.db").unlink(missing_ok=True)
print()
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("all explain checks passed")
