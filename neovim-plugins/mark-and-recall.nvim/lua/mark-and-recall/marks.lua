local parser = require("mark-and-recall.parser")

local M = {}

-- Cache state
local _cache = {
  marks = nil, ---@type Mark[]|nil
  mtime_sec = nil, ---@type number|nil
  mtime_nsec = nil, ---@type number|nil
}

-- Flag to prevent circular file-watcher triggers
M._is_updating = false

-- Config (set by init.lua setup)
M.config = {
  marks_file = "marks.md",
}

--- Walk up from cwd looking for marks.md, fallback to cwd.
--- @return string
function M.get_workspace_root()
  local cwd = vim.fn.getcwd()
  local dir = cwd
  while true do
    local marks_path = dir .. "/" .. M.config.marks_file
    local stat = vim.uv.fs_stat(marks_path)
    if stat then
      return dir
    end
    local parent = vim.fn.fnamemodify(dir, ":h")
    if parent == dir then
      break
    end
    dir = parent
  end
  return cwd
end

--- Resolve the marks file path.
--- @return string
function M.get_marks_file_path()
  local configured = M.config.marks_file
  if configured:sub(1, 1) == "/" then
    return configured
  end
  return M.get_workspace_root() .. "/" .. configured
end

--- Read and parse marks with mtime-based caching. Adds .index field (0-based).
--- @return table[] marks with index field
function M.read_marks()
  local path = M.get_marks_file_path()
  local stat = vim.uv.fs_stat(path)
  if not stat then
    _cache.marks = {}
    _cache.mtime_sec = nil
    _cache.mtime_nsec = nil
    return {}
  end

  if _cache.marks
    and _cache.mtime_sec == stat.mtime.sec
    and _cache.mtime_nsec == stat.mtime.nsec then
    return _cache.marks
  end

  local lines = vim.fn.readfile(path)
  local content = table.concat(lines, "\n")
  local workspace_root = M.get_workspace_root()
  local marks = parser.parse_marks_file(content, workspace_root)

  -- Add 0-based index
  for i, mark in ipairs(marks) do
    mark.index = i - 1
  end

  _cache.marks = marks
  _cache.mtime_sec = stat.mtime.sec
  _cache.mtime_nsec = stat.mtime.nsec
  return marks
end

--- Invalidate the cache so next read_marks() re-reads the file.
function M.invalidate_cache()
  _cache.marks = nil
  _cache.mtime_sec = nil
  _cache.mtime_nsec = nil
end

--- Compute the relative path from workspace root, or absolute if outside.
--- @param file_path string absolute path
--- @return string display path
function M.relative_path(file_path)
  local root = M.get_workspace_root()
  -- Normalize: ensure root ends without /
  root = root:gsub("/+$", "")
  if file_path:sub(1, #root + 1) == root .. "/" then
    return file_path:sub(#root + 2)
  end
  return file_path
end

--- Check if a mark already exists at file_path:line.
--- @param file_path string absolute path
--- @param line number 1-based
--- @return boolean
function M.has_mark_at(file_path, line)
  local marks = M.read_marks()
  for _, mark in ipairs(marks) do
    if mark.file_path == file_path and mark.line == line then
      return true
    end
  end
  return false
end

--- Add a mark at the current cursor position. Appends to marks file.
function M.add_mark()
  local bufnr = vim.api.nvim_get_current_buf()
  local file_path = vim.fn.resolve(vim.api.nvim_buf_get_name(bufnr))
  local cursor = vim.api.nvim_win_get_cursor(0)
  local line = cursor[1] -- already 1-based

  if M.has_mark_at(file_path, line) then
    vim.notify("Mark already exists at this location", vim.log.levels.INFO)
    return
  end

  local display_path = M.relative_path(file_path)
  local entry = display_path .. ":" .. line .. "\n"

  local marks_path = M.get_marks_file_path()

  -- Read existing content or start fresh
  local content = ""
  local stat = vim.uv.fs_stat(marks_path)
  if stat then
    local lines = vim.fn.readfile(marks_path)
    content = table.concat(lines, "\n")
  end

  -- Ensure trailing newline before appending
  if content ~= "" and content:sub(-1) ~= "\n" then
    content = content .. "\n"
  end
  content = content .. entry

  M._is_updating = true
  vim.fn.writefile(vim.split(content, "\n", { plain = true, trimempty = false }), marks_path)
  M._is_updating = false

  M.invalidate_cache()
  vim.notify("Mark added", vim.log.levels.INFO)
end

--- Delete the mark at the current cursor position.
function M.delete_mark_at_cursor()
  local bufnr = vim.api.nvim_get_current_buf()
  local file_path = vim.fn.resolve(vim.api.nvim_buf_get_name(bufnr))
  local cursor = vim.api.nvim_win_get_cursor(0)
  local current_line = cursor[1]

  local marks = M.read_marks()

  -- Find mark at current position
  local mark_to_delete = nil
  for _, mark in ipairs(marks) do
    if mark.file_path == file_path and mark.line == current_line then
      mark_to_delete = mark
      break
    end
  end

  if not mark_to_delete then
    vim.notify("No mark at current line", vim.log.levels.INFO)
    return
  end

  local marks_path = M.get_marks_file_path()
  local lines = vim.fn.readfile(marks_path)

  local line_to_delete = parser.mark_index_to_file_line(lines, mark_to_delete.index)

  if not line_to_delete then
    vim.notify("Could not find mark in marks file", vim.log.levels.ERROR)
    return
  end

  table.remove(lines, line_to_delete)

  M._is_updating = true
  vim.fn.writefile(lines, marks_path)
  M._is_updating = false

  M.invalidate_cache()
  vim.notify("Mark deleted", vim.log.levels.INFO)
end

return M
