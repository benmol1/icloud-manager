# iCloud Manager — Project TODO

*Last updated: 2026-06-17 11:38*

## 🅿️ Project Status — SHELVED (2026-06-17), primary objective achieved
The original goal is **done**: a large slice of the live iCloud library has been
offloaded into an organised `YYYY/MM` folder structure on `D:\icloud-photos`,
content-deduped, and recorded in a searchable SQLite index — and iCloud now has
plenty of free space again. Pausing here.

**Two original goals are being dropped, not deferred:**
1. **Telegram notifier / manual Approve-Skip flow — dropped.** There's no real
   scenario where manually approving each file one-by-one is worth it; the
   auto-offload + content-dedup pipeline does the job unattended.
2. **`local` vs `network` storage tier — no longer a concern.** It doesn't
   matter much where a given asset physically lives now that there's an easy
   *programmatic* way to move assets between iCloud / local / network (which
   exists today). Keep the `storage_tier` column, but stop treating the
   distinction as something to optimise around.

The interesting future work has shifted from *storage management* to
**memory curation** — see "Future Direction" at the bottom. If/when the project
is resumed, start there, not with the deployment phases.

## MVP Scope (original — largely met)
Build a Dockerised Python service that scans iCloud photo/video storage weekly, scores assets, and pushes recommendations + auto-actions via Telegram.

---

## Phase 1 — Project Setup ✅ COMPLETE
- [x] Initialise Python project structure (`app/`, `tests/`, `Dockerfile`, `docker-compose.yml`)
- [x] Create `pyproject.toml` with core deps via uv (`pyicloud`, `python-telegram-bot`, `apscheduler`, `pysmb`, `python-dotenv`)
- [x] Create `.env.example` with all required variables
- [x] Set up `pytest` with a basic smoke test
- [x] Write `Dockerfile` and `docker-compose.yml` (including SMB mount)
- [x] Write project `README.md` (overview, setup, configuration, safety)

## Phase 2 — iCloud Scanner ✅ COMPLETE
- [x] Implement `config.py` — load all settings from environment
- [x] Implement `scanner.py` — authenticate with pyicloud, handle 2FA
- [x] Fetch photo/video asset list with metadata: filename, size, date created, album/source, `isFavorite`
- [x] Implement WhatsApp-origin heuristic (album name + `IMG-YYYYMMDD-WA\d+` filename pattern)
- [x] Write tests for source-detection heuristic with sample filenames
- [x] Upgrade pyicloud 1.0.0 → 2.6.5 and migrate `scanner.py` to the 2.x photos API (favourite read from asset record, album iteration) + fix `twofactor.py` for modern auto-pushed 2FA. Verified real login + session trust against the live account.

## Phase 3 — Analyser & Recommender ✅ COMPLETE
- [x] Implement `analyser.py` — score assets by: age, file size, duplication, source app, favourite flag
- [x] Define scoring weights and thresholds in config
- [x] Implement `recommender.py` — split into auto-offload bucket vs. review bucket
- [x] Define rules for "non-controversial" auto-offload (e.g. WhatsApp-origin, not favourite, >6 months old)
- [x] ~~Review auto-offload rules with Emma~~ — N/A: project now scopes to Ben's account only. Rules finalised: auto-offload requires non-favourite + WhatsApp-origin + ≥ `min_age_days` (180). `min_age_days` is now actually enforced (was previously unused).
- [x] Write unit tests for scoring logic with fixture data

## Dry-run Findings & Tuning (2026-06-14) ✅ COMPLETE
*From the first full dry run against the live library (~17.5k assets): 1,158 auto-offload, 8,769 review, ~7,500 keep.*
- [x] Make the review bucket manageable — the review bucket is now ranked by reclaimable size (largest first) and capped per run via `REVIEW_MAX_ITEMS` (default 50; 0 = unlimited) in [`recommender._prioritise_review`](app/recommender.py). Overflow goes to a `review_deferred` bucket that resurfaces in later runs as the surfaced items get actioned — so the weekly Telegram Approve/Skip flow is bounded and front-loads the biggest space wins instead of dumping ~8,769 items at once. Summary + `main.py` log report the deferred count; 5 new unit tests; suite green (86 passed). *Note: the size-ranked cap is independent of the better dedup just landed, which should also trim review over time.*
- [x] Use human-readable filenames for offload destinations — investigated against the live account: camera-roll assets already decode to real names (`IMG_2351.JPG`); only app-saved media (WhatsApp/AirDrop) genuinely has opaque UUID filenames in iCloud (the UUID *is* the real filename, not a decode bug). Implemented [`actions._offload_filename`](app/actions.py): keeps meaningful names as-is, synthesises a sortable `YYYYMMDD_HHMMSS_<source>_<short>.ext` for UUID-stem names. 3 unit tests; suite green (57 passed)

