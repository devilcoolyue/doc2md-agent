#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

cd "$PROJECT_DIR"

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

if [ -f "frontend/package.json" ]; then
  pushd frontend >/dev/null
  npm install
  npm run build
  popd >/dev/null
fi

exec uvicorn backend.server:app --host 0.0.0.0 --port 8080
