#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="$SCRIPT_DIR/venv/bin/python3"

if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

cd "$SCRIPT_DIR/FRIDAY_OS" || exit 1
echo "Launching FRIDAY MK1..."
"$PYTHON_BIN" -m Core_Cognition.main
