import logging
import re
from datetime import datetime, timezone

from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudFailedLoginException

from app.config import config
from app.geocoder import reverse_geocode
from app.models import Asset, MediaType, Source

logger = logging.getLogger(__name__)

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
        index: dict[str, set[str]] = {}
        for album_name, album in self._api.photos.albums.items():
            for photo in album:
                asset_id = photo.id
                index.setdefault(asset_id, set()).add(album_name)
        return index

    def _photo_to_asset(
        self, photo, album_index: dict[str, set[str]]
    ) -> Asset:
        albums = list(album_index.get(photo.id, []))
        source = _detect_source(photo.filename, albums)
        is_favorite = _extract_is_favorite(photo)
        media_type = _map_media_type(getattr(photo, "item_type", ""))
        created = _normalise_datetime(photo.created)
        added_date = _normalise_datetime_optional(getattr(photo, "added_date", None))

        lat = _get_field(photo, "locationLatitude")
        lon = _get_field(photo, "locationLongitude")
        location = reverse_geocode(
            float(lat) if lat is not None else None,
            float(lon) if lon is not None else None,
        )

        duration_raw = _get_field(photo, "duration")
        burst_id = _get_field(photo, "burstId")

        return Asset(
            asset_id=photo.id,
            filename=photo.filename,
            size=photo.size or 0,
            created=created,
            media_type=media_type,
            is_favorite=is_favorite,
            source=source,
            albums=albums,
            added_date=added_date,
            dimensions=getattr(photo, "dimensions", None),
            duration=float(duration_raw) if duration_raw is not None else None,
            is_burst=burst_id is not None,
            is_burst_key=bool(_get_field(photo, "isKeyAsset")),
            is_hidden=bool(_get_field(photo, "isHidden")),
            has_edits=bool(_get_field(photo, "adjustmentType")),
            asset_subtype=_get_field(photo, "assetSubtype"),
            location=location,
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
    # pyicloud exposes isFavorite in the master record fields
    try:
        return bool(
            photo._master_record["fields"].get("isFavorite", {}).get("value", False)
        )
    except (AttributeError, KeyError, TypeError):
        return False


def _get_field(photo, field_name: str):
    """Read a raw CloudKit field, checking master record then asset record."""
    for rec_attr in ("_master_record", "_asset_record"):
        try:
            return getattr(photo, rec_attr)["fields"][field_name]["value"]
        except (AttributeError, KeyError, TypeError):
            continue
    return None


def _map_media_type(item_type: str) -> MediaType:
    mapping = {"image": MediaType.IMAGE, "movie": MediaType.VIDEO}
    return mapping.get(item_type.lower(), MediaType.UNKNOWN)


def _normalise_datetime(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(tz=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _normalise_datetime_optional(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
