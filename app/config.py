import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(f"Required environment variable '{key}' is not set")
    return value


def _bool(key: str, default: str) -> bool:
    return os.getenv(key, default).lower() == "true"


class Config:
    """Runtime configuration, read from the environment on construction.

    Reading happens in ``__init__`` (not at class-definition time) so a fresh
    ``Config()`` always reflects the *current* environment — which keeps tests
    isolated: they can ``monkeypatch.setenv``/``delenv`` and construct a new
    ``Config()`` without reloading the module (and so without re-running
    ``load_dotenv()`` and pulling ``.env`` values back in).
    """

    def __init__(self) -> None:
        self.icloud_username: str = _require("ICLOUD_USERNAME") if os.getenv("ICLOUD_USERNAME") else ""
        self.icloud_password: str = _require("ICLOUD_PASSWORD") if os.getenv("ICLOUD_PASSWORD") else ""

        self.telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

        self.smb_host: str = os.getenv("SMB_HOST", "")
        self.smb_share: str = os.getenv("SMB_SHARE", "")
        self.smb_username: str = os.getenv("SMB_USERNAME", "")
        self.smb_password: str = os.getenv("SMB_PASSWORD", "")
        self.smb_mount_path: str = os.getenv("SMB_MOUNT_PATH", "/mnt/storage")

        # Searchable asset index (SQLite). Lives on a Docker volume in production.
        self.index_db_path: str = os.getenv("INDEX_DB_PATH", "data/asset_index.db")

        self.dry_run: bool = _bool("DRY_RUN", "true")
        self.min_age_days: int = int(os.getenv("MIN_AGE_DAYS", "180"))

        # Cap on how many assets a single live offload run will move. Handy to
        # keep an initial real test to a small handful. 0 = unlimited.
        self.offload_max_items: int = int(os.getenv("OFFLOAD_MAX_ITEMS", "0"))

        # Where offloaded files land for this deployment: "local" (a directly
        # attached drive, e.g. this PC's D:) or "network" (the Pi/NAS share).
        # Recorded against each offloaded asset so the index can summarise tiers.
        self.storage_tier: str = os.getenv("STORAGE_TIER", "local")

        # Album membership index cache. Avoids rebuilding the full album index
        # (~19 min) on every run by persisting it next to INDEX_DB_PATH. 0 =
        # always rebuild (disables cache). Default: 168 h (one week — matches
        # scan cadence).
        self.album_cache_max_age_hours: int = int(os.getenv("ALBUM_CACHE_MAX_AGE_HOURS", "168"))

        # Index-only fast mode: skip the live iCloud scan entirely and read
        # assets straight from the SQLite index (seconds vs ~10 min). Requires a
        # prior full scan to have populated the index. Honours
        # SCAN_SINCE/SCAN_UNTIL for a targeted slice. Won't see assets added
        # since the last full scan.
        self.scan_from_index: bool = _bool("SCAN_FROM_INDEX", "false")

        # Optional capture-date window (inclusive, ISO YYYY-MM-DD) to limit a
        # scan — handy for testing against a small slice (e.g.
        # SCAN_SINCE=2020-01-01 SCAN_UNTIL=2020-12-31). Empty = no limit.
        self.scan_since: str = os.getenv("SCAN_SINCE", "")
        self.scan_until: str = os.getenv("SCAN_UNTIL", "")
        self.scan_day_of_week: str = os.getenv("SCAN_DAY_OF_WEEK", "sunday")
        self.scan_time: str = os.getenv("SCAN_TIME", "02:00")

        # Scoring weights (must sum to 100)
        self.weight_age: int = int(os.getenv("WEIGHT_AGE", "35"))
        self.weight_size: int = int(os.getenv("WEIGHT_SIZE", "20"))
        self.weight_source: int = int(os.getenv("WEIGHT_SOURCE", "30"))
        self.weight_duplicate: int = int(os.getenv("WEIGHT_DUPLICATE", "15"))

        # Score thresholds
        # Assets scoring >= auto_offload_threshold AND not favourite are offloaded automatically
        self.auto_offload_threshold: int = int(os.getenv("AUTO_OFFLOAD_THRESHOLD", "65"))
        # Assets scoring >= review_threshold are sent for manual approval
        self.review_threshold: int = int(os.getenv("REVIEW_THRESHOLD", "40"))
        # Cap on how many assets the review bucket surfaces per run, prioritised
        # by reclaimable size. The overflow is deferred to later runs (it
        # reappears as the top items get actioned), keeping the Telegram approval
        # flow manageable. 0 = unlimited (surface everything).
        self.review_max_items: int = int(os.getenv("REVIEW_MAX_ITEMS", "50"))

        # Favourite assets are never auto-offloaded regardless of score
        self.favorite_score_penalty: int = int(os.getenv("FAVORITE_SCORE_PENALTY", "60"))

        # Size threshold above which an asset gets the full size score (MB)
        self.large_file_mb: int = int(os.getenv("LARGE_FILE_MB", "50"))

    def validate(self) -> None:
        required = {
            "ICLOUD_USERNAME": self.icloud_username,
            "ICLOUD_PASSWORD": self.icloud_password,
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_CHAT_ID": self.telegram_chat_id,
            "SMB_HOST": self.smb_host,
            "SMB_SHARE": self.smb_share,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")


config = Config()
