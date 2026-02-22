local M = {}

--- @class Mark
--- @field name string|nil
--- @field file_path string
--- @field line number

--- Parse a marks.md file content into a list of marks.
--- Pure Lua — no vim.* dependencies.
--- @param content string
--- @param workspace_root string
--- @return Mark[]
function M.parse_marks_file(content, workspace_root)
  local marks = {}
  local lines = {}
  for line in (content .. "\n"):gmatch("([^\n]*)\n") do
    lines[#lines + 1] = line
  end

  local in_html_comment = false

  for _, line in ipairs(lines) do
    local trimmed = line:match("^%s*(.-)%s*$")

    -- Handle HTML-style markdown comments (<!-- ... -->)
    if in_html_comment then
      if trimmed:find("-->", 1, true) then
        in_html_comment = false
      end
      goto continue
    end

    if trimmed:sub(1, 4) == "<!--" then
      if not trimmed:find("-->", 5, true) then
        in_html_comment = true
      end
      goto continue
    end

    -- Skip empty lines and # comments
    if trimmed == "" or trimmed:sub(1, 1) == "#" then
      goto continue
    end

    -- Find the last colon (before the line number)
    local last_colon = nil
    for i = #trimmed, 1, -1 do
      if trimmed:sub(i, i) == ":" then
        last_colon = i
        break
      end
    end
    if not last_colon then
      goto continue
    end

    local line_str = trimmed:sub(last_colon + 1):match("^%s*(.-)%s*$")
    local line_num = tonumber(line_str)
    if not line_num or line_num ~= math.floor(line_num) or line_num < 1 then
      goto continue
    end

    local before_line_num = trimmed:sub(1, last_colon - 1):match("^%s*(.-)%s*$")

    -- Check if there's a name: prefix
    -- Using ": " (colon-space) instead of just ":" allows C++ namespaces
    local colon_space_index = before_line_num:find(": ", 1, true)

    local name, file_path

    if colon_space_index then
      local potential_name = before_line_num:sub(1, colon_space_index - 1):match("^%s*(.-)%s*$")
      local potential_path = before_line_num:sub(colon_space_index + 2):match("^%s*(.-)%s*$")

      if #potential_name > 0
        and not potential_name:find("/", 1, true)
        and not potential_name:find("\\", 1, true)
        and #potential_path > 0 then
        name = potential_name
        file_path = potential_path
      else
        name = nil
        file_path = before_line_num
      end
    else
      name = nil
      file_path = before_line_num
    end

    -- Resolve relative paths against workspace root
    local resolved_path
    if file_path:sub(1, 1) == "/" then
      resolved_path = file_path
    else
      resolved_path = workspace_root .. "/" .. file_path
    end

    marks[#marks + 1] = {
      name = name,
      file_path = resolved_path,
      line = line_num,
    }

    ::continue::
  end

  return marks
end

--- Scan from top, skip `#` lines and empty lines, return 1-based insertion
--- point after header block. Used for prepend mode.
--- Pure Lua — no vim.* dependencies.
--- @param file_lines string[]
--- @return number 1-based insertion point
function M.find_header_end(file_lines)
  local i = 1
  while i <= #file_lines do
    local trimmed = file_lines[i]:match("^%s*(.-)%s*$")
    if trimmed == "" or trimmed:sub(1, 1) == "#" then
      i = i + 1
    else
      break
    end
  end
  return i
end

--- Validate a mark name. Rejects empty/nil and names containing `": "`.
--- Pure Lua — no vim.* dependencies.
--- @param name string|nil
--- @return boolean ok
--- @return string|nil err
function M.validate_mark_name(name)
  if name == nil or name == "" then
    return false, "Name cannot be empty"
  end
  if name:find(": ", 1, true) then
    return false, "Name cannot contain ': '"
  end
  return true, nil
end

--- Compute line adjustments for marks after a buffer change.
--- Pure Lua — no vim.* dependencies.
--- @param mark_lines number[] 1-based mark line numbers
--- @param first_line number 0-based first changed line (from nvim_buf_attach)
--- @param last_line number 0-based last changed line (from nvim_buf_attach)
--- @param new_last_line number 0-based new last line (from nvim_buf_attach)
--- @return table<number,number> map of old_line → new_line (only changed entries)
function M.compute_line_adjustments(mark_lines, first_line, last_line, new_last_line)
  local delta = new_last_line - last_line
  if delta == 0 then return {} end

  local result = {}
  for _, ml in ipairs(mark_lines) do
    -- Mark is after the changed region (1-based mark > last_line 0-based)
    if ml > last_line then
      result[ml] = ml + delta
    -- Mark is in a deleted range (delta < 0, mark is between first_line+1 and last_line inclusive)
    elseif delta < 0 and ml > first_line and ml <= last_line then
      result[ml] = first_line + 1
    end
  end
  return result
end

--- Rewrite the trailing line number in a mark line string.
--- Pure Lua — no vim.* dependencies.
--- @param line_str string e.g. "@std::chrono: src/time.cpp:5"
--- @param new_line number the new line number
--- @return string the rewritten line
function M.rewrite_line_number(line_str, new_line)
  return (line_str:gsub(":%d+%s*$", ":" .. new_line))
end

--- Given raw file lines and a 0-based mark index, return the 1-based file line
--- number of that mark. Returns nil if not found.
--- Pure Lua — no vim.* dependencies.
--- @param file_lines string[]
--- @param target_index number 0-based mark index
--- @return number|nil 1-based file line number
function M.mark_index_to_file_line(file_lines, target_index)
  local mark_index = 0
  local in_html_comment = false

  for i, line in ipairs(file_lines) do
    local trimmed = line:match("^%s*(.-)%s*$")

    if in_html_comment then
      if trimmed:find("-->", 1, true) then
        in_html_comment = false
      end
      goto continue
    end

    if trimmed:sub(1, 4) == "<!--" then
      if not trimmed:find("-->", 5, true) then
        in_html_comment = true
      end
      goto continue
    end

    if trimmed == "" or trimmed:sub(1, 1) == "#" then
      goto continue
    end

    local last_colon = nil
    for j = #trimmed, 1, -1 do
      if trimmed:sub(j, j) == ":" then
        last_colon = j
        break
      end
    end
    if not last_colon then goto continue end

    local line_str = trimmed:sub(last_colon + 1):match("^%s*(.-)%s*$")
    local line_num = tonumber(line_str)
    if not line_num or line_num ~= math.floor(line_num) or line_num < 1 then
      goto continue
    end

    if mark_index == target_index then
      return i
    end
    mark_index = mark_index + 1

    ::continue::
  end

  return nil
end

return M
