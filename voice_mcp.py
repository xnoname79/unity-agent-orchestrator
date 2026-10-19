"""
Voice MCP Server — text to speech for agents, on this machine.

Kokoro-82M (Apache-2.0) through sherpa-onnx: 28 English voices, US and UK, male and female. Each
call writes a WAV into the system temp folder (orch-speech/), which is cleared when the computer
restarts — speech is for using now, not for keeping in the Library.

English only for now. The voice model (350 MB) is downloaded once, by the user, from Library ›
Speak text; until then speak() says so.

Mounted by the orchestrator at /voice/mcp — no separate process, no extra port:

    claude mcp add --transport http --scope user voice http://127.0.0.1:8992/voice/mcp
"""

import json

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Voice")


def _orch():
    # Import muộn: module này được build_app() import, lúc đó session_orchestrator đã nạp xong
    # (và đã alias __main__ → xem check_single_module.py).
    import session_orchestrator as orch
    return orch


@mcp.tool()
async def list_voices():
    """The voices speak() can use, and whether the voice model is downloaded yet.

    Returns JSON {ready, default, voices: [{id, label}]}. Ids start with a = American or
    b = British, then f = female or m = male: af_heart is a US female voice, bm_george a UK male.
    """
    st = _orch().tts_status()
    return json.dumps({k: st[k] for k in ("ready", "default", "voices")}, ensure_ascii=False)


@mcp.tool()
async def speak(text: str, voice: str = "af_heart", speed: float = 1.0, from_role: str = ""):
    """Turn English text into speech, as a WAV file in the system temp folder.

    Takes roughly 1.5× as long as the speech itself (a 10-second sentence ≈ 15 s), one request
    at a time, so keep each call to what you actually need said.

    Args:
        text: English text, up to 3000 characters. Punctuation shapes the pauses.
        voice: A voice id from list_voices. af_heart (US female) is the most natural.
        speed: 0.5 to 2.0. 1.0 is normal; around 0.8 is easier to follow for an English learner.
        from_role: Your session name, returned as `by` with the result.

    Returns JSON {name, path, duration, voice, speed, text, by, created}. The file is removed when
    the computer restarts: copy it from `path` into your project if it has to last.
    """
    try:
        r = await _orch().tts_speak(text, voice, speed, from_role)
    except (ValueError, LookupError, RuntimeError) as e:
        return f"⛔ {e}"
    return json.dumps(r, ensure_ascii=False)
