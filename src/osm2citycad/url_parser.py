"""
Parse latitude/longitude from popular map URLs.

Supported formats:
  - Google Maps: https://www.google.com/maps/place/.../@48.8583701,2.2919064,17z/
  - OpenStreetMap search with bbox: https://www.openstreetmap.org/search?...#map=18/48.858/2.294
  - OpenStreetMap element: https://www.openstreetmap.org/way/5013364
    (resolved via the OSM API to get the element's centroid)
"""

from __future__ import annotations

import logging
import re
from typing import NamedTuple
from urllib.parse import parse_qs, urlparse

import requests

logger = logging.getLogger(__name__)

OSM_API_BASE = "https://api.openstreetmap.org/api/0.6"


class LocationResult(NamedTuple):
    lat: float
    lon: float
    bbox: dict[str, float] | None  # west/south/east/north if available


def parse_google_maps_url(url: str) -> LocationResult | None:
    """Extract lat/lon from a Google Maps URL containing /@lat,lon,zoom."""
    m = re.search(r"@(-?\d+\.?\d*),(-?\d+\.?\d*)", url)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        return LocationResult(lat=lat, lon=lon, bbox=None)
    return None


def parse_osm_search_url(url: str) -> LocationResult | None:
    """
    Parse an OpenStreetMap search URL.

    Two possible coordinate sources:
      1. Fragment: #map=zoom/lat/lon
      2. Query params: minlon, minlat, maxlon, maxlat (compute center)
    """
    parsed = urlparse(url)

    if parsed.fragment:
        m = re.search(r"map=\d+/(-?\d+\.?\d*)/(-?\d+\.?\d*)", parsed.fragment)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            qs = parse_qs(parsed.query)
            bbox = None
            if all(k in qs for k in ("minlon", "minlat", "maxlon", "maxlat")):
                bbox = {
                    "west": float(qs["minlon"][0]),
                    "south": float(qs["minlat"][0]),
                    "east": float(qs["maxlon"][0]),
                    "north": float(qs["maxlat"][0]),
                }
            return LocationResult(lat=lat, lon=lon, bbox=bbox)

    qs = parse_qs(parsed.query)
    if all(k in qs for k in ("minlon", "minlat", "maxlon", "maxlat")):
        w, s = float(qs["minlon"][0]), float(qs["minlat"][0])
        e, n = float(qs["maxlon"][0]), float(qs["maxlat"][0])
        return LocationResult(
            lat=(s + n) / 2,
            lon=(w + e) / 2,
            bbox={"west": w, "south": s, "east": e, "north": n},
        )

    return None


def parse_osm_element_url(url: str) -> LocationResult | None:
    """
    Resolve an OSM element URL (node/way/relation) via the OSM API.

    Uses the read-only OSM API 0.6 which is intended for light programmatic access.
    """
    m = re.search(r"openstreetmap\.org/(node|way|relation)/(\d+)", url)
    if not m:
        return None

    element_type, element_id = m.group(1), m.group(2)
    api_url = f"{OSM_API_BASE}/{element_type}/{element_id}.json"

    logger.info("Fetching OSM element: %s/%s ...", element_type, element_id)
    resp = requests.get(api_url, timeout=30, headers={"User-Agent": "osm2citycad/0.1"})
    resp.raise_for_status()
    data = resp.json()

    elements = data.get("elements", [])
    if not elements:
        return None

    el = elements[0]

    if element_type == "node":
        return LocationResult(lat=el["lat"], lon=el["lon"], bbox=None)

    bounds = el.get("bounds")
    if bounds:
        s, n = bounds["minlat"], bounds["maxlat"]
        w, e = bounds["minlon"], bounds["maxlon"]
        return LocationResult(
            lat=(s + n) / 2,
            lon=(w + e) / 2,
            bbox={"west": w, "south": s, "east": e, "north": n},
        )

    return None


def parse_map_url(url: str) -> LocationResult:
    """
    Try all known URL formats and return a LocationResult.
    Raises ValueError if the URL can't be parsed.
    """
    url = url.strip()

    if "google.com/maps" in url or "goo.gl/maps" in url:
        result = parse_google_maps_url(url)
        if result:
            return result

    if "openstreetmap.org" in url:
        if re.search(r"/(node|way|relation)/\d+", url):
            result = parse_osm_element_url(url)
            if result:
                return result

        result = parse_osm_search_url(url)
        if result:
            return result

    # Last resort: look for anything that resembles @lat,lon or lat/lon in the URL
    m = re.search(r"@(-?\d+\.?\d*),(-?\d+\.?\d*)", url)
    if m:
        return LocationResult(lat=float(m.group(1)), lon=float(m.group(2)), bbox=None)

    raise ValueError(
        f"Could not extract coordinates from URL: {url}\n"
        "Supported: Google Maps URLs, OpenStreetMap search/element URLs."
    )
