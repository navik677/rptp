#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

if [ ! -d "$VENV" ]; then
    echo "Error: Python virtual environment not found in .venv/"
    echo "Please run the installer first: ./install.sh"
    exit 1
fi

"$VENV/bin/python3" "$SCRIPT_DIR/renpy_player.py" "$@"
