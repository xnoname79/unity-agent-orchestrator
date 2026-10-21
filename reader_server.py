#!/usr/bin/env python3
"""
Reader — the orchestrator's PDF reader and Speak text, as a web app for phones.

Several people, each with their own documents, quotes and bookmark. Nothing else from the
orchestrator is here: no agents, terminals, MCP or /v1 — those routes do not exist on this server.
Accounts are made on the server only; there is no sign-up page:

    python reader_server.py adduser alice     # asks for the password
    python reader_server.py passwd alice      # new password, signs alice out everywhere
    python reader_server.py deluser alice     # removes alice and her documents
    python reader_server.py users
    python reader_server.py fetch-voice       # downloads the Kokoro voice (350 MB), once
    python reader_server.py serve             # the default

Serve it over HTTPS (docker-compose.yml puts Caddy in front): the session cookie is Secure, so a
browser on plain http drops it and nobody can sign in. READER_INSECURE_COOKIE=1 is for trying it
on localhost only.

Settings (environment):
    READER_DATA        where accounts and documents live      (./reader-data)
    READER_HOST/PORT   address to listen on                   (127.0.0.1:8080)
    READER_HOSTS       domain(s) the site answers to, comma separated — anything else is refused
    READER_QUOTA_MB    storage per person                     (1024)
    READER_PROXY       proxy IPs whose X-Forwarded-For is trusted for the login limit (127.0.0.1)
    READER_VOICE       folder holding the Kokoro voice        (READER_DATA/voice)
    ORCH_TTS_THREADS   CPU threads per speech                 (2)
    ORCH_LLM_URL       Ollama that answers Explain            (http://127.0.0.1:11434)
    ORCH_LLM_MODEL     the model it uses                      (gemma3:270m)
"""

import asyncio
import contextlib
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from starlette.requests import Request

import session_orchestrator as so

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static" / "orchestrator"
DATA = Path(os.environ.get("READER_DATA", "reader-data")).resolve()
HOST = os.environ.get("READER_HOST", "127.0.0.1")
PORT = int(os.environ.get("READER_PORT", "8080"))
HOSTS = [h.strip() for h in os.environ.get("READER_HOSTS", "").split(",") if h.strip()]
QUOTA = int(os.environ.get("READER_QUOTA_MB", "1024")) * 2**20
PROXY = os.environ.get("READER_PROXY", "127.0.0.1")
SECURE = os.environ.get("READER_INSECURE_COOKIE", "") != "1"
SPEECH = Path(tempfile.gettempdir()) / "reader-speech"
COOKIE = "reader_session"
SESSION_DAYS = 30
SPEECH_KEEP = 3600          # giây: tiếng đọc chỉ để nghe ngay, container thì không khởi động lại
SWEEP_EVERY = 600           # giây giữa hai lần dọn tiếng đọc cũ
_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
PASSWORD_MIN = 10
# Bản host phải là trang reader: server gắn cờ này vào <html> trước khi trả trang (xem app.js READER).
_HTML_TAG = '<html lang="en">'

so.TTS_DIR = Path(os.environ.get("READER_VOICE") or DATA / "voice")

# Add to Home Screen trên iPhone: mở cả màn hình, có icon riêng.
_HOME_SCREEN = ('<link rel="manifest" href="/manifest.webmanifest">'
                '<meta name="apple-mobile-web-app-capable" content="yes">'
                '<meta name="mobile-web-app-capable" content="yes">'
                '<meta name="apple-mobile-web-app-title" content="Reader">'
                '<link rel="apple-touch-icon" href="/icon-512.png">\n')
