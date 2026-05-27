"""
Central configuration for the LEO risk analysis pipeline.

All thresholds, paths, model parameters, and dataset codes are defined here.
Change one value here to affect the whole pipeline — nothing else in the
codebase is allowed to hardcode any of these constants.

Why centralize:
- The risk-scoring formula is one of the things a reviewer or state broadband
  officer is most likely to want to tune. Keeping every threshold and weight
  in a single file makes the formula trivially auditable and re-tunable.
- Tests can import config and use the same constants, eliminating drift
  between production behavior and test expectations.
- A future "scenario" mode (e.g., stricter thresholds for emergency-services
  customers) becomes a config swap, not a code rewrite.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- Paths ---
PROJECT_ROOT: Path = Path(__file__).parent.parent
DATA_DIR: Path = PROJECT_ROOT / "data"
RAW_DIR: Path = DATA_DIR / "raw"
TCC_DIR: Path = RAW_DIR / "tcc"
LC_DIR: Path = RAW_DIR / "landcover"
DEM_DIR: Path = RAW_DIR / "dem"
PROCESSED_DIR: Path = RAW_DIR / "processed"   # Pre-computed derived rasters (e.g. slope)
SCORED_DIR: Path = PROJECT_ROOT / "outputs" / "scored"
LOG_DIR: Path = PROJECT_ROOT / "logs"
LOCATIONS_CSV: Path = DATA_DIR / "locations.csv"

# Pre-computed slope raster — see src/data/downloader.py:precompute_slope_raster.
# Computing slope per-point at 1M locations would mean 1M 3x3 windowed rasterio reads.
# Pre-computing once = 1 GDAL operation + 1M cheap pixel lookups. ~100x faster.
SLOPE_RASTER_PATH: Path = PROCESSED_DIR / "slope_degrees.tif"

# Create runtime directories on import so first-run never fails on missing dirs.
for _d in (TCC_DIR, LC_DIR, DEM_DIR, PROCESSED_DIR, SCORED_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- API ---
ANTHROPIC_API_KEY: str | None = os.getenv("ANTHROPIC_API_KEY")

# --- Agent Configuration ---
# Pipeline-orchestration design (Phase 7 redesign): Claude makes ONE API call
# and orchestrates 5 pipeline-level tools. Each tool internally runs the full
# dataset through the existing Phase 1-6 agents. No per-batch Claude reasoning
# at scale — the per-location reasoning surface is preserved only in the
# interactive mode (single-location query, ~$0.01 per call).
CLAUDE_MODEL: str = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS: int = 4096        # Headroom for the orchestrator's end-of-run summary text.
CLAUDE_TEMPERATURE: int = 0          # Deterministic — required for reproducible scoring.
MAX_AGENT_TURNS: int = 20            # Safety cap on tool call turns in the orchestrator loop.
DEMO_SAMPLE_SIZE: int | None = None  # None = full dataset. Set to int for testing (e.g. 10_000).

# --- Full scale cost projection (documented for README) ---
# At pipeline-orchestration design (5 tools, 1 Claude call):
# Total API cost for full 4.67M row run: < $1.00
# Token usage: ~2,000 input + ~1,500 output = ~3,500 tokens total
# Compare: per-batch design at 100 locations/call = ~$10,600 for full dataset

# --- Claude pricing (USD per 1M tokens) ---
# Sourced from Anthropic's published Claude Sonnet 4 pricing. Lives in
# config so the cost-estimate log emitted on every Claude response can be
# re-tuned without re-deploying the orchestrator if pricing ever changes.
CLAUDE_INPUT_COST_PER_MTOK: float = 3.00
CLAUDE_OUTPUT_COST_PER_MTOK: float = 15.00

# --- Processing ---
# Raster sampling is done in large in-memory batches for I/O efficiency.
# Each chunk opens the raster handles once and reuses them for the whole chunk
# (see EnvironmentalAgent.enrich_batch + the per-tool dataset caches).
RASTER_BATCH_SIZE: int = 50_000

# --- Continental US bounding box (for coordinate validation) ---
CONUS_LAT_MIN: float = 24.396308
CONUS_LAT_MAX: float = 49.384358
CONUS_LON_MIN: float = -124.848974
CONUS_LON_MAX: float = -66.885444

# --- CRS ---
WGS84: str = "EPSG:4326"
ALBERS: str = "EPSG:5070"            # NLCD native CRS (Albers Equal Area for CONUS).

# --- NLCD Land Cover Codes ---
# Reference: https://www.mrlc.gov/data/legends/national-land-cover-database-class-legend-and-description
# These three groups are the basis of the land-cover component of the risk score.
FOREST_CODES: list[int] = [41, 42, 43]            # 41=Deciduous, 42=Evergreen, 43=Mixed → high obstruction risk
DEVELOPED_CODES: list[int] = [21, 22, 23, 24]     # Open Space → High Intensity Developed → moderate risk
OPEN_CODES: list[int] = [31, 52, 71, 81, 82]      # Barren, Shrub, Grassland, Pasture, Crops → low risk

# Human-readable class name lookup for ScoredLocation.land_cover_class.
NLCD_CLASS_NAMES: dict[int, str] = {
    11: "Open Water",
    12: "Perennial Ice/Snow",
    21: "Developed, Open Space",
    22: "Developed, Low Intensity",
    23: "Developed, Medium Intensity",
    24: "Developed, High Intensity",
    31: "Barren Land",
    41: "Deciduous Forest",
    42: "Evergreen Forest",
    43: "Mixed Forest",
    52: "Shrub/Scrub",
    71: "Grassland/Herbaceous",
    81: "Pasture/Hay",
    82: "Cultivated Crops",
    90: "Woody Wetlands",
    95: "Emergent Herbaceous Wetlands",
}

# --- Risk Scoring Weights (must sum to 1.0) ---
# Justification documented in docs/decision_log.md and src/agents/scoring.py.
#   TCC 50% — install guide names tree branches as the primary obstruction.
#   Terrain 30% — slope is a hard physical constraint on the 25° elevation minimum.
#   Land cover 20% — cross-validates TCC; same dataset family; zero extra infra.
TCC_WEIGHT: float = 0.50
TERRAIN_WEIGHT: float = 0.30
LANDCOVER_WEIGHT: float = 0.20

assert abs((TCC_WEIGHT + TERRAIN_WEIGHT + LANDCOVER_WEIGHT) - 1.0) < 1e-9, (
    "Risk scoring weights must sum to 1.0"
)

# --- Individual Factor Thresholds ---
# Tree Canopy Cover (NLCD TCC, 0–100%)
CANOPY_HIGH_THRESHOLD: int = 50      # >50% → score 1.0 (high obstruction risk)
CANOPY_MOD_THRESHOLD: int = 20       # 20–50% → score 0.5 (moderate risk)

# Terrain slope (degrees)
SLOPE_HIGH_THRESHOLD: float = 20.0   # >20° → score 1.0 (significantly constrains sky arc)
SLOPE_MOD_THRESHOLD: float = 10.0    # 10–20° → score 0.5 (moderate)

# --- Risk Tier Thresholds (composite score 0.0–1.0) ---
RISK_HIGH_THRESHOLD: float = 0.6
RISK_MOD_THRESHOLD: float = 0.3

# --- Raster Dataset Sources -----------------------------------------------
# Switched from the original MRLC S3 zip URLs to MRLC's WCS (OGC Web Coverage
# Service) endpoint in May 2026 after the S3 bucket policy locked down
# anonymous bulk-zip downloads (HTTP 403 on both
# ``nlcd_tcc_conus_2021_v2021-4.zip`` and
# ``nlcd_2021_land_cover_l48_20230630.zip``). The WCS service serves the
# same source rasters (same provenance, same CRS, same year/version), and
# lets the downloader request only the state subset it needs instead of
# pulling the national 3 GB zip and using 0.4% of it. See
# ``docs/data_sourcing.md`` § "MRLC bulk zips → WCS" for the full rationale
# and ``AI_TOOLS.md`` § "Phase 8 follow-up — MRLC source migration" for the
# decision log.
MRLC_WCS_BASE: str = "https://www.mrlc.gov/geoserver/mrlc_download/wcs"
MRLC_WCS_VERSION: str = "2.0.1"
# Coverage IDs are taken verbatim from the WCS ``GetCapabilities`` document;
# the prefix (``mrlc_download__``) and case are the server's, not ours.
MRLC_TCC_COVERAGE_ID: str = "mrlc_download__nlcd_tcc_conus_2021_v2021-4"
MRLC_LANDCOVER_COVERAGE_ID: str = "mrlc_download__NLCD_2021_Land_Cover_L48"

# Both NLCD coverages are served in EPSG:5070 (NAD83 Conus Albers, metres).
# Bbox subsets sent over WCS must be in this CRS, so the downloader transforms
# user-supplied WGS84 bboxes once per call before issuing the GetCoverage.
NLCD_RASTER_CRS: str = "EPSG:5070"

# DEM: tile-based, no single URL — downloaded per bounding box via the USGS
# National Map API. The original ``polyType=state&polyCode=<FIPS>`` filter
# stopped working in May 2026 (returns geographically wrong tiles, e.g.
# Oregon for ``polyCode=37``); the downloader now uses the bbox filter,
# which is the API path TNM's own viewer uses internally.

# --- CSV ingestion contract ---
EXPECTED_CSV_COLUMNS: list[str] = ["location_id", "latitude", "longitude", "state", "county"]

# --- CONUS state -> FIPS code lookup ---
# Used by src/data/downloader.py:download_dem_tiles to query the USGS National Map
# API (https://tnmaccess.nationalmap.gov) with polyType=state&polyCode=<FIPS> for
# per-state DEM tile filtering. CONUS only — AK / HI / territories excluded
# because Starlink coverage commitments and the install guide target the lower 48.
STATE_FIPS: dict[str, str] = {
    "AL": "01", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09",
    "DE": "10", "DC": "11", "FL": "12", "GA": "13", "ID": "16", "IL": "17",
    "IN": "18", "IA": "19", "KS": "20", "KY": "21", "LA": "22", "ME": "23",
    "MD": "24", "MA": "25", "MI": "26", "MN": "27", "MS": "28", "MO": "29",
    "MT": "30", "NE": "31", "NV": "32", "NH": "33", "NJ": "34", "NM": "35",
    "NY": "36", "NC": "37", "ND": "38", "OH": "39", "OK": "40", "OR": "41",
    "PA": "42", "RI": "44", "SC": "45", "SD": "46", "TN": "47", "TX": "48",
    "UT": "49", "VT": "50", "VA": "51", "WA": "53", "WV": "54", "WI": "55",
    "WY": "56",
}

# Reverse lookup: FIPS code (e.g. "37") -> state abbreviation (e.g. "NC").
# Derived from STATE_FIPS so the two stay in sync automatically. Non-CONUS
# FIPS codes (AK=02, HI=15, PR=72, etc.) are intentionally absent — any
# row whose Census GEOID resolves to one of those codes is out of scope
# for this CONUS-only pipeline.
STATE_FIPS_TO_ABBR: dict[str, str] = {fips: abbr for abbr, fips in STATE_FIPS.items()}

# --- USGS National Map endpoints (versioned for reproducibility) ---
TNM_API_BASE: str = "https://tnmaccess.nationalmap.gov/api/v1/products"
TNM_DEM_DATASET: str = "National Elevation Dataset (NED) 1 arc-second"
# TNM's bbox endpoint serves up to ``max`` items in a single page (default
# 50, capped server-side around the low hundreds). For NC's ~3° × 9° bbox
# the catalogue contains ~126 raw items (3+ vintages × ~38 land quads), so
# the default 50 was clipping the western mountain quads off the page.
# 200 captures the full NC catalogue with the dedupe-by-quad layer
# downstream collapsing it to ~38 tiles. Server-side pagination via
# ``offset`` is currently flaky (``total=0`` after the first page), so the
# downloader uses one large request and dedupes by ``(round(min_lat),
# round(min_lon))`` keeping the latest vintage per quad.
TNM_PAGE_SIZE: int = 200

# --- State bounding boxes (WGS84, lon_min, lat_min, lon_max, lat_max) -----
# Used by the downloader to subset NLCD rasters via WCS and DEM tiles via
# TNM bbox. NC's bbox is the one already encoded in the orchestrator's
# geographic-sanity validation check. Other states added as they're brought
# into scope (this dict starts at NC because the challenge dataset is
# NC-only; extending to additional states is intentionally minimal change).
STATE_BBOX_WGS84: dict[str, tuple[float, float, float, float]] = {
    "NC": (-84.32, 33.75, -75.46, 36.59),
}
