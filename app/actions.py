"""
Offload actions: download an asset from iCloud, write it to the SMB share
(organised by year/month), then delete it from iCloud.

The live iCloud download/delete is performed through an `AssetSource` so the
logic here stays testable with a fake source. `offload` defaults to **dry-run**,
in which it computes destinations and logs what it *would* do without touching
the filesystem or iCloud.
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from app.analyser import ScoredAsset
from app.config import config
from app.models import Asset

logger = logging.getLogger(__name__)

# iCloud keeps real names for camera-roll shots (IMG_2351.JPG) but stores
# app-saved media (WhatsApp/AirDrop) under an opaque UUID stem. Detect the
# latter so we can give it a recognisable name on the NAS.
_UUID_STEM_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class AssetSource(Protocol):
    """Live iCloud access needed to actually offload an asset."""

    def download(self, asset: Asset) -> bytes: ...

    def delete(self, asset: Asset) -> None: ...


class OffloadStatus(str, Enum):
    WOULD_OFFLOAD = "would_offload"  # dry-run only
    OFFLOADED = "offloaded"
    ALREADY_ARCHIVED = "already_archived"  # identical bytes already on disk; not re-written
    FAILED = "failed"


@dataclass
class OffloadResult:
    asset_id: str
    filename: str
    destination: str
    status: OffloadStatus
    detail: str = ""


def offload(
    items: list[ScoredAsset],
    *,
    dry_run: bool,
    source: AssetSource | None = None,
    mount_path: str | None = None,
    max_items: int | None = None,
    on_offloaded: Callable[[OffloadResult], None] | None = None,
) -> list[OffloadResult]:
    """
    Offload each scored asset to the SMB share.

    In dry-run mode nothing is downloaded, written, or deleted — each item is
    logged and reported as ``WOULD_OFFLOAD``. In live mode each asset is
    downloaded, written to its year/month destination, and only then deleted
    from iCloud; a failure at any step leaves the iCloud copy untouched.

    ``on_offloaded`` (when given) is invoked with each ``OffloadResult`` the
    instant a live offload succeeds — *before* moving to the next asset — so the
    caller can durably record progress. This keeps the index accurate even if a
    large batch is interrupted partway through.

    ``max_items`` (when > 0) caps how many assets are processed this run — used
    to keep an initial live test to a small, safe handful.
    """
    base = Path(mount_path or config.smb_mount_path)
    reserved: set[Path] = set()
    results: list[OffloadResult] = []

    if max_items and max_items > 0:
        items = items[:max_items]

    # Content-dedup: index the destination tree by file size so a downloaded
    # asset whose bytes already exist on disk (under any name/path — filenames
    # don't survive the iCloud→archive round-trip) is skipped instead of stored
    # twice. Size is the cheap pre-filter; the SHA-256 check only runs on a size
    # collision. Built once per run (live mode only — dry-run never downloads).
    size_index: dict[int, list[Path]] = {} if dry_run else _build_size_index(base)
    hash_cache: dict[Path, str] = {}

    for item in items:
        asset = item.asset
        dest = _unique_destination(_destination_path(asset, base), reserved)
        reserved.add(dest)

        if dry_run:
            logger.info("[dry-run] would offload %s -> %s", asset.filename, dest)
            results.append(_result(asset, dest, OffloadStatus.WOULD_OFFLOAD))
            continue

        result = _do_offload(item, dest, source, size_index, hash_cache)
        results.append(result)
        # Record success immediately so an interrupted batch leaves an accurate,
        # resumable index rather than losing every offload done this run. An
        # already-archived asset is just as "done" (bytes safe on disk + removed
        # from iCloud), so it's recorded the same way, pointing at the existing copy.
        if on_offloaded is not None and result.status in (
            OffloadStatus.OFFLOADED,
            OffloadStatus.ALREADY_ARCHIVED,
        ):
            on_offloaded(result)

    return results


# ------------------------------------------------------------------
# Internals
# ------------------------------------------------------------------

def _do_offload(
    item: ScoredAsset,
    dest: Path,
    source: AssetSource | None,
    size_index: dict[int, list[Path]],
    hash_cache: dict[Path, str],
) -> OffloadResult:
    asset = item.asset
    if source is None:
        msg = "no AssetSource provided for live offload"
        logger.error("Cannot offload %s: %s", asset.filename, msg)
        return _result(asset, dest, OffloadStatus.FAILED, msg)

    try:
        data = source.download(asset)
    except Exception as exc:  # noqa: BLE001 — report, never delete on failure
        logger.exception("Failed to download %s", asset.filename)
        return _result(asset, dest, OffloadStatus.FAILED, str(exc))

    # If these exact bytes already live in the archive, don't store a second
    # copy — point at the existing file and still reclaim the iCloud space.
    digest = hashlib.sha256(data).hexdigest()
    existing = _find_duplicate(digest, len(data), size_index, hash_cache)
    if existing is not None:
        try:
            source.delete(asset)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Found duplicate of %s but failed to delete from iCloud", asset.filename)
            return _result(
                asset, existing, OffloadStatus.FAILED,
                f"identical to {existing} but not deleted: {exc}",
            )
        logger.info(
            "Skipped %s — identical content already archived at %s", asset.filename, existing
        )
        return _result(asset, existing, OffloadStatus.ALREADY_ARCHIVED)

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to write %s to %s", asset.filename, dest)
        return _result(asset, dest, OffloadStatus.FAILED, str(exc))

    # Register the new file so a later identical asset in the *same* batch dedups
    # against it (the on-disk index was snapshotted before the run started).
    size_index.setdefault(len(data), []).append(dest)
    hash_cache[dest] = digest

    # Only delete from iCloud once the local copy is safely written.
    try:
        source.delete(asset)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Wrote %s but failed to delete from iCloud", dest)
        return _result(
            asset, dest, OffloadStatus.FAILED, f"written but not deleted: {exc}"
        )

    logger.info("Offloaded %s -> %s", asset.filename, dest)
    return _result(asset, dest, OffloadStatus.OFFLOADED)


def _build_size_index(base: Path) -> dict[int, list[Path]]:
    """Map ``size_bytes -> [path, ...]`` for every file under *base*.

    The cheap pre-filter for content-dedup: only files sharing an asset's exact
    byte size are candidates for a SHA-256 comparison. Returns empty when the
    archive root doesn't exist yet (first ever offload).
    """
    index: dict[int, list[Path]] = {}
    if not base.exists():
        return index
    for path in base.rglob("*"):
        try:
            if path.is_file():
                index.setdefault(path.stat().st_size, []).append(path)
        except OSError:  # noqa: PERF203 — skip unreadable entries, keep going
            continue
    return index


def _find_duplicate(
    digest: str,
    size: int,
    size_index: dict[int, list[Path]],
    hash_cache: dict[Path, str],
) -> Path | None:
    """Return an existing archive file whose bytes equal *digest*, or ``None``.

    Only files of the same *size* are hashed (and each is hashed at most once,
    cached in *hash_cache*), so the common no-collision case costs nothing.
    """
    for path in size_index.get(size, []):
        cached = hash_cache.get(path)
        if cached is None:
            try:
                cached = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            hash_cache[path] = cached
        if cached == digest:
            return path
    return None


def _destination_path(asset: Asset, base: Path) -> Path:
    """``<base>/<YYYY>/<MM>/<filename>`` based on the asset's creation date."""
    return base / f"{asset.created:%Y}" / f"{asset.created:%m}" / _offload_filename(asset)