_MANIFEST = {"name": "Reader", "short_name": "Reader", "start_url": "/", "display": "standalone",
             "background_color": "#0e1117", "theme_color": "#161b22",
             "icons": [{"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}]}


# ─── Tài khoản + phiên ────────────────────────────────────────────────────────

_made = False


def _db():
    global _made
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    c = sqlite3.connect(DATA / "reader.db")
    c.row_factory = sqlite3.Row
    if not _made:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                                              pw TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                                                 expires REAL NOT NULL);""")
        _made = True
    return c


def _q(sql, args=(), one=False):
    with contextlib.closing(_db()) as c:
        rows = c.execute(sql, args).fetchall()
        c.commit()
    return (rows[0] if rows else None) if one else rows


# scrypt của stdlib: không thêm dependency. n=2^14, r=8 ≈ 16 MB RAM, ~50 ms một lần thử.
_N, _R, _P = 2**14, 8, 1


def hash_pw(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${h.hex()}"


def check_pw(pw, stored):
    _, n, r, p, salt, h = stored.split("$")
    got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                         dklen=len(h) // 2)
    return hmac.compare_digest(got.hex(), h)


# Tên không tồn tại vẫn tốn đúng một lần scrypt: đo thời gian trả lời không đoán được ai có tài khoản.
_DUMMY = hash_pw(secrets.token_hex(8))


def _token_key(token):
    # DB chỉ giữ hash của token: lộ file DB không thành lộ phiên đang đăng nhập.
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(user_id):
    token = secrets.token_urlsafe(32)
    _q("DELETE FROM sessions WHERE expires < ?", (time.time(),))
    _q("INSERT INTO sessions (token, user_id, expires) VALUES (?, ?, ?)",
       (_token_key(token), user_id, time.time() + SESSION_DAYS * 86400))
    return token


def session_user(token):
    if not token:
        return None
    row = _q("SELECT u.id, u.name FROM sessions s JOIN users u ON u.id = s.user_id "
             "WHERE s.token = ? AND s.expires > ?", (_token_key(token), time.time()), one=True)
    return dict(row) if row else None


# Đoán mật khẩu: tối đa 10 lần sai mỗi 15 phút, đếm riêng theo IP và theo tên.
_FAIL_WINDOW, _FAIL_MAX = 900, 10
_fails = {}


def _limited(*keys):
    now = time.time()
    for k in keys:
        _fails[k] = [t for t in _fails.get(k, []) if t > now - _FAIL_WINDOW]
    return any(len(_fails[k]) >= _FAIL_MAX for k in keys)


def _failed(*keys):
    if len(_fails) > 10_000:      # ai đó đổi tên liên tục để phình bộ nhớ: bỏ sổ cũ
        _fails.clear()
    for k in keys:
        _fails.setdefault(k, []).append(time.time())


# ─── Thư mục của từng người ───────────────────────────────────────────────────

def user_dir(user_id):
    return DATA / "users" / str(int(user_id))


def used_bytes(user_id):
    return sum(p.stat().st_size for p in user_dir(user_id).rglob("*") if p.is_file())


def _uid(request):
    return request.scope["user"]["id"]


def sweep_speech():
    """Xoá tiếng đọc quá SPEECH_KEEP của MỌI người. Server ít khi khởi động lại nên /tmp không tự
    sạch; dọn theo giờ chứ không đợi chính người đó đọc tiếp — người thôi dùng cũng không để lại gì."""
    cutoff = time.time() - SPEECH_KEEP
    for p in SPEECH.glob("*/*.wav"):
        with contextlib.suppress(OSError):   # file vừa bị xoá ở chỗ khác
            if p.stat().st_mtime < cutoff:
                p.unlink()


@contextlib.asynccontextmanager
async def _lifespan(app):
    async def loop():
        while True:
            await asyncio.to_thread(sweep_speech)
            await asyncio.sleep(SWEEP_EVERY)
    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()


# ─── Cổng: phiên đăng nhập + header an toàn ───────────────────────────────────

_OPEN = {"/api/login"}
_HEADERS = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin"),
            (b"x-frame-options", b"DENY"), (b"cache-control", b"no-cache")]
# Việc nặng: mỗi người một lượt mỗi loại, kẻo một người xếp hàng cả server.
_SLOW = {"/api/tts/speak": "speech", "/api/explain": "explanation"}
_busy = set()           # (user id, path) đang chờ


class Gate:
    """ASGI thuần, như OriginGuard. Mọi /api/ trừ /api/login cần phiên hợp lệ; scope["user"] là
    người đang hỏi — route lấy thư mục từ đó, không bao giờ từ thứ client gửi lên."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_safe(msg):
            if msg["type"] == "http.response.start":
                have = {k.lower() for k, _ in msg.get("headers", [])}
                msg["headers"] = list(msg.get("headers", [])) + [h for h in _HEADERS if h[0] not in have]
            await send(msg)

        path = scope["path"]
        if path.startswith("/api/") and path not in _OPEN:
            user = session_user(Request(scope).cookies.get(COOKIE))
            if not user:
                return await _refuse(send_safe, 401, "sign in first")
            scope["user"] = user
            if path in _SLOW and scope["method"] == "POST":
                key = (user["id"], path)
                if key in _busy:
                    return await _refuse(send_safe, 429,
                                         f"one at a time — wait for the {_SLOW[path]} you asked for")
                _busy.add(key)
                try:
                    return await self.app(scope, receive, send_safe)
                finally:
                    _busy.discard(key)
        await self.app(scope, receive, send_safe)


async def _refuse(send, status, message):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": json.dumps({"error": message}).encode()})


# ─── App ──────────────────────────────────────────────────────────────────────

def build_app():
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    if _HTML_TAG not in html:
        raise RuntimeError(f"index.html has no {_HTML_TAG} to mark as the reader page")
    page_html = html.replace(_HTML_TAG, _HTML_TAG[:-1] + ' data-mode="reader">', 1).replace(
        "</head>", _HOME_SCREEN + "</head>", 1)
    # Terminal (xterm) là của agent: điện thoại khỏi tải ~300 KB vô ích, image khỏi mang nó.
    for tag in ('<link rel="stylesheet" href="/vendor/xterm.css">\n', '<script src="/vendor/xterm.js"></script>\n',
                '<script src="/vendor/xterm-addon-fit.js"></script>\n'):
        page_html = page_html.replace(tag, "")

    async def page(request):
        return HTMLResponse(page_html)

    async def login(request):
        b = await so._json_body(request)
        name = str(b.get("name") or "").strip().lower()[:64]
        pw = str(b.get("password") or "")[:1024]
        ip = request.client.host if request.client else ""
        if _limited("ip:" + ip, "name:" + name):
            return JSONResponse({"error": "Too many tries — wait 15 minutes."}, status_code=429)
        row = _q("SELECT id, name, pw FROM users WHERE name = ?", (name,), one=True)
        ok = await asyncio.to_thread(check_pw, pw, row["pw"] if row else _DUMMY)
        if not (row and ok):
            _failed("ip:" + ip, "name:" + name)
            return JSONResponse({"error": "Wrong name or password."}, status_code=401)
        r = JSONResponse({"name": row["name"]})
        r.set_cookie(COOKIE, new_session(row["id"]), max_age=SESSION_DAYS * 86400, path="/",
                     httponly=True, secure=SECURE, samesite="lax")
        return r

    async def logout(request):
        _q("DELETE FROM sessions WHERE token = ?", (_token_key(request.cookies.get(COOKIE, "")),))
        r = JSONResponse({"signed_out": True})
        r.delete_cookie(COOKIE, path="/", httponly=True, secure=SECURE, samesite="lax")
        return r

    async def manifest(request):
        return JSONResponse(_MANIFEST, media_type="application/manifest+json")

    async def me(request):
        u = request.scope["user"]
        return JSONResponse({"name": u["name"], "used": used_bytes(u["id"]), "quota": QUOTA})

    mimetypes_fix()
    routes = [
        Route("/", page), Route("/index.html", page),
        Route("/manifest.webmanifest", manifest),
        Route("/api/login", login, methods=["POST"]),
        Route("/api/logout", logout, methods=["POST"]),
        Route("/api/me", me),
        *so.doc_routes(lambda r: user_dir(_uid(r)) / "documents",
                       lambda r: max(0, QUOTA - used_bytes(_uid(r)))),
        *so.tts_routes(lambda r: SPEECH / str(_uid(r)), by_of=lambda r: r.scope["user"]["name"],
                       download=False),
        *so.explain_routes(),
        Mount("/", StaticFiles(directory=str(STATIC))),
    ]
    # Ngoài cùng trước: sai tên miền → từ chối ngay; rồi chặn request xuyên site (CSRF); rồi phiên.
    middleware = ([Middleware(TrustedHostMiddleware, allowed_hosts=HOSTS)] if HOSTS else []) + [
        Middleware(so.OriginGuard), Middleware(Gate)]
    return Starlette(routes=routes, middleware=middleware, lifespan=_lifespan)


def mimetypes_fix():
    import mimetypes
    mimetypes.add_type("text/javascript", ".mjs")   # pdf.js là ES module — xem build_app của orchestrator
    mimetypes.add_type("application/wasm", ".wasm")


# ─── Lệnh quản trị ────────────────────────────────────────────────────────────

def _ask_password():
    # getpass, không bao giờ qua argv: argv hiện trong `ps` và lịch sử shell.
    while True:
        pw = getpass.getpass("Password: ")
        if len(pw) < PASSWORD_MIN:
            print(f"At least {PASSWORD_MIN} characters.")
        elif pw != getpass.getpass("Again: "):
            print("They do not match.")
        else:
            return pw


def _user(name):
    row = _q("SELECT id, name FROM users WHERE name = ?", (name.lower(),), one=True)
    if not row:
        sys.exit(f"No account named {name!r}.")
    return row


def cli(argv):
    cmd, *rest = argv or ["serve"]
    if cmd == "serve":
        import uvicorn
        if not _q("SELECT id FROM users LIMIT 1"):
            print("[reader] no accounts yet — add one: python reader_server.py adduser NAME")
        if not so._tts_ready():
            print("[reader] the Kokoro voice is missing — Speak text will not work until: "
                  "python reader_server.py fetch-voice")
        print(f"[reader] http://{HOST}:{PORT}  data: {DATA}")
        uvicorn.run(build_app(), host=HOST, port=PORT, proxy_headers=True,
                    forwarded_allow_ips=PROXY, log_level="warning")
    elif cmd == "adduser" and len(rest) == 1:
        name = rest[0].lower()
        if not _NAME_OK.match(name):
            sys.exit("Names are 2–32 characters: a–z, 0–9, dot, dash, underscore.")
        if _q("SELECT id FROM users WHERE name = ?", (name,), one=True):
            sys.exit(f"{name!r} already exists — use passwd to change the password.")
        _q("INSERT INTO users (name, pw, created) VALUES (?, ?, ?)", (name, hash_pw(_ask_password()), time.time()))
        print(f"Added {name}.")
    elif cmd == "passwd" and len(rest) == 1:
        u = _user(rest[0])
        _q("UPDATE users SET pw = ? WHERE id = ?", (hash_pw(_ask_password()), u["id"]))
        _q("DELETE FROM sessions WHERE user_id = ?", (u["id"],))
        print(f"Changed — {u['name']} is signed out everywhere.")
    elif cmd == "deluser" and len(rest) == 1:
        u = _user(rest[0])
        if input(f"Type {u['name']} again to delete the account and ALL its documents: ").strip() != u["name"]:
            sys.exit("Nothing deleted.")
        _q("DELETE FROM sessions WHERE user_id = ?", (u["id"],))
        _q("DELETE FROM users WHERE id = ?", (u["id"],))
        shutil.rmtree(user_dir(u["id"]), ignore_errors=True)
        print(f"Deleted {u['name']}.")
    elif cmd == "users":
        for u in _q("SELECT id, name FROM users ORDER BY name"):
            print(f"{u['name']:<32} {used_bytes(u['id']) / 2**20:8.1f} MB")
    elif cmd == "fetch-voice":
        if so._tts_ready():
            return print(f"The voice is already in {so.TTS_DIR}.")
        print(f"Downloading the Kokoro voice (350 MB) into {so.TTS_DIR}…")
        asyncio.run(so.tts_download())
        if so._tts_dl["state"] == "failed":
            sys.exit("Download failed: " + so._tts_dl["error"])
        print("Done.")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    cli(sys.argv[1:])
