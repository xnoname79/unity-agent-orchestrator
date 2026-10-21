<img src="images/logo.png" width="72" align="right" alt="">

# Agent Orchestrator

Claude Code, Codex and Antigravity CLI on one canvas, working together. It drives the CLIs already
installed on your machine, so each provider keeps its own login and subscription.

![The canvas](images/screenshot-canvas.png)

[Download](../../releases) · [Report a bug](../../issues/new)

## Features

**Agents**
- Live terminal cards for Claude Code, Codex and Antigravity (Gemini) on one canvas, with the model
  and reasoning effort picked per agent.
- Agents hand work to each other with `send_signal`. Every run is queued, capped and logged in
  History.
- Role playbooks that fill themselves in from the project. Rename an agent at any time.
- Workspaces (isolated folders and agents) filed into groups, two of them side by side.
- A folder card to browse a project from the dashboard; `--dev` swaps it for neovim with a git diff
  view.
- Guard rails: approval gate, ping-pong and daily caps, kill switch, one prompt at a time per
  agent, reachable from your own machine only by default.

**Boards**
- A goal becomes a task list. A read-only board agent nudges you at the right moments and reads
  your chat with your agent to stay current.
- An optional research worker picks open tasks by itself and writes findings on the card. It never
  edits files.

**Media**
- **Images** — generate and edit pictures, with up to three reference images.
- **Sounds** — search YouTube (AI-generated, filtered by length), pull out the audio, crop it in a
  waveform editor.
- **Library** — every kept image, sound and PDF in one drawer. Upload an mp3 and crop it.
- **PDF reader** — bookmark a line and the PDF reopens right there (otherwise where you stopped),
  keep highlighted quotes, and hear any selection read aloud. Each PDF keeps its own state.
  Can also run on your own server for phones, with an account per person (see setup step 7).
- **Speak text** — natural English speech made on your machine (Kokoro, 28 US and UK voices).
- **Media tools** — checks for ffmpeg, ImageMagick, yt-dlp, SoX and ExifTool, and tells you how to
  install what is missing.

**For agents and other apps**
- Built-in MCP servers, connected to every CLI with one click: `signal` (message other agents),
  `boards` (read and update boards), `library` (search media, measure sounds), `voice` (speak).
- An MCP manager that adds any HTTP MCP server to all installed CLIs at once. Tokens never touch a
  command line.
- An OpenAI-compatible API: chat with an agent at `/v1/chat/completions`, make images at
  `/v1/images/generations`. API docs at `/docs`.

## Setup

### 1. Install a provider CLI

At least one, installed and logged in:

