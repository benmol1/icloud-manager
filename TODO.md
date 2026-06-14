# iCloud Manager — Project TODO

*Last updated: 2026-06-14 07:41*

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

## Searchable Asset Index 🔍 (needs research spike)
Goal: a searchable, persistent index of **every** asset in iCloud, plus a record
of which assets we've offloaded to local storage and where they ended up. Useful
for "where did file X go?", auditing actions, and avoiding re-processing.

- [ ] **Research spike** — choose the storage approach for the index
  - Working instinct: a small **DuckDB** database with a single table, updated after every scan/offload action
  - Compare alternatives (e.g. **SQLite**, a Parquet/JSON file) on: footprint on the Pi, concurrent-/incremental-write safety, query ergonomics, backup/restore. (Note: SQLite is a row-store built for frequent small transactional updates; DuckDB is columnar/analytics-oriented — worth weighing for an update-on-every-action workload.)
- [ ] Define the schema (e.g. `asset_id`, `filename`, `size`, `created`, `source`, `is_favorite`, `score`, `status` [`in_icloud`/`offloaded`], `local_path`, `offloaded_at`, `last_seen`)
- [ ] Persist the index to a Docker volume so it survives container restarts
- [ ] Wire the index into the pipeline: upsert assets on scan; mark `offloaded` + `local_path` in `actions.py` after a confirmed write
- [ ] Add a simple query/CLI to search the index (by filename, source, status, date range)

## Deferred / Future
- [ ] Web dashboard for browsing recommendations
- [ ] Statistics over time (storage freed, assets offloaded)
