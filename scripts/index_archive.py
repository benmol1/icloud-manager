"""
Index the local photo archive (``D:\\icloud-photos``) into the asset index so the
pre-existing files — the 2014–2023 history that was never moved by this tool —
show up in ``stats`` / ``breakdown`` / ``search`` alongside the iCloud assets.

Design (see also scripts/reconcile_archive.py):
  * status='archived'        — distinct from 'offloaded' (which means *we* moved
                               it off iCloud); these were always local.
  * asset_id='local:<relpath>' — path-based, stable, idempotent on re-run. No
                               need to hash 50 GB; iCloud ids are uppercase UUIDs
                               so there's no collision.
  * captured_at from the YYYY/MM folder layout (day 01). misc/ and any file not
                               under a year/month folder fall back to mtime.
  * Files already tracked by the offload pipeline (a known local_path) are
                               skipped, so the ~1,210 already-offloaded files
                               aren't double-counted.

Usage:
    uv run python scripts/index_archive.py            # preview (no writes)
    uv run python scripts/index_archive.py --commit   # write to the index
    uv run python scripts/index_archive.py --commit /some/other/root
"""

from __future__ import annotations

import argparse
import os
from collections import Counter
from datetime import datetime, timezone

from app.config import config
from app.index import AssetIndex
from app.models import MediaType
from app.scanner import _detect_source

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".3gp", ".avi"}
_SKIP_NAMES = {"copy_log.txt"}


def _media_type(ext: str) -> MediaType:
    ext = ext.lower()
    if ext in _IMAGE_EXTS:
        return MediaType.IMAGE
    if ext in _VIDEO_EXTS:
        return MediaType.VIDEO
    return MediaType.UNKNOWN


def _captured_at(parts: list[str], full_path: str) -> str:
    """ISO-8601 UTC capture date from the ``YYYY/MM`` folders, else file mtime."""
    year = parts[0] if parts and parts[0].isdigit() and len(parts[0]) == 4 else None
    month = parts[1] if len(parts) > 1 and parts[1].isdigit() else "01"
    if year:
        return f"{year}-{month}-01T00:00:00+00:00"
    try:
        ts = os.path.getmtime(full_path)
    except OSError:
        ts = 0
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def build_records(root: str, known: set[str]) -> tuple[list[dict], int, int]:
    """Walk *root* and build archive records, skipping already-tracked paths."""
    records: list[dict] = []
    walked = skipped = 0
    for dirpath, _dirs, files in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        parts = [] if rel_dir == "." else rel_dir.split(os.sep)
        for name in files:
            if name in _SKIP_NAMES:
                continue
            walked += 1
            full = os.path.join(dirpath, name)
            if os.path.normpath(full) in known:
                skipped += 1
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            ext = os.path.splitext(name)[1]
            records.append(
                {
                    "asset_id": f"local:{rel}",
                    "filename": name,
                    "size_bytes": size,
                    "media_type": _media_type(ext).value,
                    "source": _detect_source(name, []).value,
                    "captured_at": _captured_at(parts, full),
                    "local_path": full,
                }
            )
    return records, walked, skipped


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Index the local photo archive.")
    parser.add_argument("root", nargs="?", default=config.smb_mount_path,
                        help="Archive root (default: SMB_MOUNT_PATH)")
    parser.add_argument("--commit", action="store_true",
                        help="Write to the index (default: preview only)")
    args = parser.parse_args(argv)

    print(f"Archive root : {args.root}")
    print(f"Index DB     : {config.index_db_path}")

    with AssetIndex() as index:
        known = index.known_local_paths()
        records, walked, skipped = build_records(args.root, known)

        by_year = Counter(r["captured_at"][:4] for r in records)
        by_type = Counter(r["media_type"] for r in records)
        by_source = Counter(r["source"] for r in records)

        print(f"\nFiles walked              : {walked}")
        print(f"Already tracked (skipped) : {skipped}")
        print(f"New archive files to index: {len(records)}")
        print(f"  by media type : {dict(by_type)}")
        print(f"  by source     : {dict(by_source)}")
        print("  by year       :")
        for y in sorted(by_year):
            print(f"    {y}: {by_year[y]}")

        if not args.commit:
            print("\n(preview only — re-run with --commit to write to the index)")
            return

        n = index.upsert_archive_files(records)
        print(f"\nIndexed {n} archive files (status='archived').")


if __name__ == "__main__":
    main()
