from datetime import datetime, timezone

import pytest

from app.models import MediaType, Source
from app.scanner import (
    _detect_source,
    _map_media_type,
    _normalise_datetime,
)


class TestDetectSource:
    def test_whatsapp_filename_pattern(self):
        assert _detect_source("IMG-20240101-WA0001.jpg", []) == Source.WHATSAPP

    def test_whatsapp_filename_case_insensitive(self):
        assert _detect_source("img-20231215-wa0042.JPG", []) == Source.WHATSAPP

    def test_whatsapp_album_name(self):
        assert _detect_source("photo.jpg", ["WhatsApp"]) == Source.WHATSAPP

    def test_whatsapp_album_name_case_insensitive(self):
        assert _detect_source("photo.jpg", ["WHATSAPP Images"]) == Source.WHATSAPP

    def test_regular_photo_no_match(self):
        assert _detect_source("IMG_1234.jpg", ["Recents", "Favourites"]) == Source.PHOTOS

    def test_nursery_whatsapp_image(self):
        # Real-world pattern from nursery WhatsApp chats
        assert _detect_source("IMG-20240903-WA0007.jpg", []) == Source.WHATSAPP

    def test_non_whatsapp_img_filename(self):
        # IMG_ prefix (iPhone default) should not match
        assert _detect_source("IMG_4821.HEIC", []) == Source.PHOTOS

    def test_video_whatsapp_filename(self):
        assert _detect_source("IMG-20240101-WA0001.mp4", []) == Source.WHATSAPP

    def test_empty_albums_non_whatsapp_file(self):
        assert _detect_source("photo_2024.jpg", []) == Source.PHOTOS


class TestMapMediaType:
    def test_image(self):
        assert _map_media_type("image") == MediaType.IMAGE

    def test_movie(self):
        assert _map_media_type("movie") == MediaType.VIDEO

    def test_unknown(self):
        assert _map_media_type("") == MediaType.UNKNOWN

    def test_case_insensitive(self):
        assert _map_media_type("IMAGE") == MediaType.IMAGE


class TestNormaliseDatetime:
    def test_naive_datetime_gets_utc(self):
        naive = datetime(2024, 1, 1, 12, 0, 0)
        result = _normalise_datetime(naive)
        assert result.tzinfo == timezone.utc

    def test_aware_datetime_unchanged(self):
        aware = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert _normalise_datetime(aware) == aware

    def test_none_returns_current_time(self):
        result = _normalise_datetime(None)
        assert result.tzinfo == timezone.utc
