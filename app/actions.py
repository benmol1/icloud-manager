"""
Offload actions: download an asset from iCloud, write it to the SMB share
(organised by year/month), then delete it from iCloud.

The live iCloud download/delete is performed through an `AssetSource` so the
logic here stays testable with a fake source. `offload` defaults to **dry-run**,
in which it computes destinations and logs what it *would* do without touching
the filesystem or iCloud.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from app.analyser import ScoredAsset
from app.config import config
from app.models import Asset

logger = logging.getLogger(__name__)


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
) -> list[OffloadResult]:
    """
    Offload each scored asset to the SMB share.

    In dry-run mode nothing is downloaded, written, or deleted — each item is
    logged and reported as ``WOULD_OFFLOAD``. In live mode each asset is
    downloaded, written to its year/month destination, and only then deleted
    from iCloud; a failure at any step leaves the iCloud copy untouched.
    """
    base = Path(mount_path or config.smb_mount_path)
    reserved: set[Path] = set()
    results: list[OffloadResult] = []

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
    return base / f"{asset.created:%Y}" / f"{asset.created:%m}" / asset.filename


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
