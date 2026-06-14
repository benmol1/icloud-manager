"""
Searchable asset index (SQLite).

A persistent record of every asset we've seen in iCloud plus what we've done
with it (offloaded to the NAS, where, when). See
``docs/asset-index-research-spike.md`` for the design rationale.

The store is a single ``assets`` table in a SQLite file (stdlib ``sqlite3``,
no extra deps). The schema is the full forward-looking design; the scanner
currently populates the core/lifecycle columns, and the richer metadata columns
(location, fingerprint, EXIF, …) are nullable and filled in as later TODOs land.

Typical use:

    with AssetIndex() as index:
        index.upsert_scored(scored_assets)
        index.mark_offloaded(asset_id, local_path)

Ad-hoc queries:  uv run python -m app.index stats
                 uv run python -m app.index search --source whatsapp --status in_icloud
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.analyser import ScoredAsset
from app.config import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    asset_id        TEXT PRIMARY KEY,
    master_id       TEXT,
    filename        TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    media_type      TEXT NOT NULL,
    file_type       TEXT,
    source          TEXT NOT NULL,
    albums          TEXT,                              -- JSON array of album names

    is_favorite     INTEGER NOT NULL DEFAULT 0,
    is_hidden       INTEGER NOT NULL DEFAULT 0,
    is_live_photo   INTEGER NOT NULL DEFAULT 0,
    is_duplicate    INTEGER NOT NULL DEFAULT 0,

    caption         TEXT,
    width           INTEGER,
    height          INTEGER,
    duration        REAL,
    subtype         TEXT,
    hdr_type        TEXT,
    has_adjustments INTEGER DEFAULT 0,

    latitude        REAL,
    longitude       REAL,

    device_make     TEXT,
    device_model    TEXT,
    lens            TEXT,
    aperture        REAL,
    iso             INTEGER,
    focal_length    REAL,

    fingerprint     TEXT,
    change_tag      TEXT,

    captured_at     TEXT NOT NULL,                     -- assetDate (ISO-8601 UTC)
    added_at        TEXT,                              -- addedDate (ISO-8601 UTC)
    tz_offset       INTEGER,

    score           REAL,
    status          TEXT NOT NULL DEFAULT 'in_icloud', -- in_icloud | offloaded | gone
    local_path      TEXT,
    offloaded_at    TEXT,

    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assets_status      ON assets(status);
CREATE INDEX IF NOT EXISTS idx_assets_captured    ON assets(captured_at);
CREATE INDEX IF NOT EXISTS idx_assets_source      ON assets(source);
CREATE INDEX IF NOT EXISTS idx_assets_media_type  ON assets(media_type);
CREATE INDEX IF NOT EXISTS idx_assets_is_favorite ON assets(is_favorite);
CREATE INDEX IF NOT EXISTS idx_assets_fingerprint ON assets(fingerprint);
"""

