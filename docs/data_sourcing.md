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

### Ingestion-level data quality flags

The ingestion-agent log at the end of each run prints counts per reason code, plus the total valid-row percentage.

- `NULL_COORDINATE` — latitude or longitude missing
- `OUT_OF_BOUNDS` — coordinate outside the CONUS bounding box defined in `src/config.py`
- `PARSE_ERROR` — non-numeric coordinate, malformed row
- `DUPLICATE_DROPPED` — repeated `location_id`, first occurrence wins
- `INVALID_STATE` — `state` field not in the US state list

### NLCD TCC NoData on non-tree land cover classes (discovered May 2026)

The first real-data pipeline run on 10,000 NC locations surfaced a **33.29% TCC-missing rate**, well above the 10% threshold that flips the validator's `missing_data_rate` check from `passed` to `warning`. Investigation confirmed this is **not** a raster coverage gap — it is NLCD TCC's documented behavior: TCC is only computed for pixels where tree canopy is a meaningful component of the land surface. The per-class missing rates from the 10k-NC sample show the pattern unambiguously:

| NLCD code | Class | TCC-missing rate |
|---|---|---:|
| 11 | Open Water | 87.5% |
| 21 | Developed, Open Space | 20.9% |
| 22 | Developed, Low Intensity | 21.3% |
| 23 | Developed, Medium Intensity | 50.9% |
| 24 | Developed, High Intensity | 86.7% |
| 31 | Barren Land | 86.7% |
| 41/42/43 | Forest (Deciduous / Evergreen / Mixed) | 0–20.0% |
| 52 | Shrub/Scrub | 33.1% |
| 71 | Grassland/Herbaceous | 78.1% |
| 81 | Pasture/Hay | 68.4% |
| 82 | Cultivated Crops | 88.0% |
| 90 | Woody Wetlands | 19.3% |
| 95 | Emergent Herbaceous Wetlands | 71.4% |

The signal: as urban density rises (21 → 24) or as the class becomes inherently non-tree (water, barren, crops, herbaceous), NoData rises with it. Forested classes (41/42/43) are near-complete, woody wetlands are near-complete, and developed open space — which routinely has scattered trees — is also near-complete. This is the expected behavior of any "tree canopy %" raster.

Every null-TCC row in the 10k-NC run carried the `TCC_MISSING` env-fetch flag from the EnvironmentalAgent, confirming the value came back NoData from the raster rather than being silently dropped.

**Scoring implication (real bias to be aware of, not a bug to fix yet).** `score_components` in `src/agents/scoring.py` treats `tcc_pct is None` as `tcc_score = 0.0` — the most-favorable bucket. A null-TCC row's composite is therefore capped at `0.0 × 0.50 + 1.0 × 0.30 + 1.0 × 0.20 = 0.50`, which is **below the 0.60 High threshold** under every other combination of inputs. Concretely from the same 10k-NC run:

| Subset | Rows | High | Moderate | Low | High % |
|---|---:|---:|---:|---:|---:|
| Null TCC | 3,329 | 0 | 1 | 3,328 | 0.00% |
| Non-null TCC | 6,671 | 584 | 3,438 | 2,649 | 8.75% |
| All | 10,000 | 584 | 3,439 | 5,977 | 5.84% |

Restricting the High-share calculation to rows with TCC data gives **8.75% High**, vs **5.84%** across the full sample. The headline number is conservative by ~3 percentage points on a sample that is heavily Developed (coastal NC, ~62% developed codes), and would be even further understated on a sample with more Cultivated Crops or Pasture coverage.

Three reasons we are not fixing this in Phase 9:

1. **Mapping `tcc_pct is None` → `_SCORE_LOW` is a defensible default** for non-tree land cover. A pixel classified as Open Water or Developed High Intensity genuinely has near-zero canopy obstruction, so giving it `tcc_score = 0` is closer to truth than refusing to score it.
2. **The classes where TCC NoData is most concerning** — Pasture/Hay (81), Grassland (71), Emergent Wetlands (95), Cultivated Crops (82) — are already captured by the **land cover score** at 20% of the composite weight. Those classes map to `_SCORE_LOW` in the landcover bucket as well, so the composite is internally consistent: no canopy expected, no canopy scored, no landcover risk scored.
3. **A future refinement (Phase 12 candidate)** is to re-normalise the weights when TCC is null: `composite = (terrain × 0.30 + landcover × 0.20) / 0.50` would lift a steep-and-forested null-TCC row to its proper High tier. This requires (a) cross-validating against ground-truth installs to verify that the lift improves operational decisions, and (b) extending the report to flag every "TCC-derived" tier separately so a broadband officer can see when a High classification rests on incomplete data.

For now, every analysis report includes the per-signal missing rate table in its Data Quality Summary section, and the validator emits a `proceed_with_caveats` recommendation whenever any signal exceeds 10% missing — so the conservatism is surfaced, not hidden.

## What cannot be modeled with public data

These factors materially affect real-world Starlink performance but cannot be captured by any nationally available public dataset. They are documented here so reviewers know what the pipeline is **not** claiming to model:

- **Exact tree heights** — TCC measures canopy area %, not height. A 90% TCC pixel could be shrubs or 100 ft pines.
- **Building heights** — no national dataset exists. OSM has footprints, not heights.
- **Seasonal canopy variation** — NLCD TCC is a peak-summer 2021 snapshot; deciduous trees shed leaves in winter.
- **Sub-30m obstructions** — a single tall tree on a property edge may not register in a 30m pixel.
- **Microsite conditions** — rooftop access, mounting options, HOA restrictions, landlord permission.
- **Temporary obstructions** — construction cranes, seasonal scaffolding, parked vehicles.
