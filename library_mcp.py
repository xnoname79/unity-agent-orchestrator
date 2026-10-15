"""
Library MCP Server — the dashboard's Library (kept sounds and images), as read-only tools for agents.

An agent picking media has no ears and, for files, no eyes: a sound is a title and a length. These
tools give it the rest. search_media finds what is in the library; inspect_media measures a sound
(loudness over time, where it is quiet, how bright it is) or shows an image. A sound's measurements
are saved beside the file, so later searches return them without measuring again.

Read-only for now: nothing here adds, changes or deletes media.

Mounted by the orchestrator at /library/mcp — no separate process, no extra port. Register it once
per CLI (the Library drawer has a button that does this for every installed CLI):

    claude mcp add --transport http --scope user library http://127.0.0.1:8992/library/mcp
"""

import json

from mcp.server.fastmcp import FastMCP, Image

mcp = FastMCP("Library")

# Claude nhận ảnh tới ~5 MB sau base64; quá ngưỡng này thì thu nhỏ bằng ffmpeg trước khi gửi.
SHOW_MAX = 3_500_000


def _orch():
    # Import muộn: module này được build_app() import, lúc đó session_orchestrator đã nạp xong
    # (và đã alias __main__ → xem check_single_module.py).
    import session_orchestrator as orch
    return orch


def _fail(e):
    return f"⛔ {e}"


@mcp.tool()
async def search_media(query: str = "", kind: str = "", min_seconds: float = 0,
                       max_seconds: float = 0, limit: int = 20):
    """Find sounds and images the user has kept in the Library.

    Words in `query` match titles, image prompts, channels and file names (any word counts; more
    matching words rank higher). Leave it empty to list everything, newest first.

    Args:
        query: e.g. "rain thunder" or "sword". Empty = everything.
        kind: "sound", "image", or empty for both.
        min_seconds, max_seconds: only sounds of this length (0 = no limit). Setting either one
            leaves images out.
        limit: how many to return, 1 to 100.

    Returns JSON {total, items}. Every item has kind, name and path (copy the file from path).
    Sounds: title, channel, seconds, format, license, source, by, inspected, and when inspected
    the same `inspect` measurements inspect_media returns. Images: prompt, width, height.
    A sound with inspected=false has not been measured yet: call inspect_media on it.
    `license` is empty when the source did not state one — check before shipping a sound.
    """
    try:
        r = _orch().library_search(query, kind, min_seconds, max_seconds, limit)
    except ValueError as e:
        return _fail(e)
    return json.dumps(r, ensure_ascii=False)


@mcp.tool()
async def inspect_media(name: str):
    """Look closely at one item from search_media, by its `name`.

    An image comes back as the picture itself plus its prompt and size, so you can see it.

    A sound comes back measured, since you cannot listen to it:
    - `seconds`: the real length, decoded from the file.
    - `level`: loudness over time as 60 digits, 0 = silence up to 9 = loudest; `step` seconds per
      digit. Read it for the shape: a hit (one spike), a swell (rising), a steady bed (flat).
    - `loud` / `quiet`: [start, end] stretches in seconds that carry sound and that do not — a
      field recording that opens with two seconds of room tone wants to start at 2, not 0.
    - `peak_dbfs` near 0 means already as loud as it gets. `rms_dbfs` is the average. How far
      the peak sits above it tells steady from spiky — measured examples: a held tone 3 dB,
      music 10, noise or rain 12-16, a boom or a sword hit 15-17, birdsong or a cymbal dying away
      21-23.
    - `highs`: how much sits up high, in dB against the whole sound, per second of sound —
      `above_1_5k` and `above_3k` as median and p95, plus `brightest_at` (a second). Measured
      `above_1_5k.median`: about 0 is very bright (cymbals, birdsong, hiss); -5 to -10 airy
      (rain, sword swishes); -10 to -20 full-range (most music); below -25 dark (booms, hums,
      rumble). A bed under someone talking wants above_1_5k.p95 at or below about -18.
    There is no tempo or key here: `level` shows phrases, not downbeats.

    Measuring takes a few seconds per minute of sound; the result is saved, so next time (and in
    search_media) it is instant.
    """
    orch = _orch()
    item = next((i for i in orch.library_items() if i["name"] == name), None)
    if not item:
        return _fail(f"no item called {name!r} in the library — search_media lists them")
    if item["kind"] == "sound":
        try:
            item["inspect"] = {k: v for k, v in (await orch.sound_inspect(name)).items()
                               if k not in ("v", "mtime")}
        except (LookupError, RuntimeError) as e:
            return _fail(e)
        item["inspected"] = True
        item["seconds"] = item["seconds"] or item["inspect"]["seconds"]
        return json.dumps(item, ensure_ascii=False)
    text = json.dumps(item, ensure_ascii=False)
    p = orch.IMAGES_DIR / name
    if p.stat().st_size <= SHOW_MAX:
        return [text, Image(path=str(p))]
    # Ảnh lớn (tải lên tới 10 MB): thu nhỏ còn cạnh dài 1568 px — cỡ lớn nhất Claude còn nhìn.
    try:
        rc, data, _ = await orch._ffmpeg(["-i", str(p), "-vf", "scale='min(1568,iw)':-2",
                                          "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg",
                                          "-q:v", "4", "-"], 60)
    except (LookupError, RuntimeError):
        rc, data = 1, b""
    if rc or not data:
        return [text, f"(the picture is {p.stat().st_size // 2**20} MB, too big to show here "
                      f"without ffmpeg — open it from `path`)"]
    return [text, Image(data=data, format="jpeg")]
