"""
OSM data fetch, projection, and DXF export for CityCAD.

Fetches OpenStreetMap features via OSMnx (Overpass API), reprojects to a local
projected CRS (meters), optionally simplifies geometry, and writes 2D LWPolyline
entities on named layers via ezdxf.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

import ezdxf
import geopandas as gpd
import osmnx as ox
import pandas as pd
from ezdxf import units
from osmnx._errors import InsufficientResponseError
from shapely.affinity import translate
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.validation import make_valid

logger = logging.getLogger(__name__)

_EXCLUDED_HIGHWAY_TYPES = frozenset(
    {
        "footway",
        "path",
        "bridleway",
        "steps",
        "corridor",
        "elevator",
        "cycleway",
        "platform",
        "raceway",
        "proposed",
        "construction",
        "pedestrian",
    }
)

DXF_LAYERS = (
    "roads_main",
    "roads_local",
    "buildings",
    "water",
    "rail",
    "parks",
)

ALL_LAYER_NAMES = set(DXF_LAYERS)

# User-friendly aliases that map to one or more internal layer names
LAYER_ALIASES: dict[str, list[str]] = {
    "roads": ["roads_main", "roads_local"],
}

CITYCAD_MAX_KM = 50.0
DEFAULT_SIZE_KM = 5.0


def utm_epsg_from_lonlat(lon: float, lat: float) -> str:
    """Return the EPSG code for the UTM zone covering a given lon/lat."""
    zone_number = int((lon + 180) / 6) + 1
    if lat >= 0:
        return f"EPSG:326{zone_number:02d}"
    return f"EPSG:327{zone_number:02d}"


def bbox_from_center(
    lat: float,
    lon: float,
    width_km: float = DEFAULT_SIZE_KM,
    height_km: float = DEFAULT_SIZE_KM,
) -> dict[str, float]:
    """
    Build a WGS84 bounding box centered on (lat, lon).

    Returns dict with keys west, south, east, north (degrees).
    Uses a spherical-earth approximation (accurate enough for bbox sizing).
    """
    km_per_deg_lat = 111.32
    km_per_deg_lon = 111.32 * math.cos(math.radians(lat))

    dlat = (height_km / 2.0) / km_per_deg_lat
    dlon = (width_km / 2.0) / km_per_deg_lon

    return {
        "west": lon - dlon,
        "south": lat - dlat,
        "east": lon + dlon,
        "north": lat + dlat,
    }


def validate_area_size(bbox: dict[str, float]) -> tuple[float, float]:
    """
    Estimate the width and height (in km) of a WGS84 bbox.
    Raises ValueError if either dimension exceeds CityCAD's 50 km limit.
    Returns (width_km, height_km).
    """
    mid_lat = (bbox["south"] + bbox["north"]) / 2.0
    km_per_deg_lat = 111.32
    km_per_deg_lon = 111.32 * math.cos(math.radians(mid_lat))

    width_km = abs(bbox["east"] - bbox["west"]) * km_per_deg_lon
    height_km = abs(bbox["north"] - bbox["south"]) * km_per_deg_lat

    if width_km > CITYCAD_MAX_KM or height_km > CITYCAD_MAX_KM:
        raise ValueError(
            f"Requested area is ~{width_km:.1f} km x {height_km:.1f} km, which exceeds "
            f"CityCAD's maximum ground plane of {CITYCAD_MAX_KM:.0f} km x {CITYCAD_MAX_KM:.0f} km. "
            f"Please reduce the area size."
        )
    return width_km, height_km


def _features_from_polygon(polygon: BaseGeometry, tags: dict[str, Any]) -> gpd.GeoDataFrame:
    fn = getattr(ox, "features_from_polygon", None) or getattr(ox, "geometries_from_polygon", None)
    if fn is None:
        raise RuntimeError("OSMnx version is too old: no features/geometries_from_polygon.")
    try:
        gdf = fn(polygon, tags)
    except InsufficientResponseError:
        logger.info("No OSM features for tag filter %s (skipping).", tags)
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    if gdf is None or gdf.empty:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    return gdf


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def get_clip_polygon(config: dict[str, Any]) -> BaseGeometry:
    """
    Return a Shapely polygon in WGS84 for the query area.
    Priority: polygon_wkt > bbox > place_name.
    """
    wkt = config.get("polygon_wkt")
    if wkt:
        from shapely import wkt as wkt_mod
        geom = wkt_mod.loads(wkt)
        if geom.is_empty:
            raise ValueError("polygon_wkt is empty.")
        return geom

    bbox = config.get("bbox")
    if bbox is not None:
        for k in ("west", "south", "east", "north"):
            if k not in bbox:
                raise KeyError(f"bbox must include keys: west, south, east, north")
        return box(bbox["west"], bbox["south"], bbox["east"], bbox["north"])

    place = config.get("place_name")
    if not place:
        raise ValueError("Set place_name, bbox, or polygon_wkt in config.")

    gdf_place = ox.geocode_to_gdf(place)
    if gdf_place is None or gdf_place.empty:
        raise RuntimeError(f"Geocoder returned no boundary for: {place!r}")
    geom = gdf_place.union_all()
    if geom.is_empty:
        raise RuntimeError(f"Empty geometry after geocoding: {place!r}")
    return geom


def configure_osmnx(config: dict[str, Any]) -> None:
    timeout = int(config.get("overpass_timeout_seconds", 180))
    ox.settings.requests_timeout = timeout
    ox.settings.use_cache = bool(config.get("use_osmnx_cache", True))
    over_url = config.get("overpass_url")
    if over_url:
        ox.settings.overpass_url = str(over_url).rstrip("/")
        logger.info("Using Overpass endpoint: %s", ox.settings.overpass_url)
    mem_gb = config.get("overpass_memory_gb")
    if mem_gb is not None:
        ox.settings.overpass_memory = int(float(mem_gb) * (1024**3))
    ox.settings.log_console = False


def _safe_make_valid(geom: BaseGeometry | None) -> BaseGeometry | None:
    if geom is None or geom.is_empty:
        return geom
    if not geom.is_valid:
        return make_valid(geom)
    return geom


def _project_and_simplify(
    gdf: gpd.GeoDataFrame,
    target_crs: str,
    tol_m: float,
    clip_polygon: BaseGeometry | None = None,
) -> gpd.GeoDataFrame:
    if gdf.empty:
        return gdf

    if clip_polygon is not None:
        gdf = gpd.clip(gdf, clip_polygon)
        gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]
        if gdf.empty:
            return gdf

    out = gdf.to_crs(target_crs)
    out["geometry"] = out["geometry"].map(_safe_make_valid)
    out = out[~out.geometry.is_empty & out.geometry.notna()]
    if tol_m and tol_m > 0:
        out = out.copy()
        out["geometry"] = out["geometry"].simplify(tol_m, preserve_topology=True)
        out = out[~out.geometry.is_empty]
    return out


def fetch_roads(polygon: BaseGeometry, config: dict[str, Any]) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    roads_cfg = config.get("roads", {})
    main_types = set(roads_cfg.get("main_highway_types", []))
    local_types = set(roads_cfg.get("local_highway_types", []))
    service_types = set(roads_cfg.get("service_highway_types", []))
    include_service = bool(roads_cfg.get("include_service", False))

    gdf = _features_from_polygon(polygon, {"highway": True})
    if gdf.empty:
        return gpd.GeoDataFrame(), gpd.GeoDataFrame()

    gdf = gdf.reset_index(drop=False)
    if "highway" not in gdf.columns:
        logger.warning("Road features returned no 'highway' column; skipping roads.")
        return gpd.GeoDataFrame(), gpd.GeoDataFrame()

    mask_line = gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])
    gdf = gdf.loc[mask_line].copy()
    gdf["highway"] = gdf["highway"].astype(str)

    exclude = gdf["highway"].isin(_EXCLUDED_HIGHWAY_TYPES)
    gdf = gdf.loc[~exclude].copy()

    if not include_service:
        gdf = gdf.loc[~gdf["highway"].isin(service_types)].copy()

    main_mask = gdf["highway"].isin(main_types)
    local_mask = gdf["highway"].isin(local_types)
    if include_service:
        local_mask = local_mask | gdf["highway"].isin(service_types)

    main_gdf = gdf.loc[main_mask].copy()
    local_gdf = gdf.loc[local_mask].copy()

    leftover_mask = ~(main_mask | local_mask)
    if leftover_mask.any():
        logger.info("Dropped %d road features with unlisted highway=* tags.", int(leftover_mask.sum()))

    return main_gdf, local_gdf


def fetch_buildings(polygon: BaseGeometry) -> gpd.GeoDataFrame:
    gdf = _features_from_polygon(polygon, {"building": True})
    if gdf.empty:
        return gdf
    gdf = gdf.reset_index(drop=False)
    mask_poly = gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
    return gdf.loc[mask_poly].copy()


def fetch_water(polygon: BaseGeometry) -> gpd.GeoDataFrame:
    tag_sets: list[dict[str, Any]] = [
        {"natural": "water"},
        {"natural": "bay"},
        {"waterway": "riverbank"},
        {"waterway": "dock"},
        {"landuse": "reservoir"},
    ]
    parts: list[gpd.GeoDataFrame] = []
    for tags in tag_sets:
        gdf = _features_from_polygon(polygon, tags)
        if not gdf.empty:
            parts.append(gdf.reset_index(drop=False))
    if not parts:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    merged = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)
    mask_poly = merged.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
    return merged.loc[mask_poly]


def fetch_rail(polygon: BaseGeometry) -> gpd.GeoDataFrame:
    gdf = _features_from_polygon(polygon, {"railway": ["rail", "light_rail", "tram"]})
    if gdf.empty:
        return gdf
    gdf = gdf.reset_index(drop=False)
    mask_line = gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])
    return gdf.loc[mask_line].copy()


def fetch_parks(polygon: BaseGeometry) -> gpd.GeoDataFrame:
    tag_sets: list[dict[str, Any]] = [
        {"leisure": "park"},
        {"leisure": "nature_reserve"},
        {"leisure": "recreation_ground"},
        {"landuse": "recreation_ground"},
    ]
    parts: list[gpd.GeoDataFrame] = []
    for tags in tag_sets:
        gdf = _features_from_polygon(polygon, tags)
        if not gdf.empty:
            parts.append(gdf.reset_index(drop=False))
    if not parts:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    merged = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)
    mask_poly = merged.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
    return merged.loc[mask_poly]


def _add_geometry_to_modelspace(msp, geom: BaseGeometry, layer: str) -> int:
    if geom is None or geom.is_empty:
        return 0

    t = geom.geom_type
    if t == "LineString":
        xy = [(float(x), float(y)) for x, y, *_ in geom.coords]
        if len(xy) >= 2:
            msp.add_lwpolyline(xy, dxfattribs={"layer": layer})
            return 1
        return 0

    if t == "MultiLineString":
        return sum(_add_geometry_to_modelspace(msp, g, layer) for g in geom.geoms)

    if t == "Polygon":
        n = 0
        ext = [(float(x), float(y)) for x, y, *_ in geom.exterior.coords]
        if len(ext) >= 3:
            msp.add_lwpolyline(ext, dxfattribs={"layer": layer}, close=True)
            n += 1
        for ring in geom.interiors:
            hole = [(float(x), float(y)) for x, y, *_ in ring.coords]
            if len(hole) >= 3:
                msp.add_lwpolyline(hole, dxfattribs={"layer": layer}, close=True)
                n += 1
        return n

    if t == "MultiPolygon":
        return sum(_add_geometry_to_modelspace(msp, g, layer) for g in geom.geoms)

    if t == "GeometryCollection":
        return sum(_add_geometry_to_modelspace(msp, g, layer) for g in geom.geoms)

    return 0


def apply_local_origin(
    layer_gdfs: dict[str, gpd.GeoDataFrame],
    subtract_xy: tuple[float, float] | None = None,
) -> tuple[dict[str, gpd.GeoDataFrame], tuple[float, float]]:
    """
    Shift all projected coordinates so the drawing sits near the origin.

    CityCAD rejects or clips geometry when coordinates are in global UTM space
    (e.g. X~350000, Y~4750000). Subtracting the data's minimum easting/northing
    keeps shape and scale identical while values become O(city size).
    """
    if subtract_xy is not None:
        ox_val, oy_val = float(subtract_xy[0]), float(subtract_xy[1])
    else:
        mins: list[tuple[float, float]] = []
        for gdf in layer_gdfs.values():
            if gdf is not None and not gdf.empty:
                b = gdf.total_bounds
                mins.append((float(b[0]), float(b[1])))
        if not mins:
            return layer_gdfs, (0.0, 0.0)
        ox_val = min(t[0] for t in mins)
        oy_val = min(t[1] for t in mins)

    if ox_val == 0.0 and oy_val == 0.0:
        return layer_gdfs, (0.0, 0.0)

    out: dict[str, gpd.GeoDataFrame] = {}
    for name, gdf in layer_gdfs.items():
        if gdf is None or gdf.empty:
            out[name] = gdf
            continue
        g2 = gdf.copy()
        g2["geometry"] = g2["geometry"].apply(lambda g: translate(g, xoff=-ox_val, yoff=-oy_val))
        out[name] = g2
    return out, (ox_val, oy_val)


def write_dxf(
    layer_to_gdf: dict[str, gpd.GeoDataFrame],
    output_path: Path,
    dxf_version: str = "R2010",
) -> dict[str, int]:
    doc = ezdxf.new(dxfversion=dxf_version, setup=True)
    doc.header["$INSUNITS"] = units.M
    doc.header["$LUNITS"] = 2
    doc.header["$AUNITS"] = 0

    for name in DXF_LAYERS:
        if not doc.layers.has_entry(name):
            doc.layers.add(name)

    msp = doc.modelspace()
    stats: dict[str, int] = {}

    for layer_name, gdf in layer_to_gdf.items():
        n_ent = 0
        if gdf is None or gdf.empty:
            stats[layer_name] = 0
            continue
        for geom in gdf.geometry:
            n_ent += _add_geometry_to_modelspace(msp, geom, layer_name)
        stats[layer_name] = n_ent
        logger.info("DXF layer %r: %d entities", layer_name, n_ent)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(output_path))
    return stats


def run_pipeline(config: dict[str, Any], project_root: Path | None = None) -> Path:
    """Full fetch -> project/simplify -> DXF.  Returns path to written DXF."""
    project_root = project_root or Path.cwd()
    configure_osmnx(config)

    target_crs = config.get("target_crs")
    tol = float(config.get("simplify_tolerance_meters", 0.0))
    layers_on: dict[str, bool] = config.get("layers", {})

    clip = get_clip_polygon(config)
    if clip.geom_type not in ("Polygon", "MultiPolygon"):
        clip = unary_union(clip)
    if clip.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError(
            f"Clip geometry must be a polygon or multipolygon; got {clip.geom_type!r}. "
            "Try a tighter bbox or a polygon_wkt that covers only the area of interest."
        )

    bounds = clip.bounds
    if target_crs is None:
        center_lon = (bounds[0] + bounds[2]) / 2.0
        center_lat = (bounds[1] + bounds[3]) / 2.0
        target_crs = utm_epsg_from_lonlat(center_lon, center_lat)
        logger.info("Auto-detected CRS: %s", target_crs)

    logger.info("Clip bounds WGS84 (west, south, east, north): %.6f, %.6f, %.6f, %.6f", *bounds)

    layer_gdfs: dict[str, gpd.GeoDataFrame] = {}

    try:
        if layers_on.get("roads_main") or layers_on.get("roads_local"):
            logger.info("Downloading roads ...")
            main_r, local_r = fetch_roads(clip, config)
            if layers_on.get("roads_main"):
                layer_gdfs["roads_main"] = _project_and_simplify(main_r, target_crs, tol, clip)
            if layers_on.get("roads_local"):
                layer_gdfs["roads_local"] = _project_and_simplify(local_r, target_crs, tol, clip)

        if layers_on.get("buildings"):
            logger.info("Downloading buildings ...")
            b = fetch_buildings(clip)
            layer_gdfs["buildings"] = _project_and_simplify(b, target_crs, tol, clip)

        if layers_on.get("water"):
            logger.info("Downloading water features ...")
            w = fetch_water(clip)
            layer_gdfs["water"] = _project_and_simplify(w, target_crs, tol, clip)

        if layers_on.get("rail"):
            logger.info("Downloading rail ...")
            r = fetch_rail(clip)
            layer_gdfs["rail"] = _project_and_simplify(r, target_crs, tol, clip)

        if layers_on.get("parks"):
            logger.info("Downloading parks ...")
            p = fetch_parks(clip)
            layer_gdfs["parks"] = _project_and_simplify(p, target_crs, tol, clip)

    except Exception as exc:
        logger.exception("Failed while downloading or processing OSM features.")
        raise RuntimeError(
            "OSM fetch/processing failed. Check network, Overpass status, and clip area size."
        ) from exc

    out_dir = project_root / config.get("output_dir", ".")
    out_name = config.get("output_filename", "citycad_export.dxf")
    out_path = out_dir / out_name

    if config.get("translate_to_local_origin", True):
        manual = config.get("local_origin_subtract_meters")
        sub: tuple[float, float] | None = None
        if manual is not None and len(manual) == 2:
            sub = (float(manual[0]), float(manual[1]))
        layer_gdfs, (origin_x, origin_y) = apply_local_origin(layer_gdfs, subtract_xy=sub)
        logger.info(
            "Local origin shift: easting=%.3f  northing=%.3f m (add back to recover projected coords).",
            origin_x,
            origin_y,
        )

    write_dxf(layer_gdfs, out_path, dxf_version=str(config.get("dxf_version", "R2010")))
    logger.info("Wrote DXF: %s", out_path.resolve())
    return out_path
