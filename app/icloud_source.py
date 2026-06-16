"""
Concrete pyicloud-backed :class:`~app.actions.AssetSource`.

This is the live download/delete seam that lets :func:`app.actions.offload`
actually move files. It resolves one of our :class:`~app.models.Asset` records
back to the live pyicloud ``PhotoAsset`` (by ``asset_id``), downloads the
original bytes, and soft-deletes the asset from iCloud.

``delete`` performs iCloud's *soft* delete (``isDeleted = 1``), so the asset
lands in **Recently Deleted** and stays recoverable for ~30 days — a safety net
on top of ``actions.offload``'s write-before-delete ordering.
"""

import logging

from pyicloud import PyiCloudService

from app.models import Asset

logger = logging.getLogger(__name__)


class AssetNotFoundError(RuntimeError):
    """The asset could no longer be resolved in the live iCloud library."""


class PyiCloudAssetSource:
    """Live iCloud access for offload, backed by an authenticated pyicloud API."""

    def __init__(self, api: PyiCloudService) -> None:
        self._api = api
        # Cache resolved PhotoAssets so download() and the subsequent delete()
        # reuse a single per-id CloudKit lookup instead of querying twice.
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

    def _resolve(self, asset: Asset):
        cached = self._cache.get(asset.asset_id)
        if cached is not None:
            return cached
        photo = self._api.photos.all.get(asset.asset_id)
        if photo is None:
            raise AssetNotFoundError(
                f"Asset {asset.filename} ({asset.asset_id}) not found in iCloud"
            )
        self._cache[asset.asset_id] = photo
        return photo
