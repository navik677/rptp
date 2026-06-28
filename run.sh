#!/usr/bin/env bash
# Quick launcher for the Ren'Py TUI Player
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/../renpy_venv"

if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install textual pillow
fi

"$VENV/bin/python3" "$SCRIPT_DIR/renpy_player.py" "$@"
