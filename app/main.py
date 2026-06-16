"""
End-to-end pipeline runner: scan -> analyse -> recommend -> offload.

Run with ``uv run python -m app.main``. With ``DRY_RUN=true`` (the default) the
offload step only logs what *would* move. With ``DRY_RUN=false`` the
auto-offload bucket is offloaded for real via the live pyicloud
:class:`~app.icloud_source.PyiCloudAssetSource` — downloaded to
``SMB_MOUNT_PATH/YYYY/MM/`` and then soft-deleted from iCloud. ``OFFLOAD_MAX_ITEMS``
caps how many move in a single run. The review bucket still awaits the Telegram
approval flow (Phase 4).
"""

import logging

from app import actions
from app.actions import OffloadStatus
from app.analyser import score_assets
from app.config import config
from app.icloud_source import PyiCloudAssetSource
from app.index import AssetIndex
from app.recommender import recommend
from app.scanner import ICloudScanner, _parse_window

logger = logging.getLogger(__name__)


def run() -> None:
    scanner = ICloudScanner()

    with AssetIndex() as index:
        if config.scan_from_index:
            since, until = _parse_window(config.scan_since, config.scan_until)
            assets = index.load_assets(
                since=since.isoformat() if since else None,
                until=until.isoformat() if until else None,
            )
            logger.info(
                "Index-only mode: loaded %d assets from the index (no iCloud scan)",
                len(assets),
            )
            if not assets:
                logger.warning(
                    "Index is empty for this query — run a full scan first "
                    "(SCAN_FROM_INDEX=false) to populate it."
                )
        else:
            cached_assets = index.get_cached_assets()
            assets = scanner.scan(cached_assets=cached_assets or None)

        scored = score_assets(assets)
        recommendations = recommend(scored)
        logger.info("Recommendations:\n%s", recommendations.summary())

        # In index-only mode we didn't actually see iCloud, so don't bump the
        # index's last_seen_at / overwrite it from a non-scan. mark_offloaded
        # below still records genuine state changes.
        if not config.scan_from_index:
            upserted = index.upsert_scored(scored)
            logger.info("Asset index: upserted %d assets", upserted)

        source = None
        if not config.dry_run:
            # A live offload needs an authenticated session even in index-only
            # mode (scan() would otherwise have logged us in already).
            scanner.ensure_authenticated()
            source = PyiCloudAssetSource(scanner.api)

        def _record_offload(result) -> None:
            # Called the moment each asset is offloaded, so an interrupted batch
            # leaves the index accurate rather than losing the whole run's work.
            index.mark_offloaded(
                result.asset_id,
                result.destination,
                storage_tier=config.storage_tier,
            )

        results = actions.offload(
            recommendations.auto_offload,
            dry_run=config.dry_run,
            source=source,
            max_items=config.offload_max_items,
            on_offloaded=_record_offload,
        )
        offloaded = [r for r in results if r.status == OffloadStatus.OFFLOADED]
        failed = [r for r in results if r.status == OffloadStatus.FAILED]

        verb = "would be moved (dry-run)" if config.dry_run else "moved"
        logger.info("Auto-offload: %d assets %s", len(results), verb)
        if offloaded:
            logger.info("Recorded %d confirmed offloads in the index", len(offloaded))
        if failed:
            logger.warning("%d offloads FAILED (iCloud copy left intact):", len(failed))
            for result in failed:
                logger.warning("  %s -> %s", result.filename, result.detail)
        logger.info(
            "Review bucket holds %d assets awaiting approval (Telegram, not yet built)",
            len(recommendations.review),
        )
        if recommendations.review_deferred:
            logger.info(
                "Deferred %d further review-eligible assets to a later run (cap=%d)",
                len(recommendations.review_deferred),
                config.review_max_items,
            )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run()


if __name__ == "__main__":
    main()
