local parser = require("mark-and-recall.parser")

local workspace_root = "/workspace"

describe("parseMarksFile", function()

  describe("C++ namespace support", function()
    it("parses C++ namespaced symbol marks correctly", function()
      local content = "@mlir::populateVectorToSPIRVPatterns: llvm-project/mlir/lib/Conversion/VectorToSPIRV/VectorToSPIRV.cpp:812"
      local marks = parser.parse_marks_file(content, workspace_root)

      assert_eq(#marks, 1)
      assert_eq(marks[1].name, "@mlir::populateVectorToSPIRVPatterns")
      assert_eq(marks[1].file_path, "/workspace/llvm-project/mlir/lib/Conversion/VectorToSPIRV/VectorToSPIRV.cpp")
      assert_eq(marks[1].line, 812)
    end)

    it("parses deeply nested C++ namespaces", function()
      local content = "@std::chrono::system_clock::now: src/time.cpp:5"
      local marks = parser.parse_marks_file(content, workspace_root)

      assert_eq(#marks, 1)
      assert_eq(marks[1].name, "@std::chrono::system_clock::now")
      assert_eq(marks[1].file_path, "/workspace/src/time.cpp")
      assert_eq(marks[1].line, 5)
    end)

    it("handles multiple C++ namespace marks", function()
      local content = "@mlir::populateVectorToSPIRVPatterns: src/test.cpp:10\n@std::chrono::system_clock: src/time.cpp:5\n@llvm::StringRef::empty: src/strings.cpp:42"
      local marks = parser.parse_marks_file(content, workspace_root)

      assert_eq(#marks, 3)
      assert_eq(marks[1].name, "@mlir::populateVectorToSPIRVPatterns")
      assert_eq(marks[2].name, "@std::chrono::system_clock")
      assert_eq(marks[3].name, "@llvm::StringRef::empty")
    end)
  end)

  describe("basic named marks", function()
    it("parses simple named marks", function()
      local content = "simple: src/file.ts:20"
      local marks = parser.parse_marks_file(content, workspace_root)

      assert_eq(#marks, 1)
      assert_eq(marks[1].name, "simple")
      assert_eq(marks[1].file_path, "/workspace/src/file.ts")
      assert_eq(marks[1].line, 20)
    end)

    it("parses symbol marks with @ prefix", function()
      local content = "@parseConfig: src/utils.ts:42"
      local marks = parser.parse_marks_file(content, workspace_root)

      assert_eq(#marks, 1)
      assert_eq(marks[1].name, "@parseConfig")
      assert_eq(marks[1].file_path, "/workspace/src/utils.ts")
      assert_eq(marks[1].line, 42)
    end)
  end)

  describe("anonymous marks", function()
    it("parses anonymous marks (path:line only)", function()
      local content = "src/anonymous.ts:30"
      local marks = parser.parse_marks_file(content, workspace_root)

      assert_eq(#marks, 1)
      assert_nil(marks[1].name)
      assert_eq(marks[1].file_path, "/workspace/src/anonymous.ts")
      assert_eq(marks[1].line, 30)
    end)

    it("parses anonymous marks with nested paths", function()
      local content = "src/components/Button/index.tsx:15"
      local marks = parser.parse_marks_file(content, workspace_root)

      assert_eq(#marks, 1)
      assert_nil(marks[1].name)
      assert_eq(marks[1].file_path, "/workspace/src/components/Button/index.tsx")
      assert_eq(marks[1].line, 15)
    end)
  end)

  describe("absolute paths", function()
    it("preserves absolute paths", function()
      local content = "mymark: /home/user/project/src/file.ts:100"
      local marks = parser.parse_marks_file(content, workspace_root)

      assert_eq(#marks, 1)
      assert_eq(marks[1].name, "mymark")
      assert_eq(marks[1].file_path, "/home/user/project/src/file.ts")
      assert_eq(marks[1].line, 100)
    end)
  end)

  describe("comments and empty lines", function()
    it("skips empty lines", function()
      local content = "\nmark1: src/a.ts:1\n\nmark2: src/b.ts:2\n"
      local marks = parser.parse_marks_file(content, workspace_root)

      assert_eq(#marks, 2)
    end)

    it("skips hash comments", function()
      local content = "# This is a comment\nmark1: src/a.ts:1\n# Another comment\nmark2: src/b.ts:2"
      local marks = parser.parse_marks_file(content, workspace_root)

      assert_eq(#marks, 2)
    end)

    it("skips HTML comments (single line)", function()
      local content = "<!-- This is a comment -->\nmark1: src/a.ts:1"
      local marks = parser.parse_marks_file(content, workspace_root)

      assert_eq(#marks, 1)
    end)

    it("skips HTML comments (multi-line)", function()
      local content = "<!-- This is a\nmulti-line\ncomment -->\nmark1: src/a.ts:1"
      local marks = parser.parse_marks_file(content, workspace_root)

      assert_eq(#marks, 1)
    end)
  end)

  describe("mixed marks", function()
    it("parses a realistic marks.md file", function()
      local content = "# Mark and Recall File\n# Named marks\nmymark: src/utils.ts:10\n\n# Symbol marks\n@parseConfig: src/config.ts:42\n@mlir::populateVectorToSPIRVPatterns: llvm/lib/file.cpp:812\n\n# Anonymous marks\nsrc/helpers.ts:18\n"
      local marks = parser.parse_marks_file(content, workspace_root)

      assert_eq(#marks, 4)
      assert_table_eq(marks[1], { name = "mymark", file_path = "/workspace/src/utils.ts", line = 10 })
      assert_table_eq(marks[2], { name = "@parseConfig", file_path = "/workspace/src/config.ts", line = 42 })
      assert_table_eq(marks[3], { name = "@mlir::populateVectorToSPIRVPatterns", file_path = "/workspace/llvm/lib/file.cpp", line = 812 })
      assert_table_eq(marks[4], { file_path = "/workspace/src/helpers.ts", line = 18 })
    end)
  end)

  describe("find_header_end", function()
    it("returns 1 for empty file", function()
      assert_eq(parser.find_header_end({}), 1)
    end)

    it("skips headers only", function()
      local lines = { "# Title", "# Comment", "## Section" }
      assert_eq(parser.find_header_end(lines), 4) -- after all headers
    end)

    it("returns 1 when content starts immediately", function()
      local lines = { "src/file.ts:10", "src/other.ts:20" }
      assert_eq(parser.find_header_end(lines), 1)
    end)

    it("skips mixed headers and blank lines", function()
      local lines = { "# Title", "", "# Comment", "", "src/file.ts:10" }
      assert_eq(parser.find_header_end(lines), 5)
    end)
  end)

  describe("validate_mark_name", function()
    it("accepts simple names", function()
      local ok, err = parser.validate_mark_name("mymark")
      assert_eq(ok, true)
      assert_nil(err)
    end)

    it("accepts @symbol names", function()
      local ok, err = parser.validate_mark_name("@parseConfig")
      assert_eq(ok, true)
      assert_nil(err)
    end)

    it("accepts C++ namespace symbols", function()
      local ok, err = parser.validate_mark_name("@std::chrono::now")
      assert_eq(ok, true)
      assert_nil(err)
    end)

    it("rejects empty string", function()
      local ok, err = parser.validate_mark_name("")
      assert_eq(ok, false)
      assert_eq(err, "Name cannot be empty")
    end)

    it("rejects nil", function()
      local ok, err = parser.validate_mark_name(nil)
      assert_eq(ok, false)
      assert_eq(err, "Name cannot be empty")
    end)

    it("rejects names containing colon-space", function()
      local ok, err = parser.validate_mark_name("bad: name")
      assert_eq(ok, false)
      assert_eq(err, "Name cannot contain ': '")
    end)
  end)

  describe("compute_line_adjustments", function()
    it("shifts marks down on insertion", function()
      -- Insert 2 lines at line 3 (0-based: first=2, last=2, new_last=4)
      local result = parser.compute_line_adjustments({ 1, 3, 5, 10 }, 2, 2, 4)
      -- Mark at line 1: before change, no shift
      assert_nil(result[1])
      -- Mark at line 3: > last_line(2)? 3 > 2 yes → 3+2=5
      assert_eq(result[3], 5)
      -- Mark at line 5: > 2 → 5+2=7
      assert_eq(result[5], 7)
      -- Mark at line 10: > 2 → 10+2=12
      assert_eq(result[10], 12)
    end)

    it("shifts marks up on deletion", function()
      -- Delete 2 lines starting at line 3 (0-based: first=2, last=4, new_last=2)
      local result = parser.compute_line_adjustments({ 1, 3, 4, 5, 10 }, 2, 4, 2)
      -- Mark at 1: before change, no shift
      assert_nil(result[1])
      -- Mark at 3: in deleted range (3 > 2 and 3 <= 4) → first_line+1 = 3
      assert_eq(result[3], 3)
      -- Mark at 4: in deleted range (4 > 2 and 4 <= 4) → 3
      assert_eq(result[4], 3)
      -- Mark at 5: after change (5 > 4) → 5+(-2)=3
      assert_eq(result[5], 3)
      -- Mark at 10: after change → 10-2=8
      assert_eq(result[10], 8)
    end)

    it("returns empty when zero delta", function()
      local result = parser.compute_line_adjustments({ 1, 5, 10 }, 3, 5, 5)
      assert_eq(next(result), nil)
    end)

    it("handles single line insertion", function()
      -- Insert 1 line at line 1 (0-based: first=0, last=0, new_last=1)
      local result = parser.compute_line_adjustments({ 1, 2, 3 }, 0, 0, 1)
      -- Mark at 1: > last_line(0) → 1+1=2
      assert_eq(result[1], 2)
      assert_eq(result[2], 3)
      assert_eq(result[3], 4)
    end)

    it("moves marks in deleted range to start", function()
      -- Delete lines 2-5 (0-based: first=1, last=5, new_last=1), delta=-4
      local result = parser.compute_line_adjustments({ 2, 3, 4, 5 }, 1, 5, 1)
      -- All marks in range [2,5] (> first_line(1) and <= last_line(5))
      assert_eq(result[2], 2)
      assert_eq(result[3], 2)
      assert_eq(result[4], 2)
      assert_eq(result[5], 2)
    end)

    it("handles empty mark list", function()
      local result = parser.compute_line_adjustments({}, 0, 0, 5)
      assert_eq(next(result), nil)
    end)

    it("mark exactly at change boundary is not shifted on insertion", function()
      -- Insert at 0-based line 5 (first=5, last=5, new_last=6), delta=1
      -- Mark at 1-based line 5: 5 > 5? No, not shifted
      local result = parser.compute_line_adjustments({ 5 }, 5, 5, 6)
      assert_nil(result[5])
    end)
  end)

  describe("rewrite_line_number", function()
    it("rewrites anonymous mark line number", function()
      assert_eq(parser.rewrite_line_number("src/file.ts:10", 42), "src/file.ts:42")
    end)

    it("rewrites named mark line number", function()
      assert_eq(parser.rewrite_line_number("mymark: src/file.ts:10", 42), "mymark: src/file.ts:42")
    end)

    it("rewrites C++ namespace symbol mark", function()
      assert_eq(
        parser.rewrite_line_number("@std::chrono::system_clock: src/time.cpp:5", 100),
        "@std::chrono::system_clock: src/time.cpp:100"
      )
    end)

    it("handles trailing whitespace", function()
      assert_eq(parser.rewrite_line_number("src/file.ts:10  ", 42), "src/file.ts:42")
    end)
  end)

  describe("edge cases", function()
    it("handles whitespace around separators", function()
      local content = "  mymark:   src/file.ts:10  "
      local marks = parser.parse_marks_file(content, workspace_root)

      assert_eq(#marks, 1)
      assert_eq(marks[1].name, "mymark")
      assert_eq(marks[1].file_path, "/workspace/src/file.ts")
      assert_eq(marks[1].line, 10)
    end)

    it("ignores lines without line numbers", function()
      local content = "invalid line without colon\nmark: src/file.ts:abc\nvalid: src/file.ts:10"
      local marks = parser.parse_marks_file(content, workspace_root)

      assert_eq(#marks, 1)
      assert_eq(marks[1].name, "valid")
    end)

    it("handles empty content", function()
      local marks = parser.parse_marks_file("", workspace_root)
      assert_eq(#marks, 0)
    end)

    it("handles content with only comments", function()
      local content = "# Just comments\n# Nothing else\n<!-- HTML comment -->"
      local marks = parser.parse_marks_file(content, workspace_root)
      assert_eq(#marks, 0)
    end)

    it("rejects negative line numbers", function()
      local content = "src/file.ts:-5"
      local marks = parser.parse_marks_file(content, workspace_root)
      assert_eq(#marks, 0)
    end)

    it("rejects zero line numbers", function()
      local content = "src/file.ts:0"
      local marks = parser.parse_marks_file(content, workspace_root)
      assert_eq(#marks, 0)
    end)
  end)
end)
