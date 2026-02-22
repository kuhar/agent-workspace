local parser = require("mark-and-recall.parser")

local M = {}

-- Map of buffer numbers currently being tracked.
M._attached_bufs = {}
-- Pending line adjustments per file path: { [file_path] = { [original_line] = { current = n } } }.
M._pending_updates = {}
-- Debounce timer for flushing pending updates.
M._debounce_timer = nil

--- Attach to a buffer if it has marks, detach if it no longer does.
--- Skips unnamed buffers and the marks file itself.
--- @param bufnr number
function M.maybe_attach(bufnr)
  local marks_mod = require("mark-and-recall.marks")
  local buf_name = vim.api.nvim_buf_get_name(bufnr)
  if buf_name == "" then return end

  local file_path = vim.fn.resolve(buf_name)

  -- Skip the marks file itself
  local marks_path = marks_mod.get_marks_file_path()
  if file_path == marks_path then return end

  -- Check if any marks point to this file
  local marks = marks_mod.read_marks()
  local has_marks = false
  for _, mark in ipairs(marks) do
    if mark.file_path == file_path then
      has_marks = true
      break
    end
  end

  if not has_marks then
    -- Detach by clearing state; the on_lines callback will return true next
    -- time it fires, completing the detach.
    M._attached_bufs[bufnr] = nil
    return
  end

  -- Already attached — nothing to do
  if M._attached_bufs[bufnr] then return end

  M._attached_bufs[bufnr] = true

  vim.api.nvim_buf_attach(bufnr, false, {
    on_lines = function(_, buf, _, first, last, new_last)
      if not vim.api.nvim_buf_is_valid(buf) or not M._attached_bufs[buf] then
        M._attached_bufs[buf] = nil
        return true -- detach
      end
      local name = vim.api.nvim_buf_get_name(buf)
      if name == "" then return end
      local fp = vim.fn.resolve(name)
      M._on_lines_changed(buf, fp, first, last, new_last)
    end,
    on_detach = function(_, buf)
      M._attached_bufs[buf] = nil
    end,
  })
end

--- Handle buffer line changes.
--- @param _buf number buffer number (unused)
--- @param file_path string resolved file path
--- @param first number 0-based first changed line
--- @param last number 0-based last changed line
--- @param new_last number 0-based new last line
function M._on_lines_changed(_buf, file_path, first, last, new_last)
  local marks_mod = require("mark-and-recall.marks")
  local marks = marks_mod.read_marks()

  -- Get 1-based mark lines for this file, applying any pending adjustments
  local mark_lines = {}
  local pending = M._pending_updates[file_path]
  for _, mark in ipairs(marks) do
    if mark.file_path == file_path then
      local line = mark.line
      if pending and pending[line] then
        line = pending[line].current
      end
      mark_lines[#mark_lines + 1] = line
    end
  end

  if #mark_lines == 0 then return end

  local adjustments = parser.compute_line_adjustments(mark_lines, first, last, new_last)
  if not next(adjustments) then return end

  -- Merge into pending updates
  if not M._pending_updates[file_path] then
    M._pending_updates[file_path] = {}
  end

  -- Build reverse map: current_line → original_line for existing pending entries
  local current_to_original = {}
  for orig, entry in pairs(M._pending_updates[file_path]) do
    current_to_original[entry.current] = orig
  end

  -- Also track marks with no pending entry (original == current)
  for _, mark in ipairs(marks) do
    if mark.file_path == file_path then
      if not M._pending_updates[file_path][mark.line] then
        current_to_original[mark.line] = mark.line
      end
    end
  end

  -- Apply adjustments: key in adjustments is old current line → new current line
  for old_current, new_current in pairs(adjustments) do
    local original = current_to_original[old_current]
    if original then
      M._pending_updates[file_path][original] = { current = new_current }
    end
  end

  -- Schedule debounced flush
  M._schedule_flush()
end

--- Schedule a debounced flush of pending updates.
function M._schedule_flush()
  if M._debounce_timer then
    M._debounce_timer:stop()
  end
  M._debounce_timer = vim.uv.new_timer()
  if M._debounce_timer then
    M._debounce_timer:start(500, 0, vim.schedule_wrap(function()
      M._flush_pending()
    end))
  end
end

--- Flush all pending line number updates to the marks file.
function M._flush_pending()
  if M._debounce_timer then
    M._debounce_timer:stop()
    M._debounce_timer:close()
    M._debounce_timer = nil
  end

  local marks_mod = require("mark-and-recall.marks")
  local marks_path = marks_mod.get_marks_file_path()

  -- Re-read fresh (handles concurrent manual edits)
  local stat = vim.uv.fs_stat(marks_path)
  if not stat then
    M._pending_updates = {}
    return
  end

  local lines = vim.fn.readfile(marks_path)
  marks_mod.invalidate_cache()
  local content = table.concat(lines, "\n")
  local workspace_root = marks_mod.get_workspace_root()
  local marks = parser.parse_marks_file(content, workspace_root)

  local updated = 0
  for i, mark in ipairs(marks) do
    local idx = i - 1
    local pending = M._pending_updates[mark.file_path]
    if pending and pending[mark.line] then
      local new_line = pending[mark.line].current
      if new_line ~= mark.line then
        local file_ln = parser.mark_index_to_file_line(lines, idx)
        if file_ln then
          lines[file_ln] = parser.rewrite_line_number(lines[file_ln], new_line)
          updated = updated + 1
        end
      end
    end
  end

  M._pending_updates = {}

  if updated > 0 then
    marks_mod._is_updating = true
    vim.fn.writefile(lines, marks_path)
    marks_mod._is_updating = false
    marks_mod.invalidate_cache()
  end
end

--- Teardown: clear state and stop timer. Called on setup() re-entry.
function M.teardown()
  M._attached_bufs = {}
  M._pending_updates = {}
  if M._debounce_timer then
    M._debounce_timer:stop()
    if not M._debounce_timer:is_closing() then
      M._debounce_timer:close()
    end
    M._debounce_timer = nil
  end
end

--- Re-check all loaded buffers for tracking attachment.
--- Attaches to buffers that gained marks, detaches from buffers that lost them.
--- Called when marks file changes (marks may have been added/removed).
function M.recheck_all_buffers()
  for _, buf in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_loaded(buf) and vim.bo[buf].buflisted then
      M.maybe_attach(buf)
    end
  end
end

return M
