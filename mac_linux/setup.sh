#!/usr/bin/env bash
# Faculty Emails Extractor - one-time setup (macOS / Linux)
# Run from a terminal:  bash mac_linux/setup.sh
set -e
cd "$(dirname "$0")/.."
echo "=== Faculty Emails Extractor - setup ==="

PY=python3
command -v "$PY" >/dev/null 2>&1 || { echo "Python 3.10+ is not installed: https://www.python.org/downloads/"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "Python 3.10 or newer is needed."; exit 1; }
if [ ! -d "/Applications/Google Chrome.app" ] && ! command -v google-chrome >/dev/null 2>&1; then
  echo "WARNING: Google Chrome was not found. Install it from https://www.google.com/chrome/"
fi

[ -x .venv/bin/python ] || "$PY" -m venv .venv
echo "Installing packages (this can take a few minutes) ..."
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env - open it in a text editor and add your API keys."
fi
echo
echo "Setup complete. Next: add your API keys to .env, then run:  bash mac_linux/run_module1.sh"
