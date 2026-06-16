from datetime import datetime, timezone

import pytest

from app.icloud_source import AssetNotFoundError, PyiCloudAssetSource
from app.models import Asset, MediaType, Source


def _asset(asset_id: str = "abc123", filename: str = "IMG-0001.jpg") -> Asset:
    return Asset(
        asset_id=asset_id,
        filename=filename,
        size=1024,
        created=datetime(2023, 7, 9, tzinfo=timezone.utc),
        media_type=MediaType.IMAGE,
        is_favorite=False,
        source=Source.WHATSAPP,
    )


class FakePhoto:
    def __init__(self, data: bytes | None = b"bytes"):
        self._data = data
        self.deleted = False
        self.downloads: list[str] = []

    def download(self, version: str = "original") -> bytes | None:
        self.downloads.append(version)
        return self._data

    def delete(self) -> bool:
        self.deleted = True
        return True


class FakeAllContainer:
    def __init__(self, photos: dict[str, FakePhoto]):
        self._photos = photos
        self.lookups: list[str] = []

    def get(self, key: str):
        self.lookups.append(key)
        return self._photos.get(key)


class FakeApi:
    def __init__(self, photos: dict[str, FakePhoto]):
        self.photos = type("Photos", (), {"all": FakeAllContainer(photos)})()


class TestDownload:
    def test_returns_original_bytes(self):
        photo = FakePhoto(b"hello")
        source = PyiCloudAssetSource(FakeApi({"abc123": photo}))
        assert source.download(_asset()) == b"hello"
        assert photo.downloads == ["original"]

    def test_missing_asset_raises(self):
        source = PyiCloudAssetSource(FakeApi({}))
        with pytest.raises(AssetNotFoundError):
            source.download(_asset())

    def test_no_downloadable_original_raises(self):
        source = PyiCloudAssetSource(FakeApi({"abc123": FakePhoto(data=None)}))
        with pytest.raises(AssetNotFoundError):
            source.download(_asset())


class TestDelete:
    def test_soft_deletes(self):
        photo = FakePhoto()
        source = PyiCloudAssetSource(FakeApi({"abc123": photo}))
        source.delete(_asset())
        assert photo.deleted is True

    def test_missing_asset_raises(self):
        source = PyiCloudAssetSource(FakeApi({}))
        with pytest.raises(AssetNotFoundError):
            source.delete(_asset())


class TestResolutionCache:
    def test_download_then_delete_resolves_once(self):
        photo = FakePhoto()
        api = FakeApi({"abc123": photo})
        source = PyiCloudAssetSource(api)
        asset = _asset()
        source.download(asset)
        source.delete(asset)
        # Only one CloudKit lookup despite two operations.
        assert api.photos.all.lookups == ["abc123"]
        assert photo.deleted is True
