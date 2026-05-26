"""
Ingestion Agent: validates, deduplicates, and batches the input locations CSV.

Design decisions
----------------
- **Chunked CSV reading** (``chunksize=10_000``) so a 1M-row file never loads
  whole into memory. The chunk size is large enough that the per-chunk
  overhead is negligible but small enough to keep peak RSS well under 1 GB
  even on a small laptop.
- **Pydantic validation on every row.** Type errors, coercion failures, and
  range violations are caught at the ingestion boundary, where the error
  context is richest, instead of leaking downstream and crashing the
  Environmental Data Agent on a malformed batch.
- **Invalid rows are dropped with a typed reason code, never silently
  passed.** Every drop is logged to the structured JSONL log so a reviewer
  can later compute data-quality metrics from `logs/`.
- **First-occurrence-wins deduplication.** Repeat ``location_id`` values
  are dropped after the first valid record — they cannot represent the
  same point twice in the same run.
- **Batched output**: the agent yields lists of ``ValidatedLocation`` of
  size ``CLAUDE_BATCH_SIZE`` to keep memory pressure low on the next
  stage. Each batch has a stable, monotonic ``batch_id`` (``batch-000000``,
  ``batch-000001``, …) so a single failing record can be traced end-to-end
  through the log.

Reason codes
------------
``NULL_LOCATION_ID``  — ``location_id`` is null or empty
``NULL_COORDINATE``   — ``latitude`` or ``longitude`` is null
``PARSE_ERROR``       — Pydantic validation failed (e.g. non-numeric coord
                        that pandas left as a string)
``OUT_OF_BOUNDS``     — coordinate outside the CONUS bounding box
``INVALID_STATE``     — ``state`` provided but not in ``config.STATE_FIPS``
``DUPLICATE_DROPPED`` — ``location_id`` already seen in this run

Geoid derivation
----------------
If the CSV exposes a ``geoid_cb`` column (15-digit Census Block GEOID) and the
row's ``state`` is missing or null, state and county are derived from the
GEOID prefix: digits 1–2 = state FIPS, digits 1–5 = canonical county GEOID.
Explicit user-supplied ``state`` always wins — derivation is the fallback,
not an override.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Optional

import pandas as pd
from pydantic import ValidationError

from src import config
from src.schemas.location import RawLocation, ValidatedLocation
from src.utils.logger import PipelineLogger


CHUNK_SIZE: int = 10_000


class Reason:
    """Drop reason codes — strings rather than an Enum so they serialise
    cleanly into the structured JSONL log."""

    NULL_LOCATION_ID = "NULL_LOCATION_ID"
    NULL_COORDINATE = "NULL_COORDINATE"
    PARSE_ERROR = "PARSE_ERROR"
    OUT_OF_BOUNDS = "OUT_OF_BOUNDS"
    INVALID_STATE = "INVALID_STATE"
    DUPLICATE_DROPPED = "DUPLICATE_DROPPED"


_REQUIRED_COLUMNS: tuple[str, ...] = ("location_id", "latitude", "longitude")
_OPTIONAL_COLUMNS: tuple[str, ...] = ("state", "county")


def _is_null(value: Any) -> bool:
    """``True`` if ``value`` is null in a CSV-ish sense (None, NaN, empty)."""
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    # pandas.NA, numpy.nan, and other scalar nulls.
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        # ``pd.isna`` raises on unhashable / array-like inputs, which we
        # don't expect here but want to gracefully treat as "not null".
        pass
    return False


def _derive_state_county_from_geoid(
    geoid_raw: Any,
) -> tuple[Optional[str], Optional[str]]:
    """Derive (state_abbr, county_geoid) from a Census Block GEOID.

    A Census Block GEOID is canonically 15 digits:
        2 (state FIPS) + 3 (county FIPS) + 6 (tract) + 4 (block).
    We use the first 2 chars for the state lookup and the first 5 chars
    (state + county FIPS) as the canonical county identifier — that
    5-digit form is the standard Census GEOID for a county and is
    unambiguous across state boundaries.

    Strict input contract: exactly 15 digits. No zero-padding, no
    leniency. Why: an exporter that strips the leading zero from
    ``"01..."`` (Alabama) leaves ``"1..."``, which is indistinguishable
    from the legitimate FIPS prefixes ``10``–``19`` (DE, DC, FL, GA, HI,
    ID, IL, IN, IA). Without a way to disambiguate, the conservative
    answer is to refuse to derive for any input that isn't already a
    full 15-digit GEOID. Callers see ``(None, None)`` and the affected
    rows simply pass through with ``state=None`` — the explicit
    ``state`` column on the input CSV (if any) remains the override.

    Returns ``(None, None)`` for: null inputs, non-numeric strings,
    inputs of any length other than 15, or any GEOID whose state FIPS
    isn't a CONUS state we recognise (AK=02, HI=15, PR=72 etc. all fall
    here — they are out of scope for this pipeline).
    """
    if _is_null(geoid_raw):
        return None, None
    text = str(geoid_raw).strip()
    if len(text) != 15 or not text.isdigit():
        return None, None
    state_fips = text[:2]
    county_geoid = text[:5]
    state_abbr = config.STATE_FIPS_TO_ABBR.get(state_fips)
    if state_abbr is None:
        return None, None
    return state_abbr, county_geoid


class IngestionAgent:
    """Read, validate, dedup, and batch the input locations CSV.

    Designed to be instantiated once per pipeline run. After ``run()``
    finishes, ``self.stats`` holds the final per-reason drop counts and
    ``self.seen_ids`` holds every successfully ingested ``location_id``.
    """

    def __init__(self, logger: Optional[PipelineLogger] = None) -> None:
        self.logger = logger or PipelineLogger(
            run_id=f"ingest-{uuid.uuid4().hex[:8]}"
        )
        self.stats: dict[str, int] = {}
        self.seen_ids: set[str] = set()
        self.total_rows: int = 0
        self.valid_count: int = 0

    # ------------------------------------------------------------------ helpers

    def _record_drop(
        self,
        reason: str,
        location_id: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        self.stats[reason] = self.stats.get(reason, 0) + 1
        self.logger.warning(
            stage="ingestion",
            event_type="INVALID_ROW",
            location_id=location_id,
            detail={"reason": reason, **(detail or {})},
        )

    def _validate_row(
        self, record: dict[str, Any], batch_id: str
    ) -> Optional[ValidatedLocation]:
        """Validate a single CSV row dict.

        Returns the resulting ``ValidatedLocation`` on success, or ``None`` if
        the row was dropped (in which case the drop has already been logged).

        Check order is intentional and worth defending:
            1. ``location_id`` — needed to identify drops in the log.
            2. dedup — cheapest check; rejects whole-row duplicates early.
            3. coordinates — null check before Pydantic so the reason code
               is precise (``NULL_COORDINATE`` vs the more generic
               ``PARSE_ERROR``).
            4. Pydantic validation — catches type errors that survived
               pandas dtype coercion.
            5. CONUS bbox — the cheap range check the schema can't express.
            6. State — only validates non-null values; absent state is fine.
        """
        # --- location_id ---
        loc_id_raw = record.get("location_id")
        if _is_null(loc_id_raw):
            self._record_drop(Reason.NULL_LOCATION_ID)
            return None
        location_id = str(loc_id_raw).strip()
        if not location_id:
            self._record_drop(Reason.NULL_LOCATION_ID)
            return None

        # --- dedup ---
        if location_id in self.seen_ids:
            self._record_drop(Reason.DUPLICATE_DROPPED, location_id=location_id)
            return None

        # --- coordinates: explicit null check for a precise reason code ---
        latitude_raw = record.get("latitude")
        longitude_raw = record.get("longitude")
        if _is_null(latitude_raw) or _is_null(longitude_raw):
            self._record_drop(Reason.NULL_COORDINATE, location_id=location_id)
            return None

        # --- Pydantic: coercion + type validation ---
        state_raw = record.get("state")
        county_raw = record.get("county")
        # Fallback: derive state (and county GEOID) from `geoid_cb` when the
        # explicit `state` field is missing or null. User-provided state
        # always wins over derived state.
        if _is_null(state_raw):
            derived_state, derived_county = _derive_state_county_from_geoid(
                record.get("geoid_cb")
            )
            if derived_state is not None:
                state_raw = derived_state
                if _is_null(county_raw):
                    county_raw = derived_county
        try:
            parsed = RawLocation(
                location_id=location_id,
                latitude=latitude_raw,
                longitude=longitude_raw,
                state=None if _is_null(state_raw) else str(state_raw).strip() or None,
                county=None if _is_null(county_raw) else str(county_raw).strip() or None,
            )
        except ValidationError as exc:
            self._record_drop(
                Reason.PARSE_ERROR,
                location_id=location_id,
                detail={"error": str(exc)},
            )
            return None

        # --- CONUS bounds ---
        if not (config.CONUS_LAT_MIN <= parsed.latitude <= config.CONUS_LAT_MAX) or \
           not (config.CONUS_LON_MIN <= parsed.longitude <= config.CONUS_LON_MAX):
            self._record_drop(
                Reason.OUT_OF_BOUNDS,
                location_id=location_id,
                detail={"latitude": parsed.latitude, "longitude": parsed.longitude},
            )
            return None

        # --- state ---
        canonical_state: Optional[str] = None
        if parsed.state is not None:
            canonical = parsed.state.upper()
            if canonical not in config.STATE_FIPS:
                self._record_drop(
                    Reason.INVALID_STATE,
                    location_id=location_id,
                    detail={"state": parsed.state},
                )
                return None
            canonical_state = canonical

        # --- accepted ---
        self.seen_ids.add(location_id)
        return ValidatedLocation(
            location_id=location_id,
            latitude=parsed.latitude,
            longitude=parsed.longitude,
            state=canonical_state,
            county=parsed.county,
            batch_id=batch_id,
        )

    # --------------------------------------------------------------- public API

    def run(
        self,
        csv_path: Path | str,
        batch_size: Optional[int] = None,
    ) -> Iterator[list[ValidatedLocation]]:
        """Read ``csv_path`` and yield batches of ``ValidatedLocation``.

        Parameters
        ----------
        csv_path:
            Path to the input CSV.
        batch_size:
            Number of validated rows per emitted batch. Defaults to
            ``config.CLAUDE_BATCH_SIZE``.

        Yields
        ------
        list[ValidatedLocation]
            Batches of size ``batch_size`` (the last batch may be smaller).

        Raises
        ------
        FileNotFoundError
            If ``csv_path`` does not exist.
        ValueError
            If the CSV is missing any of the required columns
            (``location_id``, ``latitude``, ``longitude``).
        """
        csv_path = Path(csv_path)
        if not csv_path.exists():
            self.logger.error(
                stage="ingestion",
                event_type="CSV_NOT_FOUND",
                detail={"csv_path": str(csv_path)},
            )
            raise FileNotFoundError(f"Locations CSV not found: {csv_path}")

        size = batch_size if batch_size is not None else config.CLAUDE_BATCH_SIZE
        self.logger.info(
            stage="ingestion",
            event_type="INGESTION_START",
            detail={"csv_path": str(csv_path), "batch_size": size, "chunk_size": CHUNK_SIZE},
        )
        start = time.monotonic()

        batch_counter = 0
        current_batch: list[ValidatedLocation] = []

        chunk_iter = pd.read_csv(
            csv_path,
            chunksize=CHUNK_SIZE,
            # ``geoid_cb`` is forced to string so leading zeros (state FIPS
            # codes 01–09) survive the read. pandas silently ignores dtype
            # keys for columns that aren't present, so this is backward-
            # compatible with CSVs that don't include the column.
            dtype={
                "location_id": "string",
                "state": "string",
                "county": "string",
                "geoid_cb": "string",
            },
            keep_default_na=True,
        )

        try:
            for chunk_idx, chunk in enumerate(chunk_iter):
                self._verify_columns(chunk.columns, chunk_idx)
                # Add any missing OPTIONAL columns as null so the row-loop
                # never needs to special-case their absence.
                for col in _OPTIONAL_COLUMNS:
                    if col not in chunk.columns:
                        chunk[col] = pd.NA

                self.total_rows += len(chunk)
                self.logger.info(
                    stage="ingestion",
                    event_type="CHUNK_READ",
                    detail={
                        "chunk_idx": chunk_idx,
                        "rows": len(chunk),
                        "running_total": self.total_rows,
                    },
                )

                for record in chunk.to_dict(orient="records"):
                    batch_id = f"batch-{batch_counter:06d}"
                    validated = self._validate_row(record, batch_id)
                    if validated is None:
                        continue
                    current_batch.append(validated)
                    self.valid_count += 1

                    if len(current_batch) >= size:
                        self.logger.info(
                            stage="ingestion",
                            event_type="BATCH_EMITTED",
                            batch_id=batch_id,
                            detail={"size": len(current_batch)},
                        )
                        yield current_batch
                        current_batch = []
                        batch_counter += 1

            # Final partial batch (if any).
            if current_batch:
                final_batch_id = f"batch-{batch_counter:06d}"
                self.logger.info(
                    stage="ingestion",
                    event_type="BATCH_EMITTED",
                    batch_id=final_batch_id,
                    detail={"size": len(current_batch), "partial": True},
                )
                yield current_batch
        finally:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            self.logger.info(
                stage="ingestion",
                event_type="INGESTION_SUMMARY",
                detail={
                    "total_rows": self.total_rows,
                    "valid_rows": self.valid_count,
                    "dropped_total": sum(self.stats.values()),
                    "dropped_by_reason": dict(self.stats),
                },
                duration_ms=elapsed_ms,
            )

    # ---------------------------------------------------------------- internals

    def _verify_columns(self, columns: pd.Index, chunk_idx: int) -> None:
        """Raise on missing *required* columns; warn on missing optional ones.

        Only checks on the first chunk so we don't re-warn on every chunk.
        """
        if chunk_idx != 0:
            return
        missing_required = [c for c in _REQUIRED_COLUMNS if c not in columns]
        if missing_required:
            self.logger.error(
                stage="ingestion",
                event_type="MISSING_REQUIRED_COLUMN",
                detail={"missing": missing_required, "csv_columns": list(columns)},
            )
            raise ValueError(
                f"CSV is missing required columns: {missing_required}. "
                f"Expected at minimum {list(_REQUIRED_COLUMNS)}."
            )
        missing_optional = [c for c in _OPTIONAL_COLUMNS if c not in columns]
        if missing_optional:
            self.logger.warning(
                stage="ingestion",
                event_type="OPTIONAL_COLUMN_MISSING",
                detail={"missing": missing_optional},
            )
