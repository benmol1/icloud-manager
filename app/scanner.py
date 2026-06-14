import logging
import re
from datetime import datetime, timezone

from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudFailedLoginException

from app.config import config
from app.models import Asset, MediaType, Source

logger = logging.getLogger(__name__)

try:
    # Canonical CloudKit field accessors (pyicloud 2.x). Imported defensively
    # because they live in an internal module that may move between releases.
    from pyicloud.services.photos_cloudkit.mappers import (
        decode_encrypted_text,
        record_change_tag,
        record_field_value,
    )
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

    def decode_encrypted_text(record, field_name):  # noqa: D401
        return None

    def record_change_tag(record):  # noqa: D401
        if isinstance(record, dict):
            return record.get("recordChangeTag")
        return getattr(record, "recordChangeTag", None)

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

        since, until = _parse_window(config.scan_since, config.scan_until)
        if since or until:
            logger.info(
                "Capture-date window active: %s … %s",
                since.date() if since else "(any)",
                until.date() if until else "(any)",
            )

        logger.info("Scanning photo library…")
        assets: list[Asset] = []
        skipped_window = 0
        for photo in self._api.photos.all:
            try:
                asset = self._photo_to_asset(photo, album_index)
            except Exception:
                logger.warning("Skipped asset %s — could not parse metadata", getattr(photo, "filename", "?"))
                continue
            if not _in_window(asset.created, since, until):
                skipped_window += 1
                continue
            assets.append(asset)

        if since or until:
            logger.info(
                "Scan complete: %d assets in window (%d outside, skipped)",
                len(assets),
                skipped_window,
            )
        else:
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

        rich = _extract_rich_metadata(photo)

        return Asset(
            asset_id=photo.id,
            filename=photo.filename,
            size=photo.size or 0,
            created=created,
            media_type=media_type,
            is_favorite=is_favorite,
            source=source,
            albums=albums,
            **rich,
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


def _safe(fn, default=None):
    """Run a best-effort metadata read, swallowing the internal-API quirks."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 — rich metadata is optional, never fatal
        return default


def _extract_rich_metadata(photo) -> dict:
    """
    Best-effort extraction of the optional, human-readable metadata iCloud
    exposes (location, dimensions, caption, fingerprint, change tag, …).

    Everything here is defensive: any field iCloud omits (or that a pyicloud
    version doesn't surface) simply comes back as ``None``/``False`` rather than
    failing the whole asset. EXIF (device/lens) is *not* available here — it
    lives in the file and is captured at offload time.
    """
    asset_rec = getattr(photo, "_asset_record", None)
    master_rec = getattr(photo, "_master_record", None)

    width, height = _safe(lambda: photo.dimensions, (None, None)) or (None, None)
    added = _safe(lambda: photo.added_date)
    if added is not None and added.year <= 1970:
        added = None  # pyicloud returns the epoch when addedDate is missing

    adjustment = _safe(lambda: record_field_value(asset_rec, "adjustmentType"))

    return {
        "master_id": _safe(lambda: photo.master_id),
        "added": _normalise_datetime(added) if added is not None else None,
        "file_type": _safe(lambda: record_field_value(master_rec, "resOriginalFileType")),
        "is_hidden": bool(_safe(lambda: record_field_value(asset_rec, "isHidden"))),
        "is_live_photo": bool(_safe(lambda: photo.is_live_photo, False)),
        "caption": _safe(lambda: decode_encrypted_text(asset_rec, "captionEnc")),
        "width": width,
        "height": height,
        "duration": _safe(lambda: record_field_value(asset_rec, "duration")),
        "subtype": _safe(lambda: record_field_value(asset_rec, "assetSubtype")),
        "hdr_type": _safe(lambda: record_field_value(asset_rec, "assetHDRType")),
        "has_adjustments": adjustment is not None,
        "latitude": _safe(lambda: record_field_value(asset_rec, "locationLatitude")),
        "longitude": _safe(lambda: record_field_value(asset_rec, "locationLongitude")),
        "fingerprint": _safe(
            lambda: record_field_value(master_rec, "resOriginalFingerprint")
        ),
        "change_tag": _safe(lambda: record_change_tag(asset_rec)),
        "tz_offset": _safe(lambda: record_field_value(asset_rec, "timeZoneOffset")),
    }


def _map_media_type(item_type: str) -> MediaType:
    mapping = {"image": MediaType.IMAGE, "movie": MediaType.VIDEO}
    return mapping.get(item_type.lower(), MediaType.UNKNOWN)


def _parse_window(
    since: str, until: str
) -> tuple[datetime | None, datetime | None]:
    """Parse inclusive ISO ``YYYY-MM-DD`` bounds into UTC datetimes."""

    def _parse(value: str, end_of_day: bool) -> datetime | None:
        value = (value or "").strip()
        if not value:
            return None
        dt = datetime.fromisoformat(value)
        if end_of_day and dt.hour == dt.minute == dt.second == 0:
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    return _parse(since, end_of_day=False), _parse(until, end_of_day=True)


def _in_window(
    created: datetime, since: datetime | None, until: datetime | None
) -> bool:
    if since is not None and created < since:
        return False
    if until is not None and created > until:
        return False
    return True


def _normalise_datetime(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(tz=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
