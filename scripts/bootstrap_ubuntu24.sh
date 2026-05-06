#!/usr/bin/env bash
set -euo pipefail

APP_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="/opt/world-state/app"
VENV_DIR="/opt/world-state/venv"
CONFIG_DIR="/etc/world-state"
DATA_DIR="/var/lib/world-state"

apt update
apt install -y python3 python3-venv python3-pip sqlite3 jq curl ca-certificates logrotate rsync

mkdir -p /opt/world-state "$CONFIG_DIR" "$DATA_DIR"

if [[ "$APP_SRC" != "$APP_DIR" ]]; then
  rm -rf "$APP_DIR"
  mkdir -p "$APP_DIR"
  cp -a "$APP_SRC"/. "$APP_DIR"/
fi

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"

if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
  cp "$APP_DIR/config.example.yaml" "$CONFIG_DIR/config.yaml"
fi

cp "$APP_DIR/systemd/world-state-collector.service" /etc/systemd/system/world-state-collector.service
cp "$APP_DIR/systemd/world-state-collector.timer" /etc/systemd/system/world-state-collector.timer
cp "$APP_DIR/systemd/world-state-rss.service" /etc/systemd/system/world-state-rss.service
cp "$APP_DIR/systemd/world-state-rss.timer" /etc/systemd/system/world-state-rss.timer
cp "$APP_DIR/systemd/world-state-treasury.service" /etc/systemd/system/world-state-treasury.service
cp "$APP_DIR/systemd/world-state-treasury.timer" /etc/systemd/system/world-state-treasury.timer
cp "$APP_DIR/systemd/world-state-gdelt.service" /etc/systemd/system/world-state-gdelt.service
cp "$APP_DIR/systemd/world-state-gdelt.timer" /etc/systemd/system/world-state-gdelt.timer
cp "$APP_DIR/logrotate/world-state" /etc/logrotate.d/world-state

systemctl daemon-reload
systemctl disable --now world-state-collector.timer 2>/dev/null || true
systemctl enable world-state-rss.timer world-state-treasury.timer world-state-gdelt.timer

echo "Installed world-state collector."
echo "Edit config: sudo nano $CONFIG_DIR/config.yaml"
echo "Run one source: sudo systemctl start world-state-rss.service"
echo "Enable split timers now: sudo systemctl enable --now world-state-rss.timer world-state-treasury.timer world-state-gdelt.timer"

