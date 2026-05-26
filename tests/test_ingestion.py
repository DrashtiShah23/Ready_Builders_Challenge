"""
Phase 4 tests: Ingestion Agent.

Synthetic CSVs are written to ``tmp_path`` and read back through the real
``pandas.read_csv`` so column-dtype coercion is exercised end-to-end. The
build plan calls for a 20-row CSV covering every failure mode at least
once — that scenario is :class:`TestTwentyRowSpec`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src import config
from src.agents.ingestion import IngestionAgent, Reason
from src.schemas.location import ValidatedLocation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> Path:
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


class _CaptureLogger:
    """In-memory stand-in for ``PipelineLogger``.

    Records every event in ``self.events`` so tests can assert on what was
    logged without touching the filesystem.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        # Mirror PipelineLogger's public attributes so any duck-typed
        # consumer is unsurprised.
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


def _valid_row(loc_id: str, lat: float = 40.0, lon: float = -100.0,
               state: str = "NE", county: str = "Lancaster") -> dict[str, Any]:
    return {
        "location_id": loc_id,
        "latitude": lat,
        "longitude": lon,
        "state": state,
        "county": county,
    }


@pytest.fixture
def logger() -> _CaptureLogger:
    return _CaptureLogger()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_valid_csv_yields_batches(
        self, tmp_path: Path, logger: _CaptureLogger
    ) -> None:
        rows = [_valid_row(f"L{i}") for i in range(10)]
        csv = _write_csv(tmp_path / "locs.csv", rows)

        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        batches = list(agent.run(csv, batch_size=5))

        assert len(batches) == 2
        assert all(len(b) == 5 for b in batches)
        assert all(isinstance(loc, ValidatedLocation) for b in batches for loc in b)
        assert agent.stats == {}
        assert agent.valid_count == 10

    def test_final_partial_batch_emitted(
        self, tmp_path: Path, logger: _CaptureLogger
    ) -> None:
        rows = [_valid_row(f"L{i}") for i in range(7)]
        csv = _write_csv(tmp_path / "locs.csv", rows)

        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        batches = list(agent.run(csv, batch_size=3))

        assert [len(b) for b in batches] == [3, 3, 1]

    def test_batch_ids_are_monotonic_and_consistent(
        self, tmp_path: Path, logger: _CaptureLogger
    ) -> None:
        rows = [_valid_row(f"L{i}") for i in range(8)]
        csv = _write_csv(tmp_path / "locs.csv", rows)

        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        batches = list(agent.run(csv, batch_size=3))

        # All rows in a batch share its batch_id; batch_ids are monotonic.
        seen_ids = []
        for batch in batches:
            ids_in_batch = {loc.batch_id for loc in batch}
            assert len(ids_in_batch) == 1, "all rows in a batch must share batch_id"
            seen_ids.append(next(iter(ids_in_batch)))
        assert seen_ids == ["batch-000000", "batch-000001", "batch-000002"]

    def test_state_normalised_to_uppercase(
        self, tmp_path: Path, logger: _CaptureLogger
    ) -> None:
        rows = [_valid_row("L1", state="ne")]
        csv = _write_csv(tmp_path / "locs.csv", rows)
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        [batch] = list(agent.run(csv, batch_size=10))
        assert batch[0].state == "NE"

    def test_county_whitespace_stripped(
        self, tmp_path: Path, logger: _CaptureLogger
    ) -> None:
        rows = [_valid_row("L1", county="  Lancaster  ")]
        csv = _write_csv(tmp_path / "locs.csv", rows)
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        [batch] = list(agent.run(csv, batch_size=10))
        assert batch[0].county == "Lancaster"


# ---------------------------------------------------------------------------
# Drop reasons
# ---------------------------------------------------------------------------


