"""
Risk Scoring Engine: translates environmental signals into a composite risk score.

Formula
-------
``risk_score = (tcc_score * 0.50) + (terrain_score * 0.30) + (landcover_score * 0.20)``

Weight justification
--------------------
- **TCC 50%** — Starlink's install guide explicitly names tree branches as the
  primary obstruction. This is the most direct, continuous, nationally
  consistent signal we have, so it carries the highest weight.
- **Terrain 30%** — Slope constrains both the 100–110 degree FOV cone and the
  25 degree elevation minimum the dish requires. A steep slope is a hard
  physical constraint — unlike canopy, elevated mounting rarely overcomes a
  deep valley. Weighted higher than the v1.0 draft for that reason.
- **Land cover 20%** — Cross-validates TCC (forest codes confirm high canopy
  readings) and adds structural-density context (developed codes for sub-canopy
  building obstructions). Uses the same NLCD download as TCC — zero added
  infrastructure cost. Weighted lower because for forested cells it is
  partially redundant with TCC.

This engine is intentionally **deterministic** rather than ML-based:

1. *Transparency.* A state broadband officer can audit the formula in their head.
2. *Reproducibility.* Same inputs always produce the same outputs.
3. *Explainability.* Every component score traces directly to a physical
   obstruction factor from the install guide.

All thresholds and weights are sourced from :mod:`src.config`. No scoring
threshold or weight is hardcoded anywhere in this module.

Public API
----------
``score_tcc``, ``score_terrain``, ``score_landcover`` — pure per-signal scorers.
``tier_for`` — maps a composite score to ``"Low" / "Moderate" / "High" / "UNSCORED"``.
``score_components`` — per-row scoring entry point used by the
orchestrator's ``score_risk`` tool (the Phase 7 redesign keeps Claude
out of the per-row hot path; this function is plain Python, called in a
loop over the enriched-locations parquet). Returns a JSON-ready dict.
``compute_risk_score`` — the high-level agent entry. Takes a full
``EnrichedLocation`` and returns a full ``ScoredLocation``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from src import config
from src.schemas.location import EnrichedLocation, ScoredLocation

# Risk-tier string constants — duplicated as Python strings (not Enum) so they
# serialise cleanly into JSONL logs and ScoredLocation.risk_tier (a `str`).
TIER_LOW: str = "Low"
TIER_MODERATE: str = "Moderate"
TIER_HIGH: str = "High"
TIER_UNSCORED: str = "UNSCORED"

# Scoring-stage flag codes. Kept consistent with Phase 5's EnvFlag names so
# the union list (`ScoredLocation.all_flags`) reads as one coherent vocabulary.
FLAG_TCC_MISSING: str = "TCC_MISSING"
FLAG_SLOPE_MISSING: str = "SLOPE_MISSING"
FLAG_LANDCOVER_MISSING: str = "LANDCOVER_MISSING"
FLAG_LANDCOVER_UNHANDLED_CODE: str = "LANDCOVER_UNHANDLED_CODE"

# Bucket-output constants. The scoring scheme buckets every per-signal score
# into exactly three values; naming them removes the magic-number smell from
# the scorer bodies AND keeps these values distinct from the configurable
# weights/thresholds in :mod:`src.config`. (``_SCORE_MODERATE`` happens to
# equal ``TCC_WEIGHT`` numerically, but they mean entirely different things.)
_SCORE_HIGH: float = 1.0
_SCORE_MODERATE: float = 0.5
_SCORE_LOW: float = 0.0

# Pre-computed for cheap membership checks. Recomputed only on import.
_HANDLED_LANDCOVER_CODES: frozenset[int] = frozenset(
    {*config.FOREST_CODES, *config.DEVELOPED_CODES, *config.OPEN_CODES}
)


# ---------------------------------------------------------------------------
# Per-signal pure scorers
# ---------------------------------------------------------------------------


def score_tcc(tcc_pct: Optional[int]) -> float:
    """TCC component score on [0.0, 1.0].

    ``None`` (signal missing) maps to ``0.0`` — the conservative default,
    consistent with the build plan and with not penalising scoring stalls
    when the rest of the signals can still produce a partial composite.

    Boundaries (with config defaults of HIGH=50, MOD=20):
        - ``tcc_pct > 50`` → ``1.0``
        - ``20 <= tcc_pct <= 50`` → ``0.5``
        - ``tcc_pct < 20`` → ``0.0``
    """
    if tcc_pct is None:
        return _SCORE_LOW
    if tcc_pct > config.CANOPY_HIGH_THRESHOLD:
        return _SCORE_HIGH
    if tcc_pct >= config.CANOPY_MOD_THRESHOLD:
        return _SCORE_MODERATE
    return _SCORE_LOW


def score_terrain(slope_deg: Optional[float]) -> float:
    """Terrain (slope) component score on [0.0, 1.0].

    ``None`` → ``0.0``. Boundaries (config defaults HIGH=20°, MOD=10°):
        - ``slope_deg > 20`` → ``1.0``
        - ``10 <= slope_deg <= 20`` → ``0.5``
        - ``slope_deg < 10`` → ``0.0``
    """
    if slope_deg is None:
        return _SCORE_LOW
    if slope_deg > config.SLOPE_HIGH_THRESHOLD:
        return _SCORE_HIGH
    if slope_deg >= config.SLOPE_MOD_THRESHOLD:
        return _SCORE_MODERATE
    return _SCORE_LOW


def score_landcover(land_cover_code: Optional[int]) -> float:
    """Land-cover component score on [0.0, 1.0].

    ``None`` → ``0.0``. ``FOREST_CODES`` → ``1.0``. ``DEVELOPED_CODES`` →
    ``0.5``. Everything else (``OPEN_CODES`` plus any non-classified NLCD
    code like 11/12/90/95) → ``0.0``.

    Non-classified codes still score ``0.0`` so the composite math is
    well-defined, but the caller is expected to add a
    ``LANDCOVER_UNHANDLED_CODE`` flag — see :func:`score_components`.
    """
    if land_cover_code is None:
        return _SCORE_LOW
    if land_cover_code in config.FOREST_CODES:
        return _SCORE_HIGH
    if land_cover_code in config.DEVELOPED_CODES:
        return _SCORE_MODERATE
    return _SCORE_LOW


def is_unhandled_landcover_code(code: Optional[int]) -> bool:
    """``True`` if ``code`` is non-null and not in any of the three scoring lists."""
    if code is None:
        return False
    return code not in _HANDLED_LANDCOVER_CODES


# ---------------------------------------------------------------------------
# Tier mapping
# ---------------------------------------------------------------------------


def tier_for(risk_score: Optional[float]) -> str:
    """Map a composite risk score to a tier label.

    ``None`` → ``"UNSCORED"``. Otherwise (config defaults HIGH=0.6, MOD=0.3):
        - ``>= 0.6`` → ``"High"``
        - ``>= 0.3`` → ``"Moderate"``
        - ``< 0.3`` → ``"Low"``

    Boundary semantics are tested explicitly: ``0.599`` → ``"Moderate"``,
    ``0.600`` → ``"High"``. No rounding is done here — callers that need
    rounding should round before calling so the tier label always
    reflects the exact value the caller intends to compare.
    """
    if risk_score is None:
        return TIER_UNSCORED
    if risk_score >= config.RISK_HIGH_THRESHOLD:
        return TIER_HIGH
    if risk_score >= config.RISK_MOD_THRESHOLD:
        return TIER_MODERATE
    return TIER_LOW


# ---------------------------------------------------------------------------
# Aggregator — called per row by the orchestrator's ``score_risk`` tool
# (Phase 7 pipeline-level dispatch, no per-row Claude reasoning).
# ---------------------------------------------------------------------------


def score_components(
    tcc_pct: Optional[int] = None,
    slope_deg: Optional[float] = None,
    land_cover_code: Optional[int] = None,
    latitude: Optional[float] = None,
) -> dict[str, Any]:
    """Compute every component score, the composite, the tier, and scoring flags.

    Called per row by the orchestrator's ``score_risk`` tool handler
    (`PipelineOrchestrator._run_score_risk`); the return shape is a
    plain JSON-serialisable ``dict`` because the original build plan
    exposed this function as a per-row Claude tool, and the dict shape
    was preserved through the Phase 7 redesign so the public API stayed
    stable for downstream callers (interactive-mode scoring still goes
    through this exact function).

    Parameters
    ----------
    tcc_pct, slope_deg, land_cover_code:
        Environmental signals. Any may be ``None`` for "signal missing".
    latitude:
        Accepted for forward compatibility with hemisphere-aware reasoning
        (see ``docs/decision_log.md``). **Not used in the v3.0 composite.**

    Returns
    -------
    dict
        Always contains the keys ``tcc_score``, ``terrain_score``,
        ``landcover_score``, ``risk_score``, ``risk_tier``, ``flags``.
        When every signal is ``None`` the component scores and
        ``risk_score`` are ``None`` and ``risk_tier == "UNSCORED"`` so a
        downstream consumer cannot accidentally treat the fallback ``0.0``
        as a real low-risk reading.
    """
    flags: list[str] = []
    if tcc_pct is None:
        flags.append(FLAG_TCC_MISSING)
    if slope_deg is None:
        flags.append(FLAG_SLOPE_MISSING)
    if land_cover_code is None:
        flags.append(FLAG_LANDCOVER_MISSING)
    elif is_unhandled_landcover_code(land_cover_code):
        flags.append(FLAG_LANDCOVER_UNHANDLED_CODE)

    # All three signals missing → UNSCORED. Returning None for every component
    # is the only honest answer — a 0.0 composite here would lie.
    if tcc_pct is None and slope_deg is None and land_cover_code is None:
        return {
            "tcc_score": None,
            "terrain_score": None,
            "landcover_score": None,
            "risk_score": None,
            "risk_tier": TIER_UNSCORED,
            "flags": flags,
        }

    tcc_s = score_tcc(tcc_pct)
    terrain_s = score_terrain(slope_deg)
    landcover_s = score_landcover(land_cover_code)

    # Round the composite to 4 dp BEFORE handing it to tier_for, so
    # binary-floating-point noise (e.g. 0.6000000000000001) does not flip
    # the tier label. Component scores are kept un-rounded because they are
    # always exactly 0.0/0.5/1.0 by construction.
    composite = round(
        config.TCC_WEIGHT * tcc_s
        + config.TERRAIN_WEIGHT * terrain_s
        + config.LANDCOVER_WEIGHT * landcover_s,
        4,
    )

    return {
        "tcc_score": tcc_s,
        "terrain_score": terrain_s,
        "landcover_score": landcover_s,
        "risk_score": composite,
        "risk_tier": tier_for(composite),
        "flags": flags,
    }


# ---------------------------------------------------------------------------
# High-level agent entry — EnrichedLocation -> ScoredLocation
# ---------------------------------------------------------------------------


def compute_risk_score(enriched: EnrichedLocation) -> ScoredLocation:
    """Score one enriched location end-to-end and return a ``ScoredLocation``.

    Merges the scoring-stage flags into the upstream
    ``EnrichedLocation.env_fetch_flags`` and de-duplicates the union so a
    flag added by both Phase 5 and Phase 6 only appears once in
    ``ScoredLocation.all_flags``.
    """
    result = score_components(
        tcc_pct=enriched.tcc_pct,
        slope_deg=enriched.slope_deg,
        land_cover_code=enriched.land_cover_code,
        latitude=enriched.latitude,
    )

    # Order-preserving dedup of the flag union. Env flags come first because
    # they describe the upstream data-quality state; scoring flags after.
    combined = list(enriched.env_fetch_flags) + list(result["flags"])
    seen: set[str] = set()
    all_flags: list[str] = []
    for flag in combined:
        if flag not in seen:
            seen.add(flag)
            all_flags.append(flag)

    return ScoredLocation(
        location_id=enriched.location_id,
        latitude=enriched.latitude,
        longitude=enriched.longitude,
        state=enriched.state,
        county=enriched.county,
        tcc_pct=enriched.tcc_pct,
        slope_deg=enriched.slope_deg,
        aspect_deg=enriched.aspect_deg,
        land_cover_code=enriched.land_cover_code,
        land_cover_class=enriched.land_cover_class,
        risk_score=result["risk_score"],
        risk_tier=result["risk_tier"],
        tcc_score=result["tcc_score"],
        terrain_score=result["terrain_score"],
        landcover_score=result["landcover_score"],
        all_flags=all_flags,
        batch_id=enriched.batch_id,
        scored_at=datetime.now(timezone.utc),
    )
