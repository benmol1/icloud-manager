"""
End-to-end pipeline runner: scan -> analyse -> recommend -> offload.

Run with ``uv run python -m app.main``. Offload currently runs in **dry-run**
only — it logs what would be moved without downloading or deleting anything.
Live offload arrives once the iCloud `AssetSource` (and Telegram approvals for
the review bucket) are wired up.
"""

import logging

from app import actions
from app.analyser import score_assets
from app.config import config
from app.recommender import recommend
from app.scanner import ICloudScanner

logger = logging.getLogger(__name__)


def run() -> None:
    scanner = ICloudScanner()
    assets = scanner.scan()

    scored = score_assets(assets)
    recommendations = recommend(scored)
    logger.info("Recommendations:\n%s", recommendations.summary())

    if not config.dry_run:
        logger.warning(
            "DRY_RUN is false, but live iCloud offload is not wired up yet — "
            "running the offload step in dry-run mode anyway. No files will be "
            "downloaded or deleted."
        )

    results = actions.offload(recommendations.auto_offload, dry_run=True)
    logger.info("Auto-offload (dry-run): %d assets would be moved", len(results))
    logger.info(
        "Review bucket holds %d assets awaiting approval (Telegram, not yet built)",
        len(recommendations.review),
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run()


if __name__ == "__main__":
    main()
