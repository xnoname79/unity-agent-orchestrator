-- Bundled config for the dashboard's editor card: nvim that looks like VS Code, with vim modes.
-- Started with `NVIM_APPNAME=orch-nvim nvim -u <this file>`, so it never reads the user's own
-- nvim config. Everything is vendored under pack/orch/start; nothing needs a compiler or install.
local root = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ':p:h')

-- ORCH_NVIM_BIN may point at an old nvim. The plugins would only print errors, so say why instead.
if vim.fn.has('nvim-0.12') == 0 then
  vim.o.loadplugins = false
  vim.o.mouse = 'a'
  vim.schedule(function()
    vim.api.nvim_echo({ { 'This nvim is too old for the editor card. '
      .. 'Install nvim 0.12+ and set ORCH_NVIM_BIN to it.', 'WarningMsg' } }, true, {})
  end)
  return
end

vim.opt.runtimepath:prepend(root)
vim.opt.packpath:prepend(root)
vim.g.mapleader = ' '
vim.g.loaded_netrw, vim.g.loaded_netrwPlugin = 1, 1

-- Options -------------------------------------------------------------------------------------
local o = vim.o
o.tabstop, o.softtabstop, o.shiftwidth, o.expandtab = 4, 4, 4, true
o.smarttab, o.autoindent = true, true
o.incsearch, o.ignorecase, o.smartcase, o.hlsearch = true, true, true, true
o.wildmenu, o.wildmode = true, 'longest,list,full'
o.number, o.signcolumn, o.cursorline = true, 'yes', true
o.hidden, o.splitright, o.splitbelow = true, true, true
o.mouse = 'a'
o.updatetime, o.termguicolors, o.encoding = 100, true, 'utf-8'
o.completeopt = 'menu,menuone,noselect'
o.laststatus, o.showtabline = 3, 2
o.showmode, o.ruler, o.showcmd = false, false, false
o.fillchars = 'eob: ,vert:│'
o.title, o.titlelen = true, 0
vim.opt.shortmess:append('IWAcF')
vim.api.nvim_create_autocmd('FileType', {
  pattern = { 'html', 'css', 'xml', 'json', 'yaml', 'markdown' },
  callback = function() vim.bo.shiftwidth, vim.bo.tabstop, vim.bo.softtabstop = 2, 2, 2 end,
})

-- A leftover swap file must not stop a newcomer at a wall of text. The file opens normally and the
-- message says how to get the older text back.
vim.api.nvim_create_autocmd('SwapExists', {
  callback = function()
    vim.v.swapchoice = 'e'
    vim.schedule(function()
      vim.notify('An earlier session left unsaved changes for this file. '
        .. 'Run :recover to load them.', vim.log.levels.WARN)
    end)
  end,
})

-- Clipboard: copying sends OSC 52 to the browser. The browser never answers a read (the handler in
-- app.js is write-only), so pasting from a register returns what nvim last copied, with no wait.
-- Pasting from the browser arrives as a bracketed paste, not through this provider.
local osc52 = require('vim.ui.clipboard.osc52')
local copied = {}
local function copy(reg)
  local send = osc52.copy(reg)
  return function(lines, regtype)
    copied[reg] = { lines, regtype }
    send(lines)
  end
end
local function paste(reg) return function() return copied[reg] or { {}, 'v' } end end
vim.g.clipboard = {
  name = 'orch-osc52',
  copy = { ['+'] = copy('+'), ['*'] = copy('*') },
  paste = { ['+'] = paste('+'), ['*'] = paste('*') },
}

vim.cmd.packloadall()

-- Theme ---------------------------------------------------------------------------------------
vim.o.background = 'dark'
require('vscode').setup({
  group_overrides = {
    NvimTreeGitDirtyIcon = { fg = '#E2C08D' }, NvimTreeGitFileDirtyHL = { fg = '#E2C08D' },
    NvimTreeGitFolderDirtyHL = { fg = '#E2C08D' }, NvimTreeGitStagedIcon = { fg = '#E2C08D' },
    NvimTreeGitFileStagedHL = { fg = '#E2C08D' }, NvimTreeGitFolderStagedHL = { fg = '#E2C08D' },
    NvimTreeGitNewIcon = { fg = '#73C991' }, NvimTreeGitFileNewHL = { fg = '#73C991' },
    NvimTreeGitFolderNewHL = { fg = '#73C991' }, NvimTreeGitRenamedIcon = { fg = '#73C991' },
    NvimTreeGitFileRenamedHL = { fg = '#73C991' }, NvimTreeGitFolderRenamedHL = { fg = '#73C991' },
    NvimTreeGitDeletedIcon = { fg = '#C74E39' }, NvimTreeGitFileDeletedHL = { fg = '#C74E39' },
    NvimTreeGitFolderDeletedHL = { fg = '#C74E39' },
    TabLineFill = { bg = '#181818' },
    OrchTabBack = { fg = '#ffffff', bg = '#007acc' },
  },
})
require('vscode').load('dark')

