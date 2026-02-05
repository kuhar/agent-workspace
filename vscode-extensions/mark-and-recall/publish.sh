#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Running tests..."
npm test

echo "==> Packaging extension..."
npx vsce package

VSIX=$(ls -t *.vsix | head -1)
echo "==> Built $VSIX"

echo "==> Publishing to VS Code Marketplace..."
npx vsce publish

echo "==> Publishing to Open VSX..."
npx ovsx publish "$VSIX"

echo "==> Done."
