"""
Sounds MCP Server — the dashboard's Sounds panel, as tools for agents.

Search videos (YouTube, through yt-dlp), pull the audio out of one, list what is already in the
library. Every tool calls the same functions as the panel, so a sound an agent extracts shows up
in the panel's library for the user, and the other way round.

Mounted by the orchestrator at /sounds/mcp — no separate process, no extra port. Register it once
per CLI (the Sounds panel has a button that does this for every installed CLI):

    claude mcp add --transport http --scope user sounds http://127.0.0.1:8992/sounds/mcp
"""

import json

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Sounds")


def _orch():
    # Import muộn: module này được build_app() import, lúc đó session_orchestrator đã nạp xong
    # (và đã alias __main__ → xem check_single_module.py).
    import session_orchestrator as orch
    return orch


def _fail(e):
    # Lời lỗi của lõi đã là câu chỉ dẫn hoàn chỉnh (thiếu tool → lệnh cài, bản cũ → lệnh nâng
    # cấp). Trả nguyên văn để agent làm theo hoặc chuyển cho người dùng, không đoán thêm.
    return f"⛔ {e}"


@mcp.tool()
async def search_videos(query: str, limit: int = 10, ai_only: bool = True):
    """Search YouTube for videos whose sound you might want.

    Args:
        query: What it should sound like, e.g. "rain on a tin roof" or "8-bit victory jingle".
        limit: How many results, 1 to 20.
        ai_only: True adds "AI generated" to the search, the same as the panel's checkbox.

    Returns JSON: {query, results: [{title, url, channel, duration (seconds), views, too_long}]}.
    Results with too_long=true cannot be extracted (see extract_audio). Live streams are left out.
    """
    try:
        return json.dumps(await _orch().sound_search(query, limit, ai_only), ensure_ascii=False)
    except (ValueError, LookupError, RuntimeError) as e:
        return _fail(e)


@mcp.tool()
async def extract_audio(url: str, format: str = "mp3", from_role: str = ""):
    """Pull the audio track out of one video and save it to the user's Sounds library.

    Waits until done — usually under a minute. The file lands in the orchestrator's sounds
    folder; copy it from `path` into your project if you need it there.

    Args:
        url: The video link, e.g. a `url` from search_videos.
        format: mp3, wav, m4a, opus or flac.
        from_role: Your session name, shown in the library as who extracted it.

    Returns JSON: {name, path, title, channel, duration, source, license, format, by, created}.
    `license` is empty when the site does not state one — check before shipping the sound.
    """
    try:
        return json.dumps(await _orch().sound_extract(url, format, from_role), ensure_ascii=False)
    except (ValueError, LookupError, RuntimeError) as e:
        return _fail(e)


@mcp.tool()
async def list_sounds():
    """Sounds already in the library, newest first: [{name, path, title, channel, duration,
    source, license, format, by, created, size}]."""
    return json.dumps(_orch().list_sounds(), ensure_ascii=False)
