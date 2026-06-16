from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import config
from app.models import Asset, Source


@dataclass
class ScoreBreakdown:
    age: float = 0.0
    size: float = 0.0
    source: float = 0.0
    duplicate: float = 0.0
    favorite_penalty: float = 0.0

    @property
    def total(self) -> float:
        return max(0.0, self.age + self.size + self.source + self.duplicate - self.favorite_penalty)


@dataclass
class ScoredAsset:
    asset: Asset
    breakdown: ScoreBreakdown
    is_duplicate: bool = False

    @property
    def score(self) -> float:
        return self.breakdown.total


def score_assets(assets: list[Asset]) -> list[ScoredAsset]:
    duplicate_ids = _find_duplicate_ids(assets)
    now = datetime.now(tz=timezone.utc)
    return [_score_one(asset, now, duplicate_ids) for asset in assets]


# ------------------------------------------------------------------
# Internals
# ------------------------------------------------------------------

def _score_one(
    asset: Asset,
    now: datetime,
    duplicate_ids: set[str],
) -> ScoredAsset:
    is_duplicate = asset.asset_id in duplicate_ids
    breakdown = ScoreBreakdown(
        age=_age_score(asset, now),
        size=_size_score(asset),
        source=_source_score(asset),
        duplicate=config.weight_duplicate if is_duplicate else 0.0,
        favorite_penalty=config.favorite_score_penalty if asset.is_favorite else 0.0,
    )
    return ScoredAsset(asset=asset, breakdown=breakdown, is_duplicate=is_duplicate)


def _age_score(asset: Asset, now: datetime) -> float:
    age_days = (now - asset.created).days
    if age_days < 30:
        return 0.0
    if age_days < 180:
        return config.weight_age * 0.3
    if age_days < 365:
        return config.weight_age * 0.6
    if age_days < 730:
        return config.weight_age * 0.85
    return float(config.weight_age)


def _size_score(asset: Asset) -> float:
    ratio = min(asset.size_mb / config.large_file_mb, 1.0)
    return ratio * config.weight_size


def _source_score(asset: Asset) -> float:
    if asset.source == Source.WHATSAPP:
        return float(config.weight_source)
    if asset.source == Source.UNKNOWN:
        return config.weight_source * 0.33
    return 0.0


def _find_duplicate_ids(assets: list[Asset]) -> set[str]:
    """Identify content-duplicate assets.

    The primary signal is Apple's ``resOriginalFingerprint`` content hash
    (``Asset.fingerprint``): assets sharing a fingerprint are true,
    byte-for-byte duplicates of the same original. For assets that lack a
    fingerprint — older or app-saved media iCloud doesn't hash — we fall back
    to the weaker ``(size, creation-minute)`` heuristic so they're not silently
    dropped from dedup. Fingerprinted and non-fingerprinted assets never
    collide because their keys are namespaced.
    """
    seen: dict[tuple, str] = {}
    duplicates: set[str] = set()
    for asset in assets:
        if asset.fingerprint:
            key: tuple = ("fingerprint", asset.fingerprint)
        else:
            key = ("size-minute", asset.size, asset.created.replace(second=0, microsecond=0))
        if key in seen:
            duplicates.add(asset.asset_id)
            duplicates.add(seen[key])
        else:
            seen[key] = asset.asset_id
    return duplicates
