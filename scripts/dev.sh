#!/usr/bin/env bash
# Start RoomLoop from a clean checkout (macOS / Linux).
set -euo pipefail
cd "$(dirname "$0")/.."

[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade pip --quiet
python -m pip install -r requirements-dev.txt --quiet
python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Berlin'); ZoneInfo('America/Denver')" \
  || { echo "Missing timezone data; installing tzdata"; python -m pip install tzdata; }

python -m roomloop.seed --reset
echo
echo "RoomLoop console: http://localhost:${PORT:-8000}/console"
echo "API docs:         http://localhost:${PORT:-8000}/docs"
echo
exec python -m uvicorn roomloop.api:app --reload --port "${PORT:-8000}"
