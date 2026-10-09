#!/usr/bin/env bash
# Start the preserved original compiled dashboard on macOS / Linux.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR/frontend"

if [[ ! -f "data/meta.json" ]]; then
  echo "==> Syncing local data snapshot for the original interface..."
  mkdir -p data
  cp -R public/data/. data/
fi

echo "==> Original interface: http://127.0.0.1:5175/legacy.html"
python3 -m http.server 5175 --bind 127.0.0.1
