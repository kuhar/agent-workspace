local M = {}

--- Recursively flatten nested document symbols into a flat list.
--- @param symbols table[] LSP DocumentSymbol[]
--- @param out table[] accumulator
function M._flatten_symbols(symbols, out)
  for _, sym in ipairs(symbols) do
    out[#out + 1] = sym
    if sym.children then
      M._flatten_symbols(sym.children, out)
    end
  end
end

--- Get all document symbols for a buffer (flat list).
--- @param bufnr number
--- @param timeout_ms? number timeout in ms (default 2000)
--- @return table[]|nil flat list of symbols, or nil on failure
function M.get_document_symbols(bufnr, timeout_ms)
  timeout_ms = timeout_ms or 2000
  local params = { textDocument = vim.lsp.util.make_text_document_params(bufnr) }
  local clients = vim.lsp.get_clients({ bufnr = bufnr, method = "textDocument/documentSymbol" })
  if #clients == 0 then return nil end

  local results = vim.lsp.buf_request_sync(bufnr, "textDocument/documentSymbol", params, timeout_ms)
  if not results then return nil end

  local flat = {}
  for _, res in pairs(results) do
    if res.result then
      M._flatten_symbols(res.result, flat)
    end
  end
  return #flat > 0 and flat or nil
end

--- Get the innermost symbol containing a cursor line.
--- When multiple symbols contain the cursor, returns the smallest (innermost).
--- @param bufnr number
--- @param cursor_line number 1-based line number
--- @param timeout_ms? number timeout in ms (default 2000)
--- @return string|nil symbol name
function M.get_symbol_at_cursor(bufnr, cursor_line, timeout_ms)
  local symbols = M.get_document_symbols(bufnr, timeout_ms)
  if not symbols then return nil end

  local best = nil
  local best_size = math.huge

  local line_0 = cursor_line - 1 -- LSP uses 0-based lines

  for _, sym in ipairs(symbols) do
    local range = sym.range or (sym.location and sym.location.range)
    if range then
      local start_line = range.start.line
      local end_line = range["end"].line
      if line_0 >= start_line and line_0 <= end_line then
        local size = end_line - start_line
        if size < best_size then
          best = sym
          best_size = size
        end
      end
    end
  end

  return best and best.name or nil
end

--- Find the closest symbol by name, picking the one nearest to reference_line
--- when multiple matches exist. On distance tie, first-in-list wins (strict <).
--- Pure logic (operates on pre-fetched symbol list).
--- @param symbols table[] flat list of LSP symbols
--- @param name string symbol name to find
--- @param reference_line number 1-based line to measure distance from
--- @return number|nil 1-based line of the closest matching symbol
function M.find_closest_symbol(symbols, name, reference_line)
  if not symbols or #symbols == 0 then return nil end

  local best_line = nil
  local best_dist = math.huge

  local ref_0 = reference_line - 1 -- LSP uses 0-based lines

  for _, sym in ipairs(symbols) do
    if sym.name == name then
      local range = sym.range or (sym.location and sym.location.range)
      if range then
        local sym_line = range.start.line -- 0-based
        local dist = math.abs(sym_line - ref_0)
        if dist < best_dist then
          best_dist = dist
          best_line = sym_line + 1 -- convert to 1-based
        end
      end
    end
  end

  return best_line
end

return M
