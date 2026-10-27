<img src="images/logo.png" width="72" align="right" alt="">

# Reader

A PDF reader for your phone, hosted on your own server, with an account per person.

- **Read** — named bookmarks on any line; each PDF reopens where you left off.
- **Quotes** — highlight text to save it as a quote, grouped by page. Tap a quote to read it whole,
  hear it, or delete it.
- **Speak** — any selection or quote read aloud with natural English voices (Kokoro, made on the
  server, 28 US and UK voices).
- **Explain** — Claude translates a quote into the language you pick, then lists its new words
  (hardest first) with their meaning and everyday example sentences. The answer is saved with the
  quote, so opening it again costs nothing.
- **Shuffle** — your quotes as cards in random order: swipe right for the next, left to go back.

It runs with Docker, behind Caddy for HTTPS. Nothing else from this repository is reachable on it.

## What you need

- A Linux server with Docker (including the Compose plugin) and git: 2 CPU cores, at least 1 GB of
  RAM, and a few GB of disk (the image is about 1 GB; each person can store up to 1 GB).
- A domain pointing at the server: an A record to its IP. On Cloudflare, set it to **DNS only**
  (grey cloud) — the certificate is fetched directly on port 443, and the proxy would block that.
- Port 443 open, TCP and UDP. Port 80 is not used, so another web server can keep it.
- For Explain only: a [Claude API key](https://platform.claude.com/settings/keys). It is billed per
  use, and promotional credits work.

## Deploy

On the server:

```bash
git clone -b pdf-reader https://github.com/xnoname79/unity-agent-orchestrator.git reader
cd reader

cat > .env <<'EOF'
READER_DOMAIN=reader.example.com
ORCH_ANTHROPIC_API_KEY=<your key>
EOF
chmod 600 .env

docker compose -f docker-compose.reader.yml up -d --build
docker compose -f docker-compose.reader.yml exec reader python reader_server.py adduser alice
```

- Clone the `pdf-reader` branch: the default branch does not have the reader.
- The first build downloads the voice (350 MB) and checks it against a fixed checksum.
- `.env` stays on the server. The image never contains it; the key reaches the container when it
  starts.
- Leave `ORCH_ANTHROPIC_API_KEY` out to run without Explain.

Check it:

1. `docker compose -f docker-compose.reader.yml ps` — `reader` and `caddy` are both running.
2. `docker compose -f docker-compose.reader.yml logs caddy` — the certificate was obtained.
3. On the phone, open `https://reader.example.com`, sign in, upload a PDF, then Share › **Add to
   Home Screen** so it opens like an app.

## Settings

In the `.env` next to `docker-compose.reader.yml`. Run `docker compose -f docker-compose.reader.yml up -d`
after a change.

| Variable | Default | What it does |
|---|---|---|
| `READER_DOMAIN` | — (required) | The domain the site answers to; anything else is refused |
| `READER_QUOTA_MB` | `1024` | Storage per person |
| `ORCH_ANTHROPIC_API_KEY` | — | Claude API key for Explain |
| `ORCH_ANTHROPIC_WORKSPACE_ID` | — | Only if Explain says the key "is not scoped to a workspace": the workspace ID (`wrkspc_…`) from the Claude Console |
| `ORCH_EXPLAIN_MODEL` | `claude-opus-5-5` | The model Explain starts on |

The key is `ORCH_ANTHROPIC_API_KEY`, not `ANTHROPIC_API_KEY`: on a machine that also runs the
orchestrator, its Claude Code agents read `ANTHROPIC_API_KEY` and would switch from your
subscription to the API.

## Accounts

There is no sign-up page. Make accounts on the server:

```bash
docker compose -f docker-compose.reader.yml exec reader python reader_server.py adduser alice   # asks for a password
docker compose -f docker-compose.reader.yml exec reader python reader_server.py passwd alice    # new password, signs her out everywhere
docker compose -f docker-compose.reader.yml exec reader python reader_server.py deluser alice   # removes her and her documents
docker compose -f docker-compose.reader.yml exec reader python reader_server.py users
```

> [!WARNING]
> Every account can upload files, use the server's CPU and spend from your Claude key through
> Explain. Only make accounts for people you trust.

## Explain and its cost

In a quote, the gear sets the language (14 to pick from) and the model. Both are saved on that
device; changing them never calls Claude. Only the **Explain in …** button does, once per quote and
language.

| Model | Rough cost per Explain |
|---|---|
| Opus 5.5 (default) | about 2¢ |
| Sonnet 5.5 | about 1¢ |
| Haiku 5.5 | under 0.1¢ |

These are estimates; the Claude Console's usage page shows the real cost. To spend only your
credits: turn off auto-reload in the Console's Billing page and set a spend limit. When the credits
run out, Explain says so and nothing is charged; the rest of the reader keeps working.

## Try it on your computer first

No Docker and no domain needed. Python 3.10 or newer:

```bash
git clone -b pdf-reader https://github.com/xnoname79/unity-agent-orchestrator.git reader
cd reader
python3 -m venv .venv
.venv/bin/pip install starlette uvicorn httpx sherpa-onnx anthropic

export READER_DATA=$HOME/reader-test        # accounts and PDFs for this test
export READER_INSECURE_COOKIE=1             # lets sign-in work over plain http
.venv/bin/python reader_server.py fetch-voice   # once, 350 MB, for Speak
.venv/bin/python reader_server.py adduser alice
.venv/bin/python reader_server.py serve
```

Open <http://127.0.0.1:8080>. For Explain, put `ORCH_ANTHROPIC_API_KEY=<your key>` in a `.env` file
next to `reader_server.py`. To try it from a phone on the same Wi-Fi, add `export READER_HOST=0.0.0.0`
and open `http://<your computer's IP>:8080` — only on a network you trust.

The Docker setup does not work on `localhost`: Caddy can only get its certificate for a real domain.

## Port 443 is already taken

If another web server on the machine owns 443, let it forward to the reader instead of running
Caddy. In `docker-compose.reader.yml`, delete the `caddy` service and give `reader` a port only the
machine itself can reach:

```yaml
  reader:
    ports:
      - "127.0.0.1:8080:8080"
```

Then add a site to your web server. With nginx:

```nginx
server {
    listen 443 ssl;
    server_name reader.example.com;
    # ssl_certificate … (the same way your other sites get theirs)
    client_max_body_size 210m;                 # PDFs up to 200 MB
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $http_host;      # the reader refuses other domains
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Keep the `127.0.0.1:` prefix: published to the internet, port 8080 would let people skip HTTPS and
fake the address that the failed-login limit counts.

To serve on another port instead (say `https://reader.example.com:8443`) you need port 80 free for
the certificate: in `Caddyfile.reader` write `{$READER_DOMAIN}:8443` and delete the `tls { … }`
block, and in `docker-compose.reader.yml` publish `8443:8443`, `8443:8443/udp` and `80:80`.

## Update

```bash
git pull
docker compose -f docker-compose.reader.yml up -d --build
```

## Back up

Accounts, documents, quotes and saved explanations live in the `reader_reader-data` Docker volume
(the prefix is the folder you cloned into). Keep `reader_caddy-data` too: it holds the certificate.

```bash
docker run --rm -v reader_reader-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/reader-data.tgz -C /data .
```

## When something goes wrong

| You see | Do this |
|---|---|
| The certificate is never obtained | The domain must point straight at the server (Cloudflare: DNS only) and port 443 must be open |
| You cannot sign in | The site must be on HTTPS. Testing over http on your computer needs `READER_INSECURE_COOKIE=1` |
| Explain: "not scoped to a workspace" | Set `ORCH_ANTHROPIC_WORKSPACE_ID`, or make a new key inside a workspace in the Claude Console |
| Explain: "credit balance is too low" | The credits are used up or expired |
| Explain: "needs a Claude API key" | Set `ORCH_ANTHROPIC_API_KEY` in `.env`, then `up -d` again |

Logs: `docker compose -f docker-compose.reader.yml logs -f reader`.
