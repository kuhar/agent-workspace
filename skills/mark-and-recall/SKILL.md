---
name: mark-and-recall
description: Read and write marks.md files to navigate codebases efficiently. Use when a marks.md file exists in the workspace, or when the user asks to document important code locations, entry points, or architectural boundaries.
---

# Mark and Recall

Mark files (`marks.md`) are human-readable bookmarks pointing to important code locations. They serve as a communication channel between user and agent.

## Format Reference

```
# Comments start with #
name: path/to/file.ts:42        # Named mark
@functionName: src/utils.ts:15  # Symbol mark (function/class/variable definition)
src/config.ts:1                 # Anonymous mark
```

- Line numbers are 1-based
- Paths are relative to workspace root (or absolute)
- `@` prefix indicates a symbol name (function, class, method, variable)
- Mark names should be unique (duplicates work but are discouraged)

## Reading Marks

When starting a task, check for `marks.md` in the workspace root and read marked locations before broader exploration.

**Example:** User asks "add input validation". Marks file contains:
```
@handleRequest: src/api/router.ts:45
@validateInput: src/api/middleware.ts:12
```
Read these locations first to understand existing patterns before implementing.

## Writing Marks

After exploring or writing code, update `marks.md` with discoveries.

1. Read existing `marks.md` first to avoid adding duplicates
2. Group related marks with `# Section` comments
3. Place most important marks first (positions 1-9 have quick keybindings)
4. Append new marks to the file (or create it if missing)
5. Show the user what was added

**Example output:**
```
# Authentication
@authenticateUser: src/auth/login.ts:23
@validateToken: src/auth/jwt.ts:45

# API Routes
@createUser: src/api/users.ts:12
@getUserById: src/api/users.ts:34
```
