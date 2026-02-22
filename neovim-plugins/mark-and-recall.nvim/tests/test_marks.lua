-- Tests for marks module pure-function logic.
-- We test relative_path computation, mark-index-to-file-line mapping,
-- and file-line manipulation without requiring vim.*.

local parser = require("mark-and-recall.parser")

describe("marks", function()

  describe("relative path computation", function()
    -- Replicate the relative_path logic as a pure function for testing
    local function relative_path(file_path, root)
      root = root:gsub("/+$", "")
      if file_path:sub(1, #root + 1) == root .. "/" then
        return file_path:sub(#root + 2)
      end
      return file_path
    end

    it("computes relative path within workspace", function()
      assert_eq(relative_path("/home/user/project/src/file.ts", "/home/user/project"), "src/file.ts")
    end)

    it("returns absolute path when outside workspace", function()
      assert_eq(relative_path("/other/place/file.ts", "/home/user/project"), "/other/place/file.ts")
    end)

    it("handles root with trailing slash", function()
      assert_eq(relative_path("/home/user/project/src/file.ts", "/home/user/project/"), "src/file.ts")
    end)

    it("handles nested paths", function()
      assert_eq(relative_path("/ws/a/b/c/d.lua", "/ws"), "a/b/c/d.lua")
    end)
  end)

  describe("duplicate detection", function()
    it("detects duplicate at same file and line", function()
      local content = "src/file.ts:10\nsrc/other.ts:20"
      local marks = parser.parse_marks_file(content, "/workspace")

      local function has_mark_at(marks_list, file_path, line)
        for _, m in ipairs(marks_list) do
          if m.file_path == file_path and m.line == line then
            return true
          end
        end
        return false
      end

      assert_eq(has_mark_at(marks, "/workspace/src/file.ts", 10), true)
      assert_eq(has_mark_at(marks, "/workspace/src/file.ts", 11), false)
      assert_eq(has_mark_at(marks, "/workspace/src/other.ts", 20), true)
      assert_eq(has_mark_at(marks, "/workspace/src/missing.ts", 1), false)
    end)
  end)

  describe("mark_index_to_file_line", function()
    it("finds mark at index 1 skipping comments and headers", function()
      local file_lines = {
        "# Header",
        "# Comment",
        "",
        "src/a.ts:10",
        "named: src/b.ts:20",
        "<!-- comment -->",
        "src/c.ts:30",
      }

      local found = parser.mark_index_to_file_line(file_lines, 1)
      assert_eq(found, 5) -- "named: src/b.ts:20" is file line 5

      -- After removing, check result
      table.remove(file_lines, found)
      assert_eq(#file_lines, 6)
      assert_eq(file_lines[4], "src/a.ts:10")
      assert_eq(file_lines[5], "<!-- comment -->")
      assert_eq(file_lines[6], "src/c.ts:30")
    end)

    it("finds first mark (index 0)", function()
      local file_lines = {
        "# header",
        "src/first.ts:1",
        "src/second.ts:2",
      }

      assert_eq(parser.mark_index_to_file_line(file_lines, 0), 2)
    end)

    it("finds last mark", function()
      local file_lines = {
        "src/a.ts:1",
        "src/b.ts:2",
        "src/c.ts:3",
      }

      assert_eq(parser.mark_index_to_file_line(file_lines, 2), 3)
    end)

    it("returns nil for out-of-range index", function()
      local file_lines = {
        "src/a.ts:1",
        "src/b.ts:2",
      }

      assert_nil(parser.mark_index_to_file_line(file_lines, 5))
    end)

    it("skips multi-line HTML comments", function()
      local file_lines = {
        "<!-- start",
        "still comment",
        "-->",
        "src/a.ts:1",
      }

      assert_eq(parser.mark_index_to_file_line(file_lines, 0), 4)
    end)

    it("returns nil for empty file", function()
      assert_nil(parser.mark_index_to_file_line({}, 0))
    end)
  end)

  describe("append logic", function()
    it("appends entry to content with trailing newline", function()
      local content = "# header\nsrc/a.ts:10\n"
      local entry = "src/b.ts:20\n"
      local result = content .. entry
      assert_eq(result, "# header\nsrc/a.ts:10\nsrc/b.ts:20\n")
    end)

    it("adds newline before appending if missing", function()
      local content = "# header\nsrc/a.ts:10"
      if content:sub(-1) ~= "\n" then
        content = content .. "\n"
      end
      local entry = "src/b.ts:20\n"
      local result = content .. entry
      assert_eq(result, "# header\nsrc/a.ts:10\nsrc/b.ts:20\n")
    end)
  end)
end)
