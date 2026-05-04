#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/jlcbk/world-state-vps.git}"
INSTALL_PARENT="${INSTALL_PARENT:-/opt/world-state}"
APP_DIR="${APP_DIR:-$INSTALL_PARENT/app}"
CONFIG_FILE="/etc/world-state/config.yaml"

if [[ "${EUID}" -eq 0 ]]; then
  SUDO=""
else
  SUDO="sudo"
fi

echo "==> Installing base packages"
$SUDO apt update
$SUDO apt install -y git ca-certificates curl

echo "==> Preparing install directory: $APP_DIR"
$SUDO mkdir -p "$INSTALL_PARENT"

if [[ -d "$APP_DIR/.git" ]]; then
  echo "==> Existing checkout found, pulling latest"
  $SUDO git -C "$APP_DIR" pull --ff-only
else
  if [[ -e "$APP_DIR" ]]; then
    echo "==> Removing non-git app directory: $APP_DIR"
    $SUDO rm -rf "$APP_DIR"
  fi
  echo "==> Cloning $REPO_URL"
  $SUDO git clone "$REPO_URL" "$APP_DIR"
fi

echo "==> Running bootstrap"
$SUDO bash "$APP_DIR/scripts/bootstrap_ubuntu24.sh"

echo "==> Starting collector once"
$SUDO systemctl start world-state-collector.service

echo "==> Enabling 15-minute timer"
$SUDO systemctl enable --now world-state-collector.timer

echo
echo "Deployment complete."
echo
echo "Config file:"
echo "  $CONFIG_FILE"
echo
echo "Useful checks:"
echo "  journalctl -u world-state-collector.service -n 100 --no-pager"
echo "  systemctl list-timers | grep world-state"
echo "  jq . /var/lib/world-state/world_state.json"
echo "  tail -n 20 /var/lib/world-state/events.jsonl"
echo "  tail -n 20 /var/lib/world-state/alerts.jsonl"

