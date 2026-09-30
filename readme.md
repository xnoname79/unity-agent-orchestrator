<a id="readme-top"></a>

[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]

<div align="center">
  <a href="https://github.com/xnoname79/unity-agent-orchestrator">
    <img src="images/logo.png" alt="Logo" width="88">
  </a>

  <h3 align="center">Agent Orchestrator</h3>

  <p align="center">
    Claude Code, Codex and Antigravity CLI, working together on one canvas.
    <br />
    <a href="#usage"><strong>Explore the docs »</strong></a>
    <br />
    <br />
    <a href="../../releases">Download</a>
    &middot;
    <a href="../../issues/new">Report Bug</a>
    &middot;
    <a href="../../issues/new">Request Feature</a>
  </p>
</div>

<details>
  <summary>Table of Contents</summary>
  <ol>
    <li>
      <a href="#about-the-project">About The Project</a>
      <ul><li><a href="#built-with">Built With</a></li></ul>
    </li>
    <li><a href="#screenshots">Screenshots</a></li>
    <li>
      <a href="#getting-started">Getting Started</a>
      <ul>
        <li><a href="#1-install-a-provider-cli">Install a provider CLI</a></li>
        <li><a href="#2-install-the-orchestrator">Install the orchestrator</a></li>
        <li><a href="#3-run-it">Run it</a></li>
        <li><a href="#4-register-the-signal-mcp">Register the signal MCP</a></li>
      </ul>
    </li>
    <li>
      <a href="#run-at-startup">Run at startup</a>
      <ul>
        <li><a href="#linux--systemd-user-service">Linux — systemd</a></li>
        <li><a href="#macos--launchd">macOS — launchd</a></li>
        <li><a href="#windows--task-scheduler">Windows — Task Scheduler</a></li>
      </ul>
    </li>
    <li>
      <a href="#usage">Usage</a>
      <ul>
        <li><a href="#spawn-an-agent">Spawn an agent</a></li>
        <li><a href="#self-filling-playbooks">Self-filling playbooks</a></li>
        <li><a href="#openai-compatible-api">OpenAI-compatible API</a></li>
        <li><a href="#command-line">Command line</a></li>
      </ul>
    </li>
    <li><a href="#configuration">Configuration</a></li>
    <li><a href="#safety">Safety</a></li>
    <li><a href="#contributing">Contributing</a></li>
    <li><a href="#license">License</a></li>
    <li><a href="#contact">Contact</a></li>
  </ol>
</details>

## About The Project

[![Product screenshot][product-screenshot]](images/screenshot-canvas.png)

A harness that puts **agents from different providers on one canvas** and lets them work
together. Claude Code, Codex CLI and Antigravity (Gemini) sessions run side by side, signal each
other, hand off work, and stream their output into a single web UI.

It does not reimplement an agent. It drives the CLIs you already have installed and logged in,
so each provider keeps its own subscription, its own auth, and its own tools.

* **One canvas, many agents.** Every card is a live terminal you can type into. Drag and resize
  them; arrows animate between cards as signals flow.
* **Agents talk to each other, in parallel.** `send_signal(to_role="...")` resolves the role,
  injects the message, and records the run. Different projects run concurrently; two messages to
  the *same* agent queue behind a lock so transcripts never interleave.
* **Two workspaces at once.** Every workspace is a tab; open two and split the window between
  them, browser-style, picking which one goes in each pane. Both canvases stay live.
* **Groups keep the list short.** Name a drawer — *work*, *hobby* — and file workspaces into it.
  Home opens on the groups; pick one and you are back to the workspaces you know. Deleting a
  group deletes the label, never what is filed under it.
* **The project folder is on the canvas.** Browse an agent's files without leaving the
  dashboard — folders, sizes, timestamps, text and image previews — or hand the folder to your
  own file manager with one button.
* **Cards hold the terminal, panels hold the actions.** A card is the agent's terminal plus the
  few controls you reach for while typing; select it and the rest — model, effort, skill, context
  — opens on the right. Ten agents, not ninety buttons.
* **OpenAI-compatible API.** Point any OpenAI client at `/v1` and chat with an agent as if it
  were a model. Streaming included.
