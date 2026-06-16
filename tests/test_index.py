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

    def test_rich_metadata_persisted(self, index):
        asset = Asset(
            asset_id="r1",
            filename="IMG.HEIC",
            size=1000,
            created=datetime(2020, 6, 1, tzinfo=timezone.utc),
            media_type=MediaType.IMAGE,
            is_favorite=False,
            source=Source.PHOTOS,
            latitude=51.5,
            longitude=-0.1,
            caption="Beach",
            fingerprint="FP1",
            change_tag="T1",
            width=4032,
            height=3024,
            added=datetime(2020, 5, 1, tzinfo=timezone.utc),
        )
        index.upsert_scored([ScoredAsset(asset=asset, breakdown=ScoreBreakdown())])
        row = index.get("r1")
        assert row["latitude"] == 51.5
        assert row["caption"] == "Beach"
        assert row["fingerprint"] == "FP1"
        assert row["change_tag"] == "T1"
        assert (row["width"], row["height"]) == (4032, 3024)
        assert row["added_at"].startswith("2020-05-01")

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

    def test_mark_offloaded_records_storage_tier(self, index):
        index.upsert_scored([_scored()])
        index.mark_offloaded("abc123", "D:/icloud-photos/x.jpg", storage_tier="local")
        assert index.get("abc123")["storage_tier"] == "local"

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

    def test_groups_offloaded_by_tier(self, index):
        index.upsert_scored(
            [_scored(asset_id="a"), _scored(asset_id="b"), _scored(asset_id="c")]
        )
        index.mark_offloaded("a", "D:/x.jpg", storage_tier="local")
        index.mark_offloaded("b", "D:/y.jpg", storage_tier="local")
        index.mark_offloaded("c", "//pi/share/z.jpg", storage_tier="network")
        by_tier = index.stats()["by_tier"]
        assert by_tier["local"]["files"] == 2
        assert by_tier["network"]["files"] == 1

    def test_offloaded_without_tier_is_unknown(self, index):
        index.upsert_scored([_scored(asset_id="a")])
        index.mark_offloaded("a", "/mnt/x.jpg")  # no tier given
        assert index.stats()["by_tier"]["unknown"]["files"] == 1


class TestTierSearch:
    def test_filters_by_tier(self, index):
        index.upsert_scored([_scored(asset_id="a"), _scored(asset_id="b")])
        index.mark_offloaded("a", "D:/x.jpg", storage_tier="local")
        index.mark_offloaded("b", "//pi/z.jpg", storage_tier="network")
        rows = index.search(storage_tier="network")
        assert [r["asset_id"] for r in rows] == ["b"]


class TestGetCachedAssets:
    def _asset(self, asset_id: str, change_tag: str | None) -> Asset:
        return Asset(
            asset_id=asset_id,
            filename=f"{asset_id}.JPG",
            size=5_000_000,
            created=datetime(2023, 7, 9, tzinfo=timezone.utc),
            media_type=MediaType.IMAGE,
            is_favorite=False,
            source=Source.PHOTOS,
            change_tag=change_tag,
        )

    def test_empty_index_returns_empty_dict(self, index):
        assert index.get_cached_assets() == {}

    def test_returns_assets_with_change_tag(self, index):
        asset = self._asset("a1", "tag_v1")
        index.upsert_scored([ScoredAsset(asset=asset, breakdown=ScoreBreakdown())])
        result = index.get_cached_assets()
        assert "a1" in result
        assert result["a1"].change_tag == "tag_v1"
        assert result["a1"].filename == "a1.JPG"

    def test_excludes_assets_without_change_tag(self, index):
        asset = self._asset("a2", None)
        index.upsert_scored([ScoredAsset(asset=asset, breakdown=ScoreBreakdown())])
        assert "a2" not in index.get_cached_assets()

    def test_excludes_offloaded_assets(self, index):
        asset = self._asset("a3", "tag_v1")
        index.upsert_scored([ScoredAsset(asset=asset, breakdown=ScoreBreakdown())])
        index.mark_offloaded("a3", "/mnt/storage/x.jpg")
        assert "a3" not in index.get_cached_assets()

    def test_roundtrips_rich_metadata(self, index):
        asset = Asset(
            asset_id="r1",
            filename="IMG.HEIC",
            size=10_000_000,
            created=datetime(2021, 3, 15, 12, 0, tzinfo=timezone.utc),
            media_type=MediaType.IMAGE,
            is_favorite=True,
            source=Source.WHATSAPP,
            albums=["WhatsApp"],
            latitude=51.5,
            longitude=-0.1,
            fingerprint="FP1",
            change_tag="CT1",
            width=4032,
            height=3024,
        )
        index.upsert_scored([ScoredAsset(asset=asset, breakdown=ScoreBreakdown())])
        result = index.get_cached_assets()
        a = result["r1"]
        assert a.is_favorite is True
        assert a.source == Source.WHATSAPP
        assert a.albums == ["WhatsApp"]
        assert a.latitude == 51.5
        assert a.fingerprint == "FP1"
        assert a.change_tag == "CT1"
        assert a.created == datetime(2021, 3, 15, 12, 0, tzinfo=timezone.utc)


class TestLoadAssets:
    def test_returns_assets_for_index_only_mode(self, index):
        index.upsert_scored([_scored(asset_id="a"), _scored(asset_id="b")])
        assets = index.load_assets()
        assert {a.asset_id for a in assets} == {"a", "b"}

    def test_excludes_offloaded_by_default(self, index):
        index.upsert_scored([_scored(asset_id="a"), _scored(asset_id="b")])
        index.mark_offloaded("a", "/mnt/x.jpg")
        assets = index.load_assets()
        assert {a.asset_id for a in assets} == {"b"}

    def test_status_none_returns_all(self, index):
        index.upsert_scored([_scored(asset_id="a"), _scored(asset_id="b")])
        index.mark_offloaded("a", "/mnt/x.jpg")
        assert len(index.load_assets(status=None)) == 2

    def test_captured_at_window_inclusive(self, index):
        index.upsert_scored(
            [
                _scored(asset_id="old", created=datetime(2019, 6, 1, tzinfo=timezone.utc)),
                _scored(asset_id="in", created=datetime(2020, 6, 1, tzinfo=timezone.utc)),
                _scored(asset_id="new", created=datetime(2021, 6, 1, tzinfo=timezone.utc)),
            ]
        )
        assets = index.load_assets(
            since="2020-01-01T00:00:00+00:00",
            until="2020-12-31T23:59:59+00:00",
        )
        assert {a.asset_id for a in assets} == {"in"}

    def test_empty_index_returns_empty_list(self, index):
        assert index.load_assets() == []


class TestMigration:
    def test_adds_storage_tier_to_legacy_db(self, tmp_path):
        import sqlite3

        # Build a DB whose assets table lacks storage_tier, like the shipped one.
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE assets (
                asset_id TEXT PRIMARY KEY, filename TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, media_type TEXT NOT NULL,
                source TEXT NOT NULL, is_favorite INTEGER NOT NULL DEFAULT 0,
                fingerprint TEXT,
                captured_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'in_icloud',
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()

        # Opening through AssetIndex should migrate it in place.
        with AssetIndex(db) as idx:
            cols = {r["name"] for r in idx._conn.execute("PRAGMA table_info(assets)")}
            assert "storage_tier" in cols
