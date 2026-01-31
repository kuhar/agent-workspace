# Mark and Recall

A VS Code extension for quick navigation to bookmarked locations defined in a `marks.md` file.

## Usage

1. Create a `marks.md` file in your workspace root
2. Add marks in the format: `name: <path>:<line>`
3. Run the command "Recall: Go to Mark" (Cmd/Ctrl+Shift+P, then type "Recall")
4. Select a mark to navigate to it

## marks.md Format

```md
tester: agents/llvm-tester.md:11
map: /home/jakub/agent-workspace/commands/populate-project-map.md:19
```

- **name**: A short identifier for the mark
- **path**: Either relative to the workspace root or an absolute path
- **line**: The line number to navigate to (1-based)

Lines starting with `#` are treated as comments and ignored.

## Development

```bash
# Install dependencies
npm install

# Compile
npm run compile

# Watch for changes
npm run watch
```

## Installing Locally

1. Run `npm run compile`
2. Copy the `mark-and-recall` folder to your VS Code extensions directory:
   - Linux: `~/.vscode/extensions/`
   - macOS: `~/.vscode/extensions/`
   - Windows: `%USERPROFILE%\.vscode\extensions\`
3. Restart VS Code
