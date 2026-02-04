# Changelog

All notable changes to the "Mark and Recall" extension will be documented in this file.

## [0.0.4] - 2026-02-04

### Fixed
- Fixed suggested vim keybindings: `<leader>ma`/`A` now correctly map to append
- Added `<leader>mp`/`P` bindings for prepend operations

## [0.0.3] - 2026-02-04

### Added
- Screenshots to the README
- Unit tests for marks file parsing
- `npm test` command for running tests

## [0.0.2] - 2026-02-04

### Fixed
- Fixed parsing of C++ namespaced symbols (e.g., `@mlir::populateVectorToSPIRVPatterns`)
- Mark names can now contain `::` for C++ namespaces and similar patterns
- Changed name/path separator from `:` to `: ` (colon-space) for unambiguous parsing

### Added
- Unit tests for marks file parsing

## [0.0.1] - 2026

### Added
- Initial release
- Numbered marks (1-9) with quick-access keybindings
- Visual indicators: blue gutter icons and line highlighting
- Automatic line tracking when inserting/deleting lines
- Symbol marks with `@` prefix auto-detected from function/class definitions
- Anonymous and named marks support
- Global navigation between marks across files
- Commands for adding, deleting, and navigating marks
- Configurable marks file path
- HTML comment support in marks.md