# Columns set on first sight and refreshed on every scan. Deliberately excludes
# the offload lifecycle (status/local_path/offloaded_at) and first_seen_at, so a
# re-scan never clobbers what an offload recorded.
_SCAN_COLUMNS = (
    "filename",
    "size_bytes",
    "media_type",
    "source",
    "albums",
    "is_favorite",
    "is_duplicate",
    "captured_at",
    "score",
    "last_seen_at",
)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class AssetIndex:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.path = Path(db_path or config.index_db_path)
        if self.path.parent and str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    # -- lifecycle -----------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "AssetIndex":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes --------------------------------------------------------

    def upsert_scored(
        self, scored: Iterable[ScoredAsset], *, seen_at: str | None = None
    ) -> int:
        """Insert new assets / refresh existing ones from a scan. Returns count."""
        seen_at = seen_at or _now_iso()
        rows = [self._scored_to_row(item, seen_at) for item in scored]
        if not rows:
            return 0

        update_clause = ", ".join(f"{col}=excluded.{col}" for col in _SCAN_COLUMNS)
        sql = f"""
            INSERT INTO assets (
                asset_id, filename, size_bytes, media_type, source, albums,
                is_favorite, is_duplicate, captured_at, score,
                first_seen_at, last_seen_at
            ) VALUES (
                :asset_id, :filename, :size_bytes, :media_type, :source, :albums,
                :is_favorite, :is_duplicate, :captured_at, :score,
                :first_seen_at, :last_seen_at
            )
            ON CONFLICT(asset_id) DO UPDATE SET {update_clause}
        """
        with self._conn:
            self._conn.executemany(sql, rows)
        return len(rows)

    def mark_offloaded(
        self, asset_id: str, local_path: str, *, offloaded_at: str | None = None
    ) -> None:
        """Record a confirmed offload: status + destination + timestamp."""
        with self._conn:
            self._conn.execute(
                """
                UPDATE assets
                SET status='offloaded', local_path=:local_path,
                    offloaded_at=:offloaded_at
                WHERE asset_id=:asset_id
                """,
                {
                    "asset_id": asset_id,
                    "local_path": local_path,
                    "offloaded_at": offloaded_at or _now_iso(),
                },
            )

    # -- reads ---------------------------------------------------------

    def get(self, asset_id: str) -> sqlite3.Row | None:
        cur = self._conn.execute(
            "SELECT * FROM assets WHERE asset_id=?", (asset_id,)
        )
        return cur.fetchone()

    def search(
        self,
        *,
        source: str | None = None,
        media_type: str | None = None,
        status: str | None = None,
        is_favorite: bool | None = None,
        filename_like: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if source is not None:
            clauses.append("source=:source")
            params["source"] = source
        if media_type is not None:
            clauses.append("media_type=:media_type")
            params["media_type"] = media_type
        if status is not None:
            clauses.append("status=:status")
            params["status"] = status
        if is_favorite is not None:
            clauses.append("is_favorite=:is_favorite")
            params["is_favorite"] = int(is_favorite)
        if filename_like is not None:
            clauses.append("filename LIKE :filename_like")
            params["filename_like"] = f"%{filename_like}%"
        if since is not None:
            clauses.append("captured_at >= :since")
            params["since"] = since
        if until is not None:
            clauses.append("captured_at <= :until")
            params["until"] = until

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params["limit"] = limit
        cur = self._conn.execute(
            f"SELECT * FROM assets {where} ORDER BY captured_at DESC LIMIT :limit",
            params,
        )
        return cur.fetchall()

    def stats(self) -> dict[str, Any]:
        cur = self._conn.execute(
            """
            SELECT status,
                   count(*)         AS files,
                   sum(size_bytes)  AS bytes
            FROM assets GROUP BY status
            """
        )
        by_status = {
            row["status"]: {"files": row["files"], "bytes": row["bytes"] or 0}
            for row in cur.fetchall()
        }
        total = self._conn.execute("SELECT count(*) AS n FROM assets").fetchone()["n"]
        return {"total": total, "by_status": by_status}

    # -- mapping -------------------------------------------------------

    @staticmethod
    def _scored_to_row(item: ScoredAsset, seen_at: str) -> dict[str, Any]:
        asset = item.asset
        return {
            "asset_id": asset.asset_id,
            "filename": asset.filename,
            "size_bytes": asset.size,
            "media_type": asset.media_type.value,
            "source": asset.source.value,
            "albums": json.dumps(asset.albums),
            "is_favorite": int(asset.is_favorite),
            "is_duplicate": int(item.is_duplicate),
            "captured_at": asset.created.isoformat(),
            "score": item.score,
            "first_seen_at": seen_at,
            "last_seen_at": seen_at,
        }


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def _format_bytes(n: int) -> str:
    gb = n / 1024 / 1024 / 1024
    return f"{gb:.2f} GB"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Query the iCloud asset index.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="Summary counts by status")

    s = sub.add_parser("search", help="Filtered lookup")
    s.add_argument("--source")
    s.add_argument("--media-type", dest="media_type")
    s.add_argument("--status")
    s.add_argument("--favorite", dest="favorite", action="store_true")
    s.add_argument("--filename", dest="filename_like")
    s.add_argument("--since")
    s.add_argument("--until")
    s.add_argument("--limit", type=int, default=50)

    args = parser.parse_args(argv)

    with AssetIndex() as index:
        if args.command == "stats":
            data = index.stats()
            print(f"Total assets: {data['total']}")
            for status, agg in sorted(data["by_status"].items()):
                print(
                    f"  {status:12} {agg['files']:>7} files  "
                    f"{_format_bytes(agg['bytes'])}"
                )
        elif args.command == "search":
            rows = index.search(
                source=args.source,
                media_type=args.media_type,
                status=args.status,
                is_favorite=True if args.favorite else None,
                filename_like=args.filename_like,
                since=args.since,
                until=args.until,
                limit=args.limit,
            )
            for row in rows:
                dest = row["local_path"] or "-"
                print(
                    f"{row['captured_at'][:10]}  {row['status']:10}  "
                    f"{row['filename']:40}  {dest}"
                )
            print(f"\n{len(rows)} row(s)")


if __name__ == "__main__":
    main()
