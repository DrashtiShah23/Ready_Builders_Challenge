"""
Phase 5 tests: Environmental Data Agent.

Every test mocks ``fetch_tcc``, ``fetch_elevation``, and ``fetch_land_cover``
plus the cache-warming primitives so the agent's flag-aggregation and
batch-logging logic is exercised in isolation from real raster I/O.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.agents import environmental as env_mod
from src.agents.environmental import EnvFlag, EnvironmentalAgent
from src.schemas.location import EnrichedLocation, ValidatedLocation


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _CaptureLogger:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.run_id = "capture"
        self.log_path = Path("/dev/null")

    def _record(self, level: str, stage: str, event_type: str, **kwargs: Any) -> None:
        self.events.append(
            {"level": level, "stage": stage, "event_type": event_type, **kwargs}
        )

    def info(self, stage: str, event_type: str, **kwargs: Any) -> None:
        self._record("INFO", stage, event_type, **kwargs)

    def warning(self, stage: str, event_type: str, **kwargs: Any) -> None:
        self._record("WARNING", stage, event_type, **kwargs)

    def error(self, stage: str, event_type: str, **kwargs: Any) -> None:
        self._record("ERROR", stage, event_type, **kwargs)


def _loc(loc_id: str, batch_id: str = "batch-000000") -> ValidatedLocation:
    return ValidatedLocation(
        location_id=loc_id,
        latitude=40.0,
        longitude=-100.0,
        state="NE",
        county="Lancaster",
        batch_id=batch_id,
    )


def _result_ok(tcc: int = 50, elev: float = 1500.0, slope: float = 5.0,
               aspect: float = 180.0, lc_code: int = 42,
               lc_class: str = "Evergreen Forest") -> dict[str, dict]:
    """Return three tool-result dicts that all signal 'present'."""
    return {
        "tcc": {"tcc_pct": tcc, "tcc_missing": False},
        "elev": {
            "elevation_m": elev,
            "slope_deg": slope,
            "aspect_deg": aspect,
            "elevation_missing": False,
        },
        "lc": {"land_cover_code": lc_code, "land_cover_class": lc_class, "lc_missing": False},
    }


def _patch_tools(monkeypatch: pytest.MonkeyPatch, fixed_result: dict[str, dict],
                 call_log: list[str] | None = None) -> None:
    """Patch the three tool functions to return ``fixed_result`` deterministically.

    If ``call_log`` is supplied, every call is appended for assertion.
    """
    def _tcc(_lat: float, _lon: float) -> dict:
        if call_log is not None:
            call_log.append("tcc")
        return fixed_result["tcc"]

    def _elev(_lat: float, _lon: float) -> dict:
        if call_log is not None:
            call_log.append("elev")
        return fixed_result["elev"]

    def _lc(_lat: float, _lon: float) -> dict:
        if call_log is not None:
            call_log.append("lc")
        return fixed_result["lc"]

    monkeypatch.setattr(env_mod, "fetch_tcc", _tcc)
    monkeypatch.setattr(env_mod, "fetch_elevation", _elev)
    monkeypatch.setattr(env_mod, "fetch_land_cover", _lc)


def _patch_cache_warmers(monkeypatch: pytest.MonkeyPatch,
                         warm_calls: list[str] | None = None,
                         dem_tiles: int = 3) -> None:
    """Patch the cache-warming primitives so no real raster I/O happens."""

    def _ensure_tcc():
        if warm_calls is not None:
            warm_calls.append("tcc_ensure")
        return object()

    def _ensure_lc():
        if warm_calls is not None:
            warm_calls.append("lc_ensure")
        return object()

    def _ensure_slope():
        if warm_calls is not None:
            warm_calls.append("slope_ensure")
        return object()

    def _build_idx() -> None:
        if warm_calls is not None:
            warm_calls.append("dem_build")

    monkeypatch.setattr(env_mod.tcc_mod, "_ensure_dataset", _ensure_tcc)
    monkeypatch.setattr(env_mod.landcover_mod, "_ensure_dataset", _ensure_lc)
    monkeypatch.setattr(env_mod.elevation_mod, "_ensure_slope_dataset", _ensure_slope)
    monkeypatch.setattr(env_mod.elevation_mod, "_build_dem_index", _build_idx)
    monkeypatch.setattr(env_mod.elevation_mod, "_dem_index",
                        [(Path(f"t{i}.tif"), None, "EPSG:4326") for i in range(dem_tiles)])


@pytest.fixture
def logger() -> _CaptureLogger:
    return _CaptureLogger()


@pytest.fixture
def agent(logger: _CaptureLogger) -> EnvironmentalAgent:
    return EnvironmentalAgent(logger=logger)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_full_signal_batch_enriched(
        self,
        agent: EnvironmentalAgent,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_cache_warmers(monkeypatch)
        _patch_tools(monkeypatch, _result_ok())

        batch = [_loc(f"L{i}") for i in range(5)]
        enriched = agent.enrich_batch(batch)

        assert len(enriched) == 5
        assert all(isinstance(e, EnrichedLocation) for e in enriched)
        for e in enriched:
            assert e.tcc_pct == 50
            assert e.elevation_m == 1500.0
            assert e.slope_deg == 5.0
            assert e.aspect_deg == 180.0
            assert e.land_cover_code == 42
            assert e.land_cover_class == "Evergreen Forest"
            assert e.env_fetch_flags == []

    def test_batch_id_preserved_per_location(
        self,
        agent: EnvironmentalAgent,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_cache_warmers(monkeypatch)
        _patch_tools(monkeypatch, _result_ok())

        batch = [_loc(f"L{i}", batch_id="batch-000042") for i in range(3)]
        enriched = agent.enrich_batch(batch)
        assert all(e.batch_id == "batch-000042" for e in enriched)

    def test_validated_location_fields_carried_through(
        self,
        agent: EnvironmentalAgent,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_cache_warmers(monkeypatch)
        _patch_tools(monkeypatch, _result_ok())

        loc = ValidatedLocation(
            location_id="LX",
            latitude=42.5,
            longitude=-105.3,
            state="WY",
            county="Albany",
            batch_id="batch-000000",
        )
        [enriched] = agent.enrich_batch([loc])
        assert enriched.location_id == "LX"
        assert enriched.latitude == 42.5
        assert enriched.longitude == -105.3
        assert enriched.state == "WY"
        assert enriched.county == "Albany"


# ---------------------------------------------------------------------------
# Missing-signal flagging
# ---------------------------------------------------------------------------


class TestMissingFlags:
    def test_tcc_missing(
        self, agent: EnvironmentalAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results = _result_ok()
        results["tcc"] = {"tcc_pct": None, "tcc_missing": True, "reason": "NoData pixel"}
        _patch_cache_warmers(monkeypatch)
        _patch_tools(monkeypatch, results)

        [enriched] = agent.enrich_batch([_loc("L1")])
        assert enriched.tcc_pct is None
        assert EnvFlag.TCC_MISSING in enriched.env_fetch_flags
        assert EnvFlag.ELEVATION_MISSING not in enriched.env_fetch_flags

    def test_elevation_missing(
        self, agent: EnvironmentalAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results = _result_ok()
        results["elev"] = {
            "elevation_m": None,
            "slope_deg": 3.0,
            "aspect_deg": 90.0,
            "elevation_missing": True,
            "reason": "No DEM tile contains the coordinate",
        }
        _patch_cache_warmers(monkeypatch)
        _patch_tools(monkeypatch, results)

        [enriched] = agent.enrich_batch([_loc("L1")])
        assert enriched.elevation_m is None
        assert enriched.slope_deg == 3.0
        assert EnvFlag.ELEVATION_MISSING in enriched.env_fetch_flags
        assert EnvFlag.SLOPE_MISSING not in enriched.env_fetch_flags

    def test_slope_missing(
        self, agent: EnvironmentalAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results = _result_ok()
        results["elev"] = {
            "elevation_m": 1000.0,
            "slope_deg": None,
            "aspect_deg": 90.0,
            "elevation_missing": False,
        }
        _patch_cache_warmers(monkeypatch)
        _patch_tools(monkeypatch, results)

        [enriched] = agent.enrich_batch([_loc("L1")])
        assert enriched.slope_deg is None
        assert EnvFlag.SLOPE_MISSING in enriched.env_fetch_flags
        assert EnvFlag.ELEVATION_MISSING not in enriched.env_fetch_flags

    def test_aspect_missing(
        self, agent: EnvironmentalAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results = _result_ok()
        results["elev"] = {
            "elevation_m": 1000.0,
            "slope_deg": 0.0,
            "aspect_deg": None,
            "elevation_missing": False,
        }
        _patch_cache_warmers(monkeypatch)
        _patch_tools(monkeypatch, results)

        [enriched] = agent.enrich_batch([_loc("L1")])
        assert enriched.aspect_deg is None
        assert EnvFlag.ASPECT_MISSING in enriched.env_fetch_flags

    def test_landcover_missing(
        self, agent: EnvironmentalAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results = _result_ok()
        results["lc"] = {
            "land_cover_code": None,
            "land_cover_class": "UNKNOWN",
            "lc_missing": True,
            "reason": "NoData pixel",
        }
        _patch_cache_warmers(monkeypatch)
        _patch_tools(monkeypatch, results)

        [enriched] = agent.enrich_batch([_loc("L1")])
        assert enriched.land_cover_code is None
        assert enriched.land_cover_class == "UNKNOWN"
        assert EnvFlag.LANDCOVER_MISSING in enriched.env_fetch_flags

    def test_all_signals_missing(
        self, agent: EnvironmentalAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results = {
            "tcc": {"tcc_pct": None, "tcc_missing": True},
            "elev": {
                "elevation_m": None,
                "slope_deg": None,
                "aspect_deg": None,
                "elevation_missing": True,
            },
            "lc": {"land_cover_code": None, "land_cover_class": "UNKNOWN", "lc_missing": True},
        }
        _patch_cache_warmers(monkeypatch)
        _patch_tools(monkeypatch, results)

        [enriched] = agent.enrich_batch([_loc("L1")])
        assert set(enriched.env_fetch_flags) == {
            EnvFlag.TCC_MISSING,
            EnvFlag.ELEVATION_MISSING,
            EnvFlag.SLOPE_MISSING,
            EnvFlag.ASPECT_MISSING,
            EnvFlag.LANDCOVER_MISSING,
        }


# ---------------------------------------------------------------------------
# Batch-level behaviour
# ---------------------------------------------------------------------------


class TestBatchBehaviour:
    def test_each_tool_called_once_per_location(
        self,
        agent: EnvironmentalAgent,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        call_log: list[str] = []
        _patch_cache_warmers(monkeypatch)
        _patch_tools(monkeypatch, _result_ok(), call_log=call_log)

        agent.enrich_batch([_loc(f"L{i}") for i in range(4)])
        assert call_log.count("tcc") == 4
        assert call_log.count("elev") == 4
        assert call_log.count("lc") == 4

    def test_warm_caches_called_once_per_batch(
        self,
        agent: EnvironmentalAgent,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        warm_calls: list[str] = []
        _patch_cache_warmers(monkeypatch, warm_calls=warm_calls)
        _patch_tools(monkeypatch, _result_ok())

        agent.enrich_batch([_loc(f"L{i}") for i in range(10)])
        # Each warmer called exactly once for the whole batch, not per location.
        assert warm_calls.count("tcc_ensure") == 1
        assert warm_calls.count("lc_ensure") == 1
        assert warm_calls.count("slope_ensure") == 1
        assert warm_calls.count("dem_build") == 1

    def test_batch_stats_logged_with_required_counts(
        self,
        logger: _CaptureLogger,
        agent: EnvironmentalAgent,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_cache_warmers(monkeypatch)
        # Custom side-effect: alternating TCC presence (3 missing of 5).
        states = iter([
            {"tcc_pct": None, "tcc_missing": True},   # L0 missing
            {"tcc_pct": 30, "tcc_missing": False},
            {"tcc_pct": None, "tcc_missing": True},   # L2 missing
            {"tcc_pct": 50, "tcc_missing": False},
            {"tcc_pct": None, "tcc_missing": True},   # L4 missing
        ])
        monkeypatch.setattr(env_mod, "fetch_tcc", lambda _lat, _lon: next(states))
        monkeypatch.setattr(env_mod, "fetch_elevation",
                            lambda _lat, _lon: _result_ok()["elev"])
        monkeypatch.setattr(env_mod, "fetch_land_cover",
                            lambda _lat, _lon: _result_ok()["lc"])

        agent.enrich_batch([_loc(f"L{i}") for i in range(5)])

        done_events = [e for e in logger.events if e["event_type"] == "ENRICH_BATCH_DONE"]
        assert len(done_events) == 1
        detail = done_events[0]["detail"]
        assert detail["size"] == 5
        assert detail["missing_tcc"] == 3
        assert detail["missing_elevation"] == 0
        assert detail["missing_landcover"] == 0
        # Also confirm rate calculation.
        assert detail["missing_rates"]["tcc"] == 0.6

    def test_batch_start_and_done_logged(
        self,
        logger: _CaptureLogger,
        agent: EnvironmentalAgent,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_cache_warmers(monkeypatch)
        _patch_tools(monkeypatch, _result_ok())
        agent.enrich_batch([_loc("L1", batch_id="batch-000007")])

        starts = [e for e in logger.events if e["event_type"] == "ENRICH_BATCH_START"]
        dones = [e for e in logger.events if e["event_type"] == "ENRICH_BATCH_DONE"]
        assert len(starts) == len(dones) == 1
        assert starts[0]["batch_id"] == "batch-000007"
        assert dones[0]["batch_id"] == "batch-000007"

    def test_cache_warmed_event_logged_with_status(
        self,
        logger: _CaptureLogger,
        agent: EnvironmentalAgent,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_cache_warmers(monkeypatch, dem_tiles=4)
        _patch_tools(monkeypatch, _result_ok())
        agent.enrich_batch([_loc("L1")])

        cache_events = [e for e in logger.events if e["event_type"] == "CACHE_WARMED"]
        assert len(cache_events) == 1
        status = cache_events[0]["detail"]["status"]
        assert status == {"tcc": True, "landcover": True, "slope": True, "dem": True}
        assert cache_events[0]["detail"]["dem_tile_count"] == 4

    def test_empty_batch_returns_empty_and_logs(
        self,
        logger: _CaptureLogger,
        agent: EnvironmentalAgent,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_cache_warmers(monkeypatch)
        _patch_tools(monkeypatch, _result_ok())
        assert agent.enrich_batch([]) == []
        assert any(e["event_type"] == "EMPTY_BATCH" for e in logger.events)
        # Don't warm caches for an empty batch.
        assert not any(e["event_type"] == "CACHE_WARMED" for e in logger.events)


# ---------------------------------------------------------------------------
# Resilience: exceptions in tools
# ---------------------------------------------------------------------------


class TestResilience:
    def test_exception_in_tcc_does_not_kill_batch(
        self,
        logger: _CaptureLogger,
        agent: EnvironmentalAgent,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_cache_warmers(monkeypatch)

        def _raise(*_args: Any, **_kwargs: Any) -> dict:
            raise RuntimeError("simulated rasterio crash")

        monkeypatch.setattr(env_mod, "fetch_tcc", _raise)
        monkeypatch.setattr(env_mod, "fetch_elevation",
                            lambda _lat, _lon: _result_ok()["elev"])
        monkeypatch.setattr(env_mod, "fetch_land_cover",
                            lambda _lat, _lon: _result_ok()["lc"])

        batch = [_loc(f"L{i}") for i in range(3)]
        enriched = agent.enrich_batch(batch)

        assert len(enriched) == 3
        for e in enriched:
            assert e.tcc_pct is None
            assert EnvFlag.TCC_MISSING in e.env_fetch_flags
            assert EnvFlag.FETCH_EXCEPTION in e.env_fetch_flags
            # Elevation and landcover should still come through.
            assert e.elevation_m == 1500.0
            assert e.land_cover_code == 42

        exception_logs = [e for e in logger.events if e["event_type"] == "FETCH_EXCEPTION"]
        assert len(exception_logs) == 3
        assert all(e["detail"]["tool"] == "tcc" for e in exception_logs)

    def test_exception_in_elevation_only_affects_elev_signals(
        self,
        agent: EnvironmentalAgent,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_cache_warmers(monkeypatch)
        monkeypatch.setattr(env_mod, "fetch_tcc",
                            lambda _lat, _lon: _result_ok()["tcc"])
        monkeypatch.setattr(env_mod, "fetch_elevation",
                            lambda _lat, _lon: (_ for _ in ()).throw(IOError("boom")))
        monkeypatch.setattr(env_mod, "fetch_land_cover",
                            lambda _lat, _lon: _result_ok()["lc"])

        [enriched] = agent.enrich_batch([_loc("L1")])
        assert enriched.tcc_pct == 50
        assert enriched.land_cover_code == 42
        assert set(enriched.env_fetch_flags) >= {
            EnvFlag.ELEVATION_MISSING,
            EnvFlag.SLOPE_MISSING,
            EnvFlag.ASPECT_MISSING,
            EnvFlag.FETCH_EXCEPTION,
        }


# ---------------------------------------------------------------------------
# Cumulative summary
# ---------------------------------------------------------------------------


class TestCumulativeSummary:
    def test_across_multiple_batches(
        self,
        logger: _CaptureLogger,
        agent: EnvironmentalAgent,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_cache_warmers(monkeypatch)
        results = _result_ok()
        # Make every TCC missing so the cumulative counter is easy to verify.
        results["tcc"] = {"tcc_pct": None, "tcc_missing": True}
        _patch_tools(monkeypatch, results)

        agent.enrich_batch([_loc(f"A{i}", batch_id="batch-000000") for i in range(3)])
        agent.enrich_batch([_loc(f"B{i}", batch_id="batch-000001") for i in range(2)])

        assert agent.batches_processed == 2
        assert agent.locations_processed == 5
        assert agent.cumulative_missing["tcc"] == 5

        agent.log_run_summary()
        summaries = [e for e in logger.events if e["event_type"] == "ENRICHMENT_SUMMARY"]
        assert len(summaries) == 1
        detail = summaries[0]["detail"]
        assert detail["batches_processed"] == 2
        assert detail["locations_processed"] == 5
        assert detail["cumulative_missing"]["tcc"] == 5
        assert detail["cumulative_missing_rates"]["tcc"] == 1.0

    def test_run_summary_safe_with_zero_locations(
        self,
        logger: _CaptureLogger,
        agent: EnvironmentalAgent,
    ) -> None:
        agent.log_run_summary()
        summary = next(e for e in logger.events if e["event_type"] == "ENRICHMENT_SUMMARY")
        assert summary["detail"]["locations_processed"] == 0
        assert summary["detail"]["cumulative_missing_rates"] == {}
