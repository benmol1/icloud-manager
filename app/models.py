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
    created: datetime
    media_type: MediaType
    is_favorite: bool
    source: Source
    albums: list[str] = field(default_factory=list)

    # Extended metadata
    added_date: datetime | None = None        # when the asset was added to the library
    dimensions: tuple[int, int] | None = None  # (width, height) pixels
    duration: float | None = None             # seconds; videos only
    is_burst: bool = False                    # part of a burst sequence
    is_burst_key: bool = False                # the kept frame in a burst
    is_hidden: bool = False                   # hidden in the library
    has_edits: bool = False                   # edits/adjustments applied
    asset_subtype: int | None = None          # raw API subtype flag (screenshots, panoramas, etc.)
    location: str | None = None               # reverse-geocoded city or county

    @property
    def size_mb(self) -> float:
        return self.size / (1024 * 1024)
