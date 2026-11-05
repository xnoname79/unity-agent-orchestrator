-- Key behaviour of the editor card: real vim modes by default, with the owner's mappings, plus a
-- `Turn off Vim modes` switch to typing-first (no modes, VS Code keys). Typing-first follows
-- $VIMRUNTIME/scripts/mswin.vim, but it is not sourced because it cannot be undone at runtime.
local M = {}
local api = vim.api

local STATE_FILE = vim.fn.stdpath('state') .. '/no-vim-modes'
local tools = {}
local tree_hidden = false       -- the user hid the sidebar: resizing must not bring it back
local saved_cursor               -- where the insert cursor was, because leaving insert moves it

function M.vim_keys() return vim.g.orch_vim_keys == true end

local function tree() return require('nvim-tree.api').tree end

-- Typing-first: editable buffers are always in insert mode -------------------------------------
local function editable()
  local bo = vim.bo
  return bo.buftype == 'terminal' or (bo.buftype == '' and bo.modifiable and not bo.readonly)
end

local function back_to_typing()
  if M.vim_keys() then return end
  local mode = api.nvim_get_mode().mode
  -- A mouse click carries insert mode into whatever window it lands on.
  if mode == 'i' and not editable() and vim.bo.buftype ~= 'prompt' then return vim.cmd.stopinsert() end
  if mode ~= 'n' or not editable() then return end
  if saved_cursor and saved_cursor.buf == api.nvim_get_current_buf() then
    pcall(api.nvim_win_set_cursor, 0, saved_cursor.pos)
  end
  saved_cursor = nil
  vim.cmd.startinsert()
end

local group = api.nvim_create_augroup('OrchTyping', {})
api.nvim_create_autocmd({ 'ModeChanged' }, { group = group, pattern = '*:n', callback = function()
  vim.schedule(back_to_typing)
end })
api.nvim_create_autocmd({ 'BufEnter', 'WinEnter', 'TermOpen' }, { group = group, callback = function()
  vim.schedule(back_to_typing)
end })
api.nvim_create_autocmd('InsertLeavePre', { group = group, callback = function()
  saved_cursor = { buf = api.nvim_get_current_buf(), pos = api.nvim_win_get_cursor(0) }
end })

