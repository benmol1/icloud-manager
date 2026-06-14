# Research Spike — Searchable Asset Index

*Spike date: 2026-06-14 · Status: recommendation ready, not yet implemented*

## 1. Goal

A persistent, queryable record of **every** asset in the iCloud library, plus
what we've done with each one (offloaded to network storage, where it landed,
when). It should answer questions like:

- "Where did file X go?" (audit / lookup after offload)
- "What WhatsApp videos older than a year are still in iCloud?"
- "How much have we freed, and when?"
- Avoid re-processing assets we've already handled.

**Hard requirement from Ben:** we must be able to run **SQL** over the stored
data. The analysis itself is deliberately simple — filter by **date**,
**favourite**, **photo vs video**, **source/tags**, and **status** (still in
iCloud vs offloaded to the NAS).

## 2. Workload characterisation

This drives the whole decision, so let's be concrete:

| Property | Value |
|---|---|
| Row count | ~17,500 today (16k photos + 1.5k videos), growing slowly |
| Write pattern | **Many small upserts** — one row touched per asset per scan, and again on each offload action |
| Read pattern | Point lookups + simple filtered scans (`WHERE` on a few indexed columns) |
| Concurrency | Single process on a Pi, one scan at a time. Effectively no concurrent writers |
| Aggregations | Trivial (`SUM(size)`, `COUNT(*) GROUP BY status`) over ~17k rows |
| Footprint budget | Small — runs on a Raspberry Pi alongside the container |

The defining facts: **small dataset, transactional row-by-row updates, single
writer, simple queries.** This is squarely a transactional/OLTP shape, *not* an
analytical/OLAP one.

## 3. Options considered

### Option A — SQLite ✅ (recommended)
- Row-store built precisely for frequent small transactional updates — matches
  our "upsert one row per asset per action" pattern.
- **Zero new dependencies**: `sqlite3` is in the Python standard library.
- Single `.db` file → backup/restore is a file copy; trivially survives a
  container restart when placed on a Docker volume.
- Full SQL, `UPSERT` (`INSERT ... ON CONFLICT DO UPDATE`), and the `json1` and
  date functions cover everything we need.
- WAL mode gives durable, crash-safe writes.
- At 17k rows, indexed filters are effectively instant.

**Cons:** columnar analytics over *millions* of rows would be slow — irrelevant
at our scale.

### Option B — DuckDB
- Columnar, analytics-oriented; superb for large aggregate scans over millions
  of rows.
- Also speaks excellent SQL and reads/writes Parquet natively.

**Cons for *this* workload:**
- Optimised for **bulk** loads and analytical scans, not row-at-a-time
  transactional upserts — the opposite of our write pattern.
- Single-writer file locking; weaker story for the "update on every action" loop.
- An **extra runtime dependency** for no measurable benefit at 17k rows.

**Where it still helps:** ad-hoc heavy analysis later. DuckDB can *attach to and
query a SQLite file directly* (or read an exported Parquet), so choosing SQLite
does **not** lock us out of DuckDB — we get it for free when/if we want it.

### Option C — Parquet / JSON flat file
- Parquet: great columnar storage, but immutable-ish — you rewrite the whole
  file to update rows. Painful for per-action upserts.
- JSON: human-readable but no indexes, no real query engine, must load it all
  into memory to filter. Doesn't satisfy "run SQL".

**Verdict:** wrong tool for an update-on-every-action store.

## 4. Recommendation

> **Use SQLite as the operational system of record.** One file on a Docker
> volume, accessed via the stdlib `sqlite3` module. Keep DuckDB in our back
> pocket for ad-hoc analytics (it can read the SQLite file directly) — do **not**
> add it as a runtime dependency now.

Rationale in one line: our workload is small + transactional + single-writer, so
the simplest, dependency-free, transactional store wins — and it doesn't
foreclose columnar analytics later.

## 5. Available iCloud metadata & the proposed data model

### 5a. What metadata pyicloud 2.6.5 actually exposes

