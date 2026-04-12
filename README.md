# osm2citycad

Fetch [OpenStreetMap](https://www.openstreetmap.org/) data for any location and convert it to layered **DXF** files ready to import into [CityCAD](https://www.citycad.co.uk/).

> **Disclaimer:** This project is not affiliated with, endorsed by, or sponsored by CityCAD Technologies Ltd. "CityCAD" is a trademark of CityCAD Technologies Ltd.

## Features

- **Point a location, get a DXF** -- provide a latitude/longitude, a bounding box, or paste a Google Maps / OpenStreetMap URL.
- **Auto-sized for CityCAD** -- defaults to a 5 km × 5 km area (CityCAD supports up to 50 km × 50 km); warns if your requested area is too large.
- **Auto-detects the right coordinate system** -- picks the correct UTM zone for any location on Earth.
- **Six categorised layers** -- `roads_main`, `roads_local`, `buildings`, `water`, `rail`, `parks`. Include only the layers you need with `--layers`, or drop ones you don't with `--exclude-layers`.
- **CityCAD-optimised** -- shifts coordinates to a local origin so they work in CityCAD's ground plane, sets DXF units to meters, writes clean 2D LWPolyline entities.
- **Configurable** -- quick use via CLI flags, or provide a JSON config file for advanced control over road types, layer toggles, simplification, Overpass settings, and more.

## Installation

Requires **Python 3.10+**.

```bash
pip install osm2citycad
```

Or install from source:

```bash
git clone https://github.com/simondorfman/osm2citycad.git
cd osm2citycad
pip install -e .
```

## Quick start

### By coordinates

```bash
# Eiffel Tower area, default 5x5 km
osm2citycad 48.8584 2.2945

# Custom size: 2x2 km
osm2citycad 48.8584 2.2945 --width 2 --height 2

# Specify output file
osm2citycad 48.8584 2.2945 -o my_export.dxf
```

### Choosing layers

By default all six layers are downloaded. You can speed things up by requesting only the layers you need, or excluding the ones you don't:

```bash
# Only roads and buildings (additive)
osm2citycad 48.8584 2.2945 --layers roads,buildings

# Everything except parks and rail (subtractive)
osm2citycad 48.8584 2.2945 --exclude-layers parks,rail

# Just buildings
osm2citycad 48.8584 2.2945 --layers buildings
```

Available layer names: `roads_main`, `roads_local`, `buildings`, `water`, `rail`, `parks`. Use `roads` as shorthand for both `roads_main` and `roads_local`.

### By bounding box

```bash
# Explicit corners: west,south,east,north
osm2citycad --bbox 2.28,48.85,2.31,48.87
```

### From a URL

Paste a Google Maps or OpenStreetMap URL and osm2citycad will extract the coordinates for you:

```bash
# Google Maps -- lat/lon is parsed from the /@lat,lon,zoom portion
osm2citycad --url "https://www.google.com/maps/place/Eiffel+Tower/@48.8583701,2.2919064,17z/"

# OpenStreetMap search -- uses the #map=zoom/lat/lon fragment, and if
# minlon/minlat/maxlon/maxlat query params are present, uses those as the bbox
osm2citycad --url "https://www.openstreetmap.org/search?query=eiffel+tower&zoom=19&minlon=-115.25087088346484&minlat=36.262746990432234&maxlon=-115.2483978867531&maxlat=36.264003509072545#map=18/48.858260/2.294501"

# OpenStreetMap element (node, way, or relation) -- resolved via the OSM API
# to get the element's centroid
osm2citycad --url "https://www.openstreetmap.org/way/5013364"
```

### With a config file (advanced)

For full control over layers, road classification, simplification tolerance, Overpass settings, and more:

```bash
osm2citycad --config config.json
```

See [`config.example.json`](config.example.json) for all available options. CLI flags (like `--bbox` or lat/lon) override the config file's location settings.

## CLI reference

```
osm2citycad [LAT LON] [options]

Positional:
  LAT LON              Center latitude and longitude (decimal degrees)

Location options (one required unless using --config with location info):
  --bbox W,S,E,N       Bounding box (west,south,east,north in decimal degrees)
  --url URL            Google Maps or OpenStreetMap URL

Size options:
  --width KM           Area width in km (default: 5.0, max: 50.0)
  --height KM          Area height in km (default: 5.0, max: 50.0)

Layer options:
  --layers LIST        Only include these layers (comma-separated)
  --exclude-layers LIST  Exclude these layers (comma-separated)

Output:
  -o, --output PATH    Output DXF path (default: ./osm2citycad_output.dxf)
  --simplify METERS    Simplification tolerance (default: 2.0, 0 to disable)

Advanced:
  --config FILE        JSON config for layers, road types, etc.
  -v, --verbose        Debug logging
  --version            Show version
```

## CityCAD import tips

1. **Scale:** When importing the DXF, set the scale so that 1 drawing unit = 1 meter (the file is tagged with `$INSUNITS = meters`). If your CityCAD model uses feet, set 1 DXF unit = 3.281 ft.
2. **Insertion point:** The DXF is shifted to a local origin (near 0,0) by default. Use "Reset to Zero" or map the center to your model origin.
3. **Ground plane:** If the DXF extends beyond the default ground plane, enlarge it in Display Preferences (up to 50 km × 50 km).

## How it works

1. **Parse location** from CLI args, URL, or config file.
2. **Download** vector features from OpenStreetMap via [OSMnx](https://github.com/gboeing/osmnx) (Overpass API), clipped to the bounding area.
3. **Classify** roads by `highway=*` tag into main vs. local; fetch buildings, water, rail, and parks using conservative tag presets.
4. **Reproject** from WGS84 to the appropriate UTM zone (auto-detected, or manually specified).
5. **Simplify** geometry in meters to reduce vertex count for CAD.
6. **Shift** coordinates to a local origin so values are in the range CityCAD expects.
7. **Write** 2D LWPolyline entities on named layers via [ezdxf](https://github.com/mozman/ezdxf).

## Configuration file

The config file is optional and provides control over settings that don't have CLI flags:

| Key | Purpose |
|-----|---------|
| `layers` | Toggle individual layers on/off (`roads_main`, `roads_local`, `buildings`, `water`, `rail`, `parks`) |
| `roads.main_highway_types` | OSM `highway=*` values classified as main roads |
| `roads.local_highway_types` | OSM `highway=*` values classified as local roads |
| `roads.include_service` | Include service roads and tracks (default: false) |
| `simplify_tolerance_meters` | Douglas–Peucker simplification tolerance in meters |
| `target_crs` | Override the auto-detected UTM zone (e.g. `EPSG:32631`) |
| `translate_to_local_origin` | Shift coordinates to near-zero origin (default: true) |
| `dxf_version` | DXF version string (default: `R2010`) |
| `overpass_url` | Custom Overpass API endpoint |
| `overpass_timeout_seconds` | Overpass query timeout |
| `overpass_memory_gb` | Overpass memory limit hint |

## Data sources and attribution

This tool downloads data from [OpenStreetMap](https://www.openstreetmap.org/) via the [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API). OpenStreetMap data is © OpenStreetMap contributors, available under the [Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/).

If you publish maps or products derived from this tool's output, you must credit OpenStreetMap per their [attribution guidelines](https://www.openstreetmap.org/copyright).

Place-name geocoding uses [Nominatim](https://nominatim.org/). Please respect the [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/).

## Dependencies and licenses

All dependencies use permissive licenses compatible with GPL-3.0:

| Package | License | Purpose |
|---------|---------|---------|
| [OSMnx](https://github.com/gboeing/osmnx) | MIT | OpenStreetMap data download |
| [GeoPandas](https://geopandas.org/) | BSD-3-Clause | Geospatial data manipulation |
| [Shapely](https://shapely.readthedocs.io/) | BSD-3-Clause | Geometry operations |
| [ezdxf](https://github.com/mozman/ezdxf) | MIT | DXF file creation |
| [NumPy](https://numpy.org/) | BSD-3-Clause | Numerical computing |
| [pandas](https://pandas.pydata.org/) | BSD-3-Clause | Data structures |
| [Requests](https://requests.readthedocs.io/) | Apache-2.0 | HTTP client |

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE) or later.
