"""
Command-line interface for osm2citycad.

Usage examples:
  osm2citycad 48.8584 2.2945
  osm2citycad 48.8584 2.2945 --width 3 --height 3
  osm2citycad --bbox 2.28,48.85,2.30,48.87
  osm2citycad --url "https://www.google.com/maps/place/Eiffel+Tower/@48.8583701,2.2919064,17z/"
  osm2citycad --config my_config.json
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import __version__
from .converter import (
    ALL_LAYER_NAMES,
    CITYCAD_MAX_KM,
    DEFAULT_SIZE_KM,
    DXF_LAYERS,
    LAYER_ALIASES,
    bbox_from_center,
    configure_osmnx,
    get_clip_polygon,
    load_config,
    run_pipeline,
    utm_epsg_from_lonlat,
    validate_area_size,
)
from .url_parser import parse_map_url

logger = logging.getLogger("osm2citycad")

_DEFAULT_LAYERS = {
    "roads_main": True,
    "roads_local": True,
    "buildings": True,
    "water": True,
    "rail": True,
    "parks": True,
}

_DEFAULT_ROADS = {
    "include_service": False,
    "main_highway_types": [
        "motorway", "motorway_link",
        "trunk", "trunk_link",
        "primary", "primary_link",
        "secondary", "secondary_link",
    ],
    "local_highway_types": [
        "tertiary", "tertiary_link",
        "residential", "living_street",
        "unclassified", "road",
    ],
    "service_highway_types": ["service", "track"],
}


def _resolve_layer_names(names: list[str]) -> set[str]:
    """Expand user-supplied layer names, supporting aliases like 'roads'."""
    result: set[str] = set()
    all_valid = ALL_LAYER_NAMES | set(LAYER_ALIASES.keys())
    for name in names:
        key = name.strip().lower()
        if key in LAYER_ALIASES:
            result.update(LAYER_ALIASES[key])
        elif key in ALL_LAYER_NAMES:
            result.add(key)
        else:
            raise ValueError(
                f"Unknown layer: {name!r}. "
                f"Available: {', '.join(sorted(all_valid))}"
            )
    return result


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not verbose:
        logging.getLogger("ezdxf").setLevel(logging.WARNING)
        logging.getLogger("OSMnx").setLevel(logging.WARNING)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="osm2citycad",
        description=(
            "Fetch OpenStreetMap data and convert to layered DXF files for CityCAD.\n\n"
            "Provide a location as lat/lon, a bounding box, a map URL, or a config file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  osm2citycad 48.8584 2.2945                          # Eiffel Tower, 5x5 km default\n"
            "  osm2citycad 48.8584 2.2945 --width 2 --height 2     # 2x2 km area\n"
            "  osm2citycad 48.8584 2.2945 --layers roads,buildings  # only roads + buildings\n"
            "  osm2citycad 48.8584 2.2945 --exclude-layers parks    # everything except parks\n"
            "  osm2citycad --bbox 2.28,48.85,2.30,48.87             # explicit bounding box\n"
            '  osm2citycad --url "https://www.google.com/maps/place/.../@48.858,2.294,17z/"\n'
            "  osm2citycad --config advanced.json                    # full config file\n"
        ),
    )

    parser.add_argument("lat", nargs="?", type=float, help="Center latitude (decimal degrees).")
    parser.add_argument("lon", nargs="?", type=float, help="Center longitude (decimal degrees).")

    parser.add_argument(
        "--width", type=float, default=None,
        help=f"Area width in km (default: {DEFAULT_SIZE_KM}). Max: {CITYCAD_MAX_KM}.",
    )
    parser.add_argument(
        "--height", type=float, default=None,
        help=f"Area height in km (default: {DEFAULT_SIZE_KM}). Max: {CITYCAD_MAX_KM}.",
    )

    parser.add_argument(
        "--bbox", type=str, default=None,
        help="Bounding box as west,south,east,north (decimal degrees). Overrides lat/lon + size.",
    )

    parser.add_argument(
        "--url", type=str, default=None,
        help="Google Maps or OpenStreetMap URL to extract coordinates from.",
    )

    parser.add_argument(
        "--config", type=Path, default=None,
        help="Path to a JSON config file for advanced settings (layers, road types, etc.).",
    )

    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output DXF file path (default: ./osm2citycad_output.dxf).",
    )

    parser.add_argument(
        "--simplify", type=float, default=2.0,
        help="Geometry simplification tolerance in meters (default: 2.0, 0 to disable).",
    )

    layer_names_help = ", ".join(sorted(ALL_LAYER_NAMES | set(LAYER_ALIASES.keys())))
    parser.add_argument(
        "--layers", type=str, default=None,
        help=(
            f"Only include these layers (comma-separated). "
            f"Available: {layer_names_help}. "
            f"'roads' is shorthand for roads_main,roads_local."
        ),
    )
    parser.add_argument(
        "--exclude-layers", type=str, default=None,
        help=(
            f"Exclude these layers (comma-separated). "
            f"Available: {layer_names_help}. "
            f"'roads' is shorthand for roads_main,roads_local."
        ),
    )

    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    parser.add_argument("--version", action="version", version=f"osm2citycad {__version__}")

    return parser


def _config_from_cli(args: argparse.Namespace) -> dict:
    """Build a pipeline config dict from CLI arguments, optionally merging a config file."""

    # Start from config file if provided, otherwise defaults
    if args.config and args.config.is_file():
        config = load_config(args.config)
        logger.info("Loaded config from %s", args.config)
    else:
        config = {}

    config.setdefault("layers", dict(_DEFAULT_LAYERS))
    config.setdefault("roads", _DEFAULT_ROADS)

    # --layers: additive (only these)
    if args.layers:
        include = _resolve_layer_names(args.layers.split(","))
        config["layers"] = {name: (name in include) for name in DXF_LAYERS}
        enabled = [n for n, on in config["layers"].items() if on]
        logger.info("Layers (include only): %s", ", ".join(enabled))

    # --exclude-layers: subtractive (all except these)
    if args.exclude_layers:
        if args.layers:
            raise ValueError("Cannot use both --layers and --exclude-layers at the same time.")
        exclude = _resolve_layer_names(args.exclude_layers.split(","))
        for name in exclude:
            config["layers"][name] = False
        enabled = [n for n, on in config["layers"].items() if on]
        logger.info("Layers (after exclusions): %s", ", ".join(enabled))
    config.setdefault("translate_to_local_origin", True)
    config.setdefault("dxf_version", "R2010")
    config.setdefault("use_osmnx_cache", True)
    config.setdefault("overpass_url", "https://z.overpass-api.de/api")
    config.setdefault("overpass_timeout_seconds", 300)
    config.setdefault("overpass_memory_gb", 2)

    # Determine location
    lat, lon, bbox_dict = None, None, None

    if args.url:
        logger.info("Parsing URL: %s", args.url)
        loc = parse_map_url(args.url)
        lat, lon = loc.lat, loc.lon
        if loc.bbox:
            bbox_dict = loc.bbox
            logger.info("Extracted bounding box from URL.")
        logger.info("Coordinates from URL: %.6f, %.6f", lat, lon)

    elif args.bbox:
        parts = [float(x.strip()) for x in args.bbox.split(",")]
        if len(parts) != 4:
            raise ValueError("--bbox requires exactly 4 comma-separated values: west,south,east,north")
        bbox_dict = {"west": parts[0], "south": parts[1], "east": parts[2], "north": parts[3]}

    elif args.lat is not None and args.lon is not None:
        lat, lon = args.lat, args.lon
    elif "bbox" not in config and "place_name" not in config and "polygon_wkt" not in config:
        raise ValueError(
            "No location specified. Provide lat/lon, --bbox, --url, or a --config with location info.\n"
            "Run 'osm2citycad --help' for usage."
        )

    # Build bbox from center point if we have lat/lon but no explicit bbox
    if bbox_dict is None and lat is not None:
        width_km = args.width or DEFAULT_SIZE_KM
        height_km = args.height or DEFAULT_SIZE_KM
        bbox_dict = bbox_from_center(lat, lon, width_km, height_km)
        logger.info("Area: %.1f km x %.1f km centered on (%.6f, %.6f)", width_km, height_km, lat, lon)

    if bbox_dict is not None:
        w_km, h_km = validate_area_size(bbox_dict)
        logger.info("Bounding box size: %.1f km x %.1f km", w_km, h_km)
        config["bbox"] = bbox_dict
        # Clear place_name / polygon_wkt so bbox takes priority
        config.pop("place_name", None)
        config.pop("polygon_wkt", None)

    # Auto-detect CRS if not set
    if "target_crs" not in config or config.get("target_crs") is None:
        if bbox_dict:
            center_lon = (bbox_dict["west"] + bbox_dict["east"]) / 2.0
            center_lat = (bbox_dict["south"] + bbox_dict["north"]) / 2.0
        elif lat is not None:
            center_lat, center_lon = lat, lon
        else:
            center_lat, center_lon = None, None

        if center_lat is not None:
            config["target_crs"] = utm_epsg_from_lonlat(center_lon, center_lat)
            logger.info("Auto-detected CRS: %s", config["target_crs"])

    config["simplify_tolerance_meters"] = args.simplify

    # Output path
    if args.output:
        config["output_dir"] = str(args.output.parent)
        config["output_filename"] = args.output.name
    else:
        config.setdefault("output_dir", ".")
        config.setdefault("output_filename", "osm2citycad_output.dxf")

    return config


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        config = _config_from_cli(args)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    configure_osmnx(config)

    t0 = time.time()
    try:
        out_path = run_pipeline(config, project_root=Path.cwd())
    except Exception as exc:
        logger.error("%s", exc)
        return 1

    elapsed = time.time() - t0
    logger.info("Done in %.1f s.  Output: %s", elapsed, out_path.resolve())
    return 0


def cli_entry() -> None:
    """Entry point for the console_scripts wrapper."""
    sys.exit(main())


if __name__ == "__main__":
    cli_entry()
