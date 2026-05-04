#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -eq 0 ]]; then
  SUDO=""
else
  SUDO="sudo"
fi

APP_DIR="${APP_DIR:-/opt/world-state/app}"
CONFIG_FILE="/etc/world-state/archive.env"

$SUDO apt update
$SUDO apt install -y rclone zstd sqlite3

if [[ ! -f "$CONFIG_FILE" ]]; then
  $SUDO tee "$CONFIG_FILE" >/dev/null <<'EOF'
# Configure this after running: rclone config
# Example:
# RCLONE_REMOTE=worlddav
# REMOTE_PATH=world-state-archive

RCLONE_REMOTE=
REMOTE_PATH=world-state-archive
DATA_DIR=/var/lib/world-state
KEEP_LOCAL_ARCHIVES_DAYS=7
EOF
  echo "Created $CONFIG_FILE"
fi

$SUDO cp "$APP_DIR/systemd/world-state-archive.service" /etc/systemd/system/world-state-archive.service
$SUDO cp "$APP_DIR/systemd/world-state-archive.timer" /etc/systemd/system/world-state-archive.timer
$SUDO systemctl daemon-reload

echo
echo "Archive support installed, but not enabled yet."
echo
echo "Next steps:"
echo "  1. Run: rclone config"
echo "  2. Edit: sudo nano $CONFIG_FILE"
echo "  3. Test: sudo systemctl start world-state-archive.service"
echo "  4. Enable: sudo systemctl enable --now world-state-archive.timer"

