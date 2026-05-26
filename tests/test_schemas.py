"""
Phase 1 tests: Pydantic schemas and config invariants.

These tests are intentionally fast and dependency-light (no rasterio, no
network) so they run in milliseconds on every push.

Coverage:
- RawLocation / ValidatedLocation / EnrichedLocation / ScoredLocation
  - happy-path construction
  - required-field enforcement
  - optional fields default correctly
  - list defaults are independent per-instance (Pydantic v2 deep-copy semantics)
  - type coercion behaves as expected (numeric strings → float)
- Config invariants
  - risk weights sum to 1.0
  - NLCD code groups are disjoint (no code in two categories)
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src import config
from src.schemas.location import (
    EnrichedLocation,
    RawLocation,
    ScoredLocation,
    ValidatedLocation,
)


# ---------------------------------------------------------------------------
# RawLocation
# ---------------------------------------------------------------------------


class TestRawLocation:
    def test_valid_minimum(self) -> None:
        loc = RawLocation(location_id="L1", latitude=37.7749, longitude=-122.4194)
        assert loc.location_id == "L1"
        assert loc.latitude == pytest.approx(37.7749)
        assert loc.longitude == pytest.approx(-122.4194)
        assert loc.state is None
        assert loc.county is None

    def test_valid_full(self) -> None:
        loc = RawLocation(
            location_id="L2",
            latitude=40.0,
            longitude=-100.0,
            state="CO",
            county="Denver",
        )
        assert loc.state == "CO"
        assert loc.county == "Denver"

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            RawLocation(latitude=40.0, longitude=-100.0)  # type: ignore[call-arg]

    def test_non_numeric_coordinate_raises(self) -> None:
        with pytest.raises(ValidationError):
            RawLocation(location_id="L3", latitude="not-a-number", longitude=-100.0)  # type: ignore[arg-type]

    def test_numeric_string_coerces(self) -> None:
        # Pydantic v2 coerces well-formed numeric strings — useful for CSV ingestion.
        loc = RawLocation(location_id="L4", latitude="40.0", longitude="-100.0")  # type: ignore[arg-type]
        assert loc.latitude == 40.0
        assert loc.longitude == -100.0


# ---------------------------------------------------------------------------
# ValidatedLocation
# ---------------------------------------------------------------------------


class TestValidatedLocation:
    def test_valid_minimum(self) -> None:
        loc = ValidatedLocation(
            location_id="L1", latitude=40.0, longitude=-100.0, batch_id="b-001"
        )
        assert loc.validation_flags == []
        assert loc.batch_id == "b-001"

    def test_validation_flags_default_is_independent_per_instance(self) -> None:
        """Pydantic v2 deep-copies mutable defaults — mutating one instance must not
        leak into another."""
        a = ValidatedLocation(
            location_id="L1", latitude=40.0, longitude=-100.0, batch_id="b-001"
        )
        b = ValidatedLocation(
            location_id="L2", latitude=41.0, longitude=-100.0, batch_id="b-001"
        )
        a.validation_flags.append("FLAG_A")
        assert b.validation_flags == []

    def test_missing_batch_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            ValidatedLocation(location_id="L1", latitude=40.0, longitude=-100.0)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# EnrichedLocation
# ---------------------------------------------------------------------------


class TestEnrichedLocation:
    def test_all_env_fields_optional(self) -> None:
        loc = EnrichedLocation(
            location_id="L1", latitude=40.0, longitude=-100.0, batch_id="b-001"
        )
        assert loc.tcc_pct is None
        assert loc.elevation_m is None
        assert loc.slope_deg is None
        assert loc.aspect_deg is None
        assert loc.land_cover_code is None
        assert loc.land_cover_class is None
        assert loc.env_fetch_flags == []

    def test_full_enrichment(self) -> None:
        loc = EnrichedLocation(
            location_id="L1",
            latitude=37.4,
            longitude=-122.1,
            state="CA",
            county="Santa Clara",
            tcc_pct=73,
            elevation_m=120.5,
            slope_deg=18.2,
            aspect_deg=340.0,
            land_cover_code=42,
            land_cover_class="Evergreen Forest",
            batch_id="b-001",
        )
        assert loc.tcc_pct == 73
        assert loc.land_cover_code == 42
        assert loc.land_cover_class == "Evergreen Forest"

    def test_negative_elevation_allowed(self) -> None:
        # Below-sea-level CONUS locations exist (Death Valley, Salton Sea).
        loc = EnrichedLocation(
            location_id="L1",
            latitude=36.0,
            longitude=-116.8,
            elevation_m=-85.0,
            batch_id="b-001",
        )
        assert loc.elevation_m == -85.0


# ---------------------------------------------------------------------------
# ScoredLocation
# ---------------------------------------------------------------------------


class TestScoredLocation:
    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def test_full_high_risk(self) -> None:
        loc = ScoredLocation(
            location_id="L1",
            latitude=37.4,
            longitude=-122.1,
            state="CA",
            county="Santa Clara",
            tcc_pct=78,
            slope_deg=14.0,
            aspect_deg=355.0,
            land_cover_code=42,
            land_cover_class="Evergreen Forest",
            risk_score=0.72,
            risk_tier="High",
            tcc_score=1.0,
            terrain_score=0.5,
            landcover_score=1.0,
            batch_id="b-001",
            scored_at=self._now(),
        )
        assert loc.risk_tier == "High"
        assert loc.risk_score == pytest.approx(0.72)

    def test_unscored_when_all_inputs_missing(self) -> None:
        loc = ScoredLocation(
            location_id="L1",
            latitude=40.0,
            longitude=-100.0,
            risk_score=None,
            risk_tier="UNSCORED",
            batch_id="b-001",
            scored_at=self._now(),
        )
        assert loc.risk_tier == "UNSCORED"
        assert loc.risk_score is None
        assert loc.all_flags == []

    def test_missing_risk_tier_raises(self) -> None:
        with pytest.raises(ValidationError):
            ScoredLocation(  # type: ignore[call-arg]
                location_id="L1",
                latitude=40.0,
                longitude=-100.0,
                batch_id="b-001",
                scored_at=self._now(),
            )

    def test_missing_scored_at_raises(self) -> None:
        with pytest.raises(ValidationError):
            ScoredLocation(  # type: ignore[call-arg]
                location_id="L1",
                latitude=40.0,
                longitude=-100.0,
                risk_tier="UNSCORED",
                batch_id="b-001",
            )


# ---------------------------------------------------------------------------
# Config invariants
# ---------------------------------------------------------------------------


class TestConfigInvariants:
    def test_risk_weights_sum_to_one(self) -> None:
        total = config.TCC_WEIGHT + config.TERRAIN_WEIGHT + config.LANDCOVER_WEIGHT
        assert total == pytest.approx(1.0, abs=1e-9)

    def test_raster_batch_size_positive(self) -> None:
        # The legacy ``CLAUDE_BATCH_SIZE`` constant was removed in the Phase 7
        # pipeline-orchestration redesign: Claude no longer reasons per batch
        # of locations, so a per-Claude-call batch size is no longer relevant.
        # ``RASTER_BATCH_SIZE`` remains because it governs how many rows the
        # Environmental Agent processes per raster-handle-warmup cycle.
        assert config.RASTER_BATCH_SIZE > 0

    def test_nlcd_code_groups_are_disjoint(self) -> None:
        forest = set(config.FOREST_CODES)
        developed = set(config.DEVELOPED_CODES)
        open_ = set(config.OPEN_CODES)
        assert forest & developed == set()
        assert forest & open_ == set()
        assert developed & open_ == set()

    def test_risk_tier_thresholds_ordered(self) -> None:
        assert config.RISK_HIGH_THRESHOLD > config.RISK_MOD_THRESHOLD
        assert 0.0 < config.RISK_MOD_THRESHOLD < config.RISK_HIGH_THRESHOLD < 1.0

    def test_canopy_thresholds_ordered(self) -> None:
        assert config.CANOPY_HIGH_THRESHOLD > config.CANOPY_MOD_THRESHOLD
        assert 0 < config.CANOPY_MOD_THRESHOLD < config.CANOPY_HIGH_THRESHOLD <= 100

    def test_slope_thresholds_ordered(self) -> None:
        assert config.SLOPE_HIGH_THRESHOLD > config.SLOPE_MOD_THRESHOLD
        assert 0 < config.SLOPE_MOD_THRESHOLD < config.SLOPE_HIGH_THRESHOLD < 90

    def test_conus_bbox_sane(self) -> None:
        assert config.CONUS_LAT_MIN < config.CONUS_LAT_MAX
        assert config.CONUS_LON_MIN < config.CONUS_LON_MAX
        # Sanity: continental US is in the northern, western hemisphere.
        assert config.CONUS_LAT_MIN > 0
        assert config.CONUS_LON_MAX < 0

    def test_slope_raster_path_under_processed_dir(self) -> None:
        assert config.SLOPE_RASTER_PATH.parent == config.PROCESSED_DIR

    def test_expected_csv_columns_present(self) -> None:
        for col in ("location_id", "latitude", "longitude", "state", "county"):
            assert col in config.EXPECTED_CSV_COLUMNS
