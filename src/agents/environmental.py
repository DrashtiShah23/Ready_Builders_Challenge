"""
Environmental Data Agent: fetches all three environmental signals for each
location in a batch.

Why batch processing not per-row
--------------------------------
rasterio file handles are expensive to open: each ``rasterio.open`` parses
the GeoTIFF header, allocates buffers, and instantiates a CRS object. At
1M locations, reopening per-row would dominate runtime. The Phase 3 tools
already cache their dataset handles at module level, so a "warm" handle
is reused on every subsequent call. This agent calls a one-time
``_warm_caches`` step before the location loop so the open cost is paid
once per batch (and observable in the structured log), not per location.

Why not one external API call per location
------------------------------------------
At 1M locations, per-row API calls would take days and either hit rate
limits or cost a fortune. Every signal here is read from a local raster
that was downloaded once in Phase 2 — fast, deterministic, and offline
after the initial download.

Note on land cover
------------------
``fetch_land_cover`` uses the same EPSG:5070 transformer pattern as
``fetch_tcc`` because both are NLCD datasets (same CRS family). The
transformers are instantiated once per dataset by the Phase 3 tools and
reused across every call in the batch.

Missing-data contract
---------------------
Any environmental field that comes back null is reflected in
``EnrichedLocation.env_fetch_flags`` with a typed reason code:

    ``TCC_MISSING``        — TCC raster unreadable, NoData, or out-of-range
    ``ELEVATION_MISSING``  — no DEM tile contains the point, or NoData pixel
    ``SLOPE_MISSING``      — pre-computed slope raster absent or NoData ring
    ``ASPECT_MISSING``     — flat 3x3 window, edge pixel, or no DEM tile
    ``LANDCOVER_MISSING``  — land-cover raster unreadable or NoData pixel
    ``FETCH_EXCEPTION``    — a tool raised unexpectedly (safety-net only)

``ASPECT_MISSING`` is ambiguous (flat terrain genuinely has no aspect) and
is included only because the field can be null for non-failure reasons.
The composite v3.0 risk formula does NOT depend on aspect — it's persisted
into ``EnrichedLocation`` alongside slope so downstream consumers (the
validation tool's geographic-sanity check, the per-county summary, any
future hemisphere-aware scoring variant) have it on hand without
re-reading the DEM.
"""
from __future__ import annotations

import time
import uuid
from typing import Optional

from src.schemas.location import EnrichedLocation, ValidatedLocation
from src.tools import elevation as elevation_mod
from src.tools import landcover as landcover_mod
from src.tools import tcc as tcc_mod
from src.tools.elevation import fetch_elevation
from src.tools.landcover import fetch_land_cover
from src.tools.tcc import fetch_tcc
from src.utils.logger import PipelineLogger


class EnvFlag:
    """Environmental-data missing-reason codes (string constants for JSONL)."""

    TCC_MISSING = "TCC_MISSING"
    ELEVATION_MISSING = "ELEVATION_MISSING"
    SLOPE_MISSING = "SLOPE_MISSING"
    ASPECT_MISSING = "ASPECT_MISSING"
    LANDCOVER_MISSING = "LANDCOVER_MISSING"
    FETCH_EXCEPTION = "FETCH_EXCEPTION"


