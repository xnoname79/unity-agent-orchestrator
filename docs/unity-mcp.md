# Unity MCP — 3D Environment Design (Coplay)

Setup and integration guide for [Coplay unity-mcp](https://github.com/CoplayDev/unity-mcp)
so Claude can operate directly on a Unity scene: place objects, adjust lighting,
materials, take screenshots and self-critique — a real art-direction loop.

## Two complementary "brains"

```
┌─────────────────────────┐        ┌──────────────────────────┐
│  unity-dev (this repo)   │        │  unity-mcp (Coplay)      │
│  = PLANNING BRAIN        │        │  = EXECUTING HANDS       │
│                          │        │                          │
│  • GDD, story, scenes    │  ───►  │  • Create/move objects   │
│  • asset tracking        │  plan  │  • Lighting, fog, PP     │
│  • C# script templates   │        │  • Materials, prefab     │
│                          │  ◄───  │  • Screenshot scene view │
│  (metadata, SQLite)      │ verify │  (real Unity operations) │
└─────────────────────────┘        └──────────────────────────┘
```

- **unity-dev** answers "what to build" (plan, story, asset list).
- **unity-mcp** performs "build it" in the Unity Editor and **captures it back for Claude to see**.

## Requirements

| Component | Version |
|-----------|---------|
| Unity | 2021.3 LTS → 6.x |
| Python | 3.10+ (via [`uv`](https://docs.astral.sh/uv/)) |
| MCP client | Claude Code |

## Setup (on a machine with Unity)

### 1. Install `uv` (package manager for the Python server)

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 2. Install the Unity package (bridge running in the Editor)

In the Unity Editor: **Window → Package Manager → + → Add package from git URL**, paste:

```
https://github.com/CoplayDev/unity-mcp.git?path=/MCPForUnity#main
```

> Pin a stable release: replace `#main` with `#v10.0.0`.
> Or use OpenUPM: `openupm add com.coplaydev.unity-mcp`

### 3. Auto-register with Claude Code

In the Unity Editor: **Window → MCP for Unity → Configure All Detected Clients**.

This command automatically writes the MCP server config into Claude Code (the bridge
downloads the Python server via `uv` and runs it over stdio transport — no manual
start needed).

### 4. Verify

Open Claude Code in the Unity project folder and ask: *"list the current scene hierarchy"*.
If Claude can read the object tree in the scene → the connection is OK.

## unity-mcp toolset (47 tool entrypoints)

Main capability groups (see the [tool catalog](https://coplaydev.github.io/unity-mcp/reference/tools/)):

| Group | Used for |
|-------|----------|
| Scene & GameObject | Create/delete/move objects, set transform, parent, instantiate prefab |
| Components | Add/config component (Light, AudioSource, Collider, ...) |
| C# Scripts | Create/edit scripts directly in the project |
| Assets | Manage/import assets, materials |
| Menu & Console | Run menu items, read console logs |
| Screenshot | **Capture scene/game view** — Claude's eyes for art-direction |
| Test & Build | Run tests, build the game |

## Suggested workflow

1. **Plan** (unity-dev): `get_gdd`, `list_scenes` → know which scene to build, what mood.
2. **Blockout** (unity-mcp): create rough geometry, place objects per layout.
3. **Screenshot** (unity-mcp): capture it.
4. **Critique & iterate**: Claude looks at the image → adjusts lighting/fog/composition → recaptures.
5. **Track** (unity-dev): `update_scene` status `in_progress`→`done`, update assets.