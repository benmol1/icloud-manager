from datetime import datetime, timedelta, timezone

import pytest

from app.analyser import ScoreBreakdown, _age_score, _find_duplicate_ids, _size_score, _source_score, score_assets
from app.models import Asset, MediaType, Source
from app.recommender import Recommendations, _classify, _is_non_controversial, recommend


def _make_asset(
    asset_id: str = "abc123",
    filename: str = "photo.jpg",
    size_mb: float = 5.0,
    age_days: int = 400,
    media_type: MediaType = MediaType.IMAGE,
    is_favorite: bool = False,
    source: Source = Source.PHOTOS,
    albums: list[str] | None = None,
) -> Asset:
    created = datetime.now(tz=timezone.utc) - timedelta(days=age_days)
    return Asset(
        asset_id=asset_id,
        filename=filename,
        size=int(size_mb * 1024 * 1024),
        created=created,
        media_type=media_type,
        is_favorite=is_favorite,
        source=source,
        albums=albums or [],
    )


# ------------------------------------------------------------------
# Age scoring
# ------------------------------------------------------------------

class TestAgeScore:
    def _score(self, age_days: int) -> float:
        asset = _make_asset(age_days=age_days)
        return _age_score(asset, datetime.now(tz=timezone.utc))

    def test_very_recent_scores_zero(self):
        assert self._score(10) == 0.0

    def test_30_days_scores_partial(self):
        assert 0 < self._score(30) < 35

    def test_over_two_years_scores_max(self):
        from app.config import config
        assert self._score(800) == float(config.weight_age)

    def test_age_increases_monotonically(self):
        scores = [self._score(d) for d in [0, 60, 200, 400, 800]]
        assert scores == sorted(scores)


# ------------------------------------------------------------------
# Size scoring
# ------------------------------------------------------------------

class TestSizeScore:
    def test_zero_size_scores_zero(self):
        asset = _make_asset(size_mb=0)
        assert _size_score(asset) == 0.0

    def test_large_file_scores_max(self):
        from app.config import config
        asset = _make_asset(size_mb=config.large_file_mb * 2)
        assert _size_score(asset) == float(config.weight_size)

    def test_score_caps_at_weight(self):
        from app.config import config
        asset = _make_asset(size_mb=9999)
        assert _size_score(asset) == float(config.weight_size)


# ------------------------------------------------------------------
# Source scoring
# ------------------------------------------------------------------

class TestSourceScore:
    def test_whatsapp_scores_full_weight(self):
        from app.config import config
        asset = _make_asset(source=Source.WHATSAPP)
        assert _source_score(asset) == float(config.weight_source)

    def test_photos_scores_zero(self):
        asset = _make_asset(source=Source.PHOTOS)
        assert _source_score(asset) == 0.0

    def test_unknown_scores_partial(self):
        asset = _make_asset(source=Source.UNKNOWN)
        score = _source_score(asset)
        assert 0 < score < 30


# ------------------------------------------------------------------
# Duplicate detection
# ------------------------------------------------------------------

class TestFindDuplicates:
    def test_no_duplicates_in_unique_assets(self):
        assets = [_make_asset(f"id{i}", size_mb=i + 1, age_days=i + 1) for i in range(5)]
        assert _find_duplicate_ids(assets) == set()

    def test_same_size_and_time_flagged(self):
        now = datetime.now(tz=timezone.utc)
        a1 = Asset("id1", "a.jpg", 5_000_000, now, MediaType.IMAGE, False, Source.PHOTOS)
        a2 = Asset("id2", "b.jpg", 5_000_000, now, MediaType.IMAGE, False, Source.PHOTOS)
        dupes = _find_duplicate_ids([a1, a2])
        assert dupes == {"id1", "id2"}

    def test_different_sizes_not_flagged(self):
        now = datetime.now(tz=timezone.utc)
        a1 = Asset("id1", "a.jpg", 5_000_000, now, MediaType.IMAGE, False, Source.PHOTOS)
        a2 = Asset("id2", "b.jpg", 6_000_000, now, MediaType.IMAGE, False, Source.PHOTOS)
        assert _find_duplicate_ids([a1, a2]) == set()


# ------------------------------------------------------------------
# Recommender
# ------------------------------------------------------------------

class TestRecommender:
    def test_favorite_never_auto_offloaded(self):
        asset = _make_asset(source=Source.WHATSAPP, is_favorite=True, age_days=800)
        scored = score_assets([asset])
        result = recommend(scored)
        assert len(result.auto_offload) == 0

    def test_old_whatsapp_non_favorite_auto_offloaded(self):
        asset = _make_asset(source=Source.WHATSAPP, is_favorite=False, age_days=800)
        scored = score_assets([asset])
        result = recommend(scored)
        assert len(result.auto_offload) == 1

    def test_recent_whatsapp_not_auto_offloaded(self):
        asset = _make_asset(source=Source.WHATSAPP, is_favorite=False, age_days=10)
        scored = score_assets([asset])
        result = recommend(scored)
        assert len(result.auto_offload) == 0

    def test_photos_source_not_auto_offloaded(self):
        asset = _make_asset(source=Source.PHOTOS, is_favorite=False, age_days=800, size_mb=200)
        scored = score_assets([asset])
        result = recommend(scored)
        assert len(result.auto_offload) == 0

    def test_summary_contains_counts(self):
        assets = [
            _make_asset("a", source=Source.WHATSAPP, age_days=800),
            _make_asset("b", source=Source.PHOTOS, age_days=5),
        ]
        result = recommend(score_assets(assets))
        summary = result.summary()
        assert "Auto-offload" in summary
        assert "Needs review" in summary
