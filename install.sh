#!/usr/bin/env bash
set -e

# Terminal colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== Ren'Py Terminal Player Installer ===${NC}"

# 1. Check Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: Python 3 is not installed. Please install it first.${NC}"
    exit 1
fi

# 2. Check mpv
if ! command -v mpv &> /dev/null; then
    echo -e "${YELLOW}Warning: 'mpv' is not installed. Background audio will not play.${NC}"
    echo -e "To install mpv:"
    echo -e "  Debian/Ubuntu: sudo apt install mpv"
    echo -e "  Arch Linux:    sudo pacman -S mpv"
    echo -e "  macOS:         brew install mpv"
    echo ""
else
    echo -e "${GREEN}✓ Found 'mpv' audio engine.${NC}"
fi

# 3. Create virtual environment inside the project directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo -e "${BLUE}Setting up Python virtual environment in .venv...${NC}"
python3 -m venv "$VENV_DIR"

echo -e "${BLUE}Installing required Python dependencies...${NC}"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install textual pillow sixel unrpa rpycdec

# 4. Make run.sh executable
chmod +x "$SCRIPT_DIR/run.sh"

# 5. Create alias in .bashrc and .zshrc
RUN_SCRIPT="$SCRIPT_DIR/run.sh"
ALIAS_LINE="alias rptp=\"$RUN_SCRIPT\""

# Update ~/.bashrc
if [ -f "$HOME/.bashrc" ]; then
    if ! grep -q "alias rptp=" "$HOME/.bashrc"; then
        echo "" >> "$HOME/.bashrc"
        echo "# Ren'Py Terminal Player Alias" >> "$HOME/.bashrc"
        echo "$ALIAS_LINE" >> "$HOME/.bashrc"
        echo -e "${GREEN}✓ Added 'rptp' alias to ~/.bashrc${NC}"
    else
        # Replace existing alias
        sed -i "s|alias rptp=.*|alias rptp=\"$RUN_SCRIPT\"|g" "$HOME/.bashrc"
        echo -e "${GREEN}✓ Updated 'rptp' alias in ~/.bashrc${NC}"
    fi
fi

# Update ~/.zshrc
if [ -f "$HOME/.zshrc" ]; then
    if ! grep -q "alias rptp=" "$HOME/.zshrc"; then
        echo "" >> "$HOME/.zshrc"
        echo "# Ren'Py Terminal Player Alias" >> "$HOME/.zshrc"
        echo "$ALIAS_LINE" >> "$HOME/.zshrc"
        echo -e "${GREEN}✓ Added 'rptp' alias to ~/.zshrc${NC}"
    else
        # Replace existing alias
        sed -i "s|alias rptp=.*|alias rptp=\"$RUN_SCRIPT\"|g" "$HOME/.zshrc"
        echo -e "${GREEN}✓ Updated 'rptp' alias in ~/.zshrc${NC}"
    fi
fi

echo ""
echo -e "${GREEN}✓ Dependencies installed successfully!${NC}"
echo -e "${GREEN}✓ Setup complete.${NC}"
echo ""
echo -e "To apply the command alias, run:"
echo -e "  ${BLUE}source ~/.bashrc${NC} (or ${BLUE}source ~/.zshrc${NC})"
echo ""
echo -e "Then you can start the player from anywhere by typing:"
echo -e "  ${BLUE}rptp /path/to/game_directory${NC}"
echo ""