class TestDropReasons:
    def test_null_location_id(self, tmp_path: Path, logger: _CaptureLogger) -> None:
        rows = [
            _valid_row("L1"),
            {**_valid_row("L2"), "location_id": None},
        ]
        csv = _write_csv(tmp_path / "locs.csv", rows)
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        [batch] = list(agent.run(csv, batch_size=10))
        assert len(batch) == 1
        assert agent.stats == {Reason.NULL_LOCATION_ID: 1}

    def test_empty_string_location_id(
        self, tmp_path: Path, logger: _CaptureLogger
    ) -> None:
        rows = [
            _valid_row("L1"),
            {**_valid_row("L2"), "location_id": "   "},  # whitespace only
        ]
        csv = _write_csv(tmp_path / "locs.csv", rows)
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        list(agent.run(csv, batch_size=10))
        assert agent.stats == {Reason.NULL_LOCATION_ID: 1}

    def test_null_coordinate_latitude(
        self, tmp_path: Path, logger: _CaptureLogger
    ) -> None:
        rows = [
            _valid_row("L1"),
            {**_valid_row("L2"), "latitude": None},
        ]
        csv = _write_csv(tmp_path / "locs.csv", rows)
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        list(agent.run(csv, batch_size=10))
        assert agent.stats == {Reason.NULL_COORDINATE: 1}

    def test_null_coordinate_longitude(
        self, tmp_path: Path, logger: _CaptureLogger
    ) -> None:
        rows = [
            _valid_row("L1"),
            {**_valid_row("L2"), "longitude": None},
        ]
        csv = _write_csv(tmp_path / "locs.csv", rows)
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        list(agent.run(csv, batch_size=10))
        assert agent.stats == {Reason.NULL_COORDINATE: 1}

    def test_out_of_bounds_latitude(
        self, tmp_path: Path, logger: _CaptureLogger
    ) -> None:
        rows = [
            _valid_row("L1"),
            _valid_row("L2", lat=60.0),  # Alaska — north of CONUS
        ]
        csv = _write_csv(tmp_path / "locs.csv", rows)
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        list(agent.run(csv, batch_size=10))
        assert agent.stats == {Reason.OUT_OF_BOUNDS: 1}

    def test_out_of_bounds_longitude(
        self, tmp_path: Path, logger: _CaptureLogger
    ) -> None:
        rows = [
            _valid_row("L1"),
            _valid_row("L2", lon=-150.0),  # west of CONUS
        ]
        csv = _write_csv(tmp_path / "locs.csv", rows)
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        list(agent.run(csv, batch_size=10))
        assert agent.stats == {Reason.OUT_OF_BOUNDS: 1}

    def test_duplicate_dropped_first_wins(
        self, tmp_path: Path, logger: _CaptureLogger
    ) -> None:
        rows = [
            _valid_row("L1", county="First"),
            _valid_row("L1", county="Second"),  # duplicate id
            _valid_row("L1", county="Third"),   # duplicate id
        ]
        csv = _write_csv(tmp_path / "locs.csv", rows)
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        [batch] = list(agent.run(csv, batch_size=10))
        assert len(batch) == 1
        assert batch[0].county == "First"
        assert agent.stats == {Reason.DUPLICATE_DROPPED: 2}

    def test_invalid_state(self, tmp_path: Path, logger: _CaptureLogger) -> None:
        rows = [_valid_row("L1", state="ZZ")]
        csv = _write_csv(tmp_path / "locs.csv", rows)
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        batches = list(agent.run(csv, batch_size=10))
        assert batches == []
        assert agent.stats == {Reason.INVALID_STATE: 1}

    def test_alaska_state_dropped(self, tmp_path: Path, logger: _CaptureLogger) -> None:
        # AK is intentionally excluded from STATE_FIPS (CONUS-only scope).
        rows = [_valid_row("L1", state="AK")]
        csv = _write_csv(tmp_path / "locs.csv", rows)
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        list(agent.run(csv, batch_size=10))
        assert agent.stats == {Reason.INVALID_STATE: 1}

    def test_null_state_accepted(self, tmp_path: Path, logger: _CaptureLogger) -> None:
        rows = [{**_valid_row("L1"), "state": None}]
        csv = _write_csv(tmp_path / "locs.csv", rows)
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        [batch] = list(agent.run(csv, batch_size=10))
        assert batch[0].state is None
        assert agent.stats == {}


# ---------------------------------------------------------------------------
# Schema / file errors
# ---------------------------------------------------------------------------


class TestSchemaErrors:
    def test_missing_required_column_raises(
        self, tmp_path: Path, logger: _CaptureLogger
    ) -> None:
        csv_path = tmp_path / "bad.csv"
        csv_path.write_text("location_id,latitude,state\nL1,40.0,NE\n")  # no longitude
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="missing required columns"):
            list(agent.run(csv_path, batch_size=10))

    def test_optional_columns_missing_warned_not_raised(
        self, tmp_path: Path, logger: _CaptureLogger
    ) -> None:
        csv_path = tmp_path / "no_state.csv"
        csv_path.write_text("location_id,latitude,longitude\nL1,40.0,-100.0\n")
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        [batch] = list(agent.run(csv_path, batch_size=10))
        assert batch[0].state is None
        assert batch[0].county is None
        warnings = [e for e in logger.events if e["event_type"] == "OPTIONAL_COLUMN_MISSING"]
        assert len(warnings) == 1
        assert set(warnings[0]["detail"]["missing"]) == {"state", "county"}

    def test_missing_csv_file_raises(
        self, tmp_path: Path, logger: _CaptureLogger
    ) -> None:
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        with pytest.raises(FileNotFoundError):
            list(agent.run(tmp_path / "does-not-exist.csv"))


