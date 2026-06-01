import logging
import re
import time

from geopy.exc import GeocoderServiceError, GeocoderTimedOut
from geopy.geocoders import Nominatim

logger = logging.getLogger(__name__)

_geolocator = Nominatim(user_agent="icloud-manager/1.0")
_cache: dict[tuple[float, float], str | None] = {}
_last_request: float = 0.0


def reverse_geocode(lat: float | None, lon: float | None) -> str | None:
    if lat is None or lon is None:
        return None
    # ~1 km bucketing to avoid redundant lookups for nearby shots
    key = (round(lat, 2), round(lon, 2))
    if key in _cache:
        return _cache[key]
    result = _fetch(key[0], key[1])
    _cache[key] = result
    return result


def _fetch(lat: float, lon: float) -> str | None:
    global _last_request
    # Nominatim usage policy: max 1 req/sec
    elapsed = time.monotonic() - _last_request
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)
    try:
        loc = _geolocator.reverse(f"{lat},{lon}", language="en", timeout=10)
        _last_request = time.monotonic()
        return _parse_address(loc.raw.get("address", {})) if loc else None
    except (GeocoderTimedOut, GeocoderServiceError, Exception):
        logger.warning("Reverse geocode failed for %.4f, %.4f", lat, lon)
        return None


def _parse_address(addr: dict) -> str | None:
    # Major cities: prefer city > town, with a London normalisation pass
    city = addr.get("city") or addr.get("town")
    if city:
        # London addresses often lack a "city" key; check the county instead
        if addr.get("county", "").lower() == "greater london":
            return "London"
        return city

    # Rural areas: use county, stripping verbose prefixes/suffixes
    county: str = addr.get("county", "")
    if county:
        county = re.sub(r"(?i)^county of ", "", county)
        county = re.sub(r"(?i) county$", "", county)
        return county

    return addr.get("state") or addr.get("country")
