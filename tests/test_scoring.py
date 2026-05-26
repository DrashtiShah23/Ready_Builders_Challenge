"""
Phase 6 tests: Risk Scoring Engine.

Test layout mirrors the spec checklist:
    * each per-signal scorer in isolation, including ``None`` and boundary values
    * every NLCD code in FOREST / DEVELOPED / OPEN, plus a handful of
      non-classified codes (11, 12, 90, 95) and a synthetic 999
    * ``tier_for`` boundary values: 0.299, 0.300, 0.599, 0.600
    * ``score_components`` aggregator, including all-None → UNSCORED
    * ``compute_risk_score`` end-to-end with EnrichedLocation in / ScoredLocation out
    * flag merging (Phase 5 flags + Phase 6 flags, de-duped, order preserved)
    * config invariants: no scoring constant is hardcoded in ``scoring.py``
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src import config
from src.agents import scoring
from src.agents.scoring import (
    FLAG_LANDCOVER_MISSING,
    FLAG_LANDCOVER_UNHANDLED_CODE,
    FLAG_SLOPE_MISSING,
    FLAG_TCC_MISSING,
    TIER_HIGH,
    TIER_LOW,
    TIER_MODERATE,
    TIER_UNSCORED,
    compute_risk_score,
    is_unhandled_landcover_code,
    score_components,
    score_landcover,
    score_terrain,
    score_tcc,
    tier_for,
)
from src.schemas.location import EnrichedLocation, ScoredLocation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enriched(
    tcc_pct=None,
    slope_deg=None,
    aspect_deg=None,
    land_cover_code=None,
    land_cover_class=None,
    env_fetch_flags=None,
    elevation_m=1500.0,
) -> EnrichedLocation:
    return EnrichedLocation(
        location_id="L1",
        latitude=40.0,
        longitude=-100.0,
        state="NE",
        county="Lancaster",
        tcc_pct=tcc_pct,
        elevation_m=elevation_m,
        slope_deg=slope_deg,
        aspect_deg=aspect_deg,
        land_cover_code=land_cover_code,
        land_cover_class=land_cover_class,
        env_fetch_flags=list(env_fetch_flags or []),
        batch_id="batch-000000",
    )


# ---------------------------------------------------------------------------
# Per-signal scorers
# ---------------------------------------------------------------------------


class TestScoreTcc:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, 0.0),
            (0, 0.0),
            (10, 0.0),
            (19, 0.0),                    # just below MOD threshold (20)
            (20, 0.5),                    # MOD lower boundary inclusive
            (35, 0.5),
            (49, 0.5),
            (50, 0.5),                    # HIGH boundary — strict-greater rule keeps it at 0.5
            (51, 1.0),                    # first value above HIGH
            (75, 1.0),
            (100, 1.0),
        ],
    )
    def test_buckets(self, value, expected):
        assert score_tcc(value) == expected


class TestScoreTerrain:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, 0.0),
            (0.0, 0.0),
            (9.99, 0.0),
            (10.0, 0.5),
            (15.0, 0.5),
            (19.99, 0.5),
            (20.0, 0.5),                  # HIGH boundary stays at 0.5 (strict-greater rule)
            (20.0001, 1.0),
            (45.0, 1.0),
            (90.0, 1.0),
        ],
    )
    def test_buckets(self, value, expected):
        assert score_terrain(value) == expected


class TestScoreLandcover:
    @pytest.mark.parametrize("code", config.FOREST_CODES)
    def test_every_forest_code_scores_1_0(self, code):
        assert score_landcover(code) == 1.0
        assert not is_unhandled_landcover_code(code)

    @pytest.mark.parametrize("code", config.DEVELOPED_CODES)
    def test_every_developed_code_scores_0_5(self, code):
        assert score_landcover(code) == 0.5
        assert not is_unhandled_landcover_code(code)

    @pytest.mark.parametrize("code", config.OPEN_CODES)
    def test_every_open_code_scores_0_0(self, code):
        assert score_landcover(code) == 0.0
        assert not is_unhandled_landcover_code(code)

    def test_none_scores_0_0(self):
        assert score_landcover(None) == 0.0
        assert is_unhandled_landcover_code(None) is False

    @pytest.mark.parametrize("code", [11, 12, 90, 95, 999])
    def test_unhandled_codes_score_0_0_and_flag(self, code):
        assert score_landcover(code) == 0.0
        assert is_unhandled_landcover_code(code) is True


# ---------------------------------------------------------------------------
# tier_for boundaries
# ---------------------------------------------------------------------------


class TestTierFor:
    def test_none_is_unscored(self):
        assert tier_for(None) == TIER_UNSCORED

    @pytest.mark.parametrize(
        "score,tier",
        [
            (0.0, TIER_LOW),
            (0.2999, TIER_LOW),
            (0.299, TIER_LOW),
            (0.3, TIER_MODERATE),         # spec boundary
            (0.3001, TIER_MODERATE),
            (0.45, TIER_MODERATE),
            (0.599, TIER_MODERATE),       # spec boundary — must be Moderate
            (0.5999, TIER_MODERATE),
            (0.6, TIER_HIGH),             # spec boundary — must be High
            (0.6001, TIER_HIGH),
            (0.75, TIER_HIGH),
            (1.0, TIER_HIGH),
        ],
    )
    def test_boundaries(self, score, tier):
        assert tier_for(score) == tier


# ---------------------------------------------------------------------------
# score_components aggregator
# ---------------------------------------------------------------------------


class TestScoreComponents:
    def test_all_none_returns_unscored_with_three_missing_flags(self):
        result = score_components(None, None, None)
        assert result["risk_score"] is None
        assert result["risk_tier"] == TIER_UNSCORED
        assert result["tcc_score"] is None
        assert result["terrain_score"] is None
        assert result["landcover_score"] is None
        assert set(result["flags"]) == {
            FLAG_TCC_MISSING, FLAG_SLOPE_MISSING, FLAG_LANDCOVER_MISSING,
        }

    def test_full_high_inputs(self):
        result = score_components(tcc_pct=80, slope_deg=30.0, land_cover_code=42)
        assert result["tcc_score"] == 1.0
        assert result["terrain_score"] == 1.0
        assert result["landcover_score"] == 1.0
        assert result["risk_score"] == 1.0
        assert result["risk_tier"] == TIER_HIGH
        assert result["flags"] == []

    def test_full_low_inputs(self):
        result = score_components(tcc_pct=5, slope_deg=2.0, land_cover_code=71)
        assert result["tcc_score"] == 0.0
        assert result["terrain_score"] == 0.0
        assert result["landcover_score"] == 0.0
        assert result["risk_score"] == 0.0
        assert result["risk_tier"] == TIER_LOW

    def test_exact_high_boundary_composite(self):
        # tcc=1.0, terrain=0.0, lc=0.5 -> 0.5*1 + 0.3*0 + 0.2*0.5 = 0.6
        result = score_components(tcc_pct=100, slope_deg=0.0, land_cover_code=21)
        assert result["risk_score"] == 0.6
        assert result["risk_tier"] == TIER_HIGH

    def test_floating_point_safe_at_boundary(self):
        # Naive float arithmetic for 0.5 + 0.0 + 0.1 can drift slightly; the
        # 4-decimal-place rounding inside score_components must hide that.
        result = score_components(tcc_pct=100, slope_deg=0.0, land_cover_code=22)
        assert result["risk_score"] == 0.6
        assert result["risk_tier"] == TIER_HIGH

    def test_partial_missing_tcc_only(self):
        result = score_components(tcc_pct=None, slope_deg=30.0, land_cover_code=42)
        assert result["tcc_score"] == 0.0
        assert result["terrain_score"] == 1.0
        assert result["landcover_score"] == 1.0
        # 0.5*0 + 0.3*1 + 0.2*1 = 0.5 -> Moderate
        assert result["risk_score"] == 0.5
        assert result["risk_tier"] == TIER_MODERATE
        assert result["flags"] == [FLAG_TCC_MISSING]

    def test_partial_missing_slope_only(self):
        result = score_components(tcc_pct=80, slope_deg=None, land_cover_code=42)
        # 0.5*1 + 0.3*0 + 0.2*1 = 0.7 -> High
        assert result["risk_score"] == 0.7
        assert result["risk_tier"] == TIER_HIGH
        assert result["flags"] == [FLAG_SLOPE_MISSING]

    def test_partial_missing_landcover_only(self):
        result = score_components(tcc_pct=80, slope_deg=30.0, land_cover_code=None)
        # 0.5*1 + 0.3*1 + 0.2*0 = 0.8 -> High
        assert result["risk_score"] == 0.8
        assert result["risk_tier"] == TIER_HIGH
        assert result["flags"] == [FLAG_LANDCOVER_MISSING]

    def test_unhandled_landcover_code_flag(self):
        result = score_components(tcc_pct=80, slope_deg=30.0, land_cover_code=11)
        assert result["landcover_score"] == 0.0
        assert FLAG_LANDCOVER_UNHANDLED_CODE in result["flags"]
        # Note: LANDCOVER_MISSING is NOT added when the code is non-null but unhandled.
        assert FLAG_LANDCOVER_MISSING not in result["flags"]

    def test_latitude_param_is_accepted_but_does_not_change_score(self):
        a = score_components(tcc_pct=80, slope_deg=30.0, land_cover_code=42, latitude=24.5)
        b = score_components(tcc_pct=80, slope_deg=30.0, land_cover_code=42, latitude=49.0)
        assert a["risk_score"] == b["risk_score"]
        assert a["risk_tier"] == b["risk_tier"]

    def test_keyword_args_default_to_none(self):
        # Phase 7's Claude tool dispatcher may pass any subset of kwargs.
        result = score_components()
        assert result["risk_tier"] == TIER_UNSCORED


# ---------------------------------------------------------------------------
# compute_risk_score: EnrichedLocation -> ScoredLocation
# ---------------------------------------------------------------------------


class TestComputeRiskScore:
    def test_returns_scored_location(self):
        enriched = _enriched(
            tcc_pct=80, slope_deg=30.0, land_cover_code=42,
            land_cover_class="Evergreen Forest", aspect_deg=180.0,
        )
        scored = compute_risk_score(enriched)
        assert isinstance(scored, ScoredLocation)
        assert scored.location_id == "L1"
        assert scored.latitude == 40.0
        assert scored.longitude == -100.0
        assert scored.state == "NE"
        assert scored.county == "Lancaster"
        assert scored.batch_id == "batch-000000"
        assert scored.land_cover_class == "Evergreen Forest"

    def test_full_high_pipeline(self):
        enriched = _enriched(tcc_pct=80, slope_deg=30.0, land_cover_code=42)
        scored = compute_risk_score(enriched)
        assert scored.tcc_score == 1.0
        assert scored.terrain_score == 1.0
        assert scored.landcover_score == 1.0
        assert scored.risk_score == 1.0
        assert scored.risk_tier == TIER_HIGH

    def test_unscored_when_all_signals_missing(self):
        enriched = _enriched(tcc_pct=None, slope_deg=None, land_cover_code=None)
        scored = compute_risk_score(enriched)
        assert scored.risk_score is None
        assert scored.risk_tier == TIER_UNSCORED
        assert scored.tcc_score is None
        assert scored.terrain_score is None
        assert scored.landcover_score is None

    def test_aspect_passed_through_but_not_used_in_scoring(self):
        enriched = _enriched(
            tcc_pct=10, slope_deg=2.0, land_cover_code=71, aspect_deg=42.0,
        )
        scored = compute_risk_score(enriched)
        assert scored.aspect_deg == 42.0
        # Aspect should not move the score off of "Low" for this combination.
        assert scored.risk_tier == TIER_LOW

    def test_scored_at_is_recent_utc(self):
        before = datetime.now(timezone.utc)
        scored = compute_risk_score(_enriched(tcc_pct=80, slope_deg=30.0, land_cover_code=42))
        after = datetime.now(timezone.utc)
        assert before <= scored.scored_at <= after
        # Pydantic stores tz-aware datetimes if you pass them; the engine uses UTC.
        assert scored.scored_at.tzinfo is not None


class TestFlagMerging:
    def test_phase5_flags_carried_into_all_flags(self):
        enriched = _enriched(
            tcc_pct=None, slope_deg=30.0, land_cover_code=42,
            env_fetch_flags=["TCC_MISSING", "ASPECT_MISSING"],
        )
        scored = compute_risk_score(enriched)
        assert "TCC_MISSING" in scored.all_flags
        assert "ASPECT_MISSING" in scored.all_flags

    def test_duplicate_flags_deduplicated(self):
        # Phase 5 already added TCC_MISSING; scoring would add it again.
        enriched = _enriched(
            tcc_pct=None, slope_deg=30.0, land_cover_code=42,
            env_fetch_flags=["TCC_MISSING"],
        )
        scored = compute_risk_score(enriched)
        assert scored.all_flags.count("TCC_MISSING") == 1

    def test_flag_order_env_first_then_scoring(self):
        enriched = _enriched(
            tcc_pct=80, slope_deg=30.0, land_cover_code=11,   # unhandled code
            env_fetch_flags=["ASPECT_MISSING"],
        )
        scored = compute_risk_score(enriched)
        # ASPECT_MISSING (env) must appear before LANDCOVER_UNHANDLED_CODE (scoring).
        assert scored.all_flags.index("ASPECT_MISSING") < scored.all_flags.index(
            FLAG_LANDCOVER_UNHANDLED_CODE
        )

    def test_unhandled_code_propagates_to_all_flags(self):
        enriched = _enriched(tcc_pct=80, slope_deg=30.0, land_cover_code=11)
        scored = compute_risk_score(enriched)
        assert FLAG_LANDCOVER_UNHANDLED_CODE in scored.all_flags


# ---------------------------------------------------------------------------
# Config invariants — guard against accidental hardcoded numbers
# ---------------------------------------------------------------------------


class TestConfigInvariants:
    def test_weights_sum_to_one(self):
        total = config.TCC_WEIGHT + config.TERRAIN_WEIGHT + config.LANDCOVER_WEIGHT
        assert abs(total - 1.0) < 1e-9

    def test_no_hardcoded_thresholds_in_scoring_module(self):
        """The STOP gate says: 'All weights sourced from config.py — no hardcoded
        numbers in scoring.py'. This test reads the module source and asserts
        that the literal threshold/weight values from config never appear in
        executable scoring code.

        The scan excludes:
            * docstrings and comments (documentation may legitimately mention
              the numeric values being justified);
            * module-level constant definitions (the bucket outputs
              ``_SCORE_HIGH``/``_SCORE_MODERATE``/``_SCORE_LOW`` are intrinsic
              to the scoring scheme and one of them happens to share its
              numeric value with ``TCC_WEIGHT``).
        """
        source = Path(scoring.__file__).read_text()

        no_triple_quoted = re.sub(r'"""[\s\S]*?"""', "", source)
        no_comments = re.sub(r"#.*", "", no_triple_quoted)

        # Drop any line that is a module-level constant definition. A constant
        # def at column 0 looks like ``NAME[: type] = literal`` — strip those.
        scrubbed_lines = []
        const_def_pattern = re.compile(
            r"^_?[A-Z][A-Z0-9_]*(\s*:\s*[\w\[\], .|]+)?\s*=\s*"
        )
        for line in no_comments.splitlines():
            if const_def_pattern.match(line):
                continue
            scrubbed_lines.append(line)
        scrubbed = "\n".join(scrubbed_lines)

        forbidden = [
            str(config.TCC_WEIGHT),
            str(config.TERRAIN_WEIGHT),
            str(config.LANDCOVER_WEIGHT),
            str(config.CANOPY_HIGH_THRESHOLD),
            str(config.CANOPY_MOD_THRESHOLD),
            str(config.SLOPE_HIGH_THRESHOLD),
            str(config.SLOPE_MOD_THRESHOLD),
            str(config.RISK_HIGH_THRESHOLD),
            str(config.RISK_MOD_THRESHOLD),
        ]
        for literal in forbidden:
            pattern = rf"(?<![\w.]){re.escape(literal)}(?![\w.])"
            assert not re.search(pattern, scrubbed), (
                f"Forbidden hardcoded literal {literal!r} appears in "
                f"scoring.py executable code. Source the value from config "
                f"instead."
            )


# Local imports for the scored_at test (avoiding a top-level circular import noise).
from datetime import datetime, timezone  # noqa: E402
