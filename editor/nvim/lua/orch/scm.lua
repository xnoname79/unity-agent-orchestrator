-- The Files and Source Control tabs. Source Control finds every git repo under the project: one
-- repo goes straight to diffview, several get a panel listing each repo and its changed files.
local M = {}
local api = vim.api
local keys = require('orch.keys')

local SKIP = { node_modules = true, venv = true, vendor = true, dist = true, build = true,
               target = true, __pycache__ = true, bower_components = true, site_packages = true }
local MAX_DEPTH, MAX_DIRS = 4, 5000      -- bound the walk so a huge folder cannot hang the editor

local function norm(path) return vim.fs.normalize(vim.fn.fnamemodify(path, ':p')):gsub('/$', '') end

-- Every repo: the one holding cwd, plus any folder under cwd with a .git folder or file.
function M.discover(cwd)
  local found, seen = {}, {}
  local function add(path)
    path = norm(path)
    if not seen[path] then
      seen[path] = true
      found[#found + 1] = path
    end
  end
  local top = vim.fn.systemlist({ 'git', '-C', cwd, 'rev-parse', '--show-toplevel' })
  if vim.v.shell_error == 0 and top[1] then add(top[1]) end

  local queue, visited = { { cwd, 0 } }, 0
  while #queue > 0 and visited < MAX_DIRS do
    local dir, depth = unpack(table.remove(queue, 1))
    visited = visited + 1
    local handle = vim.uv.fs_scandir(dir)
    while handle do
      local name, kind = vim.uv.fs_scandir_next(handle)
      if not name then break end
      if name == '.git' then
        add(dir)
      elseif kind == 'directory' and depth < MAX_DEPTH and not SKIP[name] and name:sub(1, 1) ~= '.' then
        queue[#queue + 1] = { dir .. '/' .. name, depth + 1 }
      end
    end
  end
  table.sort(found)
  return found
end

-- Parses `git status --porcelain=v1 -z --branch`. Fields end in NUL, and a rename carries the old
-- name as one extra field, which is why this cannot be split on lines.
function M.parse_status(out)
  local branch, files = '', {}
  local fields = vim.split(out, '\0', { plain = true })
  local i = 1
  while i <= #fields do
    local f = fields[i]
    if f:sub(1, 3) == '## ' then
      branch = f:sub(4)
      branch = branch:gsub('^No commits yet on ', ''):gsub('%.%.%..*$', ''):gsub(' %[.*%]$', '')
    elseif #f > 3 then
      local xy, path = f:sub(1, 2), f:sub(4)
      local x, y = xy:sub(1, 1), xy:sub(2, 2)
      local file = { path = path, letter = xy == '??' and 'U' or (x ~= ' ' and x or y) }
      if x == 'R' or x == 'C' or y == 'R' or y == 'C' then
        i = i + 1
        file.from = fields[i]
      end
      files[#files + 1] = file
    end
    i = i + 1
  end
  return branch, files
end

-- Panel ---------------------------------------------------------------------------------------
local panel = { buf = nil, rows = {}, repos = {} }

local COLOURS = { M = '#E2C08D', A = '#73C991', U = '#73C991', R = '#73C991', C = '#73C991',
                  D = '#C74E39', T = '#E2C08D' }

local function set_highlights()
  for letter, fg in pairs(COLOURS) do api.nvim_set_hl(0, 'OrchScm' .. letter, { fg = fg }) end
  api.nvim_set_hl(0, 'OrchScmRepo', { fg = '#cccccc', bold = true })
  api.nvim_set_hl(0, 'OrchScmDim', { fg = '#8b8b8b' })
end

local function label(repo, cwd)
  if repo == cwd then return vim.fn.fnamemodify(repo, ':t') end
  local rel = vim.fs.relpath(cwd, repo)
  return rel or vim.fn.fnamemodify(repo, ':t')
end

local function draw(message)
  local buf = panel.buf
  if not (buf and api.nvim_buf_is_valid(buf)) then return end
  local cwd = norm(vim.fn.getcwd())
  local lines, marks = { ' SOURCE CONTROL', message or ' Click to see changes · R refresh', '' }, {}
  panel.rows = {}
  local function push(text, group)
    lines[#lines + 1] = text
    if group then marks[#marks + 1] = { #lines - 1, group } end
    return #lines
  end
  marks[#marks + 1] = { 0, 'OrchScmRepo' }
  marks[#marks + 1] = { 1, 'OrchScmDim' }
  for _, repo in ipairs(panel.repos) do
    local count = #repo.files
    local state = repo.error and repo.error
      or count == 0 and 'clean' or (count == 1 and '1 change' or (count .. ' changes'))
    local row = push((' %s  %s  %s'):format(label(repo.path, cwd), repo.branch or '', state), 'OrchScmRepo')
    panel.rows[row] = { repo = repo }
    for _, file in ipairs(repo.files) do
      local name = file.from and (file.path .. ' ← ' .. file.from) or file.path
      row = push(('   %s  %s'):format(file.letter, name))
      marks[#marks + 1] = { row - 1, 'OrchScm' .. file.letter, 3, 4 }
      panel.rows[row] = { repo = repo, file = file }
    end
    push('')
  end
  vim.bo[buf].modifiable = true
  api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].modifiable = false
  local ns = api.nvim_create_namespace('orchscm')
  api.nvim_buf_clear_namespace(buf, ns, 0, -1)
  for _, m in ipairs(marks) do
    api.nvim_buf_set_extmark(buf, ns, m[1], m[3] or 0, {
      end_col = m[4] or #lines[m[1] + 1], hl_group = m[2], strict = false,
    })
  end
end

local function close_diffs()
  local ok, lib = pcall(require, 'diffview.lib')
  if not ok then return end
  for _, view in ipairs(vim.list_extend({}, lib.views)) do
    view:close()
    lib.dispose_view(view)
  end
end

-- diffview runs these flags through expand(), which would execute a file named `cmd` from a
-- cloned repo. It also resolves --selected-file against cwd, not -C, so the path must be absolute.
local function flag_value(path) return (path:gsub('([\\%%#*?%[$`<~])', '\\%1')) end

local function open_diff(repo, file)
  local args = { '-C' .. flag_value(repo.path) }
  if file then args[#args + 1] = '--selected-file=' .. flag_value(repo.path .. '/' .. file.path) end
  require('diffview').open(args)
end

local function open_row(row)
  local hit = panel.rows[row]
  if hit then open_diff(hit.repo, hit.file) end
end

local function refresh(done)
  local pending = #panel.repos
  if pending == 0 then
    draw()
    if done then done() end
    return
  end
  for _, repo in ipairs(panel.repos) do
    vim.system({ 'git', '-C', repo.path, 'status', '--porcelain=v1', '-z', '--branch', '-uall' },
      { text = false, timeout = 15000 }, vim.schedule_wrap(function(res)
        if res.code == 0 then
          repo.error = nil
          repo.branch, repo.files = M.parse_status(res.stdout)
          -- With -uall the only folder a status can name is an embedded repo, which is not a change.
          repo.files = vim.tbl_filter(function(f) return f.path:sub(-1) ~= '/' end, repo.files)
        else
          repo.branch, repo.files, repo.error = '', {}, 'git status failed'
        end
        pending = pending - 1
        if pending == 0 then
          draw()
          if done then done() end
        end
      end))
  end
end

local function open_panel(message)
  if not (panel.buf and api.nvim_buf_is_valid(panel.buf)) then
    panel.buf = api.nvim_create_buf(false, true)
    local b = panel.buf
    vim.bo[b].filetype, vim.bo[b].bufhidden = 'orchscm', 'hide'
    api.nvim_buf_set_name(b, 'Source Control')
    local function map(lhs, fn) vim.keymap.set('n', lhs, fn, { buffer = b, nowait = true }) end
    map('<CR>', function() open_row(vim.fn.line('.')) end)
    vim.keymap.set({ 'n', 'i' }, '<LeftRelease>', function()
      local at = vim.fn.getmousepos()
      if at.winid == api.nvim_get_current_win() then open_row(at.line) end
    end, { buffer = b })
    map('R', function() refresh() end)
    map('r', function() refresh() end)
    map('q', function() vim.cmd.OrchFiles() end)
    map('<Esc>', function() keys.focus_editor() end)
  end
  local tree = require('nvim-tree.api').tree
  if tree.is_visible() then tree.close() end
  local win
  for _, w in ipairs(api.nvim_tabpage_list_wins(0)) do
    if api.nvim_win_get_buf(w) == panel.buf then win = w end
  end
  if not win then
    vim.cmd('topleft vsplit')
    win = api.nvim_get_current_win()
    api.nvim_win_set_buf(win, panel.buf)
    api.nvim_win_set_width(win, math.max(30, math.min(46, math.floor(vim.o.columns * 0.36))))
    local wo = vim.wo[win]
    wo.number, wo.signcolumn, wo.winfixwidth, wo.wrap = false, 'no', true, false
    wo.cursorline, wo.foldcolumn = true, '0'
  end
  api.nvim_set_current_win(win)
  vim.cmd.stopinsert()
  keys.scm_open = true
  draw(message)
end

local function close_panel()
  keys.scm_open = false
  if not (panel.buf and api.nvim_buf_is_valid(panel.buf)) then return end
  for _, w in ipairs(vim.fn.win_findbuf(panel.buf)) do
    if #api.nvim_tabpage_list_wins(api.nvim_win_get_tabpage(w)) > 1 then api.nvim_win_close(w, true) end
  end
end

local function close_floats()
  for _, w in ipairs(api.nvim_list_wins()) do
    if api.nvim_win_is_valid(w) and api.nvim_win_get_config(w).relative ~= '' then
      local buf = api.nvim_win_get_buf(w)
      if vim.bo[buf].filetype == 'TelescopePrompt' then
        pcall(require('telescope.actions').close, buf)
      elseif api.nvim_win_is_valid(w) then
        pcall(api.nvim_win_close, w, true)
      end
    end
  end
end

function M.git()
  close_floats()
  close_diffs()
  if vim.fn.executable('git') == 0 then
    panel.repos = {}
    return open_panel(' git is not installed on this machine.')
  end
  local cwd = norm(vim.fn.getcwd())
  local found = M.discover(cwd)
  if #found == 1 then
    close_panel()
    return require('diffview').open({ '-C' .. flag_value(found[1]) })
  end
  panel.repos = vim.tbl_map(function(path) return { path = path, files = {} } end, found)
  if #found == 0 then
    return open_panel(' No git repositories here.')
  end
  open_panel(' Reading the repositories…')
  refresh(function() draw() end)
end

function M.files()
  close_floats()
  close_diffs()
  close_panel()
  keys.show_files()
end

-- Back from a diff: to the repo list if there is one, otherwise to the files.
function M.back()
  if #panel.repos > 1 then
    close_diffs()
    return M.git()
  end
  M.files()
end

function M.setup()
  set_highlights()
  api.nvim_create_user_command('OrchGit', M.git, {})
  api.nvim_create_user_command('OrchFiles', M.files, {})
  api.nvim_create_user_command('OrchBack', M.back, {})
end

return M
