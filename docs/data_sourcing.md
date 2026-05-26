# Data Sourcing

Mapping every dataset used in this pipeline to a specific Starlink install-guide obstruction factor, with version pins for reproducibility.

## Dataset → Obstruction-factor mapping

| Dataset | Source | Install-guide obstruction factor | Why this dataset |
|---|---|---|---|
| NLCD 2021 Tree Canopy Cover (USGS / MRLC) | https://www.mrlc.gov/data | Tree branches (primary obstruction named in the install guide) | Continuous 0–100% canopy density at 30m, nationally consistent, version-pinned by year. The only nationally available dataset that gives a continuous canopy signal rather than a binary forest/non-forest classification. |
| USGS 3DEP Elevation | https://www.usgs.gov/3d-elevation-program | Terrain blocking the 25° minimum elevation angle / 100–110° FOV cone | Only consistently national DEM at 10–30m resolution. Slope and aspect derived via Horn's method directly model how much sky a dish loses at each location. |
| NLCD 2021 Land Cover (USGS / MRLC) | https://www.mrlc.gov/data | Structural-density context (developed codes) + cross-validation of TCC (forest codes) | Same dataset family as TCC: same CRS (EPSG:5070), same resolution (30m), same download. Zero additional infrastructure for a meaningful signal. |

## Version pins

- NLCD TCC: `nlcd_tcc_conus_2021_v2021-4` (URL pinned in `src/config.py:CANOPY_RASTER_URL`)
- NLCD Land Cover: `nlcd_land_cover_l48_2021_20230630` (URL pinned in `src/config.py:LANDCOVER_RASTER_URL`)
- USGS 3DEP: 1 arc-second tiles, downloaded per-state bounding box via the USGS National Map API. Tile-level URLs are not version-pinned by USGS — captured at download time in `logs/`.

## CRS strategy

All NLCD rasters are stored in **EPSG:5070** (Albers Equal Area for CONUS). Input locations arrive in **EPSG:4326** (WGS84 lat/lon). A single `pyproj.Transformer` instance is constructed once per process and reused for every sample, avoiding per-call transformer setup overhead.

## Known quality issues

_[To be filled after the locations CSV is received from the Ready team (OI-01).]_

Examples of issues this pipeline will flag and log with typed reason codes:

- `NULL_COORDINATE` — latitude or longitude missing
- `OUT_OF_BOUNDS` — coordinate outside the CONUS bounding box defined in `src/config.py`
- `PARSE_ERROR` — non-numeric coordinate, malformed row
- `DUPLICATE_DROPPED` — repeated `location_id`, first occurrence wins
- `INVALID_STATE` — `state` field not in the US state list

The ingestion-agent log at the end of each run prints counts per reason code, plus the total valid-row percentage.

## What cannot be modeled with public data

These factors materially affect real-world Starlink performance but cannot be captured by any nationally available public dataset. They are documented here so reviewers know what the pipeline is **not** claiming to model:

- **Exact tree heights** — TCC measures canopy area %, not height. A 90% TCC pixel could be shrubs or 100 ft pines.
- **Building heights** — no national dataset exists. OSM has footprints, not heights.
- **Seasonal canopy variation** — NLCD TCC is a peak-summer 2021 snapshot; deciduous trees shed leaves in winter.
- **Sub-30m obstructions** — a single tall tree on a property edge may not register in a 30m pixel.
- **Microsite conditions** — rooftop access, mounting options, HOA restrictions, landlord permission.
- **Temporary obstructions** — construction cranes, seasonal scaffolding, parked vehicles.
