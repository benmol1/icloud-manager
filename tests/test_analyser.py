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

    def test_same_fingerprint_flagged(self):
        # Different size and capture time, but identical content fingerprint —
        # the old size/minute heuristic would miss these; fingerprint catches them.
        now = datetime.now(tz=timezone.utc)
        a1 = Asset("id1", "a.jpg", 5_000_000, now, MediaType.IMAGE, False, Source.PHOTOS, fingerprint="FP1")
        a2 = Asset(
            "id2", "b.jpg", 6_000_000, now - timedelta(hours=3),
            MediaType.IMAGE, False, Source.PHOTOS, fingerprint="FP1",
        )
        assert _find_duplicate_ids([a1, a2]) == {"id1", "id2"}

    def test_different_fingerprints_not_flagged(self):
        # Same size and capture minute, but distinct fingerprints — genuinely
        # different content that the old heuristic would have wrongly merged.
        now = datetime.now(tz=timezone.utc)
        a1 = Asset("id1", "a.jpg", 5_000_000, now, MediaType.IMAGE, False, Source.PHOTOS, fingerprint="FP1")
        a2 = Asset("id2", "b.jpg", 5_000_000, now, MediaType.IMAGE, False, Source.PHOTOS, fingerprint="FP2")
        assert _find_duplicate_ids([a1, a2]) == set()

    def test_fingerprint_and_heuristic_dont_collide(self):
        # A fingerprinted asset and a fingerprint-less one must not be paired
        # even if their fallback keys would otherwise look similar.
        now = datetime.now(tz=timezone.utc)
        a1 = Asset("id1", "a.jpg", 5_000_000, now, MediaType.IMAGE, False, Source.PHOTOS, fingerprint="FP1")
        a2 = Asset("id2", "b.jpg", 5_000_000, now, MediaType.IMAGE, False, Source.PHOTOS)
        assert _find_duplicate_ids([a1, a2]) == set()

    def test_missing_fingerprint_falls_back_to_heuristic(self):
        # No fingerprints available — still detect via size + creation minute.
        now = datetime.now(tz=timezone.utc)
        a1 = Asset("id1", "a.jpg", 5_000_000, now, MediaType.IMAGE, False, Source.PHOTOS)
        a2 = Asset("id2", "b.jpg", 5_000_000, now, MediaType.IMAGE, False, Source.PHOTOS)
        assert _find_duplicate_ids([a1, a2]) == {"id1", "id2"}


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

    def test_whatsapp_below_min_age_not_auto_offloaded(self):
        # Two duplicate large WhatsApp files just under min_age. Their score
        # clears the auto-offload threshold, so only the age rule keeps them
        # out of auto-offload — they fall to review instead.
        from app.config import config
        created = datetime.now(tz=timezone.utc) - timedelta(days=config.min_age_days - 1)
        big = int(200 * 1024 * 1024)
        a1 = Asset("d1", "IMG-1.jpg", big, created, MediaType.IMAGE, False, Source.WHATSAPP)
        a2 = Asset("d2", "IMG-2.jpg", big, created, MediaType.IMAGE, False, Source.WHATSAPP)
        result = recommend(score_assets([a1, a2]))
        assert len(result.auto_offload) == 0
        assert len(result.review) == 2

    def test_whatsapp_at_min_age_auto_offloaded(self):
        from app.config import config
        asset = _make_asset(
            source=Source.WHATSAPP,
            is_favorite=False,
            age_days=config.min_age_days + 1,
            size_mb=200,
        )
        result = recommend(score_assets([asset]))
        assert len(result.auto_offload) == 1

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


# ------------------------------------------------------------------
# Review bucket prioritisation & capping
# ------------------------------------------------------------------

class TestReviewPrioritisation:
    def _review_assets(self, n: int, base_size_mb: float = 1.0) -> list[Asset]:
        # UNKNOWN-source, old, modest-size assets reliably land in review:
        # score = age(35) + unknown-source(~10) + small size, which clears
        # review_threshold (40) but not auto_offload_threshold (65), and they
        # aren't WhatsApp so they're never non-controversial.
        return [
            _make_asset(f"r{i}", source=Source.UNKNOWN, age_days=800, size_mb=base_size_mb + i)
            for i in range(n)
        ]

    def test_review_sorted_by_size_desc(self):
        result = recommend(score_assets(self._review_assets(5)))
        sizes = [a.asset.size_mb for a in result.review]
        assert sizes == sorted(sizes, reverse=True)

    def test_review_capped_to_max(self, monkeypatch):
        from app.config import config
        monkeypatch.setattr(config, "review_max_items", 3)
        result = recommend(score_assets(self._review_assets(10)))
        assert len(result.review) == 3
        assert len(result.review_deferred) == 7

    def test_deferred_holds_the_smallest(self, monkeypatch):
        from app.config import config
        monkeypatch.setattr(config, "review_max_items", 3)
        result = recommend(score_assets(self._review_assets(10)))
        smallest_surfaced = min(a.asset.size_mb for a in result.review)
        largest_deferred = max(a.asset.size_mb for a in result.review_deferred)
        assert largest_deferred <= smallest_surfaced

    def test_cap_zero_means_unlimited(self, monkeypatch):
        from app.config import config
        monkeypatch.setattr(config, "review_max_items", 0)
        result = recommend(score_assets(self._review_assets(10)))
        assert len(result.review) == 10
        assert result.review_deferred == []

    def test_summary_mentions_deferred(self, monkeypatch):
        from app.config import config
        monkeypatch.setattr(config, "review_max_items", 2)
        result = recommend(score_assets(self._review_assets(6)))
        summary = result.summary()
        assert "deferred" in summary
        assert "4 more" in summary
