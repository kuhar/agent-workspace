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
