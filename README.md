# iCloud Storage Manager

A Python service that keeps an Apple iCloud photo/video library from filling up.
It scans iCloud via [`pyicloud`](https://pypi.org/project/pyicloud/), scores each
asset by age, size, duplication, source app, and favourite status, and offloads
clearly non-essential files to a local/NAS archive — downloading them, writing
them into a year/month folder tree (content-deduped), and then removing them from
iCloud. Every asset is recorded in a searchable SQLite index.

> ## 🅿️ Status — shelved (2026-06-17), primary objective achieved
>
> The original goal is **done**: a large slice of the live iCloud library has
> been offloaded into an organised `YYYY/MM` archive on `D:\icloud-photos`,
> content-deduped, and recorded in the index — and iCloud has plenty of free
> space again. The project is paused here.
>
> Two original goals were **dropped, not deferred**:
> - **Telegram notifier / manual Approve-Skip flow** — the unattended
>   auto-offload + content-dedup pipeline does the job; per-file approval wasn't
>   worth it.
> - **`local` vs `network` storage-tier optimisation** — moving assets between
>   iCloud / local / network is now easy and programmatic, so where an asset
>   physically lives no longer needs optimising (the `storage_tier` column stays).
>
> The interesting future work has shifted from *storage management* to **memory
> curation** (yearly photo books + per-person video reels). See
> [Future direction](#future-direction--memory-curation) and [TODO.md](TODO.md).

> **Scope:** offload/storage targets a **single iCloud account** (Ben's). The
> curation work ahead deliberately brings a **second account (Emma's)** back into
> scope, since building family photo books needs both libraries.

## How it works

Runs are **on-demand** from the CLI (`python -m app.main`) — the weekly scheduler
is on hold (see [Status](#-status--shelved-2026-06-17-primary-objective-achieved)).

```
scanner → analyser → recommender → actions
```

1. **scanner** authenticates with iCloud (handling 2FA on first run) and fetches
   photo/video metadata: filename, size, capture date, album membership, the
   `isFavorite` flag, and rich metadata (GPS, dimensions, duration, fingerprint,
   …). A live scan can be skipped entirely with `SCAN_FROM_INDEX=true`, reading
   straight from the SQLite index.
2. **analyser** scores each asset. It penalises age, large/duplicate files, and
   WhatsApp-origin media; it rewards favourites and Photos-origin assets.
   Duplicate detection uses Apple's content `fingerprint` where available.
3. **recommender** splits assets into three buckets:
   - **auto-offload** — safe to move without asking (see rules below)
   - **review** — ranked by reclaimable size and capped per run (`REVIEW_MAX_ITEMS`);
     overflow defers to later runs
   - **keep** — left in place
4. **actions** downloads auto-offload assets, writes them to the archive
   (organised by year/month), and deletes them from iCloud **only after** a
   confirmed write. Files whose iCloud name is an opaque UUID (WhatsApp/AirDrop
   media) get a recognisable `YYYYMMDD_HHMMSS_<source>_<short>.ext` name; real
   names (`IMG_2351.JPG`) are kept as-is.

Live iCloud resolution and download/delete go through
[`app/icloud_source.py`](app/icloud_source.py) (`PyiCloudAssetSource`), which
looks assets up directly by their CloudKit master record (≈100–300× faster than
pyicloud's linear library scan). Each asset is marked offloaded in the index the
instant its write succeeds, so an interrupted batch never loses progress.

> The **review bucket is still computed and ranked**, but with the Telegram
> approval flow dropped there is currently no interactive way to action it — only
> the auto-offload bucket runs unattended.

### Content-dedup & merging into the existing archive

The offload target `D:\icloud-photos` is an existing 2014–2023 photo archive.
A reconciliation ([`scripts/reconcile_archive.py`](scripts/reconcile_archive.py))
showed the live iCloud library and the archive are **essentially disjoint**
(~1% overlap), so new offloads **merge into** the archive rather than living in a
separate tree. This is safe because of a **content-dedup guard**: at offload time
[`actions._do_offload`](app/actions.py) SHA-256s the downloaded bytes and, if an
identical file already exists in the destination tree (size-prefiltered), skips
the write and records `ALREADY_ARCHIVED` — the iCloud copy is still deleted
(space reclaimed) and the index points at the existing file. (Size is the only
reliable join key against the archive — iCloud UUIDs and `resOriginalFingerprint`
don't reproduce from downloaded bytes.)

### Auto-offload rules

An asset is moved automatically **only** if **all** of these hold:

- it is **not** a favourite,
- its source is **WhatsApp** (nursery chats etc. are the main culprit),
- it is at least `MIN_AGE_DAYS` (default **180**) old, **and**
- its score is at least `AUTO_OFFLOAD_THRESHOLD`.

Everything else that scores at or above `REVIEW_THRESHOLD` is placed in the
review bucket. Favourites are never auto-offloaded.

## Asset index

Every run upserts each scanned asset into a single-table SQLite database
(`INDEX_DB_PATH`, default `data/asset_index.db`, persisted to a Docker volume).
It records the rich metadata the scanner extracts — capture/added dates, GPS
(decoded from iCloud's `locationEnc` binary plist, since the plain lat/long
fields are empty), dimensions, duration, fingerprint, source, favourite/hidden
flags, and more — plus each asset's offload status, `storage_tier`, and where it
ended up on disk. Re-scans refresh metadata without clobbering offload state or
`first_seen_at`.

The pre-existing `D:\icloud-photos` archive is also folded into the same index as
`status='archived'` via [`scripts/index_archive.py`](scripts/index_archive.py)
(capture dates inferred from the `YYYY/MM` folders), so `stats` / `breakdown` /
`search` cover both the live library and the historical archive.

This answers "where did file X go?", supports auditing, and (via the stored
`fingerprint`/`change_tag`) backs real dedup and incremental scans. Note that
**people/face tags, scene keywords, place names, and EXIF camera/lens data are
not available** from iCloud's metadata — see
[docs/asset-index-research-spike.md](docs/asset-index-research-spike.md).
(Harvesting these is part of the [curation future direction](#future-direction--memory-curation).)

```bash
# Summary counts by status (in_icloud / offloaded / archived)
uv run python -m app.index stats

# Detailed year x source grid of file counts + sizes (optionally by status)
uv run python -m app.index breakdown --status in_icloud

# Filtered lookup (limited to max 50 results by default — raise with --limit)
uv run python -m app.index search --source whatsapp --status in_icloud
uv run python -m app.index search --media-type video --since 2020-01-01 --until 2020-12-31
```

## Project layout

```
app/
  config.py        # Loads settings from environment / .env
  models.py        # Asset / Source / MediaType data models
  scanner.py       # Authenticates with pyicloud, walks the library + rich metadata
  icloud_source.py # Live CloudKit resolve + download + soft-delete (offload backend)
  twofactor.py     # Interactive first-time 2FA login + session caching
  analyser.py      # Scores assets by age, size, source, duplication, favourite
  recommender.py   # Splits assets into auto-offload / review / keep buckets
  actions.py       # Offload: writes by year/month, content-dedup, deletes from iCloud
  index.py         # Searchable SQLite asset index + query CLI
  main.py          # End-to-end pipeline runner (scan → … → offload)
scripts/
  reconcile_archive.py  # Compare live iCloud library against the local archive
  index_archive.py      # Ingest the existing D:\icloud-photos archive into the index
tests/
docs/
  asset-index-research-spike.md   # Index design + available iCloud metadata
docker-compose.yml
Dockerfile
.env.example
```

Project setup, the scanner (incl. rich metadata extraction), the analyser &
recommender, the live offload actions (download → dedup → write → delete), and
the searchable asset index are all in place and exercised against the live
account. The Telegram notifier is **dropped**; the weekly scheduler and Pi
deployment are **on hold** (on-demand CLI runs are sufficient). See
[TODO.md](TODO.md) for full build status and the curation roadmap.

## Getting started

### Prerequisites

- Python 3.12+ and [`uv`](https://docs.astral.sh/uv/)
- An Apple ID with two-factor authentication
- A drive to offload into — a local path (this PC's `D:\icloud-photos`) or a
  Windows PC sharing a drive over SMB
- *(On hold)* A Raspberry Pi running Docker, for always-on weekly deployment

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

# Run the full pipeline (also writes a timestamped log to logs/:
# dryrun_*.log or live_*.log depending on DRY_RUN)
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

# Pi deployment — on hold; mounts the SMB share as a volume
docker compose up -d
```

## Configuration

All settings come from environment variables (or a local `.env`). See
[.env.example](.env.example) for the full list with sample values.

| Variable | Purpose | Default |
|---|---|---|
| `ICLOUD_USERNAME` | Apple ID email | — |
| `ICLOUD_PASSWORD` | Apple ID password | — |
| `SMB_HOST` | Windows PC hostname or IP | — |
| `SMB_SHARE` | Share name (e.g. `Storage`) | — |
| `SMB_USERNAME` / `SMB_PASSWORD` | Windows credentials | — |
| `SMB_MOUNT_PATH` | Container path / archive root where offloads land | `/mnt/storage` |
| `DRY_RUN` | Log actions without downloading or deleting | `true` |
| `MIN_AGE_DAYS` | Minimum age before an asset is eligible for offload | `180` |
| `OFFLOAD_MAX_ITEMS` | Cap on assets moved per live run (`0` = unlimited) | `0` |
| `REVIEW_MAX_ITEMS` | Cap on assets the review bucket surfaces per run (`0` = unlimited) | `50` |
| `STORAGE_TIER` | Where offloads land: `local` (this PC's D:) or `network` (Pi/NAS) | `local` |
| `SCAN_FROM_INDEX` | Skip the live iCloud scan and read assets from the index | `false` |
| `INDEX_DB_PATH` | SQLite asset-index file (on a Docker volume in prod) | `data/asset_index.db` |
| `ALBUM_CACHE_MAX_AGE_HOURS` | TTL for the cached album-membership index | `168` |
| `SCAN_SINCE` / `SCAN_UNTIL` | Optional capture-date window (`YYYY-MM-DD`, inclusive) to limit a scan | — / — |
| `SCAN_DAY_OF_WEEK` / `SCAN_TIME` | Weekly scan schedule (scheduler on hold) | `sunday` / `02:00` |
| `WEIGHT_AGE` / `WEIGHT_SIZE` / `WEIGHT_SOURCE` / `WEIGHT_DUPLICATE` | Scoring weights (must sum to 100) | `35` / `20` / `30` / `15` |
| `AUTO_OFFLOAD_THRESHOLD` | Score at/above which non-favourites auto-offload | `65` |
| `REVIEW_THRESHOLD` | Score at/above which assets go to the review bucket | `40` |
| `FAVORITE_SCORE_PENALTY` | Score penalty applied to favourites | `60` |
| `LARGE_FILE_MB` | Size (MB) at which an asset gets the full size score | `50` |

> The Telegram notifier was dropped, so `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
> are no longer used.

> **Scan-time note:** `SCAN_SINCE`/`SCAN_UNTIL` filter *after* the full library is
> fetched, so they don't speed a scan up — the `photos.all` network pagination
> dominates. Use `SCAN_FROM_INDEX=true` for a fast, targeted run off the index.

> **Precedence:** real environment variables override `.env` (the app calls
> `load_dotenv()` without `override`). On Windows, a lingering session variable
> (e.g. `$env:OFFLOAD_MAX_ITEMS`) will silently shadow your `.env` edit — clear
> it or open a fresh terminal. Each run logs the effective offload settings so a
> mismatch is visible.

> **Security:** the `.env` file holds your Apple ID and Windows credentials. Keep
> it out of version control and restrict its permissions on the Pi
> (`chmod 600 .env`).

## Safety

- **Dry-run by default** (`DRY_RUN=true`) — the service logs what it *would* do
  without downloading or deleting anything. Set it to `false` only once you've
  verified behaviour.
- Files are deleted from iCloud **only after** a confirmed write to the archive
  (and deletes go to Recently Deleted, not permanent erasure).
- Content-dedup means re-saved / already-archived media is never double-stored.
- Favourites are never auto-offloaded.

## Future direction — memory curation

The storage-offload machinery (scan → index → dedup → organised archive) is now a
foundation for the real prize: **efficiently producing a yearly photo book and
per-person video reels** from the curated library, with the boring curation
largely automated. Planned threads (see [TODO.md](TODO.md) for detail):

- **EXIF capture during offload** — harvest `device_make`/`device_model` etc. from
  the downloaded bytes to tell "shot on our phones" from received media.
- **Harvest face/place tags from macOS Photos** (via `osxphotos` / the Photos
  SQLite DB) rather than building image ML, and join them back into the index.
- **Reconcile and merge Ben's + Emma's libraries** using the existing
  reconcile/dedup machinery.
- **Curation/ranking** for family moments (kids, parents/siblings; holidays over
  everyday) feeding a yearbook generator and per-subject video reels.
