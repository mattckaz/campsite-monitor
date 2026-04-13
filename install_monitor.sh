#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  Campsite Monitor — One-Time Installer
#
#  This sets up the monitor to run every hour automatically, completely
#  independent of the Claude app.  Run it once in Terminal, then you're done.
#
#  Usage:
#    chmod +x install_monitor.sh
#    ./install_monitor.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e

BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[0;33m"
RED="\033[0;31m"
RESET="\033[0m"

echo ""
echo -e "${BOLD}🏕️  Campsite Monitor — Background Service Installer${RESET}"
echo "────────────────────────────────────────────────────"
echo ""

# ── Locate files ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="$SCRIPT_DIR/campsite_monitor.py"
VENV_DIR="$SCRIPT_DIR/venv"
PLIST_LABEL="com.mattkaz.campsite-monitor"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"

echo -e "📂 ${BOLD}Script folder:${RESET} $SCRIPT_DIR"
echo ""

if [ ! -f "$PY_SCRIPT" ]; then
    echo -e "${RED}ERROR: campsite_monitor.py not found in $SCRIPT_DIR${RESET}"
    echo "Make sure you're running this from the campsite-monitor folder."
    exit 1
fi

# ── Python virtual environment ────────────────────────────────────────────────
echo -e "🐍 ${BOLD}Setting up Python virtual environment...${RESET}"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

echo -e "📦 ${BOLD}Installing Python packages...${RESET}"
pip install --quiet --upgrade pip
pip install --quiet playwright playwright-stealth

echo -e "🌐 ${BOLD}Installing browser engines (Chromium + Firefox)...${RESET}"
echo "    (This downloads ~200 MB the first time — please wait)"
playwright install chromium firefox
deactivate

echo ""
echo -e "${GREEN}✓ Python environment ready${RESET}"
echo ""

# ── Create launchd plist ──────────────────────────────────────────────────────
echo -e "⏰ ${BOLD}Creating hourly schedule...${RESET}"
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>

    <!-- Python interpreter from our venv + the monitor script -->
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_DIR}/bin/python3</string>
        <string>${PY_SCRIPT}</string>
    </array>

    <!-- Run every 3600 seconds (1 hour).
         If the Mac was asleep and missed a run, it fires on next wake. -->
    <key>StartInterval</key>
    <integer>3600</integer>

    <!-- Don't run immediately on install — wait for first interval -->
    <key>RunAtLoad</key>
    <false/>

    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>

    <!-- Capture output for debugging (separate from the monitor's own log) -->
    <key>StandardOutPath</key>
    <string>${SCRIPT_DIR}/launchd_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${SCRIPT_DIR}/launchd_stderr.log</string>

    <!-- Don't restart if the script exits normally -->
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
PLIST

# ── Load the service ──────────────────────────────────────────────────────────
# Unload any previous version first (ignore errors if it wasn't loaded)
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo ""
echo -e "────────────────────────────────────────────────────"
echo -e "${GREEN}${BOLD}✅  Installation complete!${RESET}"
echo ""
echo -e "The monitor will now run ${BOLD}every hour${RESET}, automatically,"
echo -e "even when Claude is closed. It starts at next reboot too."
echo ""
echo -e "   ${BOLD}Dashboard:${RESET}  https://matt-campsite.netlify.app"
echo -e "   ${BOLD}Log file:${RESET}   $SCRIPT_DIR/monitor.log"
echo ""
echo -e "${YELLOW}Useful commands:${RESET}"
echo -e "   Check status:  launchctl list | grep campsite"
echo -e "   Stop monitor:  launchctl unload \"$PLIST_PATH\""
echo -e "   Start again:   launchctl load \"$PLIST_PATH\""
echo -e "   Run now:       \"$VENV_DIR/bin/python3\" \"$PY_SCRIPT\""
echo ""
