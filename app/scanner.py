import logging
import re
from datetime import datetime, timezone

from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudFailedLoginException

from app.config import config
from app.models import Asset, MediaType, Source

logger = logging.getLogger(__name__)

try:
    # Canonical CloudKit field accessor (pyicloud 2.x). Imported defensively
    # because it lives in an internal module that may move between releases.
    from pyicloud.services.photos_cloudkit.mappers import record_field_value
except Exception:  # pragma: no cover - fallback if the internal path changes
    def record_field_value(record, field_name):
        fields = getattr(record, "fields", None)
        if fields is not None and hasattr(fields, "get_value"):
            value = fields.get_value(field_name)
        elif isinstance(record, dict):
            value = record.get("fields", {}).get(field_name)
        else:
            return None
        if isinstance(value, dict) and "value" in value:
            return value["value"]
        return value

# Matches WhatsApp-exported filenames: IMG-20240101-WA0001.jpg
_WHATSAPP_FILENAME_RE = re.compile(r"^IMG-\d{8}-WA\d+\.", re.IGNORECASE)
_WHATSAPP_ALBUM_KEYWORDS = {"whatsapp"}


class TwoFactorAuthRequired(Exception):
    """Raised when iCloud requires 2FA and no cached session is available."""


class ICloudScanner:
    def __init__(self) -> None:
        self._api: PyiCloudService | None = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        logger.info("Authenticating with iCloud as %s", config.icloud_username)
        try:
            self._api = PyiCloudService(
                config.icloud_username,
                config.icloud_password,
            )
        except PyiCloudFailedLoginException as exc:
            raise RuntimeError(f"iCloud login failed: {exc}") from exc

        if self._api.requires_2fa:
            raise TwoFactorAuthRequired(
                "iCloud requires two-factor authentication. "
                "Run `uv run python -m app.twofactor` interactively to complete "
                "the first-time login and cache the session, then restart the service."
            )

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def scan(self) -> list[Asset]:
        if self._api is None:
            self.authenticate()

        logger.info("Building album membership index…")
        album_index = self._build_album_index()

        logger.info("Scanning photo library…")
        assets: list[Asset] = []
        for photo in self._api.photos.all:
            try:
                asset = self._photo_to_asset(photo, album_index)
                assets.append(asset)
            except Exception:
                logger.warning("Skipped asset %s — could not parse metadata", getattr(photo, "filename", "?"))

        logger.info("Scan complete: %d assets found", len(assets))
        return assets

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_album_index(self) -> dict[str, set[str]]:
        """Return {asset_id: {album_name, ...}} for every album in the library."""
        # pyicloud 2.x exposes `photos.albums` as an iterable container (no
        # `.items()`); each album is iterable and carries its name.
        index: dict[str, set[str]] = {}
        for album in self._api.photos.albums:
            try:
                for photo in album:
                    index.setdefault(photo.id, set()).add(album.name)
            except Exception:
                logger.warning("Could not index album %s", getattr(album, "name", "?"))
        return index

    def _photo_to_asset(
        self, photo, album_index: dict[str, set[str]]
    ) -> Asset:
        albums = list(album_index.get(photo.id, []))
        source = _detect_source(photo.filename, albums)
        is_favorite = _extract_is_favorite(photo)
        media_type = _map_media_type(getattr(photo, "item_type", ""))
        created = _normalise_datetime(photo.created)

        return Asset(
            asset_id=photo.id,
            filename=photo.filename,
            size=photo.size or 0,
            created=created,
            media_type=media_type,
            is_favorite=is_favorite,
            source=source,
            albums=albums,
        )


# ------------------------------------------------------------------
# Pure helpers (importable for testing)
# ------------------------------------------------------------------

def _detect_source(filename: str, albums: list[str]) -> Source:
    if _WHATSAPP_FILENAME_RE.match(filename):
        return Source.WHATSAPP
    if any(kw in album.lower() for album in albums for kw in _WHATSAPP_ALBUM_KEYWORDS):
        return Source.WHATSAPP
    return Source.PHOTOS


def _extract_is_favorite(photo) -> bool:
    # pyicloud 2.x stores the flag as isFavorite (INT64 0/1) on the asset
    # record. There is no public read accessor — `photo.favorite` is a *setter*
    # (it marks the photo as a favourite) — so read the record field directly.
    record = getattr(photo, "_asset_record", None)
    if record is None:
        return False
    return bool(record_field_value(record, "isFavorite"))


def _map_media_type(item_type: str) -> MediaType:
    mapping = {"image": MediaType.IMAGE, "movie": MediaType.VIDEO}
    return mapping.get(item_type.lower(), MediaType.UNKNOWN)


def _normalise_datetime(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(tz=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