* **Dark mode**, a minimap once the canvas outgrows the window, and a single binary with no
  Python install required.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

### Built With

[![Python][python-shield]][python-url]
[![Starlette][starlette-shield]][starlette-url]
[![SQLite][sqlite-shield]][sqlite-url]
[![xterm.js][xterm-shield]][xterm-url]

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Screenshots

| | |
|---|---|
| ![Two workspaces side by side][screenshot-split] | ![Node inspector][screenshot-inspector] |
| Two workspaces, side by side | A selected agent and its inspector |
| ![Embedded terminal][screenshot-terminal] | ![History][screenshot-history] |
| A live CLI inside a card | Signal queue and audit log |

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Getting Started

### 1. Install a provider CLI

The orchestrator drives the CLIs you already have installed and logged in — it does not replace
them. Install at least one.

| | Install |
|---|---|
| **Claude Code** | [code.claude.com/docs/en/quickstart](https://code.claude.com/docs/en/quickstart#native-install-recommended) |
| **Codex CLI** | [learn.chatgpt.com/docs/codex/cli](https://learn.chatgpt.com/docs/codex/cli#getting-started) |
| **Antigravity CLI** | [antigravity.google/docs/cli](https://antigravity.google/docs/cli/reference) — the `agy` command, for Google models |

Nothing else is required. **neovim** + **tmux** are optional and only matter in
[developer mode](#browsing-a-project-and-developer-mode): there they turn the project button into
an editor card on the canvas, and [diffview.nvim](https://github.com/sindrets/diffview.nvim) adds
that card's **git** tab. Override the binaries with `ORCH_NVIM_BIN` / `ORCH_TMUX_BIN`.

> The orchestrator finds the CLIs through the **PATH of its own process**. Install one while it is
> running and you have to restart it. If `which claude` prints a path but the dashboard still says
> the command was not found, set `CLAUDE_BIN` / `ORCH_CODEX_BIN` / `ORCH_AGY_BIN` — see
> [Configuration](#configuration).

### 2. Install the orchestrator

**Prebuilt binary** — no Python needed. Grab it from [Releases](../../releases).

```bash
chmod +x agent-orch-linux-x64
./agent-orch-linux-x64            # no argument = serve
```

On Windows, unzip `agent-orch-windows-x64.zip` and run `agent-orch.exe` from inside the folder.
Keep the folder together — the `_internal` directory beside the `.exe` is the program. The console
window that opens *is* the server; closing it stops the orchestrator.

<details>
<summary>Windows SmartScreen may offer to <b>delete</b> the download</summary>

These builds are not code-signed, so SmartScreen has no reputation for them. Verify the hash
against `SHA256SUMS.txt`, then clear the download mark — do this on the `.zip`, *before*
extracting, since every file inside inherits the mark:

```powershell
Get-FileHash agent-orch-windows-x64.zip -Algorithm SHA256
Unblock-File agent-orch-windows-x64.zip
```

If Defender quarantines it outright, that is a false positive on the PyInstaller bundle — report
it at [Microsoft's submission portal](https://www.microsoft.com/en-us/wdsi/filesubmission). Do not
disable Defender or add an exclusion; the next release is a different file and the exclusion will
not cover it.
</details>

**From source** — Python 3.10 or newer:

```bash
git clone https://github.com/xnoname79/unity-agent-orchestrator.git
cd unity-agent-orchestrator
python3 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Run it

```bash
python3 session_orchestrator.py serve
```

| URL | What |
|---|---|
| `http://localhost:8992/` | Canvas dashboard |
| `http://localhost:8992/docs` | API documentation (Swagger UI) |

The database is created on first run under `~/.session_orch_db/`; there is no migration step.
Change the port with `ORCH_PORT` — see [Configuration](#configuration).

### 4. Register the signal MCP

Once per CLI. This is what lets agents reach each other; every session afterwards picks it up
automatically.

```bash
claude mcp add --transport http --scope user signal http://127.0.0.1:8992/signal/mcp
codex mcp add signal --url http://127.0.0.1:8992/signal/mcp
agy mcp add signal http://127.0.0.1:8992/signal/mcp
```

The signal server runs **in-process** with the orchestrator — no second service, no extra port.

### MCP servers panel

The 🔌 button in the topbar registers any HTTP MCP server for **every** session on the machine —
the same user scope `claude mcp add`, `codex mcp add` and `agy mcp add` write to. It lists what is
already registered, checks each one, and removes them.

Three things it does that the CLIs do not:

- **It writes to every CLI installed here, in one go.** An agent on codex reads
  `~/.codex/config.toml`, an agent on agy reads `~/.gemini/config/mcp_config.json`, and neither
  looks at `~/.claude.json` — so a server added for one engine is a tool that silently does not
  exist for the other two. Each row tags the CLIs that actually have it.
- **The token never reaches a command line.** All three CLIs only accept a header through a flag
  (`--header`, `-H`), so the bearer token ends up in `argv` — readable by `ps`, kept in shell
  history. The panel writes the entry directly instead. Saved tokens are never handed back out;
  the API answers with the last four characters.
- **It proves the server works before saving.** Add calls `tools/list` first; a server that
  refuses the token or does not answer leaves every config **byte-identical**. "Saved" is not a
  status anyone can act on, so the panel reports the tool count the server actually returned.

Codex keeps its MCP servers in the same `config.toml` as your model, effort and per-project trust
levels; only the one `[mcp_servers.<name>]` table is touched. Servers registered as stdio are
listed but not probed — there is no URL to call.

| Env | Default | |
|---|---|---|
| `ORCH_CLAUDE_CONFIG` | `~/.claude.json` | claude's config file |
| `ORCH_CODEX_CONFIG` | `$CODEX_HOME/config.toml` | codex's config file |
| `ORCH_AGY_MCP_CONFIG` | `~/.gemini/config/mcp_config.json` | agy's MCP config file |
| `ORCH_MCP_TIMEOUT` | `6` | seconds to wait for `tools/list` |

Guard: `python3 check_mcp.py`.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Run at startup

Run it as **your own user**, never root — the provider CLIs read their logins from your home
directory. Replace the paths below with wherever you cloned or unzipped it.

### Linux — systemd user service

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/orchestrator.service <<'EOF'
[Unit]
Description=Agent Orchestrator (dashboard + MCP, port 8992)
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
EOF

systemctl --user daemon-reload
systemctl --user enable --now orchestrator
loginctl enable-linger "$USER"     # start at boot, not only when you log in
```

Using the prebuilt binary instead? Point `ExecStart` straight at it:
`ExecStart=%h/agent-orch/agent-orch-linux-x64 serve`, and set `WorkingDirectory` to its folder so
the `.env` beside it is found.

Day to day:

```bash
systemctl --user status orchestrator
systemctl --user restart orchestrator     # after pulling changes
journalctl --user -u orchestrator -f      # live log
```

`Environment=` lines in the unit win over the `.env` file.

### macOS — launchd

```bash
cat > ~/Library/LaunchAgents/com.agent-orch.plist <<EOF
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
EOF

launchctl load -w ~/Library/LaunchAgents/com.agent-orch.plist
```

Stop it with `launchctl unload -w ~/Library/LaunchAgents/com.agent-orch.plist`.

### Windows — Task Scheduler

```powershell
$exe = "$HOME\agent-orch-windows-x64\agent-orch.exe"
Register-ScheduledTask -TaskName "AgentOrchestrator" `
  -Action  (New-ScheduledTaskAction -Execute $exe -WorkingDirectory (Split-Path $exe)) `
  -Trigger (New-ScheduledTaskTrigger -AtLogOn) `
  -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable `
               -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1))
```

Remove it with `Unregister-ScheduledTask -TaskName "AgentOrchestrator"`.

> [!WARNING]
> Starting on boot means the port is open for as long as the machine is up. `ORCH_CORS_ORIGINS`
> defaults to `*` and there is no API key unless you set one — read [Safety](#safety) first.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Usage

### Groups and workspaces

A **workspace** is the isolation boundary: its own folder, its own agents, and signals that never
cross out of it. A **group** is only a drawer you file workspaces into — *work*, *hobby*, one per
client — so Home stays readable once you have a dozen of them.

Home opens on the groups. Pick one and the grid becomes its workspaces, the tab bar lists that
group, and **New workspace** creates inside it. The picker on a workspace card moves it to another
group. Renaming a group renames a label; deleting one deletes the label and re-files its
workspaces under the default group — no workspace, agent, signal or folder is ever removed by it.

Nothing below the dashboard knows groups exist: routing, budgets and isolation all still resolve
by workspace, so filing a workspace somewhere else cannot change who can signal whom.

There is also a workspace literally named `default`. It is not one of yours — it is where the API
puts anything sent without a `workspace_id`, and where a pre-workspaces database was migrated to.
The dashboard hides it while it is empty. Put an agent in it and it reappears, because a hidden
workspace holding agents would mean no terminal and no way to delete them.

Guard: `python3 check_groups.py`.

### Spawn an agent

Use **Spawn agent** on the dashboard. It starts with **where**: pick a group, then one of that
group's workspaces — the form never lists every workspace on the machine at once, which is the
thing groups exist to avoid. The group defaults to whichever one you have open. Then four fields:

* **Role name** — the agent's identity. Signals are routed by it, so it must be unique in the
  workspace. It also names the skill directory the playbook is written to.
* **Playbook template** — which bundled template under `.claude/skills/` seeds the role. Several
  agents can share one; they differ by role name, not by playbook source.
* **Working dir** — the project the agent operates in.
* **Model** — a tab per provider:

  | Tab | Values | Runs on |
  |---|---|---|
  | Claude | `opus`, `sonnet`, `haiku`, `claude-opus-4-8`, … | Claude Code CLI |
  | Codex | `codex` (auto), `codex:gpt-5.6-terra`, `codex:gpt-5.6-luna`, … | Codex CLI |
  | Gemini | `agy` (auto), `agy:gemini-3.1-pro-high`, `agy:gemini-3.8-flash-low`, … | Antigravity CLI |

  The `codex:` and `agy:` prefixes are required — slugs like `gpt-5.6-terra` are also valid
  OpenAI API model names, and `agy models` even lists `claude-*` ones, so without a prefix there
  is no way to tell which CLI you meant.

Reasoning effort uses one ladder across providers, clamped per model rather than failing:
Claude tops out at `max`, `codex:gpt-5.6-terra` at `ultra`, `codex:gpt-5.6-luna` at `max`, the
rest at `xhigh`. Antigravity slugs carry their own level (`…-pro-high`, `…-flash-low`), so for
those the model *is* the setting and no separate effort flag is sent.

### Self-filling playbooks

A template leaves its project-specific parts blank as `<UPPERCASE>` placeholders. When a spawned
agent's `SKILL.md` still contains one, the orchestrator queues a single bootstrap run asking that
agent to survey its working directory and fill the blanks in itself.

The placeholders **are** the one-time flag: once gone, the bootstrap never fires again, so
nothing overwrites a playbook you or the agent has since edited. The result is written to
`.claude/skills/`, `.codex/skills/` and `.agents/skills/`, since each CLI only reads its own.
Antigravity is launched with `--add-dir <project>`, since it takes its workspace from that flag
rather than from the working directory — without it the `.agents` copy is ignored.

Those copies are written when the agent is spawned and when you save its SKILL, so a card created
before a CLI was supported never got that CLI's copy. Such a card shows a 🕮 button that copies the
playbook into whichever roots are missing it — the check runs against the CLI list itself, so
adding a fourth CLI later lights the button on every old card.

Peer routing is deliberately *not* baked into playbooks — the roster changes as agents come and
go, so every signal carries a reminder to call `list_agents` instead of trusting a remembered
role name.

### Browsing a project, and developer mode

The 📁 button on an agent's card opens a **folder card** clipped to the right edge of that
agent's terminal, the two framed as one block: the project folder, browsable in place. Click a
folder to go in, the breadcrumb to come back, a file to read it — text files show as text, images
show as images. Drag either half and the pair moves together; the frame's corner resizes both. It
needs nothing installed and works the same whether the orchestrator runs on your own machine or
on a server you reach over the network.

Everything it reads is locked inside that agent's project folder. Hidden entries (`.git`, `.env`)
are not listed, files are served read-only with a content type the orchestrator picks, and `..`,
an absolute path or a symlink pointing out of the tree are all refused.

The card's ⧉ button opens the same folder in **your computer's own file manager** instead —
Finder, File Explorer, Nautilus — for the things a browser cannot do, like dragging a file out or
opening it in another app. That one only works when the orchestrator runs on the machine you are
sitting at; a headless install will say so rather than doing nothing.

Start the orchestrator with `--dev` (or `ORCH_DEV_MODE=1`) and the 📁 button becomes an **editor
card**: neovim in a terminal, wrapped in tmux so closing the browser tab only detaches, with a
**git** tab that is [diffview.nvim](https://github.com/sindrets/diffview.nvim) side-by-side over
the working tree.

```bash
python3 session_orchestrator.py --dev serve
```

The mode is chosen at startup and there is no switch in the UI. Editor cards live in tmux and
outlive the orchestrator, so a card opened in developer mode is still there after a plain
restart — the flag only decides what the button opens next.

Guard: `python3 check_editor.py`.

### OpenAI-compatible API

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8992/v1", api_key="<ORCH_API_KEY>")

stream = client.chat.completions.create(
    model="<workspace_id>/<agent_alias>",          # e.g. "ws_3b99a7/backend"
    messages=[{"role": "user", "content": "Summarise today's changes"}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

Two ways it differs from OpenAI:

* **It is stateful.** The conversation lives in the CLI's own transcript on the server, so only
  the part **new since the last `assistant` message** is sent to the agent. Resending history
  would duplicate context, not restore it.
* **One request is one real agent run.** It goes through the session lock, the daily cap and the
  audit log. A single turn can take minutes (`ORCH_CHAT_TIMEOUT`, default 900s).

### Command line

```bash
python3 session_orchestrator.py init            # create the database
python3 session_orchestrator.py serve           # dashboard + API + MCP
python3 session_orchestrator.py --dev serve      # …with the nvim editor card
python3 session_orchestrator.py once            # process pending signals once
python3 session_orchestrator.py loop            # polling daemon, no web server
python3 session_orchestrator.py list-sessions   # also: list-signals, list-runs
```

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ORCH_PORT` / `ORCH_HOST` | `8992` / `0.0.0.0` | Where to listen |
| `ORCH_API_KEY` | *(unset)* | Require a key on `/api/*` and `/v1/*` |
| `ORCH_CORS_ORIGINS` | `*` | Allowed browser origins; empty disables CORS |
| `CLAUDE_BIN` / `ORCH_CODEX_BIN` / `ORCH_AGY_BIN` | `claude` / `codex` / `agy` | Paths to the provider CLIs |
| `ORCH_AGY_HOME` | `~/.gemini/antigravity-cli` | Where the Antigravity CLI keeps its conversations |
| `ORCH_DEFAULT_EFFORT` | `high` | Reasoning effort when a session sets none |
| `ORCH_MAX_CONCURRENT` | `3` | Agent runs in flight at once |
| `ORCH_CHAT_TIMEOUT` | `900` | Seconds one `/v1` turn may take |
| `ORCH_DRY_RUN` | `0` | Simulate runs without calling any CLI |
| `ORCH_DEV_MODE` | `0` | Open projects in an nvim card instead of the OS file manager (same as `--dev`) |

Values can also live in a `.env` file placed **next to the executable** — on Windows that means
inside the unzipped folder, beside `agent-orch.exe`. Full list in the header of
[session_orchestrator.py](session_orchestrator.py).

**Windows:** the embedded terminal runs on ConPTY and needs Windows 10 1809 or newer. The
prebuilt build bundles it; from source, `pip install pywinpty`. `GET /health` reports
`embedded_terminal`, and `embedded_terminal_reason` when it is unavailable.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Safety

> [!WARNING]
> `ORCH_CORS_ORIGINS` defaults to `*` and there is no API key unless you set one. Agents here run
> shell commands with permissions bypassed, so that combination lets **any website you visit**
> drive them. Set `ORCH_API_KEY`, or narrow `ORCH_CORS_ORIGINS`, before exposing the port.

Built to run many agents unattended without runaway loops:

* **Approval gate** — signals marked `requires_approval` wait for a human on the dashboard.
* **Ping-pong cap** — a pair of agents gets a bounded number of exchanges per task; the budget
  reopens when a human gives new work.
* **Daily cap** — `ORCH_MAX_RUNS_PER_DAY` blocks a session until you press **Allow +N**.
* **Kill switch** — global and per-workspace, from the dashboard.
* **Per-session lock** — one prompt in flight per session, so transcripts never mix.
* **Audit log** — every injection recorded with status, token count and the full event stream.
* **Workspaces** — each tenant gets an isolated folder; every session's `cwd` is pinned inside
  it, and signals never cross a workspace boundary.

**Codex sandbox.** Measured on Codex CLI 0.147.0: in headless `codex exec`, MCP tool calls only
run under `--dangerously-bypass-approvals-and-sandbox`. Every other combination returns *"user
cancelled MCP tool call"* — so a sandboxed Codex agent **cannot signal**. The orchestrator maps
`permission_mode` directly and prints a warning to the timeline when signalling is off, rather
than letting the hand-off fail silently.

**Antigravity permissions.** Measured on agy 1.1.26: headless `agy -p` has no one to ask, so any
tool needing permission is auto-denied — including `call_mcp_tool`, and therefore signalling —
while the run still reports success with an empty answer. The orchestrator passes
`--dangerously-skip-permissions` when a session's `permission_mode` is `bypassPermissions` (the
default), warns on the timeline when it is not, and surfaces the `denied_actions` agy reports.

The sandbox restricts **writes and network, not reads**. Directory boundaries between agents are
a convention in their prompts, not a kernel-enforced wall.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Contributing

Issues and pull requests are welcome.

1. Fork the project
2. Create your branch (`git checkout -b feat/amazing-feature`)
3. Commit your changes (`git commit -m 'feat: add amazing feature'`)
4. Push and open a pull request

The dashboard is vanilla JS with no build step. Before opening a PR that touches it, run:

```bash
python3 static/orchestrator/check_ui.py
```

It catches what breaks silently in a UI wired by string ids: a `$("id")` with no element, an
`onclick` calling a function that was never exported, a missing icon, a colour hardcoded outside
the theme tokens. CI runs it too.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## License

No licence has been chosen yet, so default copyright applies — the source is public to read, but
not yet to reuse. A `LICENSE` file will settle this.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Contact

Project link: [xnoname79/unity-agent-orchestrator](https://github.com/xnoname79/unity-agent-orchestrator) ·
[open an issue](../../issues)

<p align="right">(<a href="#readme-top">back to top</a>)</p>

[contributors-shield]: https://img.shields.io/github/contributors/xnoname79/unity-agent-orchestrator.svg?style=for-the-badge
[contributors-url]: https://github.com/xnoname79/unity-agent-orchestrator/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/xnoname79/unity-agent-orchestrator.svg?style=for-the-badge
[forks-url]: https://github.com/xnoname79/unity-agent-orchestrator/network/members
[stars-shield]: https://img.shields.io/github/stars/xnoname79/unity-agent-orchestrator.svg?style=for-the-badge
[stars-url]: https://github.com/xnoname79/unity-agent-orchestrator/stargazers
[issues-shield]: https://img.shields.io/github/issues/xnoname79/unity-agent-orchestrator.svg?style=for-the-badge
[issues-url]: https://github.com/xnoname79/unity-agent-orchestrator/issues

[python-shield]: https://img.shields.io/badge/Python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54
[python-url]: https://www.python.org/
[starlette-shield]: https://img.shields.io/badge/Starlette-1f2937?style=for-the-badge
[starlette-url]: https://www.starlette.io/
[sqlite-shield]: https://img.shields.io/badge/SQLite-07405e?style=for-the-badge&logo=sqlite&logoColor=white
[sqlite-url]: https://www.sqlite.org/
[xterm-shield]: https://img.shields.io/badge/xterm.js-0c0e12?style=for-the-badge
[xterm-url]: https://xtermjs.org/

[product-screenshot]: images/screenshot-canvas.png
[screenshot-split]: images/screenshot-split.png
[screenshot-inspector]: images/screenshot-inspector.png
[screenshot-terminal]: images/screenshot-terminal.png
[screenshot-history]: images/screenshot-history.png
