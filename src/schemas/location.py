"""
Data contracts for every stage of the LEO risk pipeline.

Enforced at every stage boundary. Bad data fails loudly here, never silently
downstream. Each model corresponds to a precise moment in the pipeline:

    CSV row              -> RawLocation
    after ingestion      -> ValidatedLocation
    after env data fetch -> EnrichedLocation
    after risk scoring   -> ScoredLocation

Design notes:
- Every model is a Pydantic `BaseModel` (v2). Validation errors are raised
  with a structured location path, not a generic ValueError — this makes
  ingestion log triage straightforward.
- Optional environmental fields default to None. The downstream scoring engine
  treats None as a missing signal and degrades gracefully (see
  `src/agents/scoring.py`), rather than silently substituting zero.
- `validation_flags`, `env_fetch_flags`, and `all_flags` are typed string lists.
  Reason codes live in the agent modules that produce them
  (e.g. NULL_COORDINATE, OUT_OF_BOUNDS, DUPLICATE_DROPPED, TCC_MISSING).
- `batch_id` is carried through every stage so a single failing record is
  traceable end-to-end via the JSONL log.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class RawLocation(BaseModel):
    """Raw row from the input CSV — no validation has run yet."""

    location_id: str
    latitude: float
    longitude: float
    state: Optional[str] = None
    county: Optional[str] = None


class ValidatedLocation(BaseModel):
    """A location that passed ingestion validation (CONUS bounds, dedup, schema)."""

    location_id: str
    latitude: float
    longitude: float
    state: Optional[str] = None
    county: Optional[str] = None
    validation_flags: list[str] = Field(default_factory=list)
    batch_id: str


class EnrichedLocation(BaseModel):
    """A validated location with environmental signals attached.

    Any of `tcc_pct`, `elevation_m`, `slope_deg`, `aspect_deg`, or
    `land_cover_code` may be None when the source raster has no data
    (NoData pixel, outside coverage, or sampling error). The
    `env_fetch_flags` list records why each missing field is missing.
    """

    location_id: str
    latitude: float
    longitude: float
    state: Optional[str] = None
    county: Optional[str] = None
    tcc_pct: Optional[int] = None           # 0–100 from NLCD TCC
    elevation_m: Optional[float] = None
    slope_deg: Optional[float] = None
    aspect_deg: Optional[float] = None
    land_cover_code: Optional[int] = None   # NLCD land-cover code (e.g. 41, 42, 43)
    land_cover_class: Optional[str] = None  # Human-readable class name
    env_fetch_flags: list[str] = Field(default_factory=list)
    batch_id: str


class ScoredLocation(BaseModel):
    """Final output. Every input location_id must appear here — scored or UNSCORED.

    `risk_tier` is always present (one of "Low", "Moderate", "High", "UNSCORED")
    so downstream aggregations never need to special-case nulls. Component
    scores are exposed so each composite score can be decomposed back into
    its TCC / terrain / land-cover contributions for explainability.
    """

    location_id: str
    latitude: float
    longitude: float
    state: Optional[str] = None
    county: Optional[str] = None

    tcc_pct: Optional[int] = None
    slope_deg: Optional[float] = None
    aspect_deg: Optional[float] = None
    land_cover_code: Optional[int] = None
    land_cover_class: Optional[str] = None

    risk_score: Optional[float] = None      # Composite 0.0–1.0, or None for UNSCORED
    risk_tier: str                          # "Low" | "Moderate" | "High" | "UNSCORED"
    tcc_score: Optional[float] = None       # Component contribution before weighting
    terrain_score: Optional[float] = None
    landcover_score: Optional[float] = None

    all_flags: list[str] = Field(default_factory=list)
    batch_id: str
    scored_at: datetime
