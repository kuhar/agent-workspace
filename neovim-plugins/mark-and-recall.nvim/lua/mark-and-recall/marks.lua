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

--- Add a mark at the current cursor position.
--- @param opts? { name: string|nil, prepend: boolean|nil }
function M.add_mark(opts)
  opts = opts or {}
  local bufnr = vim.api.nvim_get_current_buf()
  local file_path = vim.fn.resolve(vim.api.nvim_buf_get_name(bufnr))
  local cursor = vim.api.nvim_win_get_cursor(0)
  local line = cursor[1] -- already 1-based

  if M.has_mark_at(file_path, line) then
    vim.notify("Mark already exists at this location", vim.log.levels.INFO)
    return
  end

  local display_path = M.relative_path(file_path)
  local entry
  if opts.name then
    entry = opts.name .. ": " .. display_path .. ":" .. line
  else
    entry = display_path .. ":" .. line
  end

  local marks_path = M.get_marks_file_path()

  -- Read existing file lines or start fresh
  local lines = {}
  local stat = vim.uv.fs_stat(marks_path)
  if stat then
    lines = vim.fn.readfile(marks_path)
  end

  if opts.prepend then
    local insert_at = parser.find_header_end(lines)
    table.insert(lines, insert_at, entry)
  else
    -- Ensure no empty trailing line duplication
    lines[#lines + 1] = entry
  end

  M._is_updating = true
  vim.fn.writefile(lines, marks_path)
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

--- Add a named mark at the current cursor position.
--- Prompts user for a name with LSP symbol suggestion as default.
--- @param opts? { prepend: boolean|nil }
function M.add_named_mark(opts)
  opts = opts or {}
  local lsp = require("mark-and-recall.lsp")

  local bufnr = vim.api.nvim_get_current_buf()
  local cursor = vim.api.nvim_win_get_cursor(0)
  local cursor_line = cursor[1]

  -- Get suggested name: @symbol or filename stem
  local symbol = lsp.get_symbol_at_cursor(bufnr, cursor_line)
  local default_name
  if symbol then
    default_name = "@" .. symbol
  else
    local buf_name = vim.api.nvim_buf_get_name(bufnr)
    default_name = vim.fn.fnamemodify(buf_name, ":t:r")
  end

  vim.ui.input({ prompt = "Mark name: ", default = default_name }, function(input)
    if not input then return end -- cancelled

    local ok, err = parser.validate_mark_name(input)
    if not ok then
      vim.notify(err, vim.log.levels.ERROR)
      return
    end

    M.add_mark({ name = input, prepend = opts.prepend })
  end)
end

--- Delete all marks pointing to a specific file.
--- @param file_path? string absolute path, defaults to current buffer
function M.delete_marks_in_file(file_path)
  if not file_path then
    local bufnr = vim.api.nvim_get_current_buf()
    file_path = vim.fn.resolve(vim.api.nvim_buf_get_name(bufnr))
  end

  local marks = M.read_marks()

  -- Collect 0-based indices of marks in this file
  local indices = {}
  for _, mark in ipairs(marks) do
    if mark.file_path == file_path then
      indices[#indices + 1] = mark.index
    end
  end

  if #indices == 0 then
    vim.notify("No marks in this file", vim.log.levels.INFO)
    return
  end

  local marks_path = M.get_marks_file_path()
  local lines = vim.fn.readfile(marks_path)

  -- Map mark indices to file line numbers
  local file_lines_to_remove = {}
  for _, idx in ipairs(indices) do
    local ln = parser.mark_index_to_file_line(lines, idx)
    if ln then
      file_lines_to_remove[#file_lines_to_remove + 1] = ln
    end
  end

  -- Sort descending so removal doesn't shift later indices
  table.sort(file_lines_to_remove, function(a, b) return a > b end)
  for _, ln in ipairs(file_lines_to_remove) do
    table.remove(lines, ln)
  end

  M._is_updating = true
  vim.fn.writefile(lines, marks_path)
  M._is_updating = false

  M.invalidate_cache()
  vim.notify(#file_lines_to_remove .. " mark(s) deleted", vim.log.levels.INFO)
end

--- Update line numbers for @-prefixed symbol marks in the current file.
--- Only marks whose name starts with "@" are treated as symbol marks by
--- convention (e.g. "@parseConfig", "@std::chrono::now").
function M.update_symbol_marks()
  local lsp = require("mark-and-recall.lsp")
  local bufnr = vim.api.nvim_get_current_buf()
  local file_path = vim.fn.resolve(vim.api.nvim_buf_get_name(bufnr))

  local marks = M.read_marks()

  -- Filter for @-prefixed named marks pointing to this file
  local symbol_marks = {}
  for _, mark in ipairs(marks) do
    if mark.name and mark.name:sub(1, 1) == "@" and mark.file_path == file_path then
      symbol_marks[#symbol_marks + 1] = mark
    end
  end

  if #symbol_marks == 0 then
    vim.notify("No symbol marks in this file", vim.log.levels.INFO)
    return
  end

  local symbols = lsp.get_document_symbols(bufnr)
  if not symbols then
    vim.notify("No LSP symbols available", vim.log.levels.WARN)
    return
  end

  local marks_path = M.get_marks_file_path()
  local lines = vim.fn.readfile(marks_path)

  local updated = 0
  for _, mark in ipairs(symbol_marks) do
    local sym_name = mark.name:sub(2) -- strip @
    local new_line = lsp.find_closest_symbol(symbols, sym_name, mark.line)
    if new_line and new_line ~= mark.line then
      local file_ln = parser.mark_index_to_file_line(lines, mark.index)
      if file_ln then
        lines[file_ln] = parser.rewrite_line_number(lines[file_ln], new_line)
        updated = updated + 1
      end
    end
  end

  if updated > 0 then
    M._is_updating = true
    vim.fn.writefile(lines, marks_path)
    M._is_updating = false
    M.invalidate_cache()
  end

  vim.notify(updated .. " symbol mark(s) updated", vim.log.levels.INFO)
end

return M