## Next: Verification & Small-Scale Live Test ✅ COMPLETE
*Immediate priority before building the notifier. First confirm the whole
pipeline still connects after the dedup + review-cap changes, then prove a real
offload works end-to-end on a small, scoped slice.*
- [x] **Full dry run** to confirm everything connects — run `uv run python -m app.main` against the live library and verify the full chain works after the recent changes: scan → fingerprint-based dedup → analyse → size-ranked + capped review bucket → index upsert → dry-run offload. Sanity-check the new numbers (auto-offload / review / **deferred** / keep counts + MB) and that the index is populated. No files moved (dry run).
- [x] **Small-scale live test** — multiple real capped offloads (`OFFLOAD_MAX_ITEMS`, batches of 50) run against the live account via `DRY_RUN=false` in index-only mode: auto-offload assets downloaded, written to `D:\icloud-photos\YYYY\MM\`, **then** deleted from iCloud (write-before-delete), with the index recording `offloaded` + `local_path` + `storage_tier=local` per asset (durable, mid-batch). All WhatsApp JPG/MP4 from 2024-03 so far (oldest-first ordering). *Still to do for the full run: decide how the auto-offload set interacts with the existing `D:\icloud-photos` archive (see Phase 5 note), and consider scoping by year via `SCAN_SINCE`/`SCAN_UNTIL`.*
  - ~~**Blocked on**: wiring the concrete pyicloud `AssetSource`~~ — **Unblocked**: [`app/icloud_source.py`](app/icloud_source.py) now provides `PyiCloudAssetSource` (resolve by id, `download("original")`, soft-delete to Recently Deleted); [`main.py`](app/main.py) uses it when `DRY_RUN=false`. `OFFLOAD_MAX_ITEMS` cap lets the first test be scoped to a handful of files.
  - Start tiny: scope to a low-risk slice and/or a handful of files first; confirm on D: (local) before the Pi/NAS.

## Scanner Performance & UX ✅ COMPLETE
*Library baseline: ~16,000 photos + ~1,500 videos (~50 GB). First full scan is two paginated metadata sweeps — slow only the first time. **Note:** `SCAN_SINCE`/`SCAN_UNTIL` windows do NOT reduce scan time — they filter after fetching the full library. The album-index build (~19 min) alone dominates; a windowed scan costs the same ~27 min as a full scan. Incremental caching makes subsequent runs fast.*
- [x] Add per-album / interim progress logging during the album membership index build — logs every 10 albums (`_ALBUM_LOG_INTERVAL`) with asset count so the terminal shows progress instead of going silent for ~19 min
- [x] Cache the album membership index so subsequent runs skip the full album sweep — JSON file next to `INDEX_DB_PATH`, TTL configurable via `ALBUM_CACHE_MAX_AGE_HOURS` (default 168 h / 1 week); `_load_album_cache` / `_save_album_cache` helpers in `scanner.py`
- [x] Use `recordChangeTag` for incremental scans — `scan()` now accepts `cached_assets` dict (loaded via `index.get_cached_assets()`); assets whose `change_tag` matches the cached value skip `_photo_to_asset()` and reuse the stored `Asset` (album membership still refreshed from the current album index). `_row_to_asset` helper added to `index.py`. `main.py` opens the index before scanning and passes the cache.
- [x] Add an optional capture-date scan window (`SCAN_SINCE` / `SCAN_UNTIL`, inclusive `YYYY-MM-DD`) so a scan can be limited to a slice (e.g. just 2020) for testing — config + scanner (`_parse_window`/`_in_window`), documented in `.env.example`

## Scanner Performance & UX — Follow-ups (from 2026-06-16 dry run) ✅ COMPLETE
*The full dry run confirmed the album cache cut the run ~27 min → ~10 min, but the remaining ~10 min is the `photos.all` **network pagination** (16,764 assets) — which `change_tag` incremental does NOT reduce (it only skips local parsing, already sub-second). Real speed-up for targeted scans needs an index-only path.*
- [x] **Fix the misleading album-index log line** — moved the `Building album membership index…` message out of `scan()` and into the rebuild branch of [`_build_album_index`](app/scanner.py) (after the cache-miss check), so a cache hit only logs `Loaded album index from cache …`. Added a "~19 min" hint to the rebuild message.
- [x] **Add an index-only fast-scan mode** — `SCAN_FROM_INDEX=true` reads assets straight from the SQLite index ([`index.load_assets`](app/index.py), filtered by `status` + `SCAN_SINCE`/`SCAN_UNTIL`) and skips the iCloud `photos.all` pagination entirely. Verified loading all 16,764 in_icloud assets in <1 s vs the ~10 min live sweep. [`main.run`](app/main.py) branches on the flag, skips the index upsert in this mode (no real iCloud sighting), and authenticates lazily via [`scanner.ensure_authenticated`](app/scanner.py) only when a live offload needs a session. Config + `.env.example` documented; 5 new `load_assets` tests; suite green (116 passed — the 1 unrelated `test_dry_run_defaults_true` failure is from `.env` having `DRY_RUN=false` set for the live test).

## Before the Full Offload Run ✅ COMPLETE
- [x] **Decide how the auto-offload set interacts with the existing `D:\icloud-photos` archive** — **Resolved: keep merging into the archive** (not a separate `D:\icloud-offload\`). Reconciled the live iCloud library against the archive via [`scripts/reconcile_archive.py`](scripts/reconcile_archive.py): the two are **essentially disjoint** — only ~168 files (~1.1% of the live library) overlap; ~15,624 in-iCloud assets aren't in the archive at all. (Archive = 2014–2023 history already cleared off iCloud; live iCloud = mostly 2024–2026.) A separate tree would just split one timeline for no benefit. **Key finding on join keys:** filename is unreliable (iCloud UUIDs ≠ archive UUIDs; `IMG_xxxx` recycles) and `resOriginalFingerprint` is NOT reproducible from `download("original")` bytes — only exact `size_bytes` (validated against the archive's `YYYY/MM` folders) reliably matches. Merging is now safe because of the new content-dedup guard (below). *(Before the real run: clear any stray `$env:OFFLOAD_MAX_ITEMS`, set the cap, and — per the caveat — run a **full fresh scan** (`SCAN_FROM_INDEX=false`, no `SCAN_SINCE/UNTIL`) first, since the index's pre-2024 years look thinly test-scanned.)*

## Logging, Observability & Config (2026-06-16) ✅ COMPLETE
*From three small live batches: tightened run logging and fixed a config gotcha that made an offload cap silently differ from `.env`.*
- [x] **UTF-8 log output** — [`main._configure_logging`](app/main.py) forces UTF-8 on stdout/stderr (and the file handler) so non-ASCII chars (`—`, `≈`, `…`) no longer mangle to cp1252 (`ù`) when logs are written/redirected on Windows.
- [x] **Auto-write a timestamped run log** — every run writes `logs/<live|dryrun>_YYYYMMDD_HHMMSS.log` (prefix from `DRY_RUN`) alongside console output, so no manual `Tee-Object` is needed. `logs/` is gitignored.
- [x] **Index-only mode logs index freshness** — index-only runs log when the cached index was last refreshed (`AssetIndex.last_refreshed_at` = max `last_seen_at`, with a human-readable age) so it's clear how stale the recommendations are.
- [x] **`breakdown` index CLI** — `python -m app.index breakdown [--status in_icloud]` prints a year × source grid of file counts + sizes with per-year/per-source totals (`AssetIndex.breakdown`). More detail than `stats`.
- [x] **Log effective offload settings** — [`main.run`](app/main.py) logs `Offload settings: mode=…, cap=… (OFFLOAD_MAX_ITEMS), storage_tier=…` before offloading, so a cap that doesn't match `.env` is obvious.
- [x] **Config read from env in `__init__`** — [`Config`](app/config.py) now reads env vars on construction (not at class-definition time), so a fresh `Config()` reflects the current environment and tests can `monkeypatch.setenv/delenv` without `importlib.reload`. Fixes the long-standing `test_dry_run_defaults_true` failure (it broke whenever `.env` set `DRY_RUN=false`, because reload re-ran `load_dotenv`). Suite fully green (132 passed).
  - **Gotcha learned:** `load_dotenv(override=False)` means a real shell env var beats `.env`. A lingering `$env:OFFLOAD_MAX_ITEMS=50` in the PowerShell session silently capped a run to 50 despite `.env` saying 200. Correct precedence for Docker (real env wins) — clear the session var / use a fresh terminal; the new settings log now makes the mismatch visible.

## Phase 4 — Telegram Notifier ❌ DROPPED
*Dropped on 2026-06-17 — manually approving offloads one-by-one isn't worth it;
the unattended auto-offload + content-dedup pipeline covers the need. Kept here
for the record, not planned.*
- [ ] ~~Create Telegram bot via BotFather and record token + chat ID~~
- [ ] ~~Implement `notifier.py` — send weekly summary report~~
- [ ] ~~Add inline keyboard buttons for review-bucket approvals (Approve / Skip)~~
- [ ] ~~Handle approval callbacks and trigger `actions.py` accordingly~~
- [ ] ~~Test bot locally before deploying to Pi~~

## Phase 5 — Offload Actions ⏳ IN PROGRESS
- [x] Implement `actions.py` — download asset from iCloud, write to SMB share path (live download/delete via an `AssetSource` seam; concrete pyicloud source still to wire)
- [x] Organise files on NAS by year/month folder structure (`<mount>/YYYY/MM/<filename>`, with collision handling)
- [x] Delete from iCloud after confirmed write (write-before-delete; failures never delete)
- [x] Add dry-run mode (log what would happen, take no action) — default
- [x] **Fast offload resolution (direct CloudKit lookup)** — the first live batch crawled at ~2m40s/file because [`PyiCloudAssetSource`](app/icloud_source.py) resolved each asset via `photos.all.get(id)`, which falls back inside pyicloud to linearly scanning the whole date-sorted library. Rewrote `_resolve` to fetch the `CPLMaster`+`CPLAsset` records directly by name (using the `master_id` we store), with the old iteration kept as a logged fallback for missing `master_id` / pyicloud API drift. Verified live: resolve dropped from ~160 s to ~0.5–1.6 s (~100–300×); the 1,236-file run goes from ~2.5 days to ~30–50 min. 6 new tests.
- [x] **Durable per-asset offload marking** — [`actions.offload`](app/actions.py) gained an `on_offloaded` callback fired the instant each asset succeeds; [`main.run`](app/main.py) uses it to `mark_offloaded` immediately instead of after the whole batch. Previously an interrupted batch lost *all* offload records (the first live test was Ctrl-C'd at file 11/50 and the index recorded nothing). 4 new tests.
- [x] **Tested with a small live batch** — first real offload (cap 50) ran against the live account; surfaced and fixed the two issues above. 10 files genuinely offloaded before the interrupt were reconciled into the index (`status=offloaded`, `storage_tier=local`) via a one-off matching their log destinations. **Note:** `D:\icloud-photos` is Ben's existing 20,873-file photo archive (2014–2024, robocopied to `P:` — see `copy_log.txt`), so the offload target already holds a parallel library; decide how that interacts with the auto-offload set before the full 1,236 run. *(Resolved — see "Before the Full Offload Run": merge into the archive, guarded by content-dedup.)*
- [x] **Content-dedup guard at offload time** — [`actions._do_offload`](app/actions.py) SHA-256s the downloaded bytes and skips the write if identical content already exists in the destination tree (size-prefiltered, hashes cached, in-batch dupes registered as they're written). New `ALREADY_ARCHIVED` status: the iCloud copy is still deleted (space reclaimed) and the index is marked offloaded against the existing path. Makes merging into the archive safe (the ~168 overlaps + re-saved app media aren't double-stored). 5 new tests; suite green (137 passed).
- [ ] Capture EXIF camera metadata (device make/model, lens, aperture, ISO, focal length) opportunistically during offload — this is NOT in iCloud's CloudKit metadata, only inside the downloaded file, so parse it (Pillow/exifread/exiftool) while we already have the bytes and store into the nullable `device_make`/`device_model`/`lens`/`aperture`/`iso`/`focal_length` index columns. Don't bulk-download assets just to harvest EXIF; often absent on screenshots / WhatsApp / older photos. **→ Now a priority for the curation goal — see Future Direction. `device_make`/`device_model` in particular is the cheapest way to filter "shot on our phones (Ben/Emma)" vs received/other-people's media.**

## Phase 6 — Scheduler ⏸️ ON HOLD
*On hold — an unattended weekly cron made sense for the "manage storage forever"
framing; with the storage goal met and the notifier dropped, on-demand CLI runs
are enough for now. Manual run already works.*
- [ ] Implement `scheduler.py` — weekly trigger using APScheduler
- [ ] ~~Wire up full pipeline: scan → analyse → recommend → notify → act~~ (notify dropped)
- [x] Add manual trigger endpoint or CLI flag for on-demand runs (`uv run python -m app.main`)
- [x] Add `app/main.py` end-to-end runner: scan → analyse → recommend → dry-run offload

## Phase 7 — Pi Deployment ⏸️ ON HOLD
*On hold — not needed while runs are on-demand from this PC. The whole point of
the Pi was the always-on weekly service; revisit only if that's wanted again.*
- [x] Set up SMB share on Windows PC (share the 2 TB drive)
- [ ] Configure Pi to mount SMB share (or use Docker volume mount)
- [ ] Copy `.env` to Pi with `chmod 600`
- [ ] Deploy with `docker compose up -d` and verify weekly schedule fires
- [ ] Test end-to-end: scan → file appears on Windows drive

## Searchable Asset Index 🔍 ✅ COMPLETE
Goal: a searchable, persistent index of **every** asset in iCloud, plus a record
of which assets we've offloaded to local storage and where they ended up. Useful
for "where did file X go?", auditing actions, and avoiding re-processing.

- [x] **Research spike** — choose the storage approach for the index → **SQLite** (single `assets` table; DuckDB available ad-hoc since it can query the SQLite file directly). Full write-up + DDL + integration plan in [`docs/asset-index-research-spike.md`](docs/asset-index-research-spike.md). Rationale: small (~17.5k rows), single-writer, row-by-row upsert workload — transactional store wins; zero new deps (stdlib `sqlite3`).
- [x] Define the schema — implemented in [`app/index.py`](app/index.py) (SQLite `assets` table per the spike doc; rich-metadata columns included as nullable for later)
- [x] Persist the index to a Docker volume so it survives container restarts — `asset-index` volume mounted at `/app/data` in [`docker-compose.yml`](docker-compose.yml); `INDEX_DB_PATH` config + `.env.example` default resolve there; `data/`+`*.db` gitignored
- [x] Wire the index into the pipeline: upsert assets on scan; mark `offloaded` + `local_path` after a confirmed write — [`main.run`](app/main.py) upserts all scored assets per scan and calls `index.mark_offloaded` for confirmed `OFFLOADED` results (dry-run records nothing)
- [x] Add a simple query/CLI to search the index — `python -m app.index stats` and `python -m app.index search --source/--media-type/--status/--favorite/--filename/--since/--until/--limit`
- [x] Extend the scanner to populate the richer index columns — [`scanner._extract_rich_metadata`](app/scanner.py) does best-effort extraction of location/`added_date`/`file_type`/`is_hidden`/`is_live_photo`/`caption`/dimensions/`duration`/`subtype`/`hdr_type`/`has_adjustments`/`fingerprint`/`change_tag`/`tz_offset`/`master_id`; [`Asset`](app/models.py) carries them and [`index.upsert_scored`](app/index.py) persists them. (EXIF device/lens still excluded — separate offload-time item)
  - GPS is decoded from the `locationEnc` **binary plist** (`lat`/`lon`) — iCloud leaves the plain `locationLatitude`/`longitude` fields empty. Verified on real data ([`scanner._extract_location`](app/scanner.py)).
  - Verified by the **2020 dry run** (`SCAN_SINCE/UNTIL`): 400 in-window assets indexed; `added_at`/`fingerprint`/`change_tag`/dimensions/`duration`/`file_type`/`is_live_photo`/`subtype`/`master_id` all 400/400; `tz_offset` 369/400; location decoded; caption genuinely empty.
- [x] Switch duplicate detection to fingerprint-based — [`analyser._find_duplicate_ids`](app/analyser.py) now groups by Apple's `resOriginalFingerprint` content hash (`Asset.fingerprint`) for true byte-for-byte duplicate detection. Assets without a fingerprint (older / app-saved media iCloud doesn't hash) fall back to the old `(size, creation-minute)` heuristic; the two key spaces are namespaced so they never collide. 5 new unit tests; suite green (81 passed)
- [x] Add `storage_tier` column (`local` / `network`) to record where each offloaded asset lives — schema migration for existing DBs, `mark_offloaded(storage_tier=)`, `by_tier` breakdown in `stats()` output, `--tier` filter in the search CLI, and `STORAGE_TIER` config key. `.env` defaults to `local` (this PC's D:); Pi deployment will use `network`. 101 tests green.
- [x] **Index the pre-existing `D:\icloud-photos` archive into the index** — [`scripts/index_archive.py`](scripts/index_archive.py) + [`index.py`](app/index.py) ingest the ~20,862 existing archive files as `status='archived'` (path-based id, capture dates inferred from `YYYY/MM` folders), skipping files already recorded as `offloaded`, so `stats`/`breakdown`/`search` cover both the live iCloud library and the historical archive in one index.

## Deferred / Future (storage-management era)
- [ ] Web dashboard for browsing recommendations
- [ ] Statistics over time (storage freed, assets offloaded)
- [x] **True content-dedup for app-saved media** — done as the offload-time SHA-256 guard (see Phase 5). [`actions._do_offload`](app/actions.py) hashes the downloaded bytes and, if an identical file already exists in the archive (size-prefiltered via `_build_size_index`), skips the write and reports `ALREADY_ARCHIVED` — catching genuine content duplicates (incl. the same WhatsApp file saved multiple times) regardless of `resOriginalFingerprint`/date.

---

## 🎯 Future Direction — Memory Curation & Photo Books (the real prize)
*Captured 2026-06-17 while shelving the project. This is where to pick up if/when
resumed. The storage-offload machinery (scan → index → dedup → organised archive)
is now a solid **foundation**; the goal shifts to turning that organised library
into curated outputs. The headline ambition:*

> **Efficiently produce a photo book for Emma as a Christmas present each year**
> — a yearbook-style look back at the year's highlights — plus per-person video
> reels — with the boring curation (finding the *good*, *relevant* photos)
> largely automated.

### Richer searchable metadata
- [ ] **EXIF capture during offload** (promoted from Phase 5) — get `device_make`/`device_model` especially, so we can filter **"shot on Ben's or Emma's device"** vs photos received from / taken by other people. This is the cheapest, most reliable "is this our photo?" signal we have without ML.

### Face & place tagging — lean on the Mac, don't build ML
- [ ] **Research spike: harvest face/place tags from the macOS Photos app** instead of building our own facial recognition. Idea: run the library through Photos on a Mac, let Apple's built-in face-tagging + place/scene recognition do the work, then **re-capture those tags back into our index**. Spike should establish the cleanest extraction path, e.g.:
  - Read the Photos library SQLite DB directly (`Photos.sqlite` / `photos.db` in the `.photoslibrary` bundle) for person/face + place rows.
  - Or the AppleScript / `osxphotos` (Python) route, which already exposes persons, keywords, places, albums.
  - Decide how to **join** Mac-Photos assets back to our index rows (UUID? filename? capture date + size? — note our earlier finding that size is the only reliable join key against the archive).
  - *Explicitly out of scope: rolling our own image ML for faces — overkill when the Mac does it for free.*

### Multi-account consolidation (Ben + Emma)
- [ ] **Reconcile and merge Ben's and Emma's iCloud libraries** into one curated pool for book/reel building. *(NB: supersedes the earlier "single-account, Emma out of scope" decision — that was for the offload/storage policy; curation genuinely needs both libraries.)* Reuse the existing reconcile/dedup machinery ([`scripts/reconcile_archive.py`](scripts/reconcile_archive.py), content-dedup) to combine without double-storing the many shared photos.

### Curation / ranking for "our family" moments
- [ ] **Define a "people we care about" priority model** — surface photos that include **our kids (Phoebe, Zoe) and our parents/siblings** first. Likely built on the harvested face tags above.
- [ ] **Context priority bias** — holiday photos rank above home / nursery photos for the yearbook. Use location (we already decode GPS from `locationEnc`) + date clustering to detect trips/holidays vs everyday.
- [ ] **Highlight selection** — from the ranked pool, pick a manageable, representative set per year (de-duped, best-of bursts, spread across events) suitable for a ~yearbook.

### Outputs
- [ ] **Yearbook-style photo book generator** — a year-in-review of key moments & highlights (holidays + fun at home). Decide format/handoff (e.g. export a curated, ordered set + captions to a print service, or to a book-layout tool).
- [ ] **Per-subject home video reels** — highlight reels of the year's video moments, with selectable subjects (e.g. one for Phoebe, one for Zoe). **Note:** macOS Photos "Memories" + Apple TV already do automated reels well — likely lean on / trigger those rather than building a video editor; spike whether we can drive them programmatically or just curate the input set and let Photos assemble.
