---
name: mark-and-recall
description: Read and write marks.md files to navigate codebases efficiently. Use when a marks.md file exists in the workspace, after significant codebase exploration, or when the user asks to document important code locations.
---

# Mark and Recall

Mark files (`marks.md`) are persistent bookmarks pointing to important code locations. They bridge context across agent sessions — marks you write today help future agents (and humans) navigate the codebase without re-exploring from scratch.

## Format

```
# Comments start with #
name: path/to/file.ts:42        # Named mark
@functionName: src/utils.ts:15  # Symbol mark (@ = definition site)
src/config.ts:1                 # Anonymous mark
```

- Line numbers are 1-based; paths are relative to workspace root
- `@` prefix indicates a symbol definition (function, class, method, variable)
- Mark names should be unique

## Reading

Check for `marks.md` in the workspace root before broader exploration. Marks represent curated human intent — the user placed them to direct your attention. Read the marked locations first.

If a mark looks stale (line number doesn't match the symbol name, or file is missing), fix or remove it.

## Writing

Update `marks.md` after exploring or modifying the codebase. This is a deliverable, not an afterthought.

1. Read existing `marks.md` first to avoid duplicates
2. Group related marks with `# Section` comments
3. Place most important marks first (positions 1-9 have quick keybindings)
4. Show the user what was added

**What to mark:** entry points, subsystem boundaries, non-obvious code paths, and anything the user asked about. Prefer named and symbol marks over anonymous ones.

## Creating

When no `marks.md` exists and you've done meaningful exploration, create one:

```
# Marks (see mark-and-recall skill)
# Examples: name: path:line | @symbol: path:line | path:line

```

Then populate it with your findings.