-- File tree -----------------------------------------------------------------------------------
local keys = require('orch.keys')

local function tree_width() return math.max(20, math.min(34, math.floor(vim.o.columns * 0.24))) end

require('nvim-tree').setup({
  on_attach = keys.tree_attach,
  git = { enable = true, ignore = false },
  view = { width = tree_width },
  filters = { dotfiles = false, git_ignored = false, custom = { '^\\.git$' } },
  actions = { open_file = { window_picker = { enable = false } } },
  renderer = {
    group_empty = true,
    root_folder_label = ':t',
    highlight_git = 'name',
    special_files = {},
    icons = {
      git_placement = 'right_align',
      glyphs = { git = { unstaged = 'M', staged = 'M', unmerged = '!', renamed = 'R',
                         untracked = 'U', deleted = 'D', ignored = '' } },
    },
  },
  -- Same as the owner's: the tree follows the open file, but stays rooted at the project.
  update_focused_file = { enable = true, update_root = false },
})

-- Find and search -----------------------------------------------------------------------------
local actions = require('telescope.actions')
local has = function(bin) return vim.fn.executable(bin) == 1 end
-- Typing-first: Esc leaves the picker. With Vim modes it is the usual switch to normal mode.
local function picker_esc(prompt)
  if keys.vim_keys() then vim.cmd.stopinsert() else actions.close(prompt) end
end

require('telescope').setup({
  defaults = {
    vimgrep_arguments = has('rg')
        and { 'rg', '--color=never', '--no-heading', '--with-filename', '--line-number',
              '--column', '--smart-case' }
      or { 'git', 'grep', '--no-index', '--exclude-standard', '-n', '--column', '-I', '-i',
           '--color=never' },
    mappings = { i = { ['<Esc>'] = picker_esc } },
    sorting_strategy = 'ascending',
    layout_config = { prompt_position = 'top' },
  },
})

-- Skipped when walking without rg/fd, as in the source control search for repos.
local WALK_SKIP = { ['.git'] = true, node_modules = true, ['.venv'] = true, venv = true,
                    __pycache__ = true, dist = true, build = true, target = true, vendor = true }
local function walk_into(dir) return not WALK_SKIP[vim.fs.basename(dir)] end

