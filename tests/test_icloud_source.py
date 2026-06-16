from datetime import datetime, timezone

import pytest

from app.icloud_source import AssetNotFoundError, PyiCloudAssetSource
from app.models import Asset, MediaType, Source


def _asset(
    asset_id: str = "abc123",
    filename: str = "IMG-0001.jpg",
    master_id: str | None = None,
) -> Asset:
    return Asset(
        asset_id=asset_id,
        filename=filename,
        size=1024,
        created=datetime(2023, 7, 9, tzinfo=timezone.utc),
        media_type=MediaType.IMAGE,
        is_favorite=False,
        source=Source.WHATSAPP,
        master_id=master_id,
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


# ---------------------------------------------------------------------------
# Direct-lookup fast path (resolve by record name instead of scanning library)
# ---------------------------------------------------------------------------

class FakeRecord:
    def __init__(self, record_type: str):
        self.recordType = record_type


class FakeLookupResponse:
    def __init__(self, records: list):
        self.records = records


class FakePrivateClient:
    def __init__(self, records: list, *, raises: Exception | None = None):
        self._records = records
        self._raises = raises
        self.calls: list[list[str]] = []

    def lookup(self, *, record_names, zone_id, desired_keys):
        self.calls.append(list(record_names))
        if self._raises is not None:
            raise self._raises
        return FakeLookupResponse(self._records)


class FakePhotosService:
    def __init__(self, private_client, all_container):
        self._private_client = private_client
        self._root_library = object()
        self.all = all_container


class FakeDirectApi:
    def __init__(self, private_client, all_container):
        self.photos = FakePhotosService(private_client, all_container)


class FakePhotoAsset:
    """Stand-in for pyicloud's PhotoAsset, built from a master+asset pair."""

    last: "FakePhotoAsset | None" = None

    def __init__(self, service, master, asset):
        self.service = service
        self.master = master
        self.asset = asset
        self._library = None
        self.deleted = False
        self.downloads: list[str] = []
        FakePhotoAsset.last = self

    def download(self, version: str = "original") -> bytes:
        self.downloads.append(version)
        return b"DIRECT"

    def delete(self) -> bool:
        self.deleted = True
        return True


def _pair() -> list[FakeRecord]:
    return [FakeRecord("CPLMaster"), FakeRecord("CPLAsset")]


class TestDirectLookup:
    def test_used_when_master_id_present(self, monkeypatch):
        monkeypatch.setattr("app.icloud_source.PhotoAsset", FakePhotoAsset)
        pc = FakePrivateClient(_pair())
        all_container = FakeAllContainer({})  # must NOT be touched
        source = PyiCloudAssetSource(FakeDirectApi(pc, all_container))

        data = source.download(_asset(asset_id="A1", master_id="M1"))

        assert data == b"DIRECT"
        assert pc.calls == [["M1", "A1"]]
        assert all_container.lookups == []  # no library scan

    def test_skipped_without_master_id(self):
        # No master_id -> direct path skipped, iteration fallback used.
        photo = FakePhoto(b"ITER")
        pc = FakePrivateClient(_pair())
        source = PyiCloudAssetSource(FakeDirectApi(pc, FakeAllContainer({"A1": photo})))

        assert source.download(_asset(asset_id="A1")) == b"ITER"
        assert pc.calls == []  # direct lookup never attempted

    def test_falls_back_on_lookup_error(self, monkeypatch):
        monkeypatch.setattr("app.icloud_source.PhotoAsset", FakePhotoAsset)
        pc = FakePrivateClient([], raises=RuntimeError("boom"))
        photo = FakePhoto(b"ITER")
        all_container = FakeAllContainer({"A1": photo})
        source = PyiCloudAssetSource(FakeDirectApi(pc, all_container))

        assert source.download(_asset(asset_id="A1", master_id="M1")) == b"ITER"
        assert pc.calls == [["M1", "A1"]]
        assert all_container.lookups == ["A1"]  # fell back to iteration

    def test_falls_back_when_no_record_pair(self, monkeypatch):
        monkeypatch.setattr("app.icloud_source.PhotoAsset", FakePhotoAsset)
        pc = FakePrivateClient([FakeRecord("CPLMaster")])  # asset record missing
        photo = FakePhoto(b"ITER")
        all_container = FakeAllContainer({"A1": photo})
        source = PyiCloudAssetSource(FakeDirectApi(pc, all_container))

        assert source.download(_asset(asset_id="A1", master_id="M1")) == b"ITER"
        assert all_container.lookups == ["A1"]

    def test_raises_when_both_paths_miss(self, monkeypatch):
        monkeypatch.setattr("app.icloud_source.PhotoAsset", FakePhotoAsset)
        pc = FakePrivateClient([])
        source = PyiCloudAssetSource(FakeDirectApi(pc, FakeAllContainer({})))

        with pytest.raises(AssetNotFoundError):
            source.download(_asset(asset_id="A1", master_id="M1"))

    def test_direct_resolution_cached(self, monkeypatch):
        monkeypatch.setattr("app.icloud_source.PhotoAsset", FakePhotoAsset)
        pc = FakePrivateClient(_pair())
        source = PyiCloudAssetSource(FakeDirectApi(pc, FakeAllContainer({})))
        asset = _asset(asset_id="A1", master_id="M1")

        source.download(asset)
        source.delete(asset)

        assert pc.calls == [["M1", "A1"]]  # single lookup for both ops
        assert FakePhotoAsset.last.deleted is True
