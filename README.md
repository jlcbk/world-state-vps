# World State VPS Node

Lightweight Ubuntu 24 collector node for building a rolling "world state" feed.

Designed for a small VPS: 2 vCPU, 2GB RAM, and 15-30GB free disk.

## What Runs On The VPS

- Pulls GDELT DOC API queries.
- Pulls selected RSS feeds.
- Pulls Treasury Press Releases from HTML and stores full article text.
- Stores compact metadata plus selected high-value full text in SQLite.
- Writes `world_state.json`, `alerts.jsonl`, and `events.jsonl`.
- Keeps only recent hot data on VPS.

Heavy analysis, BettaFish, MiroFish, backtests, graph building, and long-term archives should run on the Mac mini.

## Quick Install On Ubuntu 24

### One-command deploy after logging into the VPS

```bash
curl -fsSL https://raw.githubusercontent.com/jlcbk/world-state-vps/main/scripts/deploy_on_vps_ubuntu24.sh -o /tmp/deploy_world_state.sh
bash /tmp/deploy_world_state.sh
```

For a private repository, the VPS must be able to access GitHub first. Use a GitHub deploy key, `gh auth login`, or clone/upload the repository manually.

You can override the repository URL:

```bash
REPO_URL=git@github.com:jlcbk/world-state-vps.git bash /tmp/deploy_world_state.sh
```

### Manual install from a local checkout

```bash
sudo apt update
sudo apt install -y git
git clone <your-copy-of-this-folder> world-state-vps
cd world-state-vps
sudo bash scripts/bootstrap_ubuntu24.sh
```

If you are copying files manually:

```bash
sudo mkdir -p /opt/world-state
sudo cp -r . /opt/world-state/app
sudo bash /opt/world-state/app/scripts/bootstrap_ubuntu24.sh
```

Then edit:

```bash
sudo nano /etc/world-state/config.yaml
```

Run one source once:

```bash
sudo systemctl start world-state-rss.service
sudo systemctl start world-state-treasury.service
sudo systemctl start world-state-gdelt.service
```

Enable split source timers:

```bash
sudo systemctl enable --now world-state-rss.timer world-state-treasury.timer world-state-gdelt.timer
```

Default collection schedule:

```text
RSS official feeds:       every 15 minutes, at :00/:15/:30/:45
Treasury HTML full text:  every 30 minutes, at :05/:35
GDELT DOC API:            every 60 minutes, at :10
```

The split services use `/usr/bin/flock /run/world-state-collector.lock` so only one collector writes SQLite/state files at a time. The legacy all-in-one `world-state-collector.timer` is installed for compatibility but disabled by bootstrap.

Check logs:

```bash
journalctl -u world-state-rss.service -n 100 --no-pager
journalctl -u world-state-treasury.service -n 100 --no-pager
journalctl -u world-state-gdelt.service -n 100 --no-pager
systemctl list-timers | grep world-state
```

Outputs:

```text
/var/lib/world-state/world_state.db
/var/lib/world-state/world_state.json
/var/lib/world-state/events.jsonl
/var/lib/world-state/alerts.jsonl
```

## Disk Budget

Default config is conservative enough for a VPS with about 15GB free disk:

- SQLite metadata: target under 1-3GB.
- JSONL hot output: target under 500MB-1GB.
- No full-page snapshots on VPS.
- Sync older data to Mac mini, then delete or compact.

Default retention:

- Raw article metadata: 3 days
- State snapshots: 7 days
- Alerts/events JSONL: use logrotate, 14 rotated files

For a larger VPS, you can raise:

```yaml
storage:
  hot_retention_days: 14
  snapshot_retention_days: 30

collector:
  max_articles_per_query: 25
  gdelt_timespan: "2h"
  gdelt_request_delay_seconds: 15
```

## Manual Commands

Run all sources once:

```bash
/opt/world-state/venv/bin/python /opt/world-state/app/world_state_collector.py --config /etc/world-state/config.yaml
```

Run one source group:

```bash
/opt/world-state/venv/bin/python /opt/world-state/app/world_state_collector.py --config /etc/world-state/config.yaml --sources rss
/opt/world-state/venv/bin/python /opt/world-state/app/world_state_collector.py --config /etc/world-state/config.yaml --sources html
/opt/world-state/venv/bin/python /opt/world-state/app/world_state_collector.py --config /etc/world-state/config.yaml --sources gdelt
```

Show latest state:

```bash
jq . /var/lib/world-state/world_state.json
```

Backup to Mac mini over Tailscale or LAN:

```bash
rsync -avz /var/lib/world-state/ macmini:/Users/cui/world-state-vps/
```

## Optional Cold Archive With Rclone

Cold archive support is optional and disabled by default. Use it only after you have a WebDAV, S3, R2, B2, or other rclone-compatible storage target.

Install archive support:

```bash
sudo bash /opt/world-state/app/scripts/setup_archive_timer.sh
```

Configure your remote:

```bash
rclone config
sudo nano /etc/world-state/archive.env
```

Example `/etc/world-state/archive.env`:

```bash
RCLONE_REMOTE=worlddav
REMOTE_PATH=world-state-archive
DATA_DIR=/var/lib/world-state
KEEP_LOCAL_ARCHIVES_DAYS=7
```

Test once:

```bash
sudo systemctl start world-state-archive.service
journalctl -u world-state-archive.service -n 100 --no-pager
```

Enable daily archive:

```bash
sudo systemctl enable --now world-state-archive.timer
```

This archive job exports the previous UTC day's SQLite metadata and state snapshots, packages them with JSONL outputs, compresses the bundle with zstd, uploads it through rclone, then keeps only a short local archive cache.

Do not put `world_state.db` directly on WebDAV or any unreliable network mount. The collector writes to local disk first; archive upload is asynchronous.
