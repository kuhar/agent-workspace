---
name: working-with-mark-files
description: Read and write marks.md files to navigate codebases efficiently. Use when a marks.md file exists in the workspace, or when the user asks to document important code locations, entry points, or architectural boundaries.
---

# Working with Mark Files

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

## Workflow A: Reading Marks for Exploration

When starting a task, check for `marks.md` in the workspace root.

**If marks exist:**
1. Read `marks.md` early in exploration
2. Treat marks as user-curated pointers to important locations
3. Read marked locations before broader exploration
4. Use marks as context for where to make changes

**Example:** User asks "add input validation". Marks file contains:
```
@handleRequest: src/api/router.ts:45
@validateInput: src/api/middleware.ts:12
```
Read these locations first to understand existing patterns before implementing.

## Workflow B: Writing Marks as Output

After exploring or writing code, offer to update `marks.md` with discoveries.

**What to mark:**
- Key entry points discovered during exploration
- Important utilities or helper functions
- Code you wrote that contains core logic
- Architectural boundaries (interfaces, base classes)

**How to write marks:**
1. Use `@symbol` prefix for function/class definitions
2. Use descriptive names for conceptual marks (e.g., `config: settings.json:1`)
3. Group related marks with `# Section` comments
4. Place most important marks first (positions 1-9 have quick keybindings)

**Example output:**
```
# Authentication
@authenticateUser: src/auth/login.ts:23
@validateToken: src/auth/jwt.ts:45

# API Routes
@createUser: src/api/users.ts:12
@getUserById: src/api/users.ts:34
```

**When to offer:**
- After exploring an unfamiliar codebase
- After implementing a feature with multiple key locations
- When the user asks about important code locations
