# Mark and Recall

> *"No recall or intervention can work in this place."* -- Dagoth Ur

Fortunately, Recall works perfectly fine in VS Code, thanks to this extension. Inspired by the Mark and Recall spells from Morrowind, it lets you define marks in a `marks.md` file and teleport to them instantly.

Unlike native vim marks which are ephemeral and stored in binary format, these marks are:

- **Persistent**: Saved in a plain text `marks.md` file that survives editor restarts
- **Human-readable**: Easy to view, edit, and share with your team
- **Maintainable**: Symbol marks (`@function`) can be updated via LSP when code shifts (e.g., after pulling from upstream)
- **LLM-friendly**: Feed `marks.md` to an AI to point it to key locations in your codebase, or have the LLM explore your code and generate marks for important entry points, APIs, or architectural boundaries

## Features

- **Numbered marks (1-9)** with quick-access keybindings
- **Visual indicators**: blue gutter icons and line highlighting
- **Automatic line tracking**: marks update when you insert/delete lines
- **Symbol marks**: auto-named from function/class definitions with `@` prefix
- **Anonymous and named marks**: name is optional

## marks.md Format

Create a `marks.md` file in your workspace root:

```md
# Named marks (name: path:line) - user-specified
tester: agents/llvm-tester.md:11
config: /home/user/project/config.json:5

# Symbol marks (@symbol: path:line) - auto-detected from code
@parseConfig: src/utils.ts:42
@UserController: src/api/users.ts:15

# Anonymous marks (path:line)
src/helpers.ts:18
```

- `@` prefix indicates auto-detected symbol names (can be updated with `updateSymbolMarks`)
- Paths can be relative (to workspace root) or absolute
- Line numbers are 1-based
- Lines starting with `#` are comments

## Commands

| Command | Description |
|---------|-------------|
| `recall` | Show picker to jump to any mark |
| `recallByIndex` | Jump to mark by index (use with args) |
| `openMarks` | Open marks.md for editing |
| `prependMark` | Add mark at cursor (top of list, auto-names with @symbol if on definition) |
| `prependNamedMark` | Add named mark at cursor with prompt (top of list) |
| `appendMark` | Add mark at cursor (bottom of list, auto-names with @symbol if on definition) |
| `appendNamedMark` | Add named mark at cursor with prompt (bottom of list) |
| `deleteMarkAtCursor` | Delete mark at current line |
| `deleteAllMarksInFile` | Delete all marks in current file |
| `gotoPreviousMark` | Jump to previous mark in file (wraps) |
| `gotoNextMark` | Jump to next mark in file (wraps) |
| `updateSymbolMarks` | Update @symbol mark line numbers in current file |

## Vim Keybindings

Add to `vim.normalModeKeyBindingsNonRecursive` in settings.json:

```json
{"before": ["<leader>", "m", "r"], "commands": ["mark-and-recall.recall"]},
{"before": ["<leader>", "m", "e"], "commands": ["mark-and-recall.openMarks"]},
{"before": ["<leader>", "m", "a"], "commands": ["mark-and-recall.prependMark"]},
{"before": ["<leader>", "m", "A"], "commands": ["mark-and-recall.prependNamedMark"]},
{"before": ["<leader>", "m", "d"], "commands": ["mark-and-recall.deleteMarkAtCursor"]},
{"before": ["<leader>", "m", "D"], "commands": ["mark-and-recall.deleteAllMarksInFile"]},
{"before": ["<leader>", "m", "g"], "commands": ["mark-and-recall.gotoPreviousMark"]},
{"before": ["<leader>", "m", "G"], "commands": ["mark-and-recall.gotoNextMark"]},
{"before": ["<leader>", "m", "u"], "commands": ["mark-and-recall.updateSymbolMarks"]},
{"before": ["<leader>", "m", "1"], "commands": [{"command": "mark-and-recall.recallByIndex", "args": {"index": 0}}]},
{"before": ["<leader>", "m", "2"], "commands": [{"command": "mark-and-recall.recallByIndex", "args": {"index": 1}}]},
{"before": ["<leader>", "m", "3"], "commands": [{"command": "mark-and-recall.recallByIndex", "args": {"index": 2}}]},
{"before": ["<leader>", "m", "4"], "commands": [{"command": "mark-and-recall.recallByIndex", "args": {"index": 3}}]},
{"before": ["<leader>", "m", "5"], "commands": [{"command": "mark-and-recall.recallByIndex", "args": {"index": 4}}]},
{"before": ["<leader>", "m", "6"], "commands": [{"command": "mark-and-recall.recallByIndex", "args": {"index": 5}}]},
{"before": ["<leader>", "m", "7"], "commands": [{"command": "mark-and-recall.recallByIndex", "args": {"index": 6}}]},
{"before": ["<leader>", "m", "8"], "commands": [{"command": "mark-and-recall.recallByIndex", "args": {"index": 7}}]},
{"before": ["<leader>", "m", "9"], "commands": [{"command": "mark-and-recall.recallByIndex", "args": {"index": 8}}]}
```

## Installing

Clone the repo and symlink to your extensions directory:

```bash
# For VS Code (local)
ln -s /path/to/mark-and-recall ~/.vscode/extensions/mark-and-recall

# For Cursor (local)
ln -s /path/to/mark-and-recall ~/.cursor/extensions/mark-and-recall

# For VS Code (remote/SSH)
ln -s /path/to/mark-and-recall ~/.vscode-server/extensions/mark-and-recall

# For Cursor (remote/SSH)
ln -s /path/to/mark-and-recall ~/.cursor-server/extensions/mark-and-recall
```

Restart VS Code / Cursor and you're done. The compiled extension is included in the repo.

## Development

```bash
npm install
npm run compile
npm run watch  # for development
```

