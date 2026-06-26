```sh
pip install -r requirements.txt

# ─── sync-bridge (shared HTTP server) ───────────────────────────────
# Start the server once — both BE and FE connect to the same instance.
# The server auto-triggers FE when BE updates API specs (and vice versa).

# 1. Start the sync-bridge HTTP server
python3 /path/to/main.py my-project
# DB auto-created at ~/.sync_bridge_db/my-project.db
# Server runs at http://localhost:8989/mcp
# Health check: curl http://localhost:8989/health

# 2. In BE's Claude session — connect to the shared server
claude mcp add --transport http sync-bridge http://localhost:8989/mcp

# 3. In FE's Claude session — connect to the same server
claude mcp add --transport http sync-bridge http://localhost:8989/mcp

# Optional env vars:
#   SYNC_HOST  — bind address (default: 0.0.0.0)
#   SYNC_PORT  — port (default: 8989)
#   DB_FILE    — override DB path (default: ~/.sync_bridge_db/<name>.db)

# ─── issue-fetcher ──────────────────────────────────────────────────
# Refer to this link to create your own github_token https://github.com/settings/tokens
claude mcp add \
-e GITHUB_TOKEN=github_token \
-- issue-fetcher /path/to/python /path/to/github_issues.py

# ─── sync-docs ──────────────────────────────────────────────────────
cd dynamic-docs && npm install

claude mcp add \
-e DOCS_DB_FILE=/path/to/docs_db.json \
-e DOCS_PROJECT_DIR=/path/to/dynamic-docs \
-- sync-docs /path/to/python /path/to/docusaurus_docs.py
```