-- shortcut: stops at 20000 files, enough to pick from; install rg for bigger trees.
local function walk_files()
  local out, cap = {}, 20000
  for name, kind in vim.fs.dir('.', { depth = 12, skip = walk_into }) do
    if kind == 'file' then
      out[#out + 1] = name
      if #out >= cap then break end
    end
  end
  return out
end

local function find_files()
  local builtin = require('telescope.builtin')
  if has('rg') then
    return builtin.find_files({ find_command = { 'rg', '--files', '--hidden', '--glob', '!.git' } })
  elseif has('fd') then
    return builtin.find_files({ find_command = { 'fd', '--type', 'f', '--hidden', '--exclude', '.git' } })
  end
  local conf = require('telescope.config').values
  require('telescope.pickers').new({}, {
    prompt_title = 'Find Files',
    finder = require('telescope.finders').new_table({
      results = walk_files(),
      entry_maker = require('telescope.make_entry').gen_from_file({}),
    }),
    sorter = conf.file_sorter({}),
    previewer = conf.file_previewer({}),
  }):find()
end

local function live_grep()
  if not (has('rg') or has('git')) then
    return vim.notify('Search needs git or ripgrep (rg) on this machine.', vim.log.levels.WARN)
  end
  require('telescope.builtin').live_grep()
end

keys.setup({ find_files = find_files, live_grep = live_grep })

-- Status bar and tab bar ----------------------------------------------------------------------
local bar = { fg = '#ffffff', bg = '#007acc' }
local function bar_mode(fg, bg)
  return { a = { fg = fg, bg = bg, gui = 'bold' }, b = bar, c = bar, x = bar, y = bar, z = bar }
end

-- Each tab: name, then its own click target. A modified file shows a dot in place of the ×.
local function tab_fmt(name, tab)
  -- lualine adds the focused window's buffer even when it is not a file. Those have nothing to close.
  local kind = vim.bo[tab.bufnr].buftype
  if (kind ~= '' and kind ~= 'terminal') or tab.filetype == 'NvimTree' then return name end
  local mark = vim.bo[tab.bufnr].modified and '●' or '×'
  return string.format('%s %%%d@OrchCloseTab@%s%%T', name, tab.bufnr, mark)
end

vim.cmd([[
  function! OrchCloseTab(bufnr, clicks, button, modifiers)
    call v:lua.require'orch.keys'.close_buffer(a:bufnr)
  endfunction
  function! OrchTabBack(minwid, clicks, button, modifiers)
    OrchBack
  endfunction
]])

-- lualine paints the gap with its status colour, so the tab bar paints its own and adds the way
-- back from a diff here.
local function tab_fill()
  local back = #vim.api.nvim_list_tabpages() > 1
      and '%#OrchTabBack#%@OrchTabBack@ ← Back %T' or ''
  return '%#TabLineFill#%=' .. back
end

local function spaces()
  return (vim.bo.expandtab and 'Spaces: ' or 'Tab Size: ') .. vim.fn.shiftwidth()
end

-- lualine says V-LINE and V-BLOCK; say what vim says.
local function mode_label()
  return (require('lualine.utils.mode').get_mode():gsub('^V%-', 'VISUAL '))
end

-- Shown to a newcomer until the first insert of this nvim session.
local HINT = 'Press i to type · Esc to stop · F1 for actions'
local hint_done = false
vim.api.nvim_create_autocmd('InsertEnter', { once = true, callback = function() hint_done = true end })

-- lualine truncates at the start of c or x, which cuts Ln/Col first, so whatever does not fit hides.
-- Room left after the mode label, the branch with a small diff, and Ln/Col.
local function bar_room()
  local branch = require('lualine.components.branch.git_branch').get_branch()
  return vim.o.columns - (keys.vim_keys() and #mode_label() + 2 or 0) - (branch ~= '' and #branch + 2 or 0) - 6 - 16
end

local function show_hint()
  local bo = vim.bo
  return not hint_done and keys.vim_keys() and vim.api.nvim_get_mode().mode == 'n'
    and bo.buftype == '' and bo.modifiable and not bo.readonly
    and bar_room() >= vim.fn.strdisplaywidth(HINT) + 2
end

-- Spaces, encoding, line ending and file type together take about 32 columns.
local function bar_has_room()
  return bar_room() - (show_hint() and vim.fn.strdisplaywidth(HINT) + 2 or 0) >= 32
end

require('lualine').setup({
  options = {
    theme = { normal = bar_mode('#ffffff', '#005a9e'), insert = bar_mode('#1e1e1e', '#73c991'),
              visual = bar_mode('#1e1e1e', '#e5a23c'), replace = bar_mode('#ffffff', '#c74e39'),
              command = bar_mode('#1e1e1e', '#d7ba7d'), inactive = bar_mode('#ffffff', '#007acc') },
    globalstatus = true,
    always_divide_middle = false,
    section_separators = '', component_separators = '',
    refresh = { tabline = 200, statusline = 200 },
  },
  sections = {
    lualine_a = { { mode_label, cond = keys.vim_keys } },
    lualine_b = { { 'branch', icon = '' }, 'diff', 'diagnostics' },
    lualine_c = { { function() return HINT end, cond = show_hint } },
    lualine_x = { function() return 'Ln ' .. vim.fn.line('.') .. ', Col ' .. vim.fn.virtcol('.') end,
                  { spaces, cond = bar_has_room } },
    lualine_y = { { 'encoding', fmt = string.upper, cond = bar_has_room }, { 'fileformat',
      symbols = { unix = 'LF', dos = 'CRLF', mac = 'CR' }, cond = bar_has_room } },
    lualine_z = { { 'filetype', cond = function() return vim.bo.buftype == '' and bar_has_room() end } },
  },
  tabline = {
    lualine_a = { {
      'buffers', mode = 0, show_filename_only = true, fmt = tab_fmt, max_length = function()
        -- lualine counts the close mark's click code as visible width.
        local tabs = #vim.fn.getbufinfo({ buflisted = 1 })
        return vim.o.columns - 10 + 22 * tabs
      end,
      symbols = { modified = '', alternate_file = '' },
      filetype_names = { NvimTree = 'Files', orchscm = 'Source Control', TelescopePrompt = 'Search',
                         DiffviewFiles = 'Changes', DiffviewFileHistory = 'History' },
      buffers_color = { active = { fg = '#ffffff', bg = '#1f1f1f' },
                        inactive = { fg = '#9d9d9d', bg = '#2d2d2d' } },
    } },
    lualine_c = { { tab_fill, padding = 0 } },
  },
})

-- Replaces lualine's own tab click, which would open the file in whichever window has focus.
vim.cmd([[
  function! LualineSwitchBuffer(bufnr, mouseclicks, mousebutton, modifiers)
    call v:lua.require'orch.keys'.switch_tab(a:bufnr)
  endfunction
]])

-- Git -----------------------------------------------------------------------------------------
require('gitsigns').setup({
  signs = {
    add = { text = '│' }, change = { text = '│' },
    delete = { text = '_' }, topdelete = { text = '‾' }, changedelete = { text = '~' },
  },
  on_attach = function(bufnr)
    local gs = require('gitsigns')
    local function map(l, r, d) vim.keymap.set('n', l, r, { buffer = bufnr, desc = d }) end
    map(']c', function() gs.nav_hunk('next') end, 'Next hunk')
    map('[c', function() gs.nav_hunk('prev') end, 'Prev hunk')
    map('<leader>hp', gs.preview_hunk, 'Preview hunk')
    map('<leader>hs', gs.stage_hunk, 'Stage hunk')
    map('<leader>hr', gs.reset_hunk, 'Reset hunk')
    map('<leader>hu', gs.undo_stage_hunk or gs.stage_hunk, 'Undo stage hunk')
    map('<leader>hb', function() gs.blame_line({ full = true }) end, 'Blame line')
    map('<leader>hd', gs.diffthis, 'Diff this')
  end,
})

local function diff_keys()
  local back = { 'n', 'q', '<Cmd>OrchBack<CR>', { desc = 'Back' } }
  return { view = { back }, file_panel = { back }, file_history_panel = { back } }
end
require('diffview').setup({ keymaps = diff_keys() })
require('orch.scm').setup()

-- Language servers: built-in LSP, started only for a server that is already on PATH -----------
local SERVERS = {
  pyright = { cmd = { 'pyright-langserver', '--stdio' }, filetypes = { 'python' },
              root_markers = { 'pyproject.toml', 'setup.py', 'requirements.txt', '.git' } },
  gopls = { cmd = { 'gopls' }, filetypes = { 'go', 'gomod', 'gowork' },
            root_markers = { 'go.work', 'go.mod', '.git' } },
  ts_ls = { cmd = { 'typescript-language-server', '--stdio' },
            filetypes = { 'javascript', 'javascriptreact', 'typescript', 'typescriptreact' },
            root_markers = { 'tsconfig.json', 'package.json', '.git' } },
  rust_analyzer = { cmd = { 'rust-analyzer' }, filetypes = { 'rust' },
                    root_markers = { 'Cargo.toml', '.git' } },
  clangd = { cmd = { 'clangd' }, filetypes = { 'c', 'cpp' },
             root_markers = { 'compile_commands.json', '.git' } },
}
for name, cfg in pairs(SERVERS) do
  if has(cfg.cmd[1]) then
    vim.lsp.config(name, cfg)
    vim.lsp.enable(name)
  end
end
vim.api.nvim_create_autocmd('LspAttach', {
  callback = function(ev)
    local client = vim.lsp.get_client_by_id(ev.data.client_id)
    if client and client:supports_method('textDocument/completion') then
      vim.lsp.completion.enable(true, client.id, ev.buf, { autotrigger = true })
    end
    local function map(l, r) vim.keymap.set('n', l, r, { buffer = ev.buf }) end
    map('gd', vim.lsp.buf.definition)
    map('gr', vim.lsp.buf.references)
    map('K', vim.lsp.buf.hover)
    map('<leader>rn', vim.lsp.buf.rename)
    map('<leader>ca', vim.lsp.buf.code_action)
    map('<leader>f', function() vim.lsp.buf.format() end)
    map('[d', function() vim.diagnostic.jump({ count = -1 }) end)
    map(']d', function() vim.diagnostic.jump({ count = 1 }) end)
  end,
})
vim.diagnostic.config({ virtual_text = { prefix = '●' } })

-- Layout on start: tree on the left, cursor in the editor.
vim.api.nvim_create_autocmd('VimEnter', { nested = true, callback = keys.on_start })
