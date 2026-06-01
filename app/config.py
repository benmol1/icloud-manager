import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(f"Required environment variable '{key}' is not set")
    return value


class Config:
    icloud_username: str = _require("ICLOUD_USERNAME") if os.getenv("ICLOUD_USERNAME") else ""
    icloud_password: str = _require("ICLOUD_PASSWORD") if os.getenv("ICLOUD_PASSWORD") else ""

    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    smb_host: str = os.getenv("SMB_HOST", "")
    smb_share: str = os.getenv("SMB_SHARE", "")
    smb_username: str = os.getenv("SMB_USERNAME", "")
    smb_password: str = os.getenv("SMB_PASSWORD", "")
    smb_mount_path: str = os.getenv("SMB_MOUNT_PATH", "/mnt/storage")

    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    min_age_days: int = int(os.getenv("MIN_AGE_DAYS", "180"))
    scan_day_of_week: str = os.getenv("SCAN_DAY_OF_WEEK", "sunday")
    scan_time: str = os.getenv("SCAN_TIME", "02:00")

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