The CloudKit field list pyicloud requests is in
[`service.py:108-212`](../.venv/Lib/site-packages/pyicloud/services/photos_cloudkit/service.py#L108-L212).
Human-readable metadata beyond the original schema, with cardinality:

| Field(s) | Meaning | Cardinality |
|---|---|---|
| `locationLatitude` / `locationLongitude` | GPS coordinates (no place names — Apple reverse-geocodes on-device only) | 1:1 |
| `locationEnc` / `locationV2Enc` | Encrypted fuller CoreLocation (altitude/heading/speed) | 1:1 |
| `addedDate` | **True "added to iCloud library" date** — accessor `photo.added_date` | 1:1 |
| `assetDate` | Capture date (what `photo.created` returns) | 1:1 |
| `timeZoneOffset` | Capture timezone offset | 1:1 |
| `captionEnc` | User-entered caption/title (only free-text label) | 1:1 |
| `duration` | Video length (seconds) | 1:1 |
| `resOriginalWidth/Height` | Pixel dimensions (accessor `photo.dimensions`) | 1:1 |
| `assetSubtype(V2)` | screenshot / panorama / portrait / etc. | 1:1 |
| `assetHDRType`, `customRenderedValue` | HDR / portrait rendering | 1:1 |
| `adjustmentType` | Whether the asset has been edited | 1:1 |
| `is_live_photo` (accessor), `resOriginalFileType` | Live Photo flag; actual format (HEIC/JPEG/MOV) | 1:1 |
| `resOriginalFingerprint` | **Apple content hash → real duplicate detection** | 1:1 |
| `recordChangeTag` | etag → **incremental-scan key** | 1:1 |
| `isHidden` | In the Hidden album | 1:1 |
| `masterRef` / `burstId` / `burstFlags` | Master link & burst grouping | 1:1 / group |
| Album membership | Which albums contain the asset | **1:many** |

**Not available via pyicloud** (confirmed — zero matches for
`person`/`face`/`keyword`/`memory`/`moment`/`exif`/`lens`/`camera` in pyicloud):
- **People / face tagging** — Apple computes faces and the "People" album
  **on-device**; it is not synced into the CloudKit asset records.
- **Auto scene/object keywords** (e.g. "beach", "dog") — likewise on-device.
- **Reverse-geocoded place names** — only raw lat/long is stored.
- **EXIF camera data — device, lens, aperture, ISO, focal length.** None of this
  is in the CloudKit metadata; it lives inside the image file's EXIF. The Photos
  app shows it because it reads the file. The **only** way to capture it is to
  **download the original and parse EXIF** (Pillow / exifread / exiftool). It is
  unreliable by nature — `LensModel`/`Make`/`Model` are stripped from
  screenshots and WhatsApp images and absent on older photos.
  → Practical plan: we already download bytes at offload time
  ([`actions._do_offload`](../app/actions.py#L85-L112)), so extract EXIF
  **opportunistically during offload** and fill the nullable `device_*`/`lens` /
  `aperture` / `iso` / `focal_length` columns then. Do **not** bulk-download
  17.5k assets purely to harvest EXIF.

> ⚠️ **Filename root cause:** `photo.filename`
> ([`service.py:1716`](../.venv/Lib/site-packages/pyicloud/services/photos_cloudkit/service.py#L1716))
> decodes `filenameEnc` and **falls back to the asset UUID** when that field is
> missing/undecodable — which is exactly the opaque UUID names seen in the dry
> run. The filename TODO is about getting `filenameEnc` to decode, not inventing
> a name.

### 5b. Why one wide table (mostly)

Almost everything above is **1:1 with the asset** — scalar attributes of a
single photo. Those belong as **columns on `assets`**; SQLite handles a
40–50-column table effortlessly and it keeps the simple filtering SQL flat. The
**only genuine 1:many** relationship in this data is **album membership** — and
per the design decision we keep that as a **JSON column** (`json_each()` is fine
at 17k rows), not a junction table. (Faces would have been the other classic
1:many, but we can't get them.)

### 5c. Schema

```sql
CREATE TABLE IF NOT EXISTS assets (
    asset_id        TEXT PRIMARY KEY,   -- CloudKit asset recordName
    master_id       TEXT,               -- CPLMaster recordName
    filename        TEXT NOT NULL,      -- decoded filenameEnc (UUID fallback)
    size_bytes      INTEGER NOT NULL,
    media_type      TEXT NOT NULL,      -- 'image' | 'video' | 'unknown'
    file_type       TEXT,               -- resOriginalFileType (HEIC/JPEG/MOV…)
    source          TEXT NOT NULL,      -- 'whatsapp' | 'photos' | 'unknown'
    albums          TEXT,               -- JSON array of album names (json1)

    -- Flags
    is_favorite     INTEGER NOT NULL,   -- 0/1
    is_hidden       INTEGER NOT NULL DEFAULT 0,
    is_live_photo   INTEGER NOT NULL DEFAULT 0,
    is_duplicate    INTEGER NOT NULL DEFAULT 0,

    -- Media characteristics
    caption         TEXT,               -- decoded captionEnc
    width           INTEGER,            -- resOriginalWidth
    height          INTEGER,            -- resOriginalHeight
    duration        REAL,               -- seconds (videos)
    subtype         TEXT,               -- assetSubtype (screenshot/panorama/portrait…)
    hdr_type        TEXT,               -- assetHDRType
    has_adjustments INTEGER DEFAULT 0,  -- adjustmentType present → edited

    -- Location (raw GPS only; no place names)
    latitude        REAL,               -- locationLatitude
    longitude       REAL,               -- locationLongitude

    -- EXIF camera data — NOT in CloudKit metadata. Populated only by parsing
    -- the downloaded original at offload time; nullable / often absent.
    device_make     TEXT,               -- EXIF Make (e.g. Apple)
    device_model    TEXT,               -- EXIF Model (e.g. iPhone 14 Pro)
    lens            TEXT,               -- EXIF LensModel (often missing)
    aperture        REAL,               -- EXIF FNumber
    iso             INTEGER,            -- EXIF ISOSpeedRatings
    focal_length    REAL,               -- EXIF FocalLength (mm)

    -- Dedup / change tracking
    fingerprint     TEXT,               -- resOriginalFingerprint (content hash)
    change_tag      TEXT,               -- recordChangeTag (incremental-scan key)

    -- Dates (all ISO-8601 UTC TEXT)
    captured_at     TEXT NOT NULL,      -- assetDate (capture date)
    added_at        TEXT,               -- addedDate (entered iCloud library)
    tz_offset       INTEGER,            -- timeZoneOffset at capture

    -- Scoring & offload lifecycle
    score           REAL,               -- last computed offload score
    status          TEXT NOT NULL DEFAULT 'in_icloud', -- in_icloud | offloaded | gone
    local_path      TEXT,               -- NAS destination once offloaded
    offloaded_at    TEXT,               -- when we moved it

    first_seen_at   TEXT NOT NULL,      -- first scan that indexed this asset
    last_seen_at    TEXT NOT NULL       -- most recent scan that saw it in iCloud
);

CREATE INDEX IF NOT EXISTS idx_assets_status      ON assets(status);
CREATE INDEX IF NOT EXISTS idx_assets_captured    ON assets(captured_at);
CREATE INDEX IF NOT EXISTS idx_assets_source      ON assets(source);
CREATE INDEX IF NOT EXISTS idx_assets_media_type  ON assets(media_type);
CREATE INDEX IF NOT EXISTS idx_assets_is_favorite ON assets(is_favorite);
CREATE INDEX IF NOT EXISTS idx_assets_fingerprint ON assets(fingerprint); -- dedup
```

### The two timestamps Ben asked for (now both captured)

- **"When it was added to iCloud"** → `added_at` from `addedDate`
  ([accessor `photo.added_date`](../.venv/Lib/site-packages/pyicloud/services/photos_cloudkit/service.py#L1740)).
  This is the *accurate* import date — distinct from `captured_at` (the capture
  date, which is what `photo.created` returns). We store **both**.
- **"When we offloaded it"** → `offloaded_at`, set in the live offload path
  alongside `local_path` and `status = 'offloaded'`.

### Design notes
- **Times as ISO-8601 UTC TEXT.** SQLite has no native datetime type; ISO text
  sorts correctly and works with its date functions. Keep everything UTC to
  match the rest of the codebase.
- **`fingerprint` replaces the weak dedup heuristic.** Current dedup keys on
  `(size, creation-minute)` in [`analyser.py:84`](../app/analyser.py#L84);
  `resOriginalFingerprint` is Apple's real content hash — set `is_duplicate` by
  grouping on it instead.
- **`change_tag` enables incremental scans.** If an asset's `recordChangeTag`
  matches what we stored, nothing changed — skip re-processing. Dovetails with
  the scan-cache TODO.
- **Albums as a JSON column** (decided), not a junction table.
- **`status` lifecycle:** `in_icloud` (default on first sight) →
  `offloaded` (after a confirmed NAS write + iCloud delete). A row whose
  `last_seen_at` is older than the latest scan and that we *didn't* offload can
  be reconciled to `gone` (e.g. Ben deleted it by hand in Photos).
- **`first_seen_at` / `last_seen_at`** give us the incremental story: on each
  scan, upsert and bump `last_seen_at`; only brand-new `asset_id`s get a fresh
  `first_seen_at`. This is also the natural hook for the scan-cache TODO — a
  warm index means we don't reprocess unchanged assets.

## 6. Pipeline integration

A thin `app/index.py` module owning the connection + upsert/query helpers,
wired in at two points:

1. **After scan/score** ([`main.run`](../app/main.py#L21-L41)): upsert every
   scored asset — refresh metadata, `score`, `last_seen_at`; insert new ones
   with `first_seen_at`.
2. **After a confirmed offload** ([`actions._do_offload`](../app/actions.py#L85-L112)):
   on `OFFLOADED`, set `status='offloaded'`, `local_path`, `offloaded_at`. In
   **dry-run** we write nothing (or optionally record `would_offload` without
   mutating status — keep dry-run side-effect-free by default).

Example queries this unlocks:

```sql
-- Big old WhatsApp videos still sitting in iCloud
SELECT filename, size_bytes/1024/1024 AS mb, captured_at
FROM assets
WHERE status='in_icloud' AND source='whatsapp' AND media_type='video'
  AND captured_at < date('now','-1 year')
ORDER BY mb DESC;

-- How much have we offloaded, and where?
SELECT count(*) AS files, sum(size_bytes)/1024/1024/1024.0 AS gb_freed,
       min(offloaded_at) AS first, max(offloaded_at) AS latest
FROM assets WHERE status='offloaded';

-- "Where did file X go?"
SELECT filename, local_path, offloaded_at FROM assets WHERE filename = ?;

-- Photos taken near a location (raw GPS, no place names)
SELECT filename, latitude, longitude, captured_at
FROM assets WHERE latitude IS NOT NULL
  AND latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?;

-- True duplicates by Apple content hash
SELECT fingerprint, count(*) n, group_concat(filename)
FROM assets WHERE fingerprint IS NOT NULL
GROUP BY fingerprint HAVING n > 1;
```

## 7. Operational notes
- **Location:** a single file (e.g. `data/asset_index.db`) on a dedicated Docker
  volume so it survives container restarts — separate from the `~/.pyicloud`
  session bind mount.
- **PRAGMAs at startup:** `journal_mode=WAL`, `foreign_keys=ON`,
  `synchronous=NORMAL` (fine for a single writer on a Pi).
- **Backup:** copy the `.db` file (or `VACUUM INTO`) — exclude it from any
  memory/dotfiles sync; it's data, not config.
- **Migrations:** at this size a tiny hand-rolled `schema_version` check is
  enough; no need for Alembic.

## 8. Suggested next steps (maps to the TODO items)
1. Add `app/index.py`: connection bootstrap + schema + `upsert_assets()` /
   `mark_offloaded()` / a couple of read helpers.
2. Wire upsert into `main.run` and `mark_offloaded` into the live offload path.
3. Point it at a Docker volume in `docker-compose.yml`.
4. Add a small `python -m app.index query ...` CLI (or just document `sqlite3`
   one-liners) for ad-hoc lookups.
5. Tests with fixture assets covering insert → re-scan upsert → offload
   transition → reconcile-to-`gone`.

**Decision:** SQLite, single `assets` table, DuckDB available ad-hoc if heavier
analysis is ever wanted.
