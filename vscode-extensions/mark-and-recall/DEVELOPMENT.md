# Development

## Manual Installation (without Marketplace)

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

## Building

```bash
npm install
npm run compile
npm run watch  # for development
```

## Testing

```bash
npm test          # run tests once
npm run test:watch  # run tests in watch mode
```

## Publishing

```bash
npm install -g @vscode/vsce
vsce publish
```

Requires `VSCE_PAT` environment variable with a Personal Access Token from Azure DevOps.
