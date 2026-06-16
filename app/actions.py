"""
Offload actions: download an asset from iCloud, write it to the SMB share
(organised by year/month), then delete it from iCloud.

The live iCloud download/delete is performed through an `AssetSource` so the
logic here stays testable with a fake source. `offload` defaults to **dry-run**,
in which it computes destinations and logs what it *would* do without touching
the filesystem or iCloud.
"""

import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

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
) -> list[OffloadResult]:
    """
    Offload each scored asset to the SMB share.

    In dry-run mode nothing is downloaded, written, or deleted — each item is
    logged and reported as ``WOULD_OFFLOAD``. In live mode each asset is
    downloaded, written to its year/month destination, and only then deleted
    from iCloud; a failure at any step leaves the iCloud copy untouched.

    ``max_items`` (when > 0) caps how many assets are processed this run — used
    to keep an initial live test to a small, safe handful.
    """
    base = Path(mount_path or config.smb_mount_path)
    reserved: set[Path] = set()
    results: list[OffloadResult] = []

    if max_items and max_items > 0:
        items = items[:max_items]

    for item in items:
        asset = item.asset
        dest = _unique_destination(_destination_path(asset, base), reserved)
        reserved.add(dest)

        if dry_run:
            logger.info("[dry-run] would offload %s -> %s", asset.filename, dest)
            results.append(_result(asset, dest, OffloadStatus.WOULD_OFFLOAD))
            continue

        results.append(_do_offload(item, dest, source))

    return results


# ------------------------------------------------------------------
# Internals
# ------------------------------------------------------------------

def _do_offload(
    item: ScoredAsset, dest: Path, source: AssetSource | None
) -> OffloadResult:
    asset = item.asset
    if source is None:
        msg = "no AssetSource provided for live offload"
        logger.error("Cannot offload %s: %s", asset.filename, msg)
        return _result(asset, dest, OffloadStatus.FAILED, msg)

    try:
        data = source.download(asset)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    except Exception as exc:  # noqa: BLE001 — report, never delete on failure
        logger.exception("Failed to write %s to %s", asset.filename, dest)
        return _result(asset, dest, OffloadStatus.FAILED, str(exc))

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
