# iCloud Manager — Project TODO

## MVP Scope
Build a Dockerised Python service that scans iCloud photo/video storage weekly, scores assets, and pushes recommendations + auto-actions via Telegram.

---

## Phase 1 — Project Setup
- [ ] Initialise Python project structure (`app/`, `tests/`, `Dockerfile`, `docker-compose.yml`)
- [ ] Create `requirements.txt` with core deps (`pyicloud`, `python-telegram-bot`, `apscheduler`, `pysmb`, `python-dotenv`)
- [ ] Create `.env.example` with all required variables
- [ ] Set up `pytest` with a basic smoke test
- [ ] Write `Dockerfile` and `docker-compose.yml` (including SMB mount)

## Phase 2 — iCloud Scanner
- [ ] Implement `config.py` — load all settings from environment
- [ ] Implement `scanner.py` — authenticate with pyicloud, handle 2FA
- [ ] Fetch photo/video asset list with metadata: filename, size, date created, album/source, `isFavorite`
- [ ] Implement WhatsApp-origin heuristic (album name + `IMG-YYYYMMDD-WA\d+` filename pattern)
- [ ] Write tests for source-detection heuristic with sample filenames

## Phase 3 — Analyser & Recommender
- [ ] Implement `analyser.py` — score assets by: age, file size, duplication, source app, favourite flag
- [ ] Define scoring weights and thresholds in config
- [ ] Implement `recommender.py` — split into auto-offload bucket vs. review bucket
- [ ] Define rules for "non-controversial" auto-offload (e.g. WhatsApp-origin, not favourite, >6 months old)
- [ ] Write unit tests for scoring logic with fixture data

## Phase 4 — Telegram Notifier
- [ ] Create Telegram bot via BotFather and record token + chat ID
- [ ] Implement `notifier.py` — send weekly summary report
- [ ] Add inline keyboard buttons for review-bucket approvals (Approve / Skip)
- [ ] Handle approval callbacks and trigger `actions.py` accordingly
- [ ] Test bot locally before deploying to Pi

## Phase 5 — Offload Actions
- [ ] Implement `actions.py` — download asset from iCloud, write to SMB share path
- [ ] Organise files on NAS by year/month folder structure
- [ ] Delete from iCloud after confirmed write
- [ ] Add dry-run mode (log what would happen, take no action)
- [ ] Test with a small batch of non-critical files first

## Phase 6 — Scheduler
- [ ] Implement `scheduler.py` — weekly trigger using APScheduler
- [ ] Wire up full pipeline: scan → analyse → recommend → notify → act
- [ ] Add manual trigger endpoint or CLI flag for on-demand runs

## Phase 7 — Pi Deployment
- [ ] Set up SMB share on Windows PC (share the 2 TB drive)
- [ ] Configure Pi to mount SMB share (or use Docker volume mount)
- [ ] Copy `.env` to Pi with `chmod 600`
- [ ] Deploy with `docker compose up -d` and verify weekly schedule fires
- [ ] Test end-to-end: scan → Telegram message received → approval → file appears on Windows drive

## Deferred / Future
- [ ] Support Emma's iCloud account (second set of credentials + separate Telegram notifications)
- [ ] Duplicate detection across both accounts
- [ ] Web dashboard for browsing recommendations
- [ ] Statistics over time (storage freed, assets offloaded)
