from datetime import datetime, timezone

import pytest

from app.models import MediaType, Source
from app.scanner import (
    _detect_source,
    _extract_is_favorite,
    _extract_rich_metadata,
    _in_window,
    _map_media_type,
    _normalise_datetime,
    _parse_window,
)


class _FakePhoto:
    def __init__(self, asset_record):
        self._asset_record = asset_record


class _RichFakePhoto:
    """Fake pyicloud asset with the accessors + records the scanner reads."""

    def __init__(
        self,
        asset_fields,
        master_fields,
        *,
        dimensions=(None, None),
        added=None,
        is_live=False,
        master_id="M1",
        change_tag="tag123",
    ):
        self._asset_record = {"fields": asset_fields, "recordChangeTag": change_tag}
        self._master_record = {"fields": master_fields}
        self._dimensions = dimensions
        self._added = added
        self._is_live = is_live
        self._master_id = master_id

    @property
    def dimensions(self):
        return self._dimensions

    @property
    def added_date(self):
        return self._added

    @property
    def is_live_photo(self):
        return self._is_live

    @property
    def master_id(self):
        return self._master_id


class TestExtractIsFavorite:
    def test_favourite_flag_set(self):
        photo = _FakePhoto({"fields": {"isFavorite": {"value": 1}}})
        assert _extract_is_favorite(photo) is True

    def test_favourite_flag_zero(self):
        photo = _FakePhoto({"fields": {"isFavorite": {"value": 0}}})
        assert _extract_is_favorite(photo) is False

    def test_field_absent_defaults_false(self):
        photo = _FakePhoto({"fields": {}})
        assert _extract_is_favorite(photo) is False

    def test_no_asset_record_defaults_false(self):
        photo = _FakePhoto(None)
        assert _extract_is_favorite(photo) is False


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


class TestExtractRichMetadata:
    def test_pulls_available_fields(self):
        asset_fields = {
            "isHidden": {"value": 1},
            "duration": {"value": 12.5},
            "assetSubtype": {"value": 2},
            "assetHDRType": {"value": 0},
            "adjustmentType": {"value": "someEdit"},
            "locationLatitude": {"value": 51.5},
            "locationLongitude": {"value": -0.12},
            "captionEnc": {"value": "QmVhY2ggZGF5"},  # base64("Beach day")
            "timeZoneOffset": {"value": 3600},
        }
        master_fields = {
            "resOriginalFileType": {"value": "public.heic"},
            "resOriginalFingerprint": {"value": "FINGERPRINT123"},
        }
        photo = _RichFakePhoto(
            asset_fields,
            master_fields,
            dimensions=(4032, 3024),
            added=datetime(2020, 5, 1, tzinfo=timezone.utc),
        )
        meta = _extract_rich_metadata(photo)

        assert meta["is_hidden"] is True
        assert meta["duration"] == 12.5
        assert meta["subtype"] == 2
        assert meta["hdr_type"] == 0
        assert meta["has_adjustments"] is True
        assert meta["latitude"] == 51.5
        assert meta["longitude"] == -0.12
        assert meta["caption"] == "Beach day"
        assert meta["file_type"] == "public.heic"
        assert meta["fingerprint"] == "FINGERPRINT123"
        assert meta["change_tag"] == "tag123"
        assert (meta["width"], meta["height"]) == (4032, 3024)
        assert meta["added"].year == 2020
        assert meta["tz_offset"] == 3600
        assert meta["master_id"] == "M1"

    def test_missing_fields_default_safely(self):
        photo = _RichFakePhoto({}, {}, dimensions=(None, None), added=None)
        meta = _extract_rich_metadata(photo)
        assert meta["caption"] is None
        assert meta["latitude"] is None
        assert meta["added"] is None
        assert meta["has_adjustments"] is False
        assert meta["is_hidden"] is False

    def test_epoch_added_date_treated_as_missing(self):
        # pyicloud returns the 1970 epoch when addedDate is absent.
        photo = _RichFakePhoto(
            {}, {}, added=datetime(1970, 1, 1, tzinfo=timezone.utc)
        )
        assert _extract_rich_metadata(photo)["added"] is None


class TestScanWindow:
    def test_no_window_is_none(self):
        assert _parse_window("", "") == (None, None)

    def test_since_and_until_inclusive(self):
        since, until = _parse_window("2020-01-01", "2020-12-31")
        assert since == datetime(2020, 1, 1, tzinfo=timezone.utc)
        assert until == datetime(2020, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

    def test_in_window_bounds(self):
        since, until = _parse_window("2020-01-01", "2020-12-31")
        assert _in_window(datetime(2020, 6, 1, tzinfo=timezone.utc), since, until)
        assert not _in_window(datetime(2019, 6, 1, tzinfo=timezone.utc), since, until)
        assert not _in_window(datetime(2021, 1, 1, tzinfo=timezone.utc), since, until)

    def test_open_ended_window(self):
        since, until = _parse_window("2020-01-01", "")
        assert _in_window(datetime(2025, 1, 1, tzinfo=timezone.utc), since, until)
        assert not _in_window(datetime(2019, 1, 1, tzinfo=timezone.utc), since, until)


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
