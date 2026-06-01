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

    @property
    def size_mb(self) -> float:
        return self.size / (1024 * 1024)
