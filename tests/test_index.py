from datetime import datetime, timezone

import pytest

from app.analyser import ScoreBreakdown, ScoredAsset
from app.index import AssetIndex
from app.models import Asset, MediaType, Source


def _scored(
    asset_id: str = "abc123",
    filename: str = "IMG_2351.JPG",
    created: datetime | None = None,
    size_mb: float = 5.0,
    media_type: MediaType = MediaType.IMAGE,
    source: Source = Source.PHOTOS,
    is_favorite: bool = False,
    is_duplicate: bool = False,
    score: float = 0.0,
    albums: list[str] | None = None,
) -> ScoredAsset:
    created = created or datetime(2023, 7, 9, 14, 30, tzinfo=timezone.utc)
    asset = Asset(
        asset_id=asset_id,
        filename=filename,
        size=int(size_mb * 1024 * 1024),
        created=created,
        media_type=media_type,
        is_favorite=is_favorite,
        source=source,
        albums=albums or [],
    )
    breakdown = ScoreBreakdown(age=score)  # total == score when others are 0
    return ScoredAsset(asset=asset, breakdown=breakdown, is_duplicate=is_duplicate)


@pytest.fixture
def index(tmp_path):
    with AssetIndex(tmp_path / "test.db") as idx:
        yield idx


class TestUpsert:
    def test_insert_and_get(self, index):
        index.upsert_scored([_scored(albums=["WhatsApp"])])
        row = index.get("abc123")
        assert row["filename"] == "IMG_2351.JPG"
        assert row["status"] == "in_icloud"
        assert row["source"] == "photos"
        assert row["albums"] == '["WhatsApp"]'

    def test_count_returned(self, index):
        n = index.upsert_scored([_scored(asset_id="a"), _scored(asset_id="b")])
        assert n == 2
        assert index.stats()["total"] == 2

    def test_empty_upsert_is_noop(self, index):
        assert index.upsert_scored([]) == 0

    def test_rescan_refreshes_but_preserves_first_seen(self, index):
        index.upsert_scored([_scored(score=10.0)], seen_at="2026-01-01T00:00:00+00:00")
        index.upsert_scored([_scored(score=42.0)], seen_at="2026-02-01T00:00:00+00:00")
        row = index.get("abc123")
        assert row["score"] == 42.0
        assert row["first_seen_at"] == "2026-01-01T00:00:00+00:00"
        assert row["last_seen_at"] == "2026-02-01T00:00:00+00:00"


class TestOffloadTransition:
    def test_mark_offloaded_sets_status_and_path(self, index):
        index.upsert_scored([_scored()])
        index.mark_offloaded("abc123", "/mnt/storage/2023/07/IMG_2351.JPG")
        row = index.get("abc123")
        assert row["status"] == "offloaded"
        assert row["local_path"] == "/mnt/storage/2023/07/IMG_2351.JPG"
        assert row["offloaded_at"] is not None

    def test_rescan_does_not_clobber_offload(self, index):
        index.upsert_scored([_scored()])
        index.mark_offloaded("abc123", "/mnt/storage/x.jpg")
        # A later scan still sees the asset metadata-wise; must keep offload state.
        index.upsert_scored([_scored(score=99.0)])
        row = index.get("abc123")
        assert row["status"] == "offloaded"
        assert row["local_path"] == "/mnt/storage/x.jpg"
        assert row["score"] == 99.0


class TestSearch:
    def test_filters_by_source_and_status(self, index):
        index.upsert_scored(
            [
                _scored(asset_id="w", source=Source.WHATSAPP),
                _scored(asset_id="p", source=Source.PHOTOS),
            ]
        )
        rows = index.search(source="whatsapp")
        assert [r["asset_id"] for r in rows] == ["w"]

    def test_filename_like(self, index):
        index.upsert_scored(
            [
                _scored(asset_id="a", filename="IMG_2351.JPG"),
                _scored(asset_id="b", filename="movie.mp4"),
            ]
        )
        rows = index.search(filename_like="movie")
        assert [r["asset_id"] for r in rows] == ["b"]

    def test_date_range(self, index):
        index.upsert_scored(
            [
                _scored(asset_id="old", created=datetime(2020, 1, 1, tzinfo=timezone.utc)),
                _scored(asset_id="new", created=datetime(2025, 1, 1, tzinfo=timezone.utc)),
            ]
        )
        rows = index.search(since="2024-01-01")
        assert [r["asset_id"] for r in rows] == ["new"]


class TestStats:
    def test_groups_by_status(self, index):
        index.upsert_scored([_scored(asset_id="a"), _scored(asset_id="b")])
        index.mark_offloaded("a", "/mnt/x.jpg")
        stats = index.stats()
        assert stats["total"] == 2
        assert stats["by_status"]["offloaded"]["files"] == 1
        assert stats["by_status"]["in_icloud"]["files"] == 1
