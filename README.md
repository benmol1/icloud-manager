# iCloud Storage Manager

A Python service that keeps an Apple iCloud photo/video library from filling up.
It runs on a Raspberry Pi in Docker, periodically scans iCloud via
[`pyicloud`](https://pypi.org/project/pyicloud/), scores each asset by age, size,
duplication, source app, and favourite status, then pushes recommendations — and,
for clearly non-essential files, acts on them automatically — through a Telegram
bot. Offloaded files are written to a Windows PC's 2 TB drive over SMB and then
removed from iCloud.

> **Scope:** this service targets a single iCloud account (Ben's). Multi-account
> support is explicitly out of scope.

## How it works

```
scheduler → scanner → analyser → recommender → notifier → actions
```

1. **scheduler** triggers a weekly scan.
2. **scanner** authenticates with iCloud (handling 2FA on first run) and fetches
   photo/video metadata: filename, size, creation date, album membership, and the
   `isFavorite` flag.
3. **analyser** scores each asset. It penalises age, large/duplicate files, and
   WhatsApp-origin media; it rewards favourites and Photos-origin assets.
4. **recommender** splits assets into three buckets:
   - **auto-offload** — safe to move without asking (see rules below)
   - **review** — surfaced for manual approval
   - **keep** — left in place
5. **notifier** sends a Telegram summary; review-bucket items need an
   inline-keyboard approval before anything happens.
6. **actions** downloads approved/auto assets, writes them to the SMB share
   (organised by year/month), and deletes them from iCloud. Files whose iCloud
   name is an opaque UUID (WhatsApp/AirDrop media) are given a recognisable
   `YYYYMMDD_HHMMSS_<source>_<short>.ext` name; real names (`IMG_2351.JPG`) are
   kept as-is.

Every scan also upserts each asset into a **searchable SQLite index**
(`app/index.py`), recording metadata plus offload status — see
[Asset index](#asset-index).

### Auto-offload rules

An asset is moved automatically **only** if **all** of these hold:

- it is **not** a favourite,
- its source is **WhatsApp** (nursery chats etc. are the main culprit),
- it is at least `MIN_AGE_DAYS` (default **180**) old, **and**
- its score is at least `AUTO_OFFLOAD_THRESHOLD`.

Everything else that scores at or above `REVIEW_THRESHOLD` is sent for manual
review. Favourites are never auto-offloaded.

## Asset index

Every run upserts each scanned asset into a single-table SQLite database
(`INDEX_DB_PATH`, default `data/asset_index.db`, persisted to a Docker volume).
It records the rich metadata the scanner extracts — capture/added dates, GPS
(decoded from iCloud's `locationEnc` binary plist), dimensions, duration,
fingerprint, source, favourite/hidden flags, and more — plus each asset's
offload status and where it ended up on the NAS. Re-scans refresh metadata
without clobbering offload state or `first_seen_at`.

This answers "where did file X go?", supports auditing, and (via the stored
`fingerprint`/`change_tag`) sets up real dedup and incremental scans. Note that
**people/face tags, scene keywords, place names, and EXIF camera/lens data are
not available** from iCloud's metadata — see
[docs/asset-index-research-spike.md](docs/asset-index-research-spike.md).

```bash
# Summary counts by status (in_icloud / offloaded)
uv run python -m app.index stats

# Filtered lookup
uv run python -m app.index search --source whatsapp --status in_icloud
uv run python -m app.index search --media-type video --since 2020-01-01 --until 2020-12-31
```

## Project layout

```
app/
  config.py        # Loads settings from environment / .env
  models.py        # Asset / Source / MediaType data models
  scanner.py       # Authenticates with pyicloud, walks the library + rich metadata
  twofactor.py     # Interactive first-time 2FA login + session caching
  analyser.py      # Scores assets by age, size, source, duplication, favourite
  recommender.py   # Splits assets into auto-offload / review / keep buckets
  actions.py       # Offload: writes to SMB by year/month, deletes from iCloud
  index.py         # Searchable SQLite asset index + query CLI
  main.py          # End-to-end pipeline runner (scan → … → offload)
  notifier.py      # Telegram bot integration            (not yet built)
  scheduler.py     # Weekly trigger + pipeline wiring     (not yet built)
tests/
docs/
  asset-index-research-spike.md   # Index design + available iCloud metadata
docker-compose.yml
Dockerfile
.env.example
```

See [TODO.md](TODO.md) for current build status. Project setup, the scanner
(incl. rich metadata extraction), the analyser & recommender, the offload
actions (dry-run; live download/delete seam awaiting a concrete iCloud source),
and the searchable asset index are in place. The Telegram notifier, scheduler,
and Pi deployment remain.

## Getting started

### Prerequisites

- Python 3.12+ and [`uv`](https://docs.astral.sh/uv/)
- An Apple ID with two-factor authentication
- A Telegram bot token and chat ID (create the bot via
  [BotFather](https://t.me/botfather))
- A Windows PC sharing a drive over SMB
- A Raspberry Pi running Docker (for deployment)

### Setup

```bash
# Install dependencies (including dev tools)
uv sync --extra dev

# Create your config and fill it in
cp .env.example .env   # then edit .env

# Complete the first-time iCloud 2FA login (caches the session)
uv run python -m app.twofactor
```

### Running

```bash
# One-off scan from the CLI
uv run python -m app.scanner

# Run the full service
uv run python -m app.main

# Tests
uv run pytest
uv run pytest tests/test_analyser.py -v   # a single file
```

### Docker

```bash
docker build -t icloud-manager .

# Single run (requires .env)
docker run --env-file .env icloud-manager

# Pi deployment (mounts the SMB share as a volume)
docker compose up -d
```

## Configuration

All settings come from environment variables (or a local `.env`). See
[.env.example](.env.example) for the full list with sample values.

| Variable | Purpose | Default |
|---|---|---|
| `ICLOUD_USERNAME` | Apple ID email | — |
| `ICLOUD_PASSWORD` | Apple ID password | — |
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather | — |
| `TELEGRAM_CHAT_ID` | Your personal chat ID | — |
| `SMB_HOST` | Windows PC hostname or IP | — |
| `SMB_SHARE` | Share name (e.g. `Storage`) | — |
| `SMB_USERNAME` / `SMB_PASSWORD` | Windows credentials | — |
| `SMB_MOUNT_PATH` | Container path where the share is mounted | `/mnt/storage` |
| `DRY_RUN` | Log actions without downloading or deleting | `true` |
| `MIN_AGE_DAYS` | Minimum age before an asset is eligible for offload | `180` |
| `INDEX_DB_PATH` | SQLite asset-index file (on a Docker volume in prod) | `data/asset_index.db` |
| `SCAN_SINCE` / `SCAN_UNTIL` | Optional capture-date window (`YYYY-MM-DD`, inclusive) to limit a scan | — / — |
| `SCAN_DAY_OF_WEEK` / `SCAN_TIME` | Weekly scan schedule | `sunday` / `02:00` |
| `WEIGHT_AGE` / `WEIGHT_SIZE` / `WEIGHT_SOURCE` / `WEIGHT_DUPLICATE` | Scoring weights (must sum to 100) | `35` / `20` / `30` / `15` |
| `AUTO_OFFLOAD_THRESHOLD` | Score at/above which non-favourites auto-offload | `65` |
| `REVIEW_THRESHOLD` | Score at/above which assets go for manual review | `40` |
| `FAVORITE_SCORE_PENALTY` | Score penalty applied to favourites | `60` |
| `LARGE_FILE_MB` | Size (MB) at which an asset gets the full size score | `50` |

> **Security:** the `.env` file holds your Apple ID and Windows credentials. Keep
> it out of version control and restrict its permissions on the Pi
> (`chmod 600 .env`).

## Safety

- **Dry-run by default** (`DRY_RUN=true`) — the service logs what it *would* do
  without downloading or deleting anything. Set it to `false` only once you've
  verified behaviour.
- Files are deleted from iCloud **only after** a confirmed write to the SMB share.
- Favourites are never auto-offloaded.
