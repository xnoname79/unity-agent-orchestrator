# Unity Game Dev MCP

MCP servers for Unity 3D game development with Claude Code.

---

## unity-dev

Game dev planning tools for Unity 3D (story, scenes, assets, GDD, C# templates).

```bash
pip install -r requirements.txt
python3 unity_dev.py          # starts on port 8990
claude mcp add --transport http unity-dev http://localhost:8990/mcp
```

DB auto-created per game project at `~/.unity_dev_db/<project>.db`.

---

## unity-mcp (3D environment design)

Direct Unity scene manipulation (GameObjects, lighting, materials, screenshots)
via [Coplay unity-mcp](https://github.com/CoplayDev/unity-mcp). Works alongside
`unity-dev`: unity-dev plans, unity-mcp executes in the Editor.

See **[docs/unity-mcp.md](docs/unity-mcp.md)** for setup and
**[memory/unity-mcp/CLAUDE.md](memory/unity-mcp/CLAUDE.md)** for art-direction rules.
