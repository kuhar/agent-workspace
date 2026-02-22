local M = {}

M.config = {
  marks_file = "marks.md",
}

-- Stored file watcher handle, closed on re-setup
M._watcher = nil

function M.setup(opts)
  opts = opts or {}
  M.config = vim.tbl_deep_extend("force", M.config, opts)

  -- Close previous file watcher and teardown tracking if re-entering setup
  if M._watcher then
    M._watcher:stop()
    if not M._watcher:is_closing() then
      M._watcher:close()
    end
    M._watcher = nil
  end
  local tracking = require("mark-and-recall.tracking")
  tracking.teardown()

  -- Forward config to marks module
  local marks = require("mark-and-recall.marks")
  marks.config.marks_file = M.config.marks_file

  -- Set up signs (highlight groups + namespace)
  local signs = require("mark-and-recall.signs")
  signs.setup()

  local nav = require("mark-and-recall.navigation")

  -- User commands
  vim.api.nvim_create_user_command("MarkAdd", function() marks.add_mark() end, { desc = "Add mark at cursor" })
  vim.api.nvim_create_user_command("MarkAddPrepend", function() marks.add_mark({ prepend = true }) end, { desc = "Add mark (prepend)" })
  vim.api.nvim_create_user_command("MarkAddNamed", function(cmd_opts)
    marks.add_named_mark({ prepend = cmd_opts.bang })
  end, { bang = true, desc = "Add named mark (bang = prepend)" })
  vim.api.nvim_create_user_command("MarkDelete", function() marks.delete_mark_at_cursor() end, { desc = "Delete mark at cursor" })
  vim.api.nvim_create_user_command("MarkDeleteAll", function() marks.delete_marks_in_file() end, { desc = "Delete all marks in file" })
  vim.api.nvim_create_user_command("MarkUpdateSymbols", function() marks.update_symbol_marks() end, { desc = "Update symbol marks" })
  vim.api.nvim_create_user_command("MarkRecall", function() nav.telescope_pick() end, { desc = "Browse marks (Telescope)" })
  vim.api.nvim_create_user_command("MarkNext", function() nav.next_mark_in_file() end, { desc = "Next mark in file" })
  vim.api.nvim_create_user_command("MarkPrev", function() nav.prev_mark_in_file() end, { desc = "Previous mark in file" })
  vim.api.nvim_create_user_command("MarkNextGlobal", function() nav.next_mark_global() end, { desc = "Next mark (global)" })
  vim.api.nvim_create_user_command("MarkPrevGlobal", function() nav.prev_mark_global() end, { desc = "Previous mark (global)" })
  vim.api.nvim_create_user_command("MarkRecallByIndex", function(cmd_opts)
    local n = tonumber(cmd_opts.args)
    if n then nav.recall_by_index(n) end
  end, { nargs = 1, desc = "Jump to mark by number" })
  vim.api.nvim_create_user_command("MarkOpen", function()
    local path = marks.get_marks_file_path()
    if not vim.uv.fs_stat(path) then
      vim.fn.writefile({
        "# Marks (see mark-and-recall)",
        "# Examples: name: path:line | @symbol: path:line | path:line",
        "",
      }, path)
    end
    vim.cmd.edit(path)
  end, { desc = "Open marks file" })

  -- Autocommands
  local group = vim.api.nvim_create_augroup("MarkAndRecall", { clear = true })

  vim.api.nvim_create_autocmd({ "BufEnter", "BufWinEnter" }, {
    group = group,
    callback = function(ev)
      signs.update_signs(ev.buf)
      tracking.maybe_attach(ev.buf)
    end,
  })

  local marks_path = marks.get_marks_file_path()
  local marks_basename = vim.fn.fnamemodify(marks_path, ":t")

  vim.api.nvim_create_autocmd("BufWritePost", {
    group = group,
    pattern = "*/" .. marks_basename,
    callback = function()
      marks.invalidate_cache()
      signs.update_all_signs()
      tracking.recheck_all_buffers()
    end,
  })

  vim.api.nvim_create_autocmd("DirChanged", {
    group = group,
    callback = function()
      marks.invalidate_cache()
      nav._last_index = nil
      signs.update_all_signs()
    end,
  })

  -- File watcher: watch the parent directory, filter to marks filename
  local marks_dir = vim.fn.fnamemodify(marks_path, ":h")

  local watcher = vim.uv.new_fs_event()
  if watcher then
    watcher:start(marks_dir, {}, function(err, filename, _)
      if err then return end
      if filename ~= marks_basename then return end
      if marks._is_updating then return end
      vim.schedule(function()
        marks.invalidate_cache()
        nav._last_index = nil
        signs.update_all_signs()
      end)
    end)
    M._watcher = watcher
  end

  -- Initial sign update
  vim.schedule(function()
    signs.update_all_signs()
  end)
end

return M
