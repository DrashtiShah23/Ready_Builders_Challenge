# Data Sourcing

Mapping every dataset used in this pipeline to a specific Starlink install-guide obstruction factor, with version pins for reproducibility.

## Dataset → Obstruction-factor mapping

| Dataset | Source | Install-guide obstruction factor | Why this dataset |
|---|---|---|---|
| NLCD 2021 Tree Canopy Cover (USGS / MRLC) | https://www.mrlc.gov/data | Tree branches (primary obstruction named in the install guide) | Continuous 0–100% canopy density at 30m, nationally consistent, version-pinned by year. The only nationally available dataset that gives a continuous canopy signal rather than a binary forest/non-forest classification. |
| USGS 3DEP Elevation | https://www.usgs.gov/3d-elevation-program | Terrain blocking the 25° minimum elevation angle / 100–110° FOV cone | Only consistently national DEM at 10–30m resolution. Slope and aspect derived via Horn's method directly model how much sky a dish loses at each location. |
| NLCD 2021 Land Cover (USGS / MRLC) | https://www.mrlc.gov/data | Structural-density context (developed codes) + cross-validation of TCC (forest codes) | Same dataset family as TCC: same CRS (EPSG:5070), same resolution (30m), same download. Zero additional infrastructure for a meaningful signal. |

## Version pins

- NLCD TCC: coverage id `mrlc_download__nlcd_tcc_conus_2021_v2021-4` (pinned in `src/config.py:MRLC_TCC_COVERAGE_ID`)
- NLCD Land Cover: coverage id `mrlc_download__NLCD_2021_Land_Cover_L48` (pinned in `src/config.py:MRLC_LANDCOVER_COVERAGE_ID`)
- USGS 3DEP: 1 arc-second GeoTIFF tiles, requested via the TNM `bbox` filter. Tile-level URLs are not version-pinned by USGS — the downloader keeps the most-recent vintage per 1° quad and captures the resolved URLs in the JSONL run log under `DEM_BBOX_QUERY_OK` and `HTTP_DOWNLOAD_DONE` events.

## MRLC bulk zips → WCS (migrated May 2026)

The downloader used to fetch the two NLCD layers as national zips from MRLC's S3 bucket:

- `https://s3-us-west-2.amazonaws.com/mrlc/nlcd_tcc_conus_2021_v2021-4.zip`
- `https://s3-us-west-2.amazonaws.com/mrlc/nlcd_land_cover_l48_2021_20230630.zip`

Both URLs began returning HTTP 403 `AccessDenied` in May 2026; the MRLC bucket policy now blocks anonymous bulk-zip downloads. The downloader was migrated to MRLC's WCS (OGC Web Coverage Service) endpoint at `https://www.mrlc.gov/geoserver/mrlc_download/wcs` — the same source rasters with the same provenance, served as queryable coverages instead of national zips.

The migration is also an architectural win independent of the bucket lockdown. Concrete numbers for the NC use case:

| | National zip (former) | WCS NC subset (current) |
|---|---|---|
| TCC download size | ~3 GB extracted | ~50-150 MB |
| Land Cover download size | ~3 GB extracted | ~50-150 MB |
| Bytes used by the NC-only pipeline | ~0.4 % | ~100 % |
| Re-pull required to scope to a different state | Yes (same 3 GB twice) | No (rerun with new `--states`) |
| Failure mode if a single URL breaks | Whole pipeline blocked | Single coverage retried via OGC service |

WCS is also the OGC-standard interface MRLC's own viewer uses, so it's the least-likely-to-disappear path going forward. See `src/data/downloader.py` for the implementation and `AI_TOOLS.md` § "Phase 8 follow-up — MRLC source migration" for the decision log.

## TNM `polyType=state` → `bbox` (migrated May 2026)

The downloader used to query USGS TNM with `polyType=state&polyCode=<FIPS>`. That filter stopped working in May 2026 — `polyCode=37` (NC) returns ~200 tiles, all of them in Oregon/Idaho (lat 41-44, lon -117 to -119). Verified by probing several FIPS codes; the upstream filter is dead, not a code bug on our side.

The downloader now uses TNM's `bbox=lon_min,lat_min,lon_max,lat_max` filter — the same query path TNM's own viewer uses internally. State-to-bbox mapping lives in `src/config.py:STATE_BBOX_WGS84`. NC's bbox is `(-84.32, 33.75, -75.46, 36.59)`, the same one the orchestrator's geographic-sanity validation check uses.

TNM's bbox endpoint serves up to ``max`` items in a single page (default 50, capped server-side around the low hundreds). The full NC catalogue contains ~126 raw items (3+ vintages × ~38 land quads), so the default 50 was clipping the western mountain quads off the page. The downloader requests ``max=200`` (`config.TNM_PAGE_SIZE`), which captures the full NC catalogue, then dedupes by ``(round(min_lat), round(min_lon))`` keeping the latest-vintage tile per 1° quad. Server-side pagination via ``offset`` is currently flaky (``total=0`` after the first page), so the single-large-request approach is the path with the cleanest failure mode.

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
