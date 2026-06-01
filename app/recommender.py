from dataclasses import dataclass, field

from app.analyser import ScoredAsset
from app.config import config
from app.models import Source


@dataclass
class Recommendations:
    auto_offload: list[ScoredAsset] = field(default_factory=list)
    review: list[ScoredAsset] = field(default_factory=list)
    keep: list[ScoredAsset] = field(default_factory=list)

    @property
    def auto_offload_mb(self) -> float:
        return sum(a.asset.size_mb for a in self.auto_offload)

    @property
    def review_mb(self) -> float:
        return sum(a.asset.size_mb for a in self.review)

    def summary(self) -> str:
        lines = [
            f"Auto-offload: {len(self.auto_offload)} files ({self.auto_offload_mb:.1f} MB)",
            f"Needs review: {len(self.review)} files ({self.review_mb:.1f} MB)",
            f"Keep:         {len(self.keep)} files",
        ]
        return "\n".join(lines)


def recommend(scored: list[ScoredAsset]) -> Recommendations:
    result = Recommendations()
    for item in scored:
        bucket = _classify(item)
        getattr(result, bucket).append(item)
    return result


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

    !! REVIEW WITH EMMA before finalising these rules !!
    See TODO.md — she may have strong opinions about what's safe to auto-migrate.

    Current rules (all must be true):
    - Not a favourite
    - Source is WhatsApp (nursery chats etc. are the main culprit)
    - Asset is older than min_age_days
    """
    asset = item.asset
    age_days = item.breakdown.age  # non-zero only if old enough (see _age_score)

    if asset.is_favorite:
        return False
    if asset.source != Source.WHATSAPP:
        return False
    if age_days == 0:
        # _age_score returns 0 for assets < 30 days old
        return False

    return True
