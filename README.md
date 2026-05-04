# World State VPS Node

Lightweight Ubuntu 24 collector node for building a rolling "world state" feed.

Designed for a small VPS: 2 vCPU, 2GB RAM, about 30GB free disk.

## What Runs On The VPS

- Pulls GDELT DOC API queries.
- Pulls selected RSS feeds.
- Stores compact metadata in SQLite.
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

Run once:

```bash
sudo systemctl start world-state-collector.service
```

Enable the timer:

```bash
sudo systemctl enable --now world-state-collector.timer
```

Check logs:

```bash
journalctl -u world-state-collector.service -n 100 --no-pager
```

Outputs:

```text
/var/lib/world-state/world_state.db
/var/lib/world-state/world_state.json
/var/lib/world-state/events.jsonl
/var/lib/world-state/alerts.jsonl
```

## Disk Budget

Recommended for 30GB free disk:

- SQLite metadata: target under 5GB.
- JSONL hot output: target under 2GB.
- No full-page snapshots on VPS.
- Sync older data to Mac mini, then delete or compact.

Default retention:

- Raw article metadata: 14 days
- State snapshots: 30 days
- Alerts/events JSONL: use logrotate, 14 rotated files

## Manual Commands

Run collector once:

```bash
/opt/world-state/venv/bin/python /opt/world-state/app/world_state_collector.py --config /etc/world-state/config.yaml
```

Show latest state:

```bash
jq . /var/lib/world-state/world_state.json
```

Backup to Mac mini over Tailscale or LAN:

```bash
rsync -avz /var/lib/world-state/ macmini:/Users/cui/world-state-vps/
```
