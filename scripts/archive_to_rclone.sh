#!/usr/bin/env bash
set -euo pipefail

CONFIG_FILE="${CONFIG_FILE:-/etc/world-state/archive.env}"
DATA_DIR="${DATA_DIR:-/var/lib/world-state}"
ARCHIVE_DIR="${ARCHIVE_DIR:-$DATA_DIR/archive}"
WORK_DIR="${WORK_DIR:-$DATA_DIR/archive-work}"

if [[ -f "$CONFIG_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi

RCLONE_REMOTE="${RCLONE_REMOTE:-}"
REMOTE_PATH="${REMOTE_PATH:-world-state-archive}"
KEEP_LOCAL_ARCHIVES_DAYS="${KEEP_LOCAL_ARCHIVES_DAYS:-7}"
RETENTION_EXPORT_DAYS="${RETENTION_EXPORT_DAYS:-1}"
DB_PATH="${DB_PATH:-$DATA_DIR/world_state.db}"

if [[ -z "$RCLONE_REMOTE" ]]; then
  echo "RCLONE_REMOTE is not configured. Edit $CONFIG_FILE after running rclone config." >&2
  exit 2
fi

if ! command -v rclone >/dev/null 2>&1; then
  echo "rclone is not installed. Run: sudo apt install -y rclone" >&2
  exit 2
fi

if ! command -v zstd >/dev/null 2>&1; then
  echo "zstd is not installed. Run: sudo apt install -y zstd" >&2
  exit 2
fi

if [[ ! -f "$DB_PATH" ]]; then
  echo "Database not found: $DB_PATH" >&2
  exit 0
fi

mkdir -p "$ARCHIVE_DIR" "$WORK_DIR"

DAY="${ARCHIVE_DAY:-$(date -u -d 'yesterday' +%F)}"
START_TS="${DAY}T00:00:00+00:00"
END_TS="$(date -u -d "${DAY} +1 day" +%FT%T+00:00)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BASE="world-state-${DAY}"
OUT_DIR="$WORK_DIR/$BASE"
FINAL_TAR="$ARCHIVE_DIR/${BASE}-${STAMP}.tar.zst"
MANIFEST="$OUT_DIR/manifest.json"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

sqlite3 -json "$DB_PATH" \
  "SELECT * FROM articles WHERE collected_at >= '$START_TS' AND collected_at < '$END_TS' ORDER BY collected_at;" \
  > "$OUT_DIR/articles.json"

sqlite3 -json "$DB_PATH" \
  "SELECT * FROM state_snapshots WHERE created_at >= '$START_TS' AND created_at < '$END_TS' ORDER BY created_at;" \
  > "$OUT_DIR/state_snapshots.json"

for name in events alerts compact_state; do
  src="$DATA_DIR/${name}.jsonl"
  if [[ -f "$src" ]]; then
    cp "$src" "$OUT_DIR/${name}.jsonl"
  fi
done

cat > "$MANIFEST" <<EOF
{
  "archive_day": "$DAY",
  "created_at": "$(date -u +%FT%TZ)",
  "hostname": "$(hostname)",
  "db_path": "$DB_PATH",
  "start_ts": "$START_TS",
  "end_ts": "$END_TS"
}
EOF

tar -C "$WORK_DIR" -cf - "$BASE" | zstd -T0 -10 -o "$FINAL_TAR" >/dev/null
sha256sum "$FINAL_TAR" > "$FINAL_TAR.sha256"

rclone copy "$FINAL_TAR" "$RCLONE_REMOTE:$REMOTE_PATH/daily/" \
  --transfers 2 \
  --checkers 4 \
  --retries 5 \
  --low-level-retries 10 \
  --timeout 60s \
  --contimeout 10s

rclone copy "$FINAL_TAR.sha256" "$RCLONE_REMOTE:$REMOTE_PATH/daily/" \
  --transfers 2 \
  --checkers 4 \
  --retries 5 \
  --low-level-retries 10 \
  --timeout 60s \
  --contimeout 10s

find "$ARCHIVE_DIR" -type f -mtime "+$KEEP_LOCAL_ARCHIVES_DAYS" -delete
rm -rf "$OUT_DIR"

echo "Uploaded archive: $FINAL_TAR -> $RCLONE_REMOTE:$REMOTE_PATH/daily/"