# ---------------------------------------------------------------------------
# Build-plan 20-row spec
# ---------------------------------------------------------------------------


class TestTwentyRowSpec:
    """The build plan's Phase 4 test description literally calls for a 20-row
    synthetic CSV with invalid AND duplicate entries. This class is that
    scenario, with every drop reason represented at least once."""

    @pytest.fixture
    def csv_path(self, tmp_path: Path) -> Path:
        rows: list[dict[str, Any]] = []
        # 12 valid unique rows (clearly inside CONUS, valid states)
        for i in range(12):
            rows.append(_valid_row(f"VALID_{i}", lat=40.0 + i * 0.01, lon=-100.0 - i * 0.01))
        # 2 duplicates of the first valid id
        rows.append(_valid_row("VALID_0", county="dup-a"))
        rows.append(_valid_row("VALID_0", county="dup-b"))
        # 2 out-of-bounds (one north, one west)
        rows.append(_valid_row("OOB_LAT", lat=60.0, state="MT"))
        rows.append(_valid_row("OOB_LON", lon=-150.0, state="MT"))
        # 2 null coordinates
        rows.append({**_valid_row("NULL_LAT"), "latitude": None})
        rows.append({**_valid_row("NULL_LON"), "longitude": None})
        # 1 null location_id
        rows.append({**_valid_row("dummy"), "location_id": None})
        # 1 invalid state
        rows.append(_valid_row("BAD_STATE", state="ZZ"))
        assert len(rows) == 20, f"build-plan spec requires 20 rows, got {len(rows)}"
        return _write_csv(tmp_path / "twenty.csv", rows)

    def test_correct_valid_count(
        self, csv_path: Path, logger: _CaptureLogger
    ) -> None:
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        batches = list(agent.run(csv_path, batch_size=5))
        assert sum(len(b) for b in batches) == 12

    def test_correct_drop_breakdown(
        self, csv_path: Path, logger: _CaptureLogger
    ) -> None:
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        list(agent.run(csv_path, batch_size=5))
        assert agent.stats == {
            Reason.DUPLICATE_DROPPED: 2,
            Reason.OUT_OF_BOUNDS: 2,
            Reason.NULL_COORDINATE: 2,
            Reason.NULL_LOCATION_ID: 1,
            Reason.INVALID_STATE: 1,
        }
        assert sum(agent.stats.values()) == 8  # 20 - 12 valid

    def test_batch_sizes(
        self, csv_path: Path, logger: _CaptureLogger
    ) -> None:
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        batches = list(agent.run(csv_path, batch_size=5))
        # 12 valid / 5 per batch -> [5, 5, 2]
        assert [len(b) for b in batches] == [5, 5, 2]

    def test_summary_event_logged(
        self, csv_path: Path, logger: _CaptureLogger
    ) -> None:
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        list(agent.run(csv_path, batch_size=5))
        summaries = [e for e in logger.events if e["event_type"] == "INGESTION_SUMMARY"]
        assert len(summaries) == 1
        detail = summaries[0]["detail"]
        assert detail["total_rows"] == 20
        assert detail["valid_rows"] == 12
        assert detail["dropped_total"] == 8


# ---------------------------------------------------------------------------
# Streaming behaviour
# ---------------------------------------------------------------------------


class TestStreaming:
    def test_chunked_reading_uses_correct_size(
        self,
        tmp_path: Path,
        logger: _CaptureLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.agents import ingestion as ing_mod

        captured: dict[str, Any] = {}
        original_read_csv = pd.read_csv

        def spy_read_csv(*args: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return original_read_csv(*args, **kwargs)

        monkeypatch.setattr(ing_mod.pd, "read_csv", spy_read_csv)

        rows = [_valid_row(f"L{i}") for i in range(3)]
        csv = _write_csv(tmp_path / "locs.csv", rows)
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]
        list(agent.run(csv, batch_size=10))

        assert captured["chunksize"] == ing_mod.CHUNK_SIZE == 10_000

    def test_summary_logged_even_on_generator_abandonment(
        self, tmp_path: Path, logger: _CaptureLogger
    ) -> None:
        """If a downstream consumer stops iterating mid-stream, we should
        still see the INGESTION_SUMMARY event (the ``finally`` block fires
        when the generator is GC'd / closed)."""
        rows = [_valid_row(f"L{i}") for i in range(20)]
        csv = _write_csv(tmp_path / "locs.csv", rows)
        agent = IngestionAgent(logger=logger)  # type: ignore[arg-type]

        gen = agent.run(csv, batch_size=5)
        next(gen)  # consume only the first batch
        gen.close()  # explicitly close the generator

        summaries = [e for e in logger.events if e["event_type"] == "INGESTION_SUMMARY"]
        assert len(summaries) == 1