class EnvironmentalAgent:
    """Enrich a batch of ``ValidatedLocation`` with TCC, elevation, and land cover.

    Designed to be instantiated once per pipeline run and called repeatedly
    with batches from the Ingestion Agent. Module-level dataset caches
    survive across batches, so subsequent batches re-use the warm handles.
    """

    def __init__(self, logger: Optional[PipelineLogger] = None) -> None:
        """Initialise the per-run cumulative counters.

        ``logger`` is optional so the agent can be driven from a notebook
        without first wiring up the structured logger. ``cumulative_missing``
        is the per-signal running total across every batch in the run;
        :meth:`log_run_summary` reads it at end-of-run to emit the
        ``ENRICHMENT_SUMMARY`` event the orchestrator's ``score_risk``
        tool reasons about.
        """
        self.logger = logger or PipelineLogger(
            run_id=f"env-{uuid.uuid4().hex[:8]}"
        )
        self.cumulative_missing: dict[str, int] = {}
        self.batches_processed: int = 0
        self.locations_processed: int = 0

    # ------------------------------------------------------------------ helpers

    def _warm_caches(self, batch_id: str) -> dict[str, bool]:
        """Pre-open every raster handle BEFORE the per-location loop.

        Returns a dict of which datasets opened successfully. Calls into
        the Phase 3 tools' private cache primitives — that's deliberate:
        the agent is the right layer to *coordinate* cache lifecycle even
        though each tool *owns* its own cache.
        """
        tcc_ok = tcc_mod._ensure_dataset() is not None
        lc_ok = landcover_mod._ensure_dataset() is not None
        slope_ok = elevation_mod._ensure_slope_dataset() is not None
        elevation_mod._build_dem_index()
        dem_ok = len(elevation_mod._dem_index) > 0

        status = {
            "tcc": tcc_ok,
            "landcover": lc_ok,
            "slope": slope_ok,
            "dem": dem_ok,
        }
        self.logger.info(
            stage="environmental",
            event_type="CACHE_WARMED",
            batch_id=batch_id,
            detail={"status": status, "dem_tile_count": len(elevation_mod._dem_index)},
        )
        return status

    def _enrich_one(
        self, loc: ValidatedLocation
    ) -> tuple[EnrichedLocation, dict[str, bool]]:
        """Enrich a single location. Returns ``(enriched, missing_dict)``.

        ``missing_dict`` records which of the five tracked signals came
        back missing, so the batch loop can aggregate counts without
        re-inspecting ``env_fetch_flags``.
        """
        flags: list[str] = []
        missing: dict[str, bool] = {
            "tcc": False, "elevation": False, "slope": False,
            "aspect": False, "landcover": False,
        }

        # Default-null payload in case any tool raises unexpectedly.
        tcc_pct: Optional[int] = None
        elev_m: Optional[float] = None
        slope_deg: Optional[float] = None
        aspect_deg: Optional[float] = None
        lc_code: Optional[int] = None
        lc_class: Optional[str] = None

        try:
            tcc_result = fetch_tcc(loc.latitude, loc.longitude)
            tcc_pct = tcc_result.get("tcc_pct")
            if tcc_result.get("tcc_missing"):
                flags.append(EnvFlag.TCC_MISSING)
                missing["tcc"] = True
        except Exception as exc:
            self._log_fetch_exception(loc, "tcc", exc)
            flags.append(EnvFlag.TCC_MISSING)
            flags.append(EnvFlag.FETCH_EXCEPTION)
            missing["tcc"] = True

        try:
            elev_result = fetch_elevation(loc.latitude, loc.longitude)
            elev_m = elev_result.get("elevation_m")
            slope_deg = elev_result.get("slope_deg")
            aspect_deg = elev_result.get("aspect_deg")
            if elev_result.get("elevation_missing"):
                flags.append(EnvFlag.ELEVATION_MISSING)
                missing["elevation"] = True
            if slope_deg is None:
                flags.append(EnvFlag.SLOPE_MISSING)
                missing["slope"] = True
            if aspect_deg is None:
                flags.append(EnvFlag.ASPECT_MISSING)
                missing["aspect"] = True
        except Exception as exc:
            self._log_fetch_exception(loc, "elevation", exc)
            flags.append(EnvFlag.ELEVATION_MISSING)
            flags.append(EnvFlag.SLOPE_MISSING)
            flags.append(EnvFlag.ASPECT_MISSING)
            flags.append(EnvFlag.FETCH_EXCEPTION)
            missing["elevation"] = True
            missing["slope"] = True
            missing["aspect"] = True

        try:
            lc_result = fetch_land_cover(loc.latitude, loc.longitude)
            lc_code = lc_result.get("land_cover_code")
            lc_class = lc_result.get("land_cover_class")
            if lc_result.get("lc_missing"):
                flags.append(EnvFlag.LANDCOVER_MISSING)
                missing["landcover"] = True
        except Exception as exc:
            self._log_fetch_exception(loc, "landcover", exc)
            flags.append(EnvFlag.LANDCOVER_MISSING)
            flags.append(EnvFlag.FETCH_EXCEPTION)
            missing["landcover"] = True

        enriched = EnrichedLocation(
            location_id=loc.location_id,
            latitude=loc.latitude,
            longitude=loc.longitude,
            state=loc.state,
            county=loc.county,
            tcc_pct=tcc_pct,
            elevation_m=elev_m,
            slope_deg=slope_deg,
            aspect_deg=aspect_deg,
            land_cover_code=lc_code,
            land_cover_class=lc_class,
            env_fetch_flags=flags,
            batch_id=loc.batch_id,
        )
        return enriched, missing

    def _log_fetch_exception(
        self, loc: ValidatedLocation, tool: str, exc: Exception
    ) -> None:
        self.logger.error(
            stage="environmental",
            event_type="FETCH_EXCEPTION",
            location_id=loc.location_id,
            batch_id=loc.batch_id,
            detail={
                "tool": tool,
                "exception_type": type(exc).__name__,
                "exception": str(exc),
            },
        )

    # --------------------------------------------------------------- public API

    def enrich_batch(
        self, batch: list[ValidatedLocation]
    ) -> list[EnrichedLocation]:
        """Enrich one batch of validated locations.

        Side effects:
            * Logs ``ENRICH_BATCH_START`` (with size), ``CACHE_WARMED``,
              and ``ENRICH_BATCH_DONE`` (with per-signal missing counts).
            * Updates ``self.cumulative_missing`` and the batch / location
              counters.
        """
        if not batch:
            self.logger.warning(
                stage="environmental",
                event_type="EMPTY_BATCH",
                detail={"size": 0},
            )
            return []

        batch_id = batch[0].batch_id
        self.logger.info(
            stage="environmental",
            event_type="ENRICH_BATCH_START",
            batch_id=batch_id,
            detail={"size": len(batch)},
        )
        start = time.monotonic()

        self._warm_caches(batch_id)

        missing_counts: dict[str, int] = {
            "tcc": 0, "elevation": 0, "slope": 0, "aspect": 0, "landcover": 0,
        }
        results: list[EnrichedLocation] = []
        for loc in batch:
            enriched, missing = self._enrich_one(loc)
            results.append(enriched)
            for key, was_missing in missing.items():
                if was_missing:
                    missing_counts[key] += 1

        elapsed_ms = int((time.monotonic() - start) * 1000)
        self.batches_processed += 1
        self.locations_processed += len(batch)
        for key, count in missing_counts.items():
            if count:
                self.cumulative_missing[key] = (
                    self.cumulative_missing.get(key, 0) + count
                )

        self.logger.info(
            stage="environmental",
            event_type="ENRICH_BATCH_DONE",
            batch_id=batch_id,
            detail={
                "size": len(batch),
                "missing_tcc": missing_counts["tcc"],
                "missing_elevation": missing_counts["elevation"],
                "missing_slope": missing_counts["slope"],
                "missing_aspect": missing_counts["aspect"],
                "missing_landcover": missing_counts["landcover"],
                "missing_rates": {
                    key: round(count / len(batch), 4)
                    for key, count in missing_counts.items()
                },
            },
            duration_ms=elapsed_ms,
        )
        return results

    def log_run_summary(self) -> None:
        """Emit a final summary across every batch processed so far.

        Called by the orchestrator (Phase 7) once ingestion is exhausted.
        Kept as a public method so the agent can be unit-tested in isolation.
        """
        self.logger.info(
            stage="environmental",
            event_type="ENRICHMENT_SUMMARY",
            detail={
                "batches_processed": self.batches_processed,
                "locations_processed": self.locations_processed,
                "cumulative_missing": dict(self.cumulative_missing),
                "cumulative_missing_rates": {
                    key: round(count / self.locations_processed, 4)
                    for key, count in self.cumulative_missing.items()
                } if self.locations_processed else {},
            },
        )
