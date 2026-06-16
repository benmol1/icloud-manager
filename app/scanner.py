import base64
import json
import logging
import plistlib
import re
from datetime import datetime, timezone
from pathlib import Path

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

# Log album progress every N albums so the terminal isn't silent for ~19 min.
_ALBUM_LOG_INTERVAL = 10


class TwoFactorAuthRequired(Exception):
    """Raised when iCloud requires 2FA and no cached session is available."""


class ICloudScanner:
    def __init__(self) -> None:
        self._api: PyiCloudService | None = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    @property
    def api(self) -> PyiCloudService:
        """The authenticated pyicloud service (call ``authenticate`` first)."""
        if self._api is None:
            raise RuntimeError("Scanner is not authenticated yet")
        return self._api

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

    def ensure_authenticated(self) -> None:
        """Authenticate only if not already logged in (no-op after a scan)."""
        if self._api is None:
            self.authenticate()

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def scan(self, cached_assets: dict[str, Asset] | None = None) -> list[Asset]:
        """Scan the iCloud photo library and return all matching assets.

        Pass *cached_assets* (a ``{asset_id: Asset}`` dict built from a previous
        run's index) to enable incremental mode: assets whose ``change_tag``
        matches the cached value skip the full metadata parse and reuse the
        stored ``Asset``, saving per-asset processing time.
        """
        if self._api is None:
            self.authenticate()

        if cached_assets:
            logger.info("Incremental mode: %d assets cached from previous scan", len(cached_assets))

        cache_path = Path(config.index_db_path).parent / "album_index_cache.json"
        album_index = self._build_album_index(cache_path)

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
        reused = 0
        for photo in self._api.photos.all:
            try:
                # Incremental fast path: skip full metadata parse for unchanged assets.
                if cached_assets:
                    asset_rec = getattr(photo, "_asset_record", None)
                    current_tag = _safe(lambda: record_change_tag(asset_rec)) if asset_rec else None
                    cached = cached_assets.get(photo.id) if current_tag else None
                    if cached is not None and cached.change_tag == current_tag:
                        # Refresh album membership in case the user reorganised albums.
                        albums = list(album_index.get(photo.id, set()))
                        if set(albums) != set(cached.albums):
                            cached.albums = albums
                            cached.source = _detect_source(cached.filename, albums)
                        if not _in_window(cached.created, since, until):
                            skipped_window += 1
                            continue
                        assets.append(cached)
                        reused += 1
                        continue

                asset = self._photo_to_asset(photo, album_index)
            except Exception:
                logger.warning("Skipped asset %s — could not parse metadata", getattr(photo, "filename", "?"))
                continue
            if not _in_window(asset.created, since, until):
                skipped_window += 1
                continue
            assets.append(asset)

        if reused:
            logger.info("Incremental: reused %d unchanged assets (skipped full parse)", reused)
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

    def _build_album_index(self, cache_path: Path) -> dict[str, set[str]]:
        """Return {asset_id: {album_name, ...}} for every album in the library.

        Loads from *cache_path* when the file exists and is within the
        configured TTL; otherwise iterates pyicloud and saves a fresh copy.
        """
        cached = _load_album_cache(cache_path, max_age_hours=config.album_cache_max_age_hours)
        if cached is not None:
            return cached

        # Only announce the (slow, ~19 min) rebuild here — a cache hit returns
        # above, so this message never misleads when the cache is used.
        logger.info("Building album membership index… (this can take ~19 min)")

        # pyicloud 2.x exposes `photos.albums` as an iterable container (no
        # `.items()`); each album is iterable and carries its name.
        index: dict[str, set[str]] = {}
        album_num = 0
        for album in self._api.photos.albums:
            album_num += 1
            try:
                for photo in album:
                    index.setdefault(photo.id, set()).add(album.name)
            except Exception:
                logger.warning("Could not index album %s", getattr(album, "name", "?"))
            if album_num % _ALBUM_LOG_INTERVAL == 0:
                logger.info(
                    "  Album index: %d albums processed, %d assets mapped so far…",
                    album_num,
                    len(index),
                )
        logger.info("Album index built: %d albums, %d assets", album_num, len(index))
        _save_album_cache(index, cache_path)
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


def _extract_location(asset_rec) -> tuple[float | None, float | None]:
    """
    Decode GPS from the ``locationEnc`` field.

    iCloud leaves the plain ``locationLatitude``/``locationLongitude`` fields
    empty and instead stores a base64-wrapped **binary plist** (``bplist00``)
    holding ``lat``/``lon``/``alt``/… Most assets carry no location at all, in
    which case the field is absent and we return ``(None, None)``.
    """
    enc = record_field_value(asset_rec, "locationEnc")
    if enc is None:
        return (None, None)

    raw = enc if isinstance(enc, bytes) else str(enc).encode("ascii", "ignore")
    if not raw.startswith(b"bplist"):
        try:
            raw = base64.b64decode(raw)
        except Exception:  # noqa: BLE001 — fall through; plistlib will reject it
            pass

    try:
        plist = plistlib.loads(raw)
    except Exception:  # noqa: BLE001 — unknown encoding; treat as no location
        return (None, None)

    return (plist.get("lat"), plist.get("lon"))


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
    latitude, longitude = _safe(lambda: _extract_location(asset_rec), (None, None)) or (
        None,
        None,
    )

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
        "latitude": latitude,
        "longitude": longitude,
        "fingerprint": _safe(
            lambda: record_field_value(master_rec, "resOriginalFingerprint")
        ),
        "change_tag": _safe(lambda: record_change_tag(asset_rec)),
        "tz_offset": _safe(lambda: record_field_value(asset_rec, "timeZoneOffset")),
    }


def _load_album_cache(
    cache_path: Path, *, max_age_hours: int
) -> dict[str, set[str]] | None:
    """Return the cached album index if it exists and is within *max_age_hours*."""
    if max_age_hours == 0 or not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        built_at = datetime.fromisoformat(data["built_at"])
        age_hours = (datetime.now(tz=timezone.utc) - built_at).total_seconds() / 3600
        if age_hours > max_age_hours:
            logger.info(
                "Album cache is %.1f h old (max %d h) — rebuilding",
                age_hours,
                max_age_hours,
            )
            return None
        index = {asset_id: set(albums) for asset_id, albums in data["index"].items()}
        logger.info(
            "Loaded album index from cache (%.1f h old, %d assets)",
            age_hours,
            len(index),
        )
        return index
    except Exception:
        logger.warning("Failed to read album cache — rebuilding", exc_info=True)
        return None


def _save_album_cache(index: dict[str, set[str]], cache_path: Path) -> None:
    """Persist *index* to *cache_path* as JSON."""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "built_at": datetime.now(tz=timezone.utc).isoformat(),
            "index": {asset_id: list(albums) for asset_id, albums in index.items()},
        }
        cache_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        logger.info("Album index cache saved (%s)", cache_path)
    except Exception:
        logger.warning("Could not save album cache", exc_info=True)


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
