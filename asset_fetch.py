"""
Asset Fetch MCP Server — search game asset libraries (search + suggest only)

MCP tool để các agent chuyên biệt tìm resource phù hợp cho scene và ĐỀ XUẤT cho user.
KHÔNG tải file — chỉ trả tên + link + metadata để user tự tải (tránh OAuth/download API).

Nguồn theo `kind`:
  kind="texture" → AmbientCG   (CC0, no auth)
  kind="model"   → Sketchfab   (search public no auth; lọc downloadable + CC)
  kind="sound"   → Freesound   (search cần API key free, không OAuth)

Env:
  ASSET_FETCH_HOST   bind (default 0.0.0.0)
  ASSET_FETCH_PORT   port (default 8994)
  FREESOUND_KEY      API key Freesound (chỉ cần cho kind="sound"); đọc từ .env nếu có.

Start:  python3 asset_fetch.py
Agent:  claude mcp add --transport http assets http://localhost:8994/mcp
Hoặc mount chung orchestrator tại /assets.
"""

import asyncio
import json
import os
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

HOST = os.environ.get("ASSET_FETCH_HOST", "0.0.0.0")
PORT = int(os.environ.get("ASSET_FETCH_PORT", "8994"))


def _load_env():
    """Đọc .env cạnh file này vào os.environ (stdlib, không cần python-dotenv).
    Chỉ set key chưa có sẵn — env thật luôn thắng file."""
    f = Path(__file__).parent / ".env"
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_env()
FREESOUND_KEY = os.environ.get("FREESOUND_KEY", "")


@asynccontextmanager
async def lifespan(server):
    yield {}


mcp = FastMCP("Asset-Fetch", lifespan=lifespan, host=HOST, port=PORT)


# ─── Adapters: mỗi nguồn chuẩn hoá về {name, page_url, thumb, license, source, meta} ──

async def _search_ambientcg(client, query, limit):
    """AmbientCG textures/materials — CC0, no auth."""
    r = await client.get("https://ambientcg.com/api/v2/full_json", params={
        "type": "Material", "q": query, "limit": limit,
        "include": "tagData,imageData",
    })
    r.raise_for_status()
    out = []
    for a in r.json().get("foundAssets", []):
        imgs = a.get("imageData") or {}
        thumb = ""
        if isinstance(imgs, dict):
            # imageData: {"<res>": {"<usage>": url}} — nhặt url đầu tiên tìm được.
            for v in imgs.values():
                if isinstance(v, dict) and v:
                    thumb = next(iter(v.values())) if isinstance(next(iter(v.values())), str) else ""
                if thumb:
                    break
        out.append({
            "name": a.get("displayName") or a.get("assetId"),
            "page_url": f"https://ambientcg.com/view?id={a.get('assetId')}",
            "thumb": thumb,
            "license": "CC0",
            "source": "AmbientCG",
            "meta": {
                "tags": a.get("tags", []),
                "download_count": a.get("downloadCount"),
                "data_type": a.get("dataTypeName"),
            },
        })
    return out


def _urllib_get_json(url, params):
    """GET + parse JSON qua urllib stdlib (blocking). Dùng cho Sketchfab: CDN chặn TLS
    fingerprint của httpx (trả 202 body rỗng) nhưng chấp nhận fingerprint OpenSSL/urllib."""
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


async def _search_sketchfab(client, query, limit):
    """Sketchfab models — search public no auth; chỉ lấy downloadable + Creative Commons.
    Qua urllib (không httpx) vì CDN Sketchfab chặn TLS fingerprint httpx."""
    data = await asyncio.to_thread(_urllib_get_json, "https://api.sketchfab.com/v3/search", {
        "type": "models", "q": query, "downloadable": "true", "count": limit,
    })
    out = []
    for m in data.get("results", []):
        thumbs = ((m.get("thumbnails") or {}).get("images") or [])
        out.append({
            "name": m.get("name"),
            "page_url": m.get("viewerUrl"),
            "thumb": thumbs[0]["url"] if thumbs else "",
            "license": (m.get("license") or {}).get("label", ""),
            "source": "Sketchfab",
            "meta": {
                "face_count": m.get("faceCount"),
                "vertex_count": m.get("vertexCount"),
                "is_downloadable": m.get("isDownloadable"),
                "uid": m.get("uid"),
            },
        })
    return out


async def _search_freesound(client, query, limit):
    """Freesound sounds — search cần API key free (query param token)."""
    if not FREESOUND_KEY:
        raise RuntimeError("thiếu FREESOUND_KEY (đăng ký free tại freesound.org, đặt vào .env)")
    r = await client.get("https://freesound.org/apiv2/search/text/", params={
        "query": query, "page_size": limit, "token": FREESOUND_KEY,
        "fields": "id,name,url,previews,license,duration,tags",
    })
    r.raise_for_status()
    out = []
    for s in r.json().get("results", []):
        out.append({
            "name": s.get("name"),
            "page_url": s.get("url"),
            "thumb": (s.get("previews") or {}).get("preview-hq-mp3", ""),  # audio preview
            "license": s.get("license", ""),
            "source": "Freesound",
            "meta": {
                "duration": s.get("duration"),
                "tags": s.get("tags", []),
                "id": s.get("id"),
            },
        })
    return out


_ADAPTERS = {
    "texture": _search_ambientcg,
    "model": _search_sketchfab,
    "sound": _search_freesound,
}


@mcp.tool()
async def search_assets(kind: str, query: str, limit: int = 10):
    """Tìm game asset phù hợp và ĐỀ XUẤT cho user (chỉ search — user tự tải từ link).

    Dùng khi cần resource cho scene: 3D model, texture/material, âm thanh. Trả danh sách
    kèm link trang nguồn + thumbnail + license + metadata để agent REVIEW độ phù hợp
    (poly count, tags, thời lượng...) rồi đề xuất cho user quyết.

    Args:
        kind: Loại asset — "texture" (AmbientCG, CC0) | "model" (Sketchfab) | "sound" (Freesound).
        query: Từ khoá tìm, vd "rusted metal", "wooden barrel", "footsteps gravel".
        limit: Số kết quả tối đa (default 10).

    Returns:
        JSON list mỗi item: {name, page_url, thumb, license, source, meta}. page_url là
        link để user tự tải. meta chứa thông tin review theo nguồn.
    """
    adapter = _ADAPTERS.get(kind)
    if not adapter:
        return f"kind không hợp lệ: '{kind}'. Chọn: {', '.join(_ADAPTERS)}."
    limit = max(1, min(int(limit), 30))
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            # Sketchfab thỉnh thoảng trả 202 (CDN warm-up) → body rỗng, .json() nổ. Retry nhẹ.
            for attempt in range(3):
                try:
                    results = await adapter(client, query, limit)
                    break
                except json.JSONDecodeError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1)
    except RuntimeError as e:
        return f"Lỗi: {e}"
    except json.JSONDecodeError:
        return f"Nguồn ({kind}) trả response không hợp lệ sau 3 lần thử — thử lại sau."
    except httpx.HTTPError as e:
        return f"Lỗi gọi API nguồn ({kind}): {e}"
    if not results:
        return f"Không tìm thấy asset '{query}' ({kind})."
    return json.dumps(results, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