-- Closing a tab -------------------------------------------------------------------------------
local function drop_buffer(buf)
  -- Every window showing it moves to another file first, so closing never collapses the layout.
  local others = vim.tbl_filter(function(b)
    return b ~= buf and vim.bo[b].buflisted
  end, api.nvim_list_bufs())
  local replacement = others[#others] or api.nvim_create_buf(true, false)
  for _, win in ipairs(vim.fn.win_findbuf(buf)) do api.nvim_win_set_buf(win, replacement) end
  pcall(api.nvim_buf_delete, buf, { force = true })
end

local function save_buffer(buf)
  if api.nvim_buf_get_name(buf) == '' then
    return M.save_as(function() drop_buffer(buf) end)
  end
  api.nvim_buf_call(buf, function() vim.cmd('silent write') end)
  drop_buffer(buf)
end

-- A small dialog that answers to the mouse, which nvim's own confirm() does not.
local function ask_unsaved(buf)
  local name = vim.fn.fnamemodify(api.nvim_buf_get_name(buf), ':t')
  name = name ~= '' and name or 'this new file'
  local title = (' Save the changes to %s? '):format(name)
  local choices = { { ' Save ', function() save_buffer(buf) end },
                    { " Don't Save ", function() drop_buffer(buf) end },
                    { ' Cancel ', function() end } }
  local row = ''
  local spans = {}
  for _, c in ipairs(choices) do
    spans[#spans + 1] = { from = #row, to = #row + #c[1] + 2, run = c[2] }
    row = row .. '[' .. c[1] .. ']  '
  end
  local lines = { title, '', ' Your changes are lost if you do not save them.', '', ' ' .. row }
  local width = 0
  for _, l in ipairs(lines) do width = math.max(width, vim.fn.strdisplaywidth(l)) end
  local dlg = api.nvim_create_buf(false, true)
  api.nvim_buf_set_lines(dlg, 0, -1, false, lines)
  vim.bo[dlg].modifiable, vim.bo[dlg].bufhidden = false, 'wipe'
  vim.bo[dlg].filetype = 'orchdialog'
  local win = api.nvim_open_win(dlg, true, {
    relative = 'editor', style = 'minimal', border = 'rounded', width = width + 1, height = #lines,
    row = math.floor((vim.o.lines - #lines) / 2) - 1, col = math.floor((vim.o.columns - width) / 2),
  })
  vim.cmd.stopinsert()
  local function finish(run)
    if api.nvim_win_is_valid(win) then api.nvim_win_close(win, true) end
    vim.schedule(run)
  end
  local function map(lhs, fn, modes)
    vim.keymap.set(modes or 'n', lhs, fn, { buffer = dlg, nowait = true })
  end
  map('<LeftRelease>', function()
    local at = vim.fn.getmousepos()
    if at.winid ~= win or at.line ~= #lines then return end
    for _, s in ipairs(spans) do
      if at.column - 2 > s.from and at.column - 2 <= s.to then return finish(s.run) end
    end
  end, { 'n', 'i' })
  map('s', function() finish(choices[1][2]) end)
  map('<CR>', function() finish(choices[1][2]) end)
  map('d', function() finish(choices[2][2]) end)
  map('<Esc>', function() finish(choices[3][2]) end)
  map('c', function() finish(choices[3][2]) end)
end

function M.close_buffer(buf)
  buf = (buf and buf > 0) and buf or api.nvim_get_current_buf()
  if not api.nvim_buf_is_valid(buf) then return end
  if vim.bo[buf].modified then return ask_unsaved(buf) end
  drop_buffer(buf)
end

function M.save_as(after)
  vim.ui.input({ prompt = 'Save as: ', completion = 'file' }, function(path)
    if not path or path == '' then return end
    vim.cmd('silent saveas ' .. vim.fn.fnameescape(path))
    if after then after() end
  end)
end

function M.save()
  if api.nvim_buf_get_name(0) == '' then return M.save_as() end
  local ok, err = pcall(vim.cmd, 'silent write')
  if not ok then vim.notify('Could not save: ' .. tostring(err), vim.log.levels.ERROR) end
end

-- Sidebar -------------------------------------------------------------------------------------
local function is_sidebar(win)
  local ft = vim.bo[api.nvim_win_get_buf(win)].filetype
  return ft == 'NvimTree' or ft == 'orchscm'
end

local function focus_editor()
  for _, win in ipairs(api.nvim_tabpage_list_wins(0)) do
    if api.nvim_win_get_config(win).relative == '' and not is_sidebar(win) then
      api.nvim_set_current_win(win)
      return
    end
  end
end
M.focus_editor = focus_editor

function M.focus_tree()
  if not tree().is_visible() then tree().open({ current_window = false }) end
  tree().focus()
end

function M.new_file()
  vim.ui.input({ prompt = 'New file: ', completion = 'file' }, function(path)
    if path and path ~= '' then vim.cmd.edit(vim.fn.fnameescape(path)) end
  end)
end

function M.toggle_sidebar()
  if M.scm_open then return vim.cmd.OrchFiles() end
  if tree().is_visible() then
    tree_hidden = true
    tree().close()
  else
    tree_hidden = false
    tree().open({ current_window = false })
    focus_editor()
  end
end

-- A narrow card keeps the room for the file; widening brings the tree back.
function M.fit_sidebar()
  if vim.o.columns < 70 then
    if tree().is_visible() then tree().close() end
  elseif not tree_hidden and not tree().is_visible() and #api.nvim_list_tabpages() == 1
    and not M.scm_open then
    tree().open({ current_window = false })
    focus_editor()
  elseif tree().is_visible() then
    tree().resize()
  end
end

-- The sidebar does not count as a window to stay in: quitting the last file quits nvim, as in plain
-- vim, so `:q` and ZZ close the card. If nvim refuses (unsaved text), the sidebar comes back.
local function quit_with_last_file()
  local cur = api.nvim_get_current_win()
  if #api.nvim_list_tabpages() > 1 or is_sidebar(cur) or api.nvim_win_get_config(cur).relative ~= '' then
    return
  end
  local sidebars = {}
  for _, win in ipairs(api.nvim_tabpage_list_wins(0)) do
    if is_sidebar(win) then
      sidebars[#sidebars + 1] = win
    elseif win ~= cur and api.nvim_win_get_config(win).relative == '' then
      return
    end
  end
  for _, win in ipairs(sidebars) do api.nvim_win_close(win, true) end
  if #sidebars > 0 then
    vim.schedule(function()
      if M.scm_open then vim.cmd.OrchGit() else M.fit_sidebar() end
    end)
  end
end

function M.on_start()
  M.fit_sidebar()
  focus_editor()
  vim.schedule(back_to_typing)
end

function M.show_files()
  tree_hidden = false
  M.scm_open = false
  if vim.o.columns >= 70 and not tree().is_visible() then
    tree().open({ current_window = false })
  end
  focus_editor()
end

-- The tree stays clickable and small: a newcomer pressing a letter here must not delete anything.
function M.tree_attach(bufnr)
  local api_tree = require('nvim-tree.api')
  if M.vim_keys() then
    api_tree.config.mappings.default_on_attach(bufnr)
  else
    local function map(lhs, fn, desc)
      vim.keymap.set('n', lhs, fn, { buffer = bufnr, desc = desc, nowait = true })
    end
    map('<CR>', api_tree.node.open.edit, 'Open')
    map('<Right>', api_tree.node.open.edit, 'Open')
    map('<Left>', api_tree.node.navigate.parent_close, 'Close folder')
    map('<F2>', api_tree.fs.rename, 'Rename')
    map('<Del>', api_tree.fs.remove, 'Delete')
    map('a', api_tree.fs.create, 'New file')
  end
  -- In both styles, because the browser keeps Ctrl+W for itself.
  vim.keymap.set('n', '<Esc>', focus_editor, { buffer = bufnr, desc = 'Back to the editor', nowait = true })
  -- One click opens, as in VS Code. nvim's click already moved the cursor onto the row. Then the
  -- keyboard goes back to the file: nothing in the tree expects typing.
  vim.keymap.set({ 'n', 'i' }, '<LeftRelease>', function()
    if vim.fn.getmousepos().winid ~= api.nvim_get_current_win() then return end
    if api_tree.tree.get_node_under_cursor() then api_tree.node.open.edit() end
    if vim.bo.filetype == 'NvimTree' and not M.vim_keys() then focus_editor() end
  end, { buffer = bufnr })
end

-- Keys Vim does not need, in both styles. Unmapped, Ctrl+Z suspends nvim, even after an operator,
-- and a tmux pane has no shell to resume it.
local NIXS = { 'n', 'i', 'x', 's' }
local both_maps = {
  { NIXS, '<C-s>', function() M.save() end },
  { 'n', '<C-p>', function() tools.find_files() end },
  { { 'n', 'i' }, '<C-b>', function() M.toggle_sidebar() end },
  { NIXS, '<M-w>', function() M.close_buffer() end },
  { NIXS, '<F3>', function() M.focus_tree() end },
  { NIXS, '<M-.>', '<Cmd>bnext<CR>' },
  { NIXS, '<M-,>', '<Cmd>bprevious<CR>' },
  { { 'n', 'i' }, '<C-z>', '<Cmd>silent! undo<CR>' },
  { { 'x', 's' }, '<C-z>', '<Esc><Cmd>silent! undo<CR>' },
  { 'o', '<C-z>', '<Esc>' },
  { 'n', '<C-_>', 'gcc', { remap = true } },
  { 'i', '<C-_>', '<Cmd>normal gcc<CR>' },
  { 's', '<C-_>', '<C-g>gcgv<C-g>', { remap = true } },
  { 'x', '<C-_>', 'gcgv', { remap = true } },
}

local function set_map(m)
  vim.keymap.set(m[1], m[2], m[3], vim.tbl_extend('force', { silent = true }, m[4] or {}))
end

-- Typing-first keymap -------------------------------------------------------------------------

-- Select mode runs a mapping's right-hand side as typed text unless it starts with <C-g>, which
-- flips to Visual for the command. The trailing gv<C-g> brings the selection back.
local function pum(if_open, otherwise)
  return function() return vim.fn.pumvisible() == 1 and if_open or otherwise end
end

local function select_all(prefix)
  return function() return editable() and (prefix .. 'gggH<C-o>G') or '' end
end

local function with_undo_break(key)
  return key .. '<C-g>u'
end

M.typing_maps = {
  { { 'i', 'x', 's' }, '<C-p>', function() tools.find_files() end },
  { NIXS, '<C-f>', function() require('telescope.builtin').current_buffer_fuzzy_find() end },
  { { 'x', 's' }, '<C-b>', function() M.toggle_sidebar() end },
  { { 'n', 'i' }, '<C-y>', '<Cmd>silent! redo<CR>' },
  { { 'x', 's' }, '<C-y>', '<Esc><Cmd>silent! redo<CR>' },
  { 'i', '<C-a>', select_all('<C-\\><C-N>'), { expr = true } },
  { 'n', '<C-a>', select_all(''), { expr = true } },
  { { 'x', 's' }, '<C-a>', '<Esc>gggH<C-o>G' },

  -- Clipboard. With a selection VS Code keeps it after a copy; without one it takes the line.
  { 's', '<C-c>', '<C-g>"+ygv<C-g>' },
  { 'x', '<C-c>', '"+ygv' },
  { 's', '<C-x>', '<C-g>"+d' },
  { 'x', '<C-x>', '"+d' },
  { { 'n', 'i' }, '<C-c>', function() vim.fn.setreg('+', vim.fn.getline('.') .. '\n', 'V') end },
  { 'i', '<C-x>', '<Cmd>normal! "+dd<CR>' },

  -- Tab indents a selection; a completion menu takes Tab first.
  { 'i', '<Tab>', pum('<C-n>', '<Tab>'), { expr = true } },
  { 'i', '<S-Tab>', pum('<C-p>', '<C-d>'), { expr = true } },
  { 's', '<Tab>', '<C-g>>gv<C-g>' },
  { 's', '<S-Tab>', '<C-g><gv<C-g>' },
  { 'x', '<Tab>', '>gv' },
  { 'x', '<S-Tab>', '<gv' },

  -- Keys that would leave typing. Esc only closes a completion menu.
  { 'i', '<Esc>', pum('<C-e>', ''), { expr = true } },
  { 'i', '<Insert>', '<Nop>' },
  { 'i', '<C-o>', '<Nop>' },
  { 'i', '<C-q>', '<Nop>' },
  { 'i', '<C-BS>', '<C-w>' },

  -- Undo steps by word and line, not one step for everything typed since the file opened.
  { 'i', '<Space>', with_undo_break('<Space>') },
  { 'i', '<CR>', pum('<C-y>', with_undo_break('<CR>')), { expr = true } },
}

local function apply_typing(on)
  for _, m in ipairs(M.typing_maps) do
    if on then
      set_map(m)
    else
      pcall(vim.keymap.del, m[1], m[2])
    end
  end
  local o = vim.o
  o.selectmode = on and 'mouse,key' or ''
  o.keymodel = on and 'startsel,stopsel' or ''
  o.selection = on and 'exclusive' or 'inclusive'
  o.virtualedit = on and 'onemore' or ''
  o.whichwrap = on and 'b,s,<,>,[,]' or 'b,s'
  o.clipboard = on and '' or 'unnamedplus'
end

-- The dashboard reads the title, which also passes through tmux: orch:<key style>:<head tab>.
-- That is how it knows who owns Ctrl+V and which head tab to light, even after a reload. `vim` means
-- Ctrl+V selects a block here, so nvim gets the key; in any other mode the browser pastes.
function M.update_title()
  local view = (M.scm_open or #api.nvim_list_tabpages() > 1) and 'git' or 'edit'
  local mode = api.nvim_get_mode().mode:sub(1, 1)
  local block = M.vim_keys() and (mode == 'n' or mode == 'v' or mode == 'V' or mode == '\22')
  vim.o.titlestring = ('orch:%s:%s'):format(block and 'vim' or 'typing', view)
end

-- A click on a tab leaves any diff or panel first, so the file lands in the editor window.
function M.switch_tab(buf)
  if buf == api.nvim_get_current_buf() then return end
  if #api.nvim_list_tabpages() > 1 then vim.cmd.OrchFiles() end
  focus_editor()
  if vim.bo[buf].buflisted then api.nvim_set_current_buf(buf) end
end

-- Owner's vim mappings, present in both styles because they only exist in normal mode ----------
local function owner_maps()
  local function n(lhs, rhs, desc) vim.keymap.set('n', lhs, rhs, { silent = true, desc = desc }) end
  local function tele(picker) return function() require('telescope.builtin')[picker]() end end
  local function gs(fn) return function() require('gitsigns')[fn]() end end
  n('<leader>e', '<Cmd>NvimTreeFindFileToggle<CR>')
  n('<leader>ff', function() tools.find_files() end)
  n('<leader>fg', function() tools.live_grep() end)
  n('<leader>fb', tele('buffers'))
  n('<leader>fh', tele('help_tags'))
  n('<leader>x', '<Cmd>bdelete<CR>')
  n('<leader><leader>', '<Cmd>noh<CR>')
  n('<leader>gs', tele('git_status'))
  -- These five used vim-fugitive, which is not bundled (no licence file in its repo).
  n('<leader>gg', '<Cmd>OrchGit<CR>')
  n('<leader>gc', '<Cmd>botright split | terminal git commit<CR>')
  n('<leader>gd', gs('diffthis'))
  n('<leader>gl', tele('git_commits'))
  n('<leader>gB', gs('blame'))
end

local function vim_only_maps(on)
  local maps = { { 'n', '<Tab>', '<Cmd>bnext<CR>' },
                 { 'n', '<S-Tab>', '<Cmd>bprevious<CR>' },
                 { 'x', '<C-c>', '"+y' } }
  for _, m in ipairs(maps) do
    if on then set_map(m) else pcall(vim.keymap.del, m[1], m[2]) end
  end
end

-- Completion keys from the owner's config, without nvim-cmp.
local function completion_maps()
  vim.keymap.set('i', '<C-Space>', function() vim.lsp.completion.get() end, { silent = true })
end

function M.set_vim_keys(on, quiet)
  vim.g.orch_vim_keys = on
  M.update_title()
  if on then
    apply_typing(false)
    vim_only_maps(true)
    vim.keymap.set('i', '<Tab>', pum('<C-n>', '<Tab>'), { expr = true })
    vim.keymap.set('i', '<S-Tab>', pum('<C-p>', '<S-Tab>'), { expr = true })
    vim.keymap.set('i', '<CR>', pum('<C-y>', '<CR>'), { expr = true })
    vim.cmd.stopinsert()
  else
    pcall(vim.keymap.del, 'i', '<S-Tab>')
    vim_only_maps(false)
    apply_typing(true)
    vim.schedule(back_to_typing)
  end
  if quiet then return end
  if on then
    vim.fn.delete(STATE_FILE)
  else
    vim.fn.mkdir(vim.fs.dirname(STATE_FILE), 'p')
    vim.fn.writefile({}, STATE_FILE)
  end
  -- The tree's key set is chosen when it attaches.
  if tree().is_visible() then
    tree().close()
    tree().open({ current_window = false })
    focus_editor()
  end
  vim.notify(on and 'Vim modes on. Press i to type, Esc to stop.'
    or 'Vim modes off: just type. Press F1 and pick Use Vim modes to switch back.')
end

-- F1: what can I do here? ---------------------------------------------------------------------
local function actions_list()
  local function terminal() vim.cmd('botright split | terminal') end
  local on = M.vim_keys()
  local function keys(vim_style, typing) return on and vim_style or typing end
  return {
    { 'Save', keys('Ctrl+S or :w', 'Ctrl+S'), function() M.save() end },
    { 'Save as…', '', function() M.save_as() end },
    { 'Find a file', 'Ctrl+P', function() tools.find_files() end },
    { 'Search all files', 'Ctrl+Shift+F or F4', function() tools.live_grep() end },
    { 'Find in this file', keys('/', 'Ctrl+F'), function() require('telescope.builtin').current_buffer_fuzzy_find() end },
    { 'Show or hide the sidebar', 'Ctrl+B', function() M.toggle_sidebar() end },
    { 'Show source control', 'Source Control tab', function() vim.cmd.OrchGit() end },
    { 'Undo', keys('u or Ctrl+Z', 'Ctrl+Z'), function() vim.cmd('silent! undo') end },
    { 'Redo', keys('Ctrl+R', 'Ctrl+Y'), function() vim.cmd('silent! redo') end },
    -- In Vim style Ctrl+A would add one to a number under the cursor.
    { 'Select all', keys('ggVG', 'Ctrl+A'), function()
      api.nvim_feedkeys(vim.keycode(keys('<C-\\><C-n>ggVG', '<C-a>')), keys('n', 'm'), false)
    end },
    { 'Copy, cut, paste', keys('y, d, p, plus Ctrl+Shift+V', 'Ctrl+C, Ctrl+X, Ctrl+V'), function() end },
    { 'Comment a line or selection', keys('gc or Ctrl+/', 'Ctrl+/'), function() vim.cmd('normal gcc') end },
    { 'Indent, outdent', keys('> and <', 'Tab, Shift+Tab'), function() end },
    { 'Next tab, previous tab', 'Alt+. and Alt+,', function() vim.cmd.bnext() end },
    { 'Close this tab', 'Alt+W or click its ×', function() M.close_buffer() end },
    { 'New file', '', function() M.new_file() end },
    { 'Work in the file tree', keys('F3, then a, r, d, Esc', 'F3, then A, F2, Delete, Esc'), function() M.focus_tree() end },
    { 'Open a terminal', '', terminal },
    { keys('Turn off Vim modes', 'Use Vim modes'), '', function() M.set_vim_keys(not on) end },
  }
end

function M.show_actions()
  local conf = require('telescope.config').values
  local items = actions_list()
  local width = 0
  for _, it in ipairs(items) do width = math.max(width, vim.fn.strdisplaywidth(it[1])) end
  require('telescope.pickers').new({}, {
    prompt_title = 'Actions',
    sorting_strategy = 'ascending',
    layout_strategy = 'center',
    layout_config = { width = math.min(80, vim.o.columns - 4), height = math.min(26, vim.o.lines - 4) },
    previewer = false,
    finder = require('telescope.finders').new_table({
      results = items,
      entry_maker = function(it)
        local pad = string.rep(' ', width - vim.fn.strdisplaywidth(it[1]) + 3)
        return { value = it, display = it[1] .. pad .. it[2], ordinal = it[1] .. ' ' .. it[2] }
      end,
    }),
    sorter = conf.generic_sorter({}),
    attach_mappings = function(prompt)
      local actions = require('telescope.actions')
      local state = require('telescope.actions.state')
      actions.select_default:replace(function()
        local entry = state.get_selected_entry()
        actions.close(prompt)
        if entry then vim.schedule(entry.value[3]) end
      end)
      return true
    end,
  }):find()
end

function M.setup(found)
  tools = found
  owner_maps()
  completion_maps()
  for _, m in ipairs(both_maps) do set_map(m) end
  vim.keymap.set({ 'n', 'i', 'x', 's', 't' }, '<F1>', function() M.show_actions() end, { silent = true })
  -- F20 is what the dashboard sends for Ctrl+Shift+F, which xterm has no code for.
  for _, key in ipairs({ '<F20>', '<F4>' }) do
    vim.keymap.set({ 'n', 'i', 'x', 's' }, key, function() tools.live_grep() end, { silent = true })
  end
  api.nvim_create_user_command('OrchVimModes', function() M.set_vim_keys(not M.vim_keys()) end, {})
  M.set_vim_keys(vim.uv.fs_stat(STATE_FILE) == nil, true)
  api.nvim_create_autocmd('ModeChanged', { callback = function() M.update_title() end })
  api.nvim_create_autocmd('QuitPre', { callback = quit_with_last_file })
  api.nvim_create_autocmd('VimResized', { callback = M.fit_sidebar })
  api.nvim_create_autocmd({ 'TabNew', 'TabClosed', 'TabEnter' }, {
    callback = function() vim.schedule(M.update_title) end,
  })
end

return M