def _offload_filename(asset: Asset) -> str:
    """
    Human-recognisable filename for the NAS.

    iCloud stores app-saved media (WhatsApp/AirDrop) under an opaque UUID such as
    ``b9d9d5e4-3467-4559-a0fd-5cc7d59a242a.mp4``. For those we synthesise a
    sortable ``YYYYMMDD_HHMMSS_<source>_<short>`` name so files are recognisable
    and auditable on disk. Names that are already meaningful (e.g.
    ``IMG_2351.JPG``) are kept exactly as-is.
    """
    original = Path(asset.filename)
    if not _UUID_STEM_RE.match(original.stem):
        return asset.filename

    timestamp = asset.created.strftime("%Y%m%d_%H%M%S")
    short_id = asset.asset_id.replace("-", "")[:8].lower()
    return f"{timestamp}_{asset.source.value}_{short_id}{original.suffix.lower()}"


def _unique_destination(dest: Path, reserved: set[Path]) -> Path:
    """
    Avoid clobbering an existing file or another file chosen earlier in this
    same run by appending ` (1)`, ` (2)`, … before the extension.
    """
    candidate = dest
    counter = 1
    while candidate in reserved or candidate.exists():
        candidate = dest.with_name(f"{dest.stem} ({counter}){dest.suffix}")
        counter += 1
    return candidate


def _result(
    asset: Asset, dest: Path, status: OffloadStatus, detail: str = ""
) -> OffloadResult:
    return OffloadResult(
        asset_id=asset.asset_id,
        filename=asset.filename,
        destination=str(dest),
        status=status,
        detail=detail,
    )
