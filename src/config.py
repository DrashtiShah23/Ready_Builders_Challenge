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
CLAUDE_MODEL: str = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS: int = 1024
CLAUDE_TEMPERATURE: int = 0          # Deterministic — required for reproducible scoring.
CLAUDE_BATCH_SIZE: int = 50          # Locations per Claude API call. Tune to your spend ceiling.

# --- Processing ---
# Raster sampling is done in large in-memory batches for I/O efficiency.
# Intentionally separate from CLAUDE_BATCH_SIZE — these are two different operations
# with different bottlenecks (memory vs. API cost / latency).
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

# --- Raster Dataset URLs (versioned for reproducibility) ---
CANOPY_RASTER_URL: str = (
    "https://s3-us-west-2.amazonaws.com/mrlc/nlcd_tcc_conus_2021_v2021-4.zip"
)
LANDCOVER_RASTER_URL: str = (
    "https://s3-us-west-2.amazonaws.com/mrlc/nlcd_land_cover_l48_2021_20230630.zip"
)
# DEM: tile-based, no single URL — downloaded per state bounding box
# via the USGS National Map API. See src/data/downloader.py:download_dem_tiles.

# --- CSV ingestion contract ---
EXPECTED_CSV_COLUMNS: list[str] = ["location_id", "latitude", "longitude", "state", "county"]
