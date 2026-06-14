# iCloud Manager — Project TODO

*Last updated: 2026-06-14 12:51*

## MVP Scope
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

## Dry-run Findings & Tuning (2026-06-14) ⏳ IN PROGRESS
*From the first full dry run against the live library (~17.5k assets): 1,158 auto-offload, 8,769 review, ~7,500 keep.*
- [ ] Make the review bucket manageable — the dry run sent **8,769 assets (half the library)** to review, far too many for the per-item Telegram Approve/Skip flow. Raise `review_threshold` and/or rethink the review UX (e.g. top-N by size, batch approval by album/category, or treat review as informational rather than per-item approval)
- [x] Use human-readable filenames for offload destinations — investigated against the live account: camera-roll assets already decode to real names (`IMG_2351.JPG`); only app-saved media (WhatsApp/AirDrop) genuinely has opaque UUID filenames in iCloud (the UUID *is* the real filename, not a decode bug). Implemented [`actions._offload_filename`](app/actions.py): keeps meaningful names as-is, synthesises a sortable `YYYYMMDD_HHMMSS_<source>_<short>.ext` for UUID-stem names. 3 unit tests; suite green (57 passed)

## Scanner Performance & UX ⏳ IN PROGRESS
*Library baseline: ~16,000 photos + ~1,500 videos (~50 GB). First full scan is two paginated metadata sweeps — slow only the first time.*
- [ ] Add per-album / interim progress logging during the album membership index build, so the terminal shows progress instead of going silent for minutes (currently only logs "Building…" then nothing until "Scanning…")
- [ ] Cache the album membership index so subsequent runs are incremental updates rather than two full paginated sweeps every run
- [ ] Use `recordChangeTag` (the asset etag) for incremental scans — skip re-processing assets whose stored `change_tag` is unchanged. Same scan-cache effort as the album-index cache above; depends on the asset index storing `change_tag`

## Phase 4 — Telegram Notifier
- [ ] Create Telegram bot via BotFather and record token + chat ID
- [ ] Implement `notifier.py` — send weekly summary report
- [ ] Add inline keyboard buttons for review-bucket approvals (Approve / Skip)
- [ ] Handle approval callbacks and trigger `actions.py` accordingly
- [ ] Test bot locally before deploying to Pi

## Phase 5 — Offload Actions ⏳ IN PROGRESS
- [x] Implement `actions.py` — download asset from iCloud, write to SMB share path (live download/delete via an `AssetSource` seam; concrete pyicloud source still to wire)
- [x] Organise files on NAS by year/month folder structure (`<mount>/YYYY/MM/<filename>`, with collision handling)
- [x] Delete from iCloud after confirmed write (write-before-delete; failures never delete)
- [x] Add dry-run mode (log what would happen, take no action) — default
- [ ] Test with a small batch of non-critical files first (needs live iCloud session)
- [ ] Capture EXIF camera metadata (device make/model, lens, aperture, ISO, focal length) opportunistically during offload — this is NOT in iCloud's CloudKit metadata, only inside the downloaded file, so parse it (Pillow/exifread/exiftool) while we already have the bytes and store into the nullable `device_make`/`device_model`/`lens`/`aperture`/`iso`/`focal_length` index columns. Don't bulk-download assets just to harvest EXIF; often absent on screenshots / WhatsApp / older photos

## Phase 6 — Scheduler ⏳ IN PROGRESS
- [ ] Implement `scheduler.py` — weekly trigger using APScheduler
- [ ] Wire up full pipeline: scan → analyse → recommend → notify → act (notify/live-act still missing)
- [x] Add manual trigger endpoint or CLI flag for on-demand runs (`uv run python -m app.main`)
- [x] Add `app/main.py` end-to-end runner: scan → analyse → recommend → dry-run offload

## Phase 7 — Pi Deployment
- [ ] Set up SMB share on Windows PC (share the 2 TB drive)
- [ ] Configure Pi to mount SMB share (or use Docker volume mount)
- [ ] Copy `.env` to Pi with `chmod 600`
- [ ] Deploy with `docker compose up -d` and verify weekly schedule fires
- [ ] Test end-to-end: scan → Telegram message received → approval → file appears on Windows drive

## Searchable Asset Index 🔍 ⏳ IN PROGRESS
Goal: a searchable, persistent index of **every** asset in iCloud, plus a record
of which assets we've offloaded to local storage and where they ended up. Useful
for "where did file X go?", auditing actions, and avoiding re-processing.

- [x] **Research spike** — choose the storage approach for the index → **SQLite** (single `assets` table; DuckDB available ad-hoc since it can query the SQLite file directly). Full write-up + DDL + integration plan in [`docs/asset-index-research-spike.md`](docs/asset-index-research-spike.md). Rationale: small (~17.5k rows), single-writer, row-by-row upsert workload — transactional store wins; zero new deps (stdlib `sqlite3`).
- [x] Define the schema — implemented in [`app/index.py`](app/index.py) (SQLite `assets` table per the spike doc; rich-metadata columns included as nullable for later)
- [x] Persist the index to a Docker volume so it survives container restarts — `asset-index` volume mounted at `/app/data` in [`docker-compose.yml`](docker-compose.yml); `INDEX_DB_PATH` config + `.env.example` default resolve there; `data/`+`*.db` gitignored
- [x] Wire the index into the pipeline: upsert assets on scan; mark `offloaded` + `local_path` after a confirmed write — [`main.run`](app/main.py) upserts all scored assets per scan and calls `index.mark_offloaded` for confirmed `OFFLOADED` results (dry-run records nothing)
- [x] Add a simple query/CLI to search the index — `python -m app.index stats` and `python -m app.index search --source/--media-type/--status/--favorite/--filename/--since/--until/--limit`
- [ ] Extend the scanner to populate the richer index columns — the schema has nullable `location`/`fingerprint`/`change_tag`/`caption`/`width`/`height`/`duration`/`added_at` etc., but the scanner doesn't extract them yet (blocks the fingerprint-dedup and changeTag-incremental items below/elsewhere)
- [ ] Switch duplicate detection to fingerprint-based — replace the weak `(size, creation-minute)` heuristic in [`analyser._find_duplicate_ids`](app/analyser.py#L84) with Apple's `resOriginalFingerprint` content hash (stored as the index `fingerprint` column); group by fingerprint for true duplicate detection

## Deferred / Future
- [ ] Web dashboard for browsing recommendations
- [ ] Statistics over time (storage freed, assets offloaded)