| CLI | Install |
|---|---|
| Claude Code | [code.claude.com/docs](https://code.claude.com/docs/en/quickstart#native-install-recommended) |
| Codex CLI | [learn.chatgpt.com/docs/codex/cli](https://learn.chatgpt.com/docs/codex/cli#getting-started) |
| Antigravity CLI | [antigravity.google/docs/cli](https://antigravity.google/docs/cli/reference) — the `agy` command |

Images need the Antigravity CLI.

### 2. Install the orchestrator

**Windows (prebuilt).** Download `agent-orch-windows-x64.zip` from [Releases](../../releases).
Unblock it *before* unzipping, or SmartScreen flags every file inside:

```powershell
Unblock-File agent-orch-windows-x64.zip
```

Unzip it and run `agent-orch.exe` inside the folder. Keep the folder together. The console window
that opens is the server: close it to stop.

**Windows (from source).** PowerShell, with Python 3.12 (`winget install Python.Python.3.12`) and
Git (`winget install Git.Git`):

```powershell
git clone https://github.com/xnoname79/unity-agent-orchestrator.git
cd unity-agent-orchestrator
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python session_orchestrator.py serve
```

**macOS.** The `python3` built into macOS is 3.9, which is too old. Use Homebrew's:

```bash
brew install python@3.12 git
git clone https://github.com/xnoname79/unity-agent-orchestrator.git
cd unity-agent-orchestrator
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python session_orchestrator.py serve
```

**Linux** (Debian, Ubuntu; Python 3.10 or newer):

```bash
sudo apt install python3 python3-venv git
git clone https://github.com/xnoname79/unity-agent-orchestrator.git
cd unity-agent-orchestrator
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python session_orchestrator.py serve
```

Then open <http://localhost:8992>.

### 3. Connect the agents' tools

In the dashboard, press 🔌 and, under **Built into the orchestrator**, press **Connect** next to
`signal` — and next to `boards`, `library` and `voice` if you want them. Agents already running
pick the tools up after a restart.

Or by hand, once per CLI (same pattern for `boards`, `library` and `voice`):

```bash
claude mcp add --transport http --scope user signal http://127.0.0.1:8992/signal/mcp
codex mcp add signal --url http://127.0.0.1:8992/signal/mcp
agy mcp add signal http://127.0.0.1:8992/signal/mcp
```

### 4. Optional extras

**Media tools**, for agents that edit video and audio, and for the Sounds panel:

```powershell
# Windows
winget install Gyan.FFmpeg
winget install ImageMagick.ImageMagick
```

```bash
# macOS
brew install ffmpeg imagemagick sox exiftool
# Linux
sudo apt install ffmpeg imagemagick sox libimage-exiftool-perl
```

```bash
# yt-dlp, any OS (needed for Sounds; the distro version is usually too old)
python3 -m pip install --upgrade yt-dlp
```

Restart the orchestrator after installing, then check the wrench in the top bar.

**Speak text** needs its voice once: **Tools › Speak text › Download** (350 MB).

### 5. Settings

Put these in a `.env` file next to `session_orchestrator.py` (or next to `agent-orch.exe`):

| Variable | Default | When to set it |
|---|---|---|
| `ORCH_PORT` | `8992` | Port 8992 is taken |
| `CLAUDE_BIN` / `ORCH_CODEX_BIN` / `ORCH_AGY_BIN` | `claude` / `codex` / `agy` | The dashboard cannot find an installed CLI |
| `ORCH_HOST` | `127.0.0.1` | Only to reach it from another machine |

> [!WARNING]
> Agents run shell commands with permissions bypassed, and there is no API key.
> `ORCH_HOST=0.0.0.0` hands that to everyone on your network.

### 6. Keep it running (optional)

Run it as your own user, never root. Replace the paths with where you unzipped or cloned it.

<details>
<summary>Windows — Task Scheduler</summary>

```powershell
$exe = "$HOME\agent-orch-windows-x64\agent-orch.exe"
Register-ScheduledTask -TaskName "AgentOrchestrator" `
  -Action  (New-ScheduledTaskAction -Execute $exe -WorkingDirectory (Split-Path $exe)) `
  -Trigger (New-ScheduledTaskTrigger -AtLogOn) `
  -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable `
               -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1))
```

Remove it with `Unregister-ScheduledTask -TaskName "AgentOrchestrator"`.
</details>

<details>
<summary>macOS — launchd</summary>

```bash
cat > ~/Library/LaunchAgents/com.agent-orch.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.agent-orch</string>
  <key>ProgramArguments</key><array>
    <string>$HOME/unity-agent-orchestrator/.venv/bin/python</string>
    <string>$HOME/unity-agent-orchestrator/session_orchestrator.py</string>
    <string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>$HOME/unity-agent-orchestrator</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/agent-orch.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/agent-orch.err</string>
</dict></plist>
PLIST

launchctl load -w ~/Library/LaunchAgents/com.agent-orch.plist
```

Stop it with `launchctl unload -w ~/Library/LaunchAgents/com.agent-orch.plist`.
</details>

<details>
<summary>Linux — systemd user service</summary>

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/orchestrator.service <<'UNIT'
[Unit]
Description=Agent Orchestrator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/unity-agent-orchestrator
ExecStart=%h/unity-agent-orchestrator/.venv/bin/python %h/unity-agent-orchestrator/session_orchestrator.py serve
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=3
TimeoutStopSec=15

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now orchestrator
loginctl enable-linger "$USER"     # start at boot, not only when you log in
```

Logs: `journalctl --user -u orchestrator -f`.
</details>

### 7. Host the reader for your phone (optional)

Just the PDF reader and Speak text, for several people, on a Linux server with Docker (about 1 GB
of RAM and 2 CPU cores). Nothing else from the orchestrator is on it.

1. Point a domain at the server and open ports 80 and 443.
2. On the server:

   ```bash
   git clone https://github.com/xnoname79/unity-agent-orchestrator.git
   cd unity-agent-orchestrator
   echo "READER_DOMAIN=reader.example.com" > .env
   docker compose -f docker-compose.reader.yml up -d --build
   docker compose -f docker-compose.reader.yml exec reader python reader_server.py adduser alice
   ```

3. On the phone, open `https://reader.example.com`, sign in, then Share › **Add to Home Screen**.

Accounts are made only on the server: `adduser`, `passwd`, `deluser`, `users` (same `exec`
command). Each person gets 1 GB; change it with `READER_QUOTA_MB=` in `.env`. Documents live in the
`reader-data` Docker volume.

> [!WARNING]
> Every account can upload files and use the server's CPU. Only make accounts for people you trust.

### Update

From source: `git pull`, then `pip install -r requirements.txt`, then restart. Prebuilt: download
the new zip and replace the folder, keeping your `.env`. Hosted reader: `git pull`, then
`docker compose -f docker-compose.reader.yml up -d --build`.
