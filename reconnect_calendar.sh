#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="$SCRIPT_DIR/venv/bin/python3"

if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

cd "$SCRIPT_DIR/FRIDAY_OS" || exit 1
echo "Removing expired Google Calendar token..."
rm -f Data/calendar_token.json
rm -f calendar_token.json
rm -f token.json
"$PYTHON_BIN" -m Sensory_Array.calendar_tools --reauthorize
