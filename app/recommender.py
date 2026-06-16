from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.analyser import ScoredAsset
from app.config import config
from app.models import Source


@dataclass
class Recommendations:
    auto_offload: list[ScoredAsset] = field(default_factory=list)
    review: list[ScoredAsset] = field(default_factory=list)
    # Review-eligible assets held back this run by the review_max_items cap.
    # They resurface in later runs as the surfaced items get actioned.
    review_deferred: list[ScoredAsset] = field(default_factory=list)
    keep: list[ScoredAsset] = field(default_factory=list)

    @property
    def auto_offload_mb(self) -> float:
        return sum(a.asset.size_mb for a in self.auto_offload)

    @property
    def review_mb(self) -> float:
        return sum(a.asset.size_mb for a in self.review)

    @property
    def review_deferred_mb(self) -> float:
        return sum(a.asset.size_mb for a in self.review_deferred)

    def summary(self) -> str:
        lines = [
            f"Auto-offload: {len(self.auto_offload)} files ({self.auto_offload_mb:.1f} MB)",
            f"Needs review: {len(self.review)} files ({self.review_mb:.1f} MB)",
        ]
        if self.review_deferred:
            lines.append(
                f"  (+{len(self.review_deferred)} more eligible, deferred — "
                f"{self.review_deferred_mb:.1f} MB)"
            )
        lines.append(f"Keep:         {len(self.keep)} files")
        return "\n".join(lines)


def recommend(scored: list[ScoredAsset]) -> Recommendations:
    result = Recommendations()
    for item in scored:
        bucket = _classify(item)
        getattr(result, bucket).append(item)
    _prioritise_review(result)
    return result


def _prioritise_review(result: Recommendations) -> None:
    """Order the review bucket by reclaimable size and cap it per run.

    Review is ranked by reclaimable size (largest first; tie-break on score) so
    the biggest space wins are surfaced first. If config.review_max_items is set
    and exceeded, the overflow moves to review_deferred — it isn't lost, it just
    waits for a later run, keeping the per-run Telegram approval flow manageable.
    """
    result.review.sort(key=lambda a: (a.asset.size_mb, a.score), reverse=True)
    cap = config.review_max_items
    if cap and len(result.review) > cap:
        result.review_deferred = result.review[cap:]
        result.review = result.review[:cap]


def _classify(item: ScoredAsset) -> str:
    # Favourites never go to auto-offload, but can be surfaced for review
    # if they're very large or very old — let the user decide.
    if item.asset.is_favorite:
        if item.score >= config.review_threshold:
            return "review"
        return "keep"

    if _is_non_controversial(item) and item.score >= config.auto_offload_threshold:
        return "auto_offload"

    if item.score >= config.review_threshold:
        return "review"

    return "keep"


def _is_non_controversial(item: ScoredAsset) -> bool:
    """
    Rules for assets that are safe to offload without asking.

    All must be true:
    - Not a favourite
    - Source is WhatsApp (nursery chats etc. are the main culprit)
    - Asset is at least config.min_age_days old
    """
    asset = item.asset

    if asset.is_favorite:
        return False
    if asset.source != Source.WHATSAPP:
        return False

    age_days = (datetime.now(tz=timezone.utc) - asset.created).days
    if age_days < config.min_age_days:
        return False

    return True
