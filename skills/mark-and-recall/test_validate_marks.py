"""Tests for validate_marks.py."""

import os
import tempfile

import pytest

from validate_marks import Error, validate


@pytest.fixture
def tmp_tree(tmp_path):
    """Create a temporary directory with sample files."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.ts").write_text("hello\n")
    (tmp_path / "src" / "utils.ts").write_text("world\n")
    (tmp_path / "README.md").write_text("readme\n")
    return str(tmp_path)


class TestValidMarks:
    def test_named_mark(self, tmp_tree):
        content = "entry: src/main.ts:1\n"
        assert validate(content, tmp_tree) == []

    def test_symbol_mark(self, tmp_tree):
        content = "@myFunc: src/utils.ts:1\n"
        assert validate(content, tmp_tree) == []

    def test_anonymous_mark(self, tmp_tree):
        content = "src/main.ts:1\n"
        assert validate(content, tmp_tree) == []

    def test_multiple_marks(self, tmp_tree):
        content = "entry: src/main.ts:1\n@helper: src/utils.ts:1\nREADME.md:1\n"
        assert validate(content, tmp_tree) == []

    def test_comments_and_blanks(self, tmp_tree):
        content = "# Section\n\nentry: src/main.ts:1\n\n# Another\nREADME.md:1\n"
        assert validate(content, tmp_tree) == []

    def test_html_comment_single_line(self, tmp_tree):
        content = "<!-- hidden -->\nentry: src/main.ts:1\n"
        assert validate(content, tmp_tree) == []

    def test_html_comment_multi_line(self, tmp_tree):
        content = "<!--\nhidden\n-->\nentry: src/main.ts:1\n"
        assert validate(content, tmp_tree) == []


class TestInvalidMarks:
    def test_file_not_found(self, tmp_tree):
        content = "entry: src/missing.ts:1\n"
        errors = validate(content, tmp_tree)
        assert len(errors) == 1
        assert "file not found" in errors[0].message
        assert "remove this mark or fix the path" in errors[0].message

    def test_invalid_line_number(self, tmp_tree):
        content = "entry: src/main.ts:abc\n"
        errors = validate(content, tmp_tree)
        assert len(errors) == 1
        assert "invalid line number" in errors[0].message

    def test_no_colon(self, tmp_tree):
        content = "just some text\n"
        errors = validate(content, tmp_tree)
        assert len(errors) == 1
        assert "no colon" in errors[0].message
        assert "expected" in errors[0].message

    def test_markdown_table(self, tmp_tree):
        content = "| col1 | col2 |\n"
        errors = validate(content, tmp_tree)
        assert len(errors) == 1
        assert "markdown table" in errors[0].message
        assert "name: path:line" in errors[0].message

    def test_duplicate_location(self, tmp_tree):
        content = "a: src/main.ts:1\nb: src/main.ts:1\n"
        errors = validate(content, tmp_tree)
        assert len(errors) == 1
        assert "duplicate location" in errors[0].message
        assert "line 1" in errors[0].message

    def test_duplicate_anonymous(self, tmp_tree):
        content = "src/main.ts:5\nsrc/main.ts:5\n"
        errors = validate(content, tmp_tree)
        assert len(errors) == 1
        assert "duplicate location" in errors[0].message


class TestLineNumbers:
    def test_error_reports_correct_line(self, tmp_tree):
        content = "# header\n\nsrc/main.ts:1\nsrc/missing.ts:1\n"
        errors = validate(content, tmp_tree)
        assert len(errors) == 1
        assert errors[0].line_no == 4

    def test_multiple_errors(self, tmp_tree):
        content = "no-colon\n| table |\nsrc/missing.ts:1\n"
        errors = validate(content, tmp_tree)
        assert len(errors) == 3
        assert errors[0].line_no == 1
        assert errors[1].line_no == 2
        assert errors[2].line_no == 3


class TestEdgeCases:
    def test_empty_file(self, tmp_tree):
        assert validate("", tmp_tree) == []

    def test_only_comments(self, tmp_tree):
        assert validate("# Just comments\n# Nothing else\n", tmp_tree) == []

    def test_cpp_namespace_in_name(self, tmp_tree):
        content = "@mlir::populatePatterns: src/main.ts:1\n"
        assert validate(content, tmp_tree) == []

    def test_absolute_path(self, tmp_tree):
        abs_file = os.path.join(tmp_tree, "src", "main.ts")
        content = f"entry: {abs_file}:1\n"
        assert validate(content, tmp_tree) == []

    def test_whitespace_around_marks(self, tmp_tree):
        content = "  entry: src/main.ts:1  \n"
        assert validate(content, tmp_tree) == []
