from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MediaType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    UNKNOWN = "unknown"


class Source(str, Enum):
    WHATSAPP = "whatsapp"
    PHOTOS = "photos"
    UNKNOWN = "unknown"


@dataclass
class Asset:
    asset_id: str
    filename: str
    size: int  # bytes
    created: datetime  # capture date (assetDate)
    media_type: MediaType
    is_favorite: bool
    source: Source
    albums: list[str] = field(default_factory=list)

    # Rich metadata — best-effort; None/0 when iCloud doesn't provide it.
    master_id: str | None = None
    added: datetime | None = None  # when it entered the iCloud library (addedDate)
    file_type: str | None = None  # resOriginalFileType (e.g. public.heic)
    is_hidden: bool = False
    is_live_photo: bool = False
    caption: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None  # seconds (videos)
    subtype: int | None = None  # assetSubtype (screenshot/panorama/portrait…)
    hdr_type: int | None = None  # assetHDRType
    has_adjustments: bool = False
    latitude: float | None = None
    longitude: float | None = None
    fingerprint: str | None = None  # resOriginalFingerprint (content hash)
    change_tag: str | None = None  # recordChangeTag (incremental-scan key)
    tz_offset: int | None = None  # timeZoneOffset at capture

    @property
    def size_mb(self) -> float:
        return self.size / (1024 * 1024)
