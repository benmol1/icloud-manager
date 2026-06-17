"""
Cross-reference the live iCloud library (the SQLite asset index) against the
existing local photo archive on disk, and report the overlap / divergence.

Why this is non-trivial — what does NOT work as a join key:
  * filename      — iCloud's UUID filenames for app-saved media are *different*
                    from the archive's UUIDs (different export lineage), and
                    camera-roll ``IMG_xxxx`` names recycle across years.
  * resOriginalFingerprint — Apple's content hash. It is NOT reproducible from
                    the bytes ``download("original")`` returns (verified against
                    files this tool downloaded itself), so it can't be recomputed
                    for archive files. Useful only for iCloud-internal dedup.

What DOES work:
  * exact ``size_bytes`` — verified byte-exact for true originals (every
    tool-offloaded file's on-disk size equals the indexed size). File sizes are
    near-unique (~99% of archive sizes are unique), so an exact size collision is
    rare. We then *validate* each size match against the archive's YYYY/MM folder
    (the archive is organised by capture date) versus the asset's captured_at, so
    size collisions across different dates are filtered out.

Usage:
    uv run python scripts/reconcile_archive.py [ARCHIVE_ROOT]

ARCHIVE_ROOT defaults to the SMB_MOUNT_PATH from config (the offload target).
Read-only: it touches no files in iCloud or on disk beyond stat()/walk.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections import defaultdict

from app.config import config


def build_size_index(root: str) -> tuple[dict[int, list[tuple[str | None, str | None, str]]], int]:
    """Map size_bytes -> [(year, month, path), ...] for every archive file.

    Year/month come from the ``<root>/YYYY/MM/`` folder layout the archive (and
    this tool's offloads) use, so a match can be validated against capture date.
    """
    size_index: dict[int, list[tuple[str | None, str | None, str]]] = defaultdict(list)
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        parts = os.path.relpath(dirpath, root).split(os.sep)
        year = parts[0] if parts and parts[0].isdigit() and len(parts[0]) == 4 else None
        month = parts[1] if len(parts) > 1 and parts[1].isdigit() else None
        for name in files:
            if name == "copy_log.txt":
                continue
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            total += 1
            size_index[size].append((year, month, full))
    return size_index, total


def reconcile(db_path: str, root: str) -> dict:
    size_index, archive_total = build_size_index(root)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    offloaded = conn.execute(
        "SELECT local_path FROM assets WHERE status='offloaded' AND local_path IS NOT NULL"
    ).fetchall()
    offload_paths = {os.path.normpath(r["local_path"]) for r in offloaded}
    offload_present = sum(1 for p in offload_paths if os.path.exists(p))

    live = conn.execute(
        "SELECT size_bytes, substr(captured_at,1,4) y, substr(captured_at,6,2) m "
        "FROM assets WHERE status='in_icloud'"
    ).fetchall()

    both_ym = both_year = collision = icloud_only = 0
    matched_paths: set[str] = set()
    for r in live:
        cands = size_index.get(r["size_bytes"])
        if not cands:
            icloud_only += 1
            continue
        yms = {(c[0], c[1]) for c in cands}
        years = {c[0] for c in cands}
        if (r["y"], r["m"]) in yms:
            both_ym += 1
            matched_paths.update(c[2] for c in cands if (c[0], c[1]) == (r["y"], r["m"]))
        elif r["y"] in years:
            both_year += 1
            matched_paths.update(c[2] for c in cands if c[0] == r["y"])
        else:
            collision += 1

    conn.close()
    return {
        "archive_total": archive_total,
        "offload_tracked": len(offload_paths),
        "offload_present": offload_present,
        "live_total": len(live),
        "both_ym": both_ym,
        "both_year": both_year,
        "collision": collision,
        "icloud_only": icloud_only,
        "overlap": both_ym + both_year,
    }


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    root = argv[0] if argv else config.smb_mount_path
    db_path = config.index_db_path

    print(f"Archive root : {root}")
    print(f"Index DB     : {db_path}")
    print("Reconciling (walking archive, this can take a moment)…\n")

    s = reconcile(db_path, root)

    print("================ RECONCILIATION ================")
    print(f"Local archive files (excl. copy_log)  : {s['archive_total']:>7}")
    print(f"  of which this tool's own offloads   : {s['offload_tracked']:>7} "
          f"({s['offload_present']} present on disk)")
    print(f"Live iCloud assets (status=in_icloud) : {s['live_total']:>7}")
    print()
    print("--- Live iCloud vs archive overlap ---")
    print(f"  In BOTH  (size + same year & month) : {s['both_ym']:>7}  high confidence")
    print(f"  In BOTH  (size + same year)         : {s['both_year']:>7}  likely")
    print(f"  Size collision (year mismatch)      : {s['collision']:>7}  -> count as iCloud-only")
    print(f"  iCloud-only (no matching size)      : {s['icloud_only']:>7}")
    print()
    overlap = s["overlap"]
    icloud_only = s["icloud_only"] + s["collision"]
    pct = 100 * overlap / s["live_total"] if s["live_total"] else 0
    print(f"  => true overlap ~ {overlap} ({pct:.1f}% of live library)")
    print(f"  => iCloud-only  ~ {icloud_only} (would be NEW files if offloaded)")


if __name__ == "__main__":
    main()
