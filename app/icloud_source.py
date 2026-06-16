"""
Concrete pyicloud-backed :class:`~app.actions.AssetSource`.

This is the live download/delete seam that lets :func:`app.actions.offload`
actually move files. It resolves one of our :class:`~app.models.Asset` records
back to the live pyicloud ``PhotoAsset``, downloads the original bytes, and
soft-deletes the asset from iCloud.

Resolution uses a **direct CloudKit record lookup** by record name (the
``CPLMaster`` + ``CPLAsset`` pair, keyed off the ``master_id`` we store in the
index). That's O(1) per asset. The old approach — ``photos.all.get(id)`` — looks
like a point lookup but falls back inside pyicloud to linearly scanning the
*entire* date-sorted library, costing minutes per asset. We keep that iteration
as a **fallback** for when the direct path can't run (missing ``master_id``, or a
future pyicloud release moving the internals we depend on), so a breakage
degrades to "slow but works" rather than failing outright.

``delete`` performs iCloud's *soft* delete (``isDeleted = 1``), so the asset
lands in **Recently Deleted** and stays recoverable for ~30 days — a safety net
on top of ``actions.offload``'s write-before-delete ordering.
"""

import logging

from pyicloud import PyiCloudService

from app.models import Asset

logger = logging.getLogger(__name__)

# The direct-lookup fast path uses pyicloud internals. Imported defensively: if a
# future release moves these, _DIRECT_LOOKUP_AVAILABLE flips False and _resolve
# silently uses the (slow but stable) public iteration fallback.
try:
    from pyicloud.common.cloudkit import CKZoneIDReq
    from pyicloud.services.photos_cloudkit.constants import PRIMARY_ZONE
    from pyicloud.services.photos_cloudkit.service import (
        PHOTO_DESIRED_KEYS,
        PhotoAsset,
    )

    _DIRECT_LOOKUP_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only on pyicloud API drift
    _DIRECT_LOOKUP_AVAILABLE = False


class AssetNotFoundError(RuntimeError):
    """The asset could no longer be resolved in the live iCloud library."""


class PyiCloudAssetSource:
    """Live iCloud access for offload, backed by an authenticated pyicloud API."""

    def __init__(self, api: PyiCloudService) -> None:
        self._api = api
        # Cache resolved PhotoAssets so download() and the subsequent delete()
        # reuse a single per-id lookup instead of resolving twice.
        self._cache: dict[str, object] = {}

    def download(self, asset: Asset) -> bytes:
        photo = self._resolve(asset)
        data = photo.download("original")
        if data is None:
            raise AssetNotFoundError(
                f"No downloadable original for {asset.filename} ({asset.asset_id})"
            )
        return data

    def delete(self, asset: Asset) -> None:
        photo = self._resolve(asset)
        photo.delete()
        # Drop the now-deleted asset from the cache.
        self._cache.pop(asset.asset_id, None)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def _resolve(self, asset: Asset):
        cached = self._cache.get(asset.asset_id)
        if cached is not None:
            return cached

        photo = self._resolve_direct(asset)
        if photo is None:
            photo = self._resolve_by_iteration(asset)
        if photo is None:
            raise AssetNotFoundError(
                f"Asset {asset.filename} ({asset.asset_id}) not found in iCloud"
            )

        self._cache[asset.asset_id] = photo
        return photo

    def _resolve_direct(self, asset: Asset):
        """Fast path: fetch the CPLMaster + CPLAsset records directly by name.

        Returns ``None`` (so the caller falls back to iteration) when the direct
        path can't run or doesn't find a usable record pair.
        """
        if not _DIRECT_LOOKUP_AVAILABLE or not asset.master_id:
            return None

        try:
            service = self._api.photos
            response = service._private_client.lookup(
                record_names=[asset.master_id, asset.asset_id],
                zone_id=CKZoneIDReq(**PRIMARY_ZONE),
                desired_keys=PHOTO_DESIRED_KEYS,
            )
            # We asked for exactly the master + asset by name, so there's at most
            # one of each; anything else (e.g. not-found error items) is ignored.
            masters = [
                r for r in response.records
                if getattr(r, "recordType", None) == "CPLMaster"
            ]
            asset_recs = [
                r for r in response.records
                if getattr(r, "recordType", None) == "CPLAsset"
            ]
            if masters and asset_recs:
                photo = PhotoAsset(service, masters[0], asset_recs[0])
                setattr(photo, "_library", service._root_library)
                return photo
        except Exception:  # noqa: BLE001 — never fail offload on the fast path
            logger.warning(
                "Direct lookup failed for %s (%s); falling back to iteration",
                asset.filename,
                asset.asset_id,
                exc_info=True,
            )
        return None

    def _resolve_by_iteration(self, asset: Asset):
        """Slow, stable fallback: ask the live library to find the asset by id.

        ``photos.all.get`` linearly scans the date-sorted library, so this can
        cost minutes per asset — only used when the direct path is unavailable.
        """
        logger.info(
            "Resolving %s via library scan (direct lookup unavailable)",
            asset.filename,
        )
        return self._api.photos.all.get(asset.asset_id)
