#!/bin/bash
# Fogwatch installer for macOS:  curl -fsSL https://imranbug.github.io/fogwatch/install.sh | bash
# Installs uv (Python runner) if missing, puts Fogwatch in ~/Fogwatch (an update keeps your data/ and .env), then starts it.
set -euo pipefail
[ "$(uname)" = "Darwin" ] || { echo "Fogwatch runs on macOS only for now."; exit 1; }
DEST="$HOME/Fogwatch"
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
echo "Downloading Fogwatch…"
curl -fsSL https://github.com/ImranBug/fogwatch/archive/refs/heads/main.tar.gz | tar -xz -C "$TMP"
mkdir -p "$DEST"
rsync -a --exclude data/ --exclude recordings/ --exclude .env --exclude .venv/ "$TMP"/fogwatch-main/ "$DEST"/
chmod +x "$DEST/start.command"
cd "$DEST" && uv sync -q
echo
echo "Fogwatch is in $DEST. Starting it now (next time: double-click $DEST/start.command)."
echo "Two one-time steps: Steam > Dota 2 > Properties > Launch Options: -gamestateintegration"
echo "                    System Settings > Privacy & Security > Screen Recording: allow Terminal"
open -a Terminal "$DEST/start.command"
