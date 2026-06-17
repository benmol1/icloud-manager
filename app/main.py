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
import sys
from datetime import datetime, timezone
from pathlib import Path

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
                "Index-only mode: using the cached index, no live iCloud scan "
                "(index last refreshed %s)",
                _describe_age(index.last_refreshed_at()),
            )
            logger.info("Loaded %d assets from the index", len(assets))
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

        cap = config.offload_max_items
        cap_desc = f"{cap} (OFFLOAD_MAX_ITEMS)" if cap else "unlimited (OFFLOAD_MAX_ITEMS=0)"
        logger.info(
            "Offload settings: mode=%s, cap=%s, storage_tier=%s",
            "dry-run" if config.dry_run else "LIVE",
            cap_desc,
            config.storage_tier,
        )

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
        already = [r for r in results if r.status == OffloadStatus.ALREADY_ARCHIVED]
        failed = [r for r in results if r.status == OffloadStatus.FAILED]

        verb = "would be moved (dry-run)" if config.dry_run else "moved"
        logger.info("Auto-offload: %d assets %s", len(results), verb)
        if offloaded:
            logger.info("Recorded %d confirmed offloads in the index", len(offloaded))
        if already:
            logger.info(
                "Skipped %d assets already present in the archive (identical bytes) "
                "— removed from iCloud, no duplicate written",
                len(already),
            )
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


def _describe_age(iso_ts: str | None) -> str:
    """Render an index ``last_seen_at`` timestamp as ``<iso> (<n><unit> ago)``."""
    if not iso_ts:
        return "never (index empty)"
    try:
        when = datetime.fromisoformat(iso_ts)
    except ValueError:
        return iso_ts
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = max((datetime.now(tz=timezone.utc) - when).total_seconds(), 0)
    if seconds < 90:
        ago = f"{int(seconds)}s ago"
    elif seconds < 5400:
        ago = f"{int(seconds // 60)}m ago"
    elif seconds < 172800:
        ago = f"{int(seconds // 3600)}h ago"
    else:
        ago = f"{int(seconds // 86400)}d ago"
    return f"{iso_ts} ({ago})"


def _configure_logging() -> Path:
    """Log to the console *and* a timestamped UTF-8 file under ``logs/``.

    The filename is ``<live|dryrun>_YYYYMMDD_HHMMSS.log`` so each run is kept
    and it's obvious at a glance whether files actually moved. Returns the path.
    """
    # Force UTF-8 on the console streams too, so non-ASCII characters in messages
    # (—, ≈, …) survive redirection on Windows, whose console/redirect default is
    # the legacy cp1252 code page and would otherwise mangle them.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    prefix = "dryrun" if config.dry_run else "live"
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.log"

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[console, file_handler])
    return log_path


def main() -> None:
    log_path = _configure_logging()
    logger.info("Logging to %s", log_path)
    try:
        config.validate()
    except EnvironmentError as exc:
        logger.error("Configuration error: %s", exc)
        raise SystemExit(1)
    run()


if __name__ == "__main__":
    main()
