# My MCP Servers

Collection of MCP (Model Context Protocol) servers for Claude Code.

---

## sync-bridge

Real-time API spec synchronization between BE and FE Claude sessions.

```
┌──────────┐     ┌──────────┐     ┌──────────┐
│ BE Claude │     │ FE-1     │     │ FE-2     │
└─────┬─────┘     └─────┬────┘     └─────┬────┘
      │                 │                 │
      └────── HTTP ─────┼────── HTTP ─────┘
                        │
                ┌───────▼───────┐
                │  Sync-Bridge  │   1 server, 1 port
                │  HTTP Server  │   unlimited projects
                └───────┬───────┘
                        │
        ┌───────────────┼───────────────┐
   ~/.sync_bridge_db/   │               │
   ├── app-a.db         │          app-b.db
   └── app-c.db         │
```

### Quick Start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Start server (one time, handles all projects)
python3 main.py

# 3. Setup any project (run inside each project directory)
./setup.sh --project my-app                          # BE (sees all specs)
./setup.sh --project my-app --tag user-app           # FE user app
./setup.sh --project my-app --tag admin-app          # FE admin app
```

### What setup.sh does

1. Downloads sync-bridge rules (CLAUDE.md) into your project
2. Configures project name and tag automatically
3. Registers the MCP server (`claude mcp add --transport http`)

### Manual Setup (without setup.sh)

```bash
# Register MCP in your Claude session
claude mcp add --transport http sync-bridge http://localhost:8989/mcp

# Add to your project's CLAUDE.md:
# When using sync-bridge MCP, always use project="my-app" and tag="user-app" for all tool calls.
```

### Tools Reference

| Tool | Description | Key Args |
|------|-------------|----------|
| `add_api_requirement` | Create new API spec | `project`, `endpoint`, `method`, `description`, `tag` |
| `get_pending_requirements` | List active (non-done) specs | `project`, `status`, `tag` |
| `list_api_requirements` | List all specs | `project`, `tag` |
| `update_api_requirement` | Update a spec by id | `project`, `id`, `endpoint`, `method`, `description`, `status`, `tag` |
| `reset_api_requirements` | Clear all specs (auto-backup) | `project` |
| `watch_for_changes` | Block until changes arrive (long-poll) | `project`, `since`, `timeout` |
| `get_change_log` | View recent changes | `project`, `limit` |
| `list_projects` | List all projects | - |

### Status Lifecycle

```
pending → discuss → confirmed → done
```

- **pending** — New, waiting for the other side
- **discuss** — Back-and-forth discussion (needs info, feedback, changes)
- **confirmed** — Both sides agreed, ready to implement
- **done** — Implemented and integrated

### Real-time Sync (watch_for_changes)

Both BE and FE can wait for and trigger updates — the flow is symmetric:

```
BE: watch_for_changes(project="my-app")           # blocks, waiting...
FE: add_api_requirement(project="my-app", ...)     # triggers BE
BE: receives change, processes it
BE: update_api_requirement(project="my-app", ...)  # triggers FE
FE: watch_for_changes(project="my-app", since=...) # receives change
```

### Example: 1 BE, 2 FE Apps

```bash
# Setup
./setup.sh --project blog --tag user-app      # in fe-user-app/
./setup.sh --project blog --tag admin-app     # in fe-admin-app/
./setup.sh --project blog                     # in be-blog/

# BE creates specs for specific apps
add_api_requirement(project="blog", endpoint="/api/posts", method="GET",
                    description="...", tag="user-app")
add_api_requirement(project="blog", endpoint="/api/posts", method="DELETE",
                    description="...", tag="admin-app")
add_api_requirement(project="blog", endpoint="/api/auth", method="POST",
                    description="...")  # shared (no tag)

# Each FE only sees its own specs
get_pending_requirements(project="blog", tag="user-app")   # FE-1
get_pending_requirements(project="blog", tag="admin-app")  # FE-2
```

### Server Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `SYNC_HOST` | `0.0.0.0` | Bind address |
| `SYNC_PORT` | `8989` | Port |
| `DB_FILE` | `~/.sync_bridge_db/<project>.db` | Override DB path |

Health check: `curl http://localhost:8989/health`

### Run as a Service (systemd, auto-start on login)

Instead of running `python3 main.py` manually, register a **systemd user service** so
the server starts automatically on login and restarts itself if it crashes.

Create `~/.config/systemd/user/agent-sync-bridge.service`:

```ini
[Unit]
Description=Agent-Sync-Bridge MCP Server (streamable-http)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/thanh/my-mcp
ExecStart=/home/thanh/my-mcp/.venv/bin/python /home/thanh/my-mcp/main.py
Restart=on-failure
RestartSec=3
# Optional: override host/port
# Environment=SYNC_HOST=0.0.0.0
# Environment=SYNC_PORT=8989

[Install]
WantedBy=default.target
```

Enable and start it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now agent-sync-bridge   # start now + on every login
```

> **Note:** A user service starts on **login**. To run even before logging in
> (e.g. right after boot), enable lingering: `sudo loginctl enable-linger $USER`

#### Common commands

```bash
systemctl --user status agent-sync-bridge        # check status
systemctl --user restart agent-sync-bridge       # reload after editing main.py
systemctl --user stop agent-sync-bridge          # stop the server
systemctl --user start agent-sync-bridge         # start the server
systemctl --user disable agent-sync-bridge       # remove from auto-start
journalctl --user -u agent-sync-bridge -f        # follow logs (live)
journalctl --user -u agent-sync-bridge -n 50     # last 50 log lines
```

---

## issue-fetcher

GitHub issues and PR management.

```bash
# Refer to https://github.com/settings/tokens to create your token
claude mcp add \
  -e GITHUB_TOKEN=github_token \
  -- issue-fetcher /path/to/python /path/to/github_issues.py
```

---

## sync-docs

Docusaurus documentation generator.

```bash
cd dynamic-docs && npm install

claude mcp add \
  -e DOCS_DB_FILE=/path/to/docs_db.json \
  -e DOCS_PROJECT_DIR=/path/to/dynamic-docs \
  -- sync-docs /path/to/python /path/to/docusaurus_docs.py
```
