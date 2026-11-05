# Bundled editor plugins

Plain copies under `pack/orch/start/`. No submodules, and nothing needs a compiler or an install step.
Removed from each copy: `.git`, dotfiles, tests, images and developer scripts. Kept: source, docs and
licence files.

| Plugin | Repository | Commit | Licence | Licence file |
|---|---|---|---|---|
| nvim-tree.lua | https://github.com/nvim-tree/nvim-tree.lua | `69eb0c34ed3d836d5a0d92d913b77dd73b054f2f` | GPL-3.0-or-later | `pack/orch/start/nvim-tree.lua/LICENSE` |
| nvim-web-devicons | https://github.com/nvim-tree/nvim-web-devicons | `ec309a0eaff6ea967e58c275996155ab5c16938d` | MIT | `pack/orch/start/nvim-web-devicons/LICENSE` |
| telescope.nvim | https://github.com/nvim-telescope/telescope.nvim | `40aedd8a68c78a656a10a8d62d80c54af59420fb` | MIT | `pack/orch/start/telescope.nvim/LICENSE` |
| plenary.nvim | https://github.com/nvim-lua/plenary.nvim | `74b06c6c75e4eeb3108ec01852001636d85a932b` | MIT | `pack/orch/start/plenary.nvim/LICENSE` |
| gitsigns.nvim | https://github.com/lewis6991/gitsigns.nvim | `4bcd99167c265f135886b282baad77afe06206b2` | MIT | `pack/orch/start/gitsigns.nvim/LICENSE` |
| diffview.nvim | https://github.com/sindrets/diffview.nvim | `4516612fe98ff56ae0415a259ff6361a89419b0a` | GPL-3.0-or-later | `pack/orch/start/diffview.nvim/LICENSE` |
| lualine.nvim | https://github.com/nvim-lualine/lualine.nvim | `221ce6b2d999187044529f49da6554a92f740a96` | MIT | `pack/orch/start/lualine.nvim/LICENSE` |
| vscode.nvim | https://github.com/Mofiqul/vscode.nvim | `6439ed89d0e10193f07e92268a321d7c6f6e93ee` | MIT | `pack/orch/start/vscode.nvim/LICENSE.md` |

`nvim-tree.lua` and `diffview.nvim` are GPL-3.0-or-later; GitHub shows them as unrecognised because
their `LICENSE` files hold the short GPL notice rather than the full text. The GPL allows
redistribution when the source and licence travel with the files. These are Lua source, their notices
stay in place, and the full licence text is in `GPL-3.0.txt`. nvim runs them as plugins in its own
process, so they ship alongside the orchestrator rather than as part of its MIT-licensed code.

## Icon font

`static/orchestrator/vendor/fonts/SymbolsNerdFontMono-Regular.woff2` is `SymbolsNerdFontMono-Regular.ttf`
from `NerdFontsSymbolsOnly.zip` of https://github.com/ryanoasis/nerd-fonts (font version 3.5.1), converted
to WOFF2 with no other change. Licence: MIT, in `SymbolsNerdFont-LICENSE.txt` next to it.

## Patches

None. `diffview.nvim` has had no commit since 2024; `check_editor.py` (live part) proves it opens and
shows a repo on the pinned nvim.

## Left out of the owner's config

- telescope-fzf-native, nvim-treesitter: need a compiler or an install step.
- nvim-lspconfig, nvim-cmp and its sources, vim-vsnip: replaced by nvim's built-in LSP and completion.
- vim-fugitive, vim-surround: the repos hold no licence file. `<leader>gg`, `gc`, `gd`, `gl` and `gB`
  still work, through `:OrchGit`, a `git commit` terminal, gitsigns and telescope.
- auto-pairs, nerdcommenter, dracula: not bundled. Comments use nvim's built-in `gc`.
