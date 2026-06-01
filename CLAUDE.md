# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

iCloud Storage Manager — a Python service that runs on a Raspberry Pi in Docker. It periodically scans iCloud photo/video storage via `pyicloud`, analyses files by size, age, duplication, source app, and favourite status, then pushes recommendations (and optionally acts on them) via a Telegram bot. Offloaded files are written to a Windows PC's 2 TB drive over SMB.

## Commands

```bash
# Install all dependencies (including dev)
uv sync --extra dev

# Run the scanner manually (one-off)
uv run python -m app.scanner

# Run the service
uv run python -m app.main

# Run tests
uv run pytest

# Run a single test
uv run pytest tests/test_scanner.py::test_name -v

# Build Docker image
docker build -t icloud-manager .

# Run in Docker (requires .env)
docker run --env-file .env icloud-manager

# Docker Compose (Pi deployment)
docker compose up -d
```

## Architecture

```
app/
  scanner.py        # Core: authenticates with pyicloud, walks photo library
  analyser.py       # Scores files by age, size, source app, favourite flag, duplicates
  recommender.py    # Decides auto-action vs recommendation based on confidence rules
  actions.py        # Executes offload: downloads from iCloud, writes to SMB share
  notifier.py       # Telegram bot integration (sends reports + awaits approvals)
  scheduler.py      # Weekly cron trigger (APScheduler or simple loop)
  config.py         # Loads settings from environment / .env
tests/
docker-compose.yml
Dockerfile
.env.example
```

### Data flow

1. `scheduler.py` triggers a weekly scan.
2. `scanner.py` authenticates via `pyicloud` and fetches photo/video metadata (filename, size, date, source album/app, `is_favourite` flag).
3. `analyser.py` scores each asset — penalises for age, small/duplicate files, WhatsApp-origin heuristic; rewards favourites and Photos-origin assets.
4. `recommender.py` splits results into **auto-offload** (low-score, non-favourite, clearly non-essential) and **review** buckets.
5. `notifier.py` sends a Telegram summary. Items in the review bucket require an inline-keyboard approval before action.
6. `actions.py` downloads approved/auto assets from iCloud and writes them to the SMB-mounted share, then deletes from iCloud.

### Key decisions

- **pyicloud** is the iCloud access layer (no Mac/iCloud Drive sync required on the Pi).
- Credentials are stored as environment variables inside Docker; the `.env` file on the Pi should have restricted permissions (`chmod 600`).
- SMB share is mounted as a volume in `docker-compose.yml` so `actions.py` treats it as a local path.
- Source-app heuristics: files in iCloud albums named "WhatsApp" or with filenames matching `IMG-YYYYMMDD-WA\d+` are flagged as WhatsApp-origin.
- `is_favourite` is read from the pyicloud asset's `isFavorite` field.

## Environment Variables

See `.env.example`. Required keys:

| Variable | Purpose |
|---|---|
| `ICLOUD_USERNAME` | Apple ID email |
| `ICLOUD_PASSWORD` | Apple ID password |
| `TELEGRAM_BOT_TOKEN` | From BotFather |
| `TELEGRAM_CHAT_ID` | Your personal chat ID |
| `SMB_HOST` | Windows PC hostname or IP |
| `SMB_SHARE` | Share name (e.g. `Storage`) |
| `SMB_USERNAME` / `SMB_PASSWORD` | Windows credentials |
| `SMB_MOUNT_PATH` | Container path where share is mounted |
