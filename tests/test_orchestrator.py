"""
Phase 7 tests: Pipeline Orchestrator.

The orchestrator is a thin agentic shell on top of the Phase 1-6 agents:
its job is dispatching tool calls, packaging summaries for Claude, running
the validation checks, and bounding the dispatch loop. These tests exercise
exactly that — never the real Anthropic client and never the real raster
files. Claude's API client is mocked everywhere; tool internals are
stubbed when a happy-path summary is all the dispatch surface needs.

Test layout mirrors the STOP-gate checklist:
    * tool dispatch routes the right name to the right Python function
    * each tool returns a JSON-serialisable summary with the expected keys
    * validation checks fire on the spec'd thresholds
    * tool failures return error JSON instead of bubbling out as exceptions
    * MAX_AGENT_TURNS safety cap bounds the dispatch loop
    * interactive mode populates the `explanation` field
    * end-to-end mock pipeline message flow works
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src import config
from src.agents import orchestrator as orch_mod
from src.agents.orchestrator import PipelineOrchestrator, TOOLS
from src.agents.scoring import TIER_HIGH, TIER_LOW, TIER_MODERATE, TIER_UNSCORED


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _CaptureLogger:
    """In-memory logger stand-in. Mirrors PipelineLogger's public surface."""

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


def _text_block(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _tool_use_block(name: str, tool_input: dict[str, Any], block_id: str = "tu_1") -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = tool_input
    block.id = block_id
    return block


def _response(stop_reason: str, content: list[Any], in_tok: int = 10, out_tok: int = 5) -> MagicMock:
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.content = content
    resp.usage = MagicMock(input_tokens=in_tok, output_tokens=out_tok)
    return resp


@pytest.fixture
def logger() -> _CaptureLogger:
    return _CaptureLogger()


@pytest.fixture
def orch(logger: _CaptureLogger) -> PipelineOrchestrator:
    """Build an orchestrator without touching the real Anthropic client.

    ``PipelineOrchestrator.__init__`` constructs ``anthropic.Anthropic(...)``
    eagerly; we patch the constructor so no network setup runs and so the
    instance carries a freshly-replaceable mock client.
    """
    with patch.object(orch_mod.anthropic, "Anthropic", return_value=MagicMock()):
        o = PipelineOrchestrator(api_key="test-key", logger=logger)  # type: ignore[arg-type]
    return o


# ---------------------------------------------------------------------------
# Fixture builders for scored parquets used by the validation tests
# ---------------------------------------------------------------------------


def _scored_df(
    rows: list[dict[str, Any]],
) -> pd.DataFrame:
    """Default-fill the ScoredLocation-shaped columns so per-test rows can
    only specify the fields they care about."""
    default = {
        "location_id": "L?",
        "latitude": 35.5,
        "longitude": -80.0,
        "state": "NC",
        "county": "37001",
        "tcc_pct": 50,
        "slope_deg": 5.0,
        "aspect_deg": 180.0,
        "land_cover_code": 42,
        "land_cover_class": "Evergreen Forest",
        "risk_score": 0.5,
        "risk_tier": TIER_MODERATE,
        "tcc_score": 0.5,
        "terrain_score": 0.0,
        "landcover_score": 1.0,
        "all_flags": [],
        "batch_id": "batch-000000",
        "elevation_m": 200.0,
    }
    filled = [{**default, **r} for r in rows]
    return pd.DataFrame(filled)


def _write_scored(tmp_path: Path, rows: list[dict[str, Any]]) -> str:
    path = tmp_path / "scored.parquet"
    _scored_df(rows).to_parquet(path, index=False)
    return str(path)


# ===========================================================================
# 1. test_tool_dispatch_calls_correct_function
# ===========================================================================


def test_tool_dispatch_calls_correct_function(orch: PipelineOrchestrator) -> None:
    """``_dispatch_tool`` must route each tool name to its handler.

    Every handler is patched to a sentinel that records the call. We verify
    that each of the five tool names lands on its own handler exactly once
    and never falls through to a sibling.
    """
    received: list[str] = []

    def _stub(name: str):
        def _inner(**_kwargs: Any) -> dict[str, Any]:
            received.append(name)
            return {"status": "ok", "handler": name}

        return _inner

    with (
        patch.object(orch, "_run_ingest_locations", side_effect=_stub("ingest_locations")),
        patch.object(orch, "_run_sample_environment", side_effect=_stub("sample_environment")),
        patch.object(orch, "_run_score_risk", side_effect=_stub("score_risk")),
        patch.object(orch, "_run_validate_results", side_effect=_stub("validate_results")),
        patch.object(orch, "_run_generate_report", side_effect=_stub("generate_report")),
    ):
        orch._dispatch_tool("ingest_locations", {"file_path": "x.csv"})
        orch._dispatch_tool("sample_environment", {"validated_locations_path": "v.parquet"})
        orch._dispatch_tool("score_risk", {"enriched_locations_path": "e.parquet"})
        orch._dispatch_tool("validate_results", {"scored_locations_path": "s.parquet"})
        orch._dispatch_tool(
            "generate_report",
            {"scored_locations_path": "s.parquet", "validation_report": {}},
        )

    assert received == [
        "ingest_locations",
        "sample_environment",
        "score_risk",
        "validate_results",
        "generate_report",
    ]


def test_unknown_tool_returns_error_json(orch: PipelineOrchestrator) -> None:
    """An unrecognised tool name must surface as JSON error, never an exception."""
    out = json.loads(orch._dispatch_tool("not_a_tool", {}))
    assert out["status"] == "error"
    assert "Unknown tool" in out["error"]


# ===========================================================================
# 2. test_ingest_tool_returns_correct_summary_shape
# ===========================================================================


def test_ingest_tool_returns_correct_summary_shape(
    orch: PipelineOrchestrator, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ingest tool must always return the documented summary keys."""
    csv_path = tmp_path / "locs.csv"
    pd.DataFrame(
        [
            {
                "location_id": f"L{i}",
                "latitude": 35.5 + i * 0.001,
                "longitude": -80.0,
                "state": "NC",
                "county": "37001",
            }
            for i in range(5)
        ]
    ).to_csv(csv_path, index=False)

    monkeypatch.setattr(orch_mod, "_VALIDATED_PARQUET", tmp_path / "v.parquet")
    monkeypatch.setattr(orch_mod, "_PROCESSED_DIR", tmp_path)

    result = orch._run_ingest_locations(str(csv_path))
    expected_keys = {
        "status",
        "total_rows",
        "valid_rows",
        "dropped_rows",
        "valid_pct",
        "drop_breakdown",
        "state_distribution",
        "output_path",
        "critical_failure",
        "notes",
    }
    assert expected_keys.issubset(result.keys())
    assert result["status"] == "ok"
    assert result["total_rows"] == 5
    assert result["valid_rows"] == 5
    assert result["valid_pct"] == 1.0
    assert result["critical_failure"] is False
    assert result["state_distribution"] == {"NC": 5}
    # Every documented Reason key is present (even when count is zero) so a
    # downstream consumer can rely on a stable schema.
    for key in (
        "NULL_LOCATION_ID",
        "NULL_COORDINATE",
        "OUT_OF_BOUNDS",
        "INVALID_STATE",
        "DUPLICATE_DROPPED",
        "PARSE_ERROR",
    ):
        assert key in result["drop_breakdown"]


# ===========================================================================
# 3. test_validate_distribution_sanity_flags_dominant_tier
# ===========================================================================


def test_validate_distribution_sanity_flags_dominant_tier(
    orch: PipelineOrchestrator, tmp_path: Path
) -> None:
    """A scored parquet with >80% in a single tier must fail the
    distribution check."""
    # 90% Low / 10% Moderate. Low share = 0.9 > 0.80 threshold.
    rows = (
        [{"location_id": f"L{i}", "risk_tier": TIER_LOW, "risk_score": 0.1} for i in range(90)]
        + [
            {"location_id": f"M{i}", "risk_tier": TIER_MODERATE, "risk_score": 0.4}
            for i in range(10)
        ]
    )
    path = _write_scored(tmp_path, rows)
    report = orch._run_validate_results(path)

    assert report["checks"]["distribution_sanity"]["passed"] is False
    assert report["checks"]["distribution_sanity"]["dominant_tier"] == TIER_LOW
    assert report["checks"]["distribution_sanity"]["dominant_pct"] > 0.80
    # Geographic-sanity also passes here, so the overall status is "warnings",
    # not "failed".
    assert report["status"] == "warnings"


# ===========================================================================
# 4. test_validate_cross_validation_flags_forest_low_tcc
# ===========================================================================


def test_validate_cross_validation_flags_forest_low_tcc(
    orch: PipelineOrchestrator, tmp_path: Path
) -> None:
    """Forest-classified pixels with tcc_pct < 10 fire the cross-validation
    flag when their rate exceeds 0.1%."""
    # 5 forest+low-tcc out of 1000 → 0.5% > 0.001 threshold → must flag.
    forest_low = [
        {
            "location_id": f"F{i}",
            "land_cover_code": 42,
            "tcc_pct": 5,
            "risk_tier": TIER_MODERATE,
        }
        for i in range(5)
    ]
    normal = [
        {
            "location_id": f"N{i}",
            "land_cover_code": 42,
            "tcc_pct": 80,
            "risk_tier": TIER_HIGH,
        }
        for i in range(995)
    ]
    path = _write_scored(tmp_path, forest_low + normal)
    report = orch._run_validate_results(path)

    cv = report["checks"]["cross_validation"]
    assert cv["passed"] is False
    assert cv["forest_low_tcc_count"] == 5
    assert cv["forest_low_tcc_pct"] > 0.001
    assert len(cv["sample_anomalies"]) > 0


def test_validate_cross_validation_passes_when_below_rate(
    orch: PipelineOrchestrator, tmp_path: Path
) -> None:
    """A single forest+low-tcc anomaly out of 10,000 is below threshold."""
    rows = [
        {
            "location_id": "F1",
            "land_cover_code": 42,
            "tcc_pct": 5,
            "risk_tier": TIER_MODERATE,
        }
    ] + [
        {
            "location_id": f"N{i}",
            "land_cover_code": 42,
            "tcc_pct": 80,
            "risk_tier": TIER_HIGH,
        }
        for i in range(9_999)
    ]
    path = _write_scored(tmp_path, rows)
    report = orch._run_validate_results(path)
    assert report["checks"]["cross_validation"]["passed"] is True


# ===========================================================================
# 5. test_validate_geographic_sanity_flags_out_of_bounds
# ===========================================================================


def test_validate_geographic_sanity_flags_out_of_bounds(
    orch: PipelineOrchestrator, tmp_path: Path
) -> None:
    """A location outside the NC bounding box must trigger the geo check."""
    rows = [
        {"location_id": "IN1", "latitude": 35.5, "longitude": -80.0},
        # Far outside NC (somewhere in California).
        {"location_id": "OUT1", "latitude": 37.0, "longitude": -120.0},
    ]
    path = _write_scored(tmp_path, rows)
    report = orch._run_validate_results(path)

    geo = report["checks"]["geographic_sanity"]
    assert geo["passed"] is False
    assert geo["out_of_bounds_count"] == 1
    assert report["status"] == "failed"
    assert report["recommendation"] == "halt"


# ===========================================================================
# 6. test_validate_missing_data_rate_flags_high_null_rate
# ===========================================================================


def test_validate_missing_data_rate_flags_high_null_rate(
    orch: PipelineOrchestrator, tmp_path: Path
) -> None:
    """A column with > 15% null values fires the missing-data check."""
    # 80 rows with tcc, 20 with null tcc → 20% missing.
    rows = (
        [{"location_id": f"O{i}", "tcc_pct": 50} for i in range(80)]
        + [{"location_id": f"N{i}", "tcc_pct": None} for i in range(20)]
    )
    path = _write_scored(tmp_path, rows)
    report = orch._run_validate_results(path)

    md = report["checks"]["missing_data_rate"]
    assert md["passed"] is False
    assert md["max_missing_signal"] == "tcc_pct_missing_pct"
    assert md["max_missing_pct"] >= 0.15


# ===========================================================================
# 7. test_tool_failure_returns_error_json_not_raise
# ===========================================================================


def test_tool_failure_returns_error_json_not_raise(
    orch: PipelineOrchestrator, logger: _CaptureLogger
) -> None:
    """When a tool's handler raises, dispatch must capture and return JSON."""
    with patch.object(
        orch,
        "_run_ingest_locations",
        side_effect=RuntimeError("simulated explosion"),
    ):
        out = orch._dispatch_tool("ingest_locations", {"file_path": "missing.csv"})

    parsed = json.loads(out)
    assert parsed["status"] == "error"
    assert "simulated explosion" in parsed["error"]
    assert parsed["exception_type"] == "RuntimeError"
    # The failure is logged as an ERROR-level TOOL_CALL event.
    assert any(
        e["level"] == "ERROR"
        and e.get("event_type") == "TOOL_CALL"
        and e.get("detail", {}).get("tool_name") == "ingest_locations"
        for e in logger.events
    )


# ===========================================================================
# 8. test_max_turns_safety_cap
# ===========================================================================


def test_max_turns_safety_cap(
    orch: PipelineOrchestrator,
    logger: _CaptureLogger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Claude never returns ``end_turn``, the loop must exit at the cap."""
    monkeypatch.setattr(config, "MAX_AGENT_TURNS", 3)

    # Always return a tool_use response that asks for a fake tool, so the
    # loop keeps issuing turns until it hits the cap.
    def _always_tool_use(**_kwargs: Any) -> MagicMock:
        return _response(
            "tool_use",
            [_tool_use_block("ingest_locations", {"file_path": "x.csv"})],
        )

    orch.client.messages.create = MagicMock(side_effect=_always_tool_use)

    # The ingest handler is replaced with a no-op summary so dispatch
    # succeeds even though the underlying CSV doesn't exist.
    with patch.object(orch, "_run_ingest_locations", return_value={"status": "ok"}):
        result = orch.run("any.csv", sample_size=None)

    assert orch.client.messages.create.call_count == 3
    assert any(
        e["event_type"] == "MAX_TURNS_REACHED" for e in logger.events
    ), "Expected MAX_TURNS_REACHED log event when the cap is hit."
    # The return value is still a dict — never an exception.
    assert isinstance(result, dict)


# ===========================================================================
# 9. test_interactive_mode_returns_explanation
# ===========================================================================


def test_interactive_mode_returns_explanation(
    orch: PipelineOrchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interactive mode returns a fully populated single-location result.

    Per-location tools are mocked at the orchestrator module so we never
    touch real rasters. Claude is mocked to return a known explanation.
    """
    monkeypatch.setattr(
        orch_mod, "fetch_tcc", lambda lat, lon: {"tcc_pct": 75, "tcc_missing": False}
    )
    monkeypatch.setattr(
        orch_mod,
        "fetch_elevation",
        lambda lat, lon: {
            "elevation_m": 250.0,
            "slope_deg": 12.0,
            "aspect_deg": 180.0,
            "elevation_missing": False,
        },
    )
    monkeypatch.setattr(
        orch_mod,
        "fetch_land_cover",
        lambda lat, lon: {
            "land_cover_code": 42,
            "land_cover_class": "Evergreen Forest",
            "lc_missing": False,
        },
    )
    orch.client.messages.create = MagicMock(
        return_value=_response(
            "end_turn",
            [_text_block("Heavy evergreen canopy is likely to block the sky cone.")],
        )
    )

    result = orch.run_interactive(latitude=35.5, longitude=-80.0)

    assert "explanation" in result
    assert "evergreen" in result["explanation"].lower()
    assert result["tcc_pct"] == 75
    assert result["risk_tier"] in {TIER_LOW, TIER_MODERATE, TIER_HIGH, TIER_UNSCORED}
    assert "component_scores" in result
    assert {"tcc_score", "terrain_score", "landcover_score"} <= set(
        result["component_scores"].keys()
    )


def test_interactive_mode_survives_claude_failure(
    orch: PipelineOrchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Claude throws, interactive mode returns a deterministic fallback."""
    monkeypatch.setattr(
        orch_mod, "fetch_tcc", lambda lat, lon: {"tcc_pct": 5, "tcc_missing": False}
    )
    monkeypatch.setattr(
        orch_mod,
        "fetch_elevation",
        lambda lat, lon: {
            "elevation_m": 100.0,
            "slope_deg": 2.0,
            "aspect_deg": 90.0,
            "elevation_missing": False,
        },
    )
    monkeypatch.setattr(
        orch_mod,
        "fetch_land_cover",
        lambda lat, lon: {
            "land_cover_code": 71,
            "land_cover_class": "Grassland/Herbaceous",
            "lc_missing": False,
        },
    )
    orch.client.messages.create = MagicMock(side_effect=RuntimeError("boom"))
    result = orch.run_interactive(latitude=35.5, longitude=-80.0)

    assert "explanation" in result
    assert "Claude explanation unavailable" in result["explanation"]


# ===========================================================================
# 10. test_full_pipeline_mock
# ===========================================================================


def test_full_pipeline_mock(orch: PipelineOrchestrator, logger: _CaptureLogger) -> None:
    """End-to-end message flow: Claude requests each of the five tools in
    order, gets a JSON summary back, then signals end_turn.

    Every tool internal is patched to a deterministic summary so we exercise
    the *orchestrator's* message handling — appending the assistant reply,
    packaging the tool_result, threading state to the next turn, and
    terminating cleanly on ``end_turn``.
    """
    tool_summaries = {
        "ingest_locations": {"status": "ok", "output_path": "v.parquet"},
        "sample_environment": {"status": "ok", "output_path": "e.parquet"},
        "score_risk": {"status": "ok", "output_path": "s.parquet"},
        "validate_results": {"status": "passed", "checks": {}, "recommendation": "proceed"},
        "generate_report": {
            "status": "ok",
            "outputs": {"report": "r.md"},
            "key_findings": {},
        },
    }

    expected_calls = [
        ("ingest_locations", {"file_path": "data/locations.csv"}, "tu_1"),
        ("sample_environment", {"validated_locations_path": "v.parquet"}, "tu_2"),
        ("score_risk", {"enriched_locations_path": "e.parquet"}, "tu_3"),
        ("validate_results", {"scored_locations_path": "s.parquet"}, "tu_4"),
        (
            "generate_report",
            {"scored_locations_path": "s.parquet", "validation_report": {}},
            "tu_5",
        ),
    ]

    responses = [
        _response("tool_use", [_tool_use_block(name, args, block_id=tid)])
        for name, args, tid in expected_calls
    ]
    responses.append(
        _response("end_turn", [_text_block("Pipeline complete. High-risk share: 21.5%.")])
    )
    orch.client.messages.create = MagicMock(side_effect=responses)

    with (
        patch.object(orch, "_run_ingest_locations", return_value=tool_summaries["ingest_locations"]),
        patch.object(orch, "_run_sample_environment", return_value=tool_summaries["sample_environment"]),
        patch.object(orch, "_run_score_risk", return_value=tool_summaries["score_risk"]),
        patch.object(orch, "_run_validate_results", return_value=tool_summaries["validate_results"]),
        patch.object(orch, "_run_generate_report", return_value=tool_summaries["generate_report"]),
    ):
        result = orch.run("data/locations.csv", sample_size=None)

    # 5 tool turns + 1 end_turn = 6 Claude calls.
    assert orch.client.messages.create.call_count == 6
    assert "Pipeline complete" in result["final_text"]
    assert len(result["tool_call_trace"]) == 5
    assert [t["tool_name"] for t in result["tool_call_trace"]] == [
        "ingest_locations",
        "sample_environment",
        "score_risk",
        "validate_results",
        "generate_report",
    ]
    # Token totals come straight from the mocked usage fields on each
    # response (10 input / 5 output × 6 responses).
    assert result["total_input_tokens"] == 60
    assert result["total_output_tokens"] == 30


# ===========================================================================
# Spec-shape guards: invariants that protect us from prompt / tools drift
# ===========================================================================


class TestToolSpecShape:
    def test_five_tools_with_expected_names(self) -> None:
        names = [t["name"] for t in TOOLS]
        assert names == [
            "ingest_locations",
            "sample_environment",
            "score_risk",
            "validate_results",
            "generate_report",
        ]

    def test_every_tool_has_input_schema_with_required_keys(self) -> None:
        for t in TOOLS:
            assert "input_schema" in t
            schema = t["input_schema"]
            assert schema["type"] == "object"
            assert "properties" in schema
            assert "required" in schema

    def test_system_prompt_loads(self) -> None:
        prompt = orch_mod._load_system_prompt()
        assert "five-step pipeline" in prompt
        assert "ingest_locations" in prompt
        assert "generate_report" in prompt


# ===========================================================================
# Helper unit tests — just enough to lock in the coercion edge cases
# ===========================================================================


class TestCoercion:
    @pytest.mark.parametrize(
        "value,expected",
        [(None, None), (float("nan"), None), (pd.NA, None), (1, 1), ("3", 3), (1.7, 1)],
    )
    def test_coerce_int(self, value: Any, expected: Optional[int]) -> None:
        assert orch_mod._coerce_int(value) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [(None, None), (float("nan"), None), (pd.NA, None), (1.5, 1.5), ("2.5", 2.5)],
    )
    def test_coerce_float(self, value: Any, expected: Optional[float]) -> None:
        assert orch_mod._coerce_float(value) == expected


# ===========================================================================
# Phase 8: --states filter, --resume short-circuit, PIPELINE_CHECKPOINT events
# ===========================================================================


def _write_locations_csv(path: Path, rows: list[dict[str, Any]]) -> Path:
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


class TestStatesFilter:
    def test_states_filter_drops_non_matching_rows(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``states`` is set on the orchestrator, ingestion must drop
        every row whose state is not in the allowlist."""
        csv = _write_locations_csv(
            tmp_path / "locs.csv",
            [
                {"location_id": "NC1", "latitude": 35.5, "longitude": -80.0, "state": "NC"},
                {"location_id": "NC2", "latitude": 35.6, "longitude": -80.1, "state": "NC"},
                {"location_id": "CA1", "latitude": 37.0, "longitude": -120.0, "state": "CA"},
                {"location_id": "TX1", "latitude": 30.0, "longitude": -97.0, "state": "TX"},
            ],
        )
        monkeypatch.setattr(orch_mod, "_VALIDATED_PARQUET", tmp_path / "v.parquet")
        monkeypatch.setattr(orch_mod, "_PROCESSED_DIR", tmp_path)

        orch._states_filter = {"NC"}
        result = orch._run_ingest_locations(str(csv))

        assert result["valid_rows"] == 2
        assert set(result["state_distribution"].keys()) == {"NC"}

    def test_states_filter_case_normalised_in_run(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``run(..., states=["nc"])`` must normalise to upper-case so the
        filter matches the canonical state column."""
        # We don't need to drive the Claude loop here — just verify the
        # filter is populated correctly when ``run`` initialises it.
        orch.client.messages.create = MagicMock(
            return_value=_response("end_turn", [_text_block("done")])
        )
        orch.run("ignored.csv", sample_size=None, states=["nc", "ca"])
        assert orch._states_filter == {"NC", "CA"}


class TestResumeShortCircuit:
    def test_resume_skips_ingest_when_validated_parquet_exists(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        logger: _CaptureLogger,
    ) -> None:
        """With ``resume=True`` and a pre-existing validated parquet, the
        ingest handler returns a summary from disk without re-running
        ``IngestionAgent.run``."""
        validated_path = tmp_path / "v.parquet"
        pd.DataFrame(
            [
                {
                    "location_id": f"L{i}",
                    "latitude": 35.5,
                    "longitude": -80.0,
                    "state": "NC",
                    "county": "37001",
                    "batch_id": "batch-000000",
                }
                for i in range(3)
            ]
        ).to_parquet(validated_path, index=False)
        monkeypatch.setattr(orch_mod, "_VALIDATED_PARQUET", validated_path)
        orch._resume = True

        # ``IngestionAgent.run`` must NOT be invoked.
        with patch.object(
            orch_mod, "IngestionAgent"
        ) as ingestion_cls:
            result = orch._run_ingest_locations("doesnt-matter.csv")
            ingestion_cls.assert_not_called()

        assert result["status"] == "ok"
        assert result["valid_rows"] == 3
        assert "RESUME" in result["notes"]
        # A RESUME_SKIP event is logged for this step.
        assert any(
            e["event_type"] == "RESUME_SKIP"
            and e["detail"]["step"] == "ingest_locations"
            for e in logger.events
        )

    def test_resume_does_not_skip_when_parquet_missing(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Resume only skips the step when the artifact actually exists."""
        csv = _write_locations_csv(
            tmp_path / "locs.csv",
            [
                {"location_id": "L1", "latitude": 35.5, "longitude": -80.0, "state": "NC"},
            ],
        )
        monkeypatch.setattr(orch_mod, "_VALIDATED_PARQUET", tmp_path / "missing.parquet")
        monkeypatch.setattr(orch_mod, "_PROCESSED_DIR", tmp_path)
        orch._resume = True

        result = orch._run_ingest_locations(str(csv))
        # Real ingestion ran — total_rows reflects the input CSV, not a
        # synthetic RESUME summary.
        assert "RESUME" not in result["notes"]
        assert result["total_rows"] == 1


class TestPipelineCheckpoint:
    def test_checkpoint_logged_after_ingest(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        logger: _CaptureLogger,
    ) -> None:
        """``_run_ingest_locations`` must emit a PIPELINE_CHECKPOINT after
        writing the validated parquet. Phase 11's metrics module scans
        for these event names."""
        csv = _write_locations_csv(
            tmp_path / "locs.csv",
            [
                {"location_id": "L1", "latitude": 35.5, "longitude": -80.0, "state": "NC"},
            ],
        )
        monkeypatch.setattr(orch_mod, "_VALIDATED_PARQUET", tmp_path / "v.parquet")
        monkeypatch.setattr(orch_mod, "_PROCESSED_DIR", tmp_path)

        orch._run_ingest_locations(str(csv))

        checkpoints = [
            e for e in logger.events if e["event_type"] == "PIPELINE_CHECKPOINT"
        ]
        assert len(checkpoints) >= 1
        cp = checkpoints[-1]["detail"]
        assert cp["step"] == "ingest_locations"
        assert cp["row_count"] == 1
        assert cp["resume_eligible"] is True


class TestPartitionedStoreWrite:
    def test_score_risk_writes_partitioned_store(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_run_score_risk`` must populate the Hive-partitioned store in
        addition to the single-file intermediate."""
        enriched_path = tmp_path / "enriched.parquet"
        pd.DataFrame(
            [
                {
                    "location_id": f"L{i}",
                    "latitude": 35.5,
                    "longitude": -80.0,
                    "state": "NC",
                    "county": "37001",
                    "tcc_pct": 60,
                    "elevation_m": 200.0,
                    "slope_deg": 5.0,
                    "aspect_deg": 180.0,
                    "land_cover_code": 42,
                    "land_cover_class": "Evergreen Forest",
                    "env_fetch_flags": [],
                    "batch_id": "batch-000000",
                }
                for i in range(3)
            ]
        ).to_parquet(enriched_path, index=False)

        scored_dir = tmp_path / "scored_store"
        scored_dir.mkdir()
        monkeypatch.setattr(orch_mod, "_SCORED_PARQUET", tmp_path / "scored.parquet")
        monkeypatch.setattr(orch_mod.config, "SCORED_DIR", scored_dir)

        result = orch._run_score_risk(str(enriched_path))
        assert result["status"] == "ok"
        assert (scored_dir / "state=NC").is_dir()
        # And the single-file intermediate exists at the patched path.
        assert (tmp_path / "scored.parquet").exists()


class TestRunPropagatesStatesAndResume:
    def test_run_threads_states_and_resume_into_instance_state(
        self, orch: PipelineOrchestrator
    ) -> None:
        orch.client.messages.create = MagicMock(
            return_value=_response("end_turn", [_text_block("done")])
        )
        orch.run(
            "ignored.csv",
            sample_size=None,
            states=["NC"],
            resume=True,
        )
        assert orch._states_filter == {"NC"}
        assert orch._resume is True


# ===========================================================================
# Cost-estimate logging (one COST_ESTIMATE event per Claude response)
# ===========================================================================


class TestCostEstimate:
    def test_estimate_cost_usd_matches_config_rates(self) -> None:
        # 1M input + 1M output at $3 / $15 per Mtok = $18 exactly.
        cost = orch_mod.estimate_cost_usd(1_000_000, 1_000_000)
        assert cost == pytest.approx(
            config.CLAUDE_INPUT_COST_PER_MTOK + config.CLAUDE_OUTPUT_COST_PER_MTOK
        )

    def test_estimate_cost_usd_zero_tokens(self) -> None:
        assert orch_mod.estimate_cost_usd(0, 0) == 0.0

    def test_cost_estimate_event_logged_once_per_response(
        self, orch: PipelineOrchestrator, logger: _CaptureLogger
    ) -> None:
        """Each Claude turn must emit a COST_ESTIMATE event with cumulative
        token totals and the dollar figure derived from config rates."""
        # Two-turn run: one tool_use, then end_turn.
        orch.client.messages.create = MagicMock(
            side_effect=[
                _response(
                    "tool_use",
                    [_tool_use_block("ingest_locations", {"file_path": "x.csv"})],
                    in_tok=100,
                    out_tok=50,
                ),
                _response(
                    "end_turn",
                    [_text_block("Done.")],
                    in_tok=200,
                    out_tok=75,
                ),
            ]
        )
        with patch.object(orch, "_run_ingest_locations", return_value={"status": "ok"}):
            orch.run("any.csv", sample_size=None)

        cost_events = [
            e for e in logger.events if e["event_type"] == "COST_ESTIMATE"
        ]
        # One per Claude response → exactly two.
        assert len(cost_events) == 2

        # Cumulative totals must accumulate, not reset.
        first = cost_events[0]["detail"]
        assert first["total_input_tokens"] == 100
        assert first["total_output_tokens"] == 50
        second = cost_events[1]["detail"]
        assert second["total_input_tokens"] == 300
        assert second["total_output_tokens"] == 125

        # Dollar figure uses the config rates exactly.
        expected_second = orch_mod.estimate_cost_usd(300, 125)
        assert second["estimated_cost_usd"] == pytest.approx(expected_second, abs=1e-6)

    def test_cost_estimate_event_in_interactive_mode(
        self,
        orch: PipelineOrchestrator,
        logger: _CaptureLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``run_interactive`` makes one Claude call → exactly one cost event."""
        monkeypatch.setattr(
            orch_mod, "fetch_tcc", lambda lat, lon: {"tcc_pct": 60, "tcc_missing": False}
        )
        monkeypatch.setattr(
            orch_mod,
            "fetch_elevation",
            lambda lat, lon: {
                "elevation_m": 150.0,
                "slope_deg": 8.0,
                "aspect_deg": 90.0,
                "elevation_missing": False,
            },
        )
        monkeypatch.setattr(
            orch_mod,
            "fetch_land_cover",
            lambda lat, lon: {
                "land_cover_code": 42,
                "land_cover_class": "Evergreen Forest",
                "lc_missing": False,
            },
        )
        orch.client.messages.create = MagicMock(
            return_value=_response("end_turn", [_text_block("Explanation.")], in_tok=120, out_tok=80)
        )

        orch.run_interactive(latitude=35.5, longitude=-80.0)

        cost_events = [
            e for e in logger.events if e["event_type"] == "COST_ESTIMATE"
        ]
        assert len(cost_events) == 1
        assert cost_events[0]["detail"]["total_input_tokens"] == 120
        assert cost_events[0]["detail"]["total_output_tokens"] == 80


# ===========================================================================
# Tool-hook callbacks (used by pipeline.py to print real-time progress)
# ===========================================================================


class TestToolHooks:
    def test_hooks_fire_on_success(
        self, orch: PipelineOrchestrator
    ) -> None:
        starts: list[tuple[str, dict[str, Any]]] = []
        ends: list[tuple[str, float, int, int]] = []

        orch.on_tool_start = lambda name, inp: starts.append((name, inp))
        orch.on_tool_end = lambda name, dur, ti, to: ends.append((name, dur, ti, to))
        orch._last_response_input_tokens = 250
        orch._last_response_output_tokens = 80

        with patch.object(orch, "_run_ingest_locations", return_value={"status": "ok"}):
            orch._dispatch_tool("ingest_locations", {"file_path": "x.csv"})

        assert starts == [("ingest_locations", {"file_path": "x.csv"})]
        assert len(ends) == 1
        name, duration, tin, tout = ends[0]
        assert name == "ingest_locations"
        assert duration >= 0.0
        assert tin == 250
        assert tout == 80

    def test_hooks_fire_on_handler_exception(
        self, orch: PipelineOrchestrator
    ) -> None:
        """``on_tool_end`` must still fire even when the handler raises,
        so a real-time UI never gets stuck on a `>>> Running ...` line."""
        ends: list[str] = []
        orch.on_tool_end = lambda name, *_args: ends.append(name)

        with patch.object(
            orch,
            "_run_ingest_locations",
            side_effect=RuntimeError("boom"),
        ):
            orch._dispatch_tool("ingest_locations", {"file_path": "x.csv"})

        assert ends == ["ingest_locations"]

    def test_hook_failure_does_not_break_dispatch(
        self, orch: PipelineOrchestrator, logger: _CaptureLogger
    ) -> None:
        """A misbehaving hook must be swallowed + logged, never propagated."""
        def _bad_hook(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("hook broke")

        orch.on_tool_start = _bad_hook
        orch.on_tool_end = _bad_hook

        with patch.object(orch, "_run_ingest_locations", return_value={"status": "ok"}):
            out = orch._dispatch_tool("ingest_locations", {"file_path": "x.csv"})

        parsed = json.loads(out)
        assert parsed["status"] == "ok"
        hook_failures = [
            e for e in logger.events if e["event_type"] == "TOOL_HOOK_FAILED"
        ]
        # Two failures: one from ``on_tool_start``, one from ``on_tool_end``.
        assert len(hook_failures) == 2
        assert {f["detail"]["hook"] for f in hook_failures} == {
            "on_tool_start",
            "on_tool_end",
        }

    def test_hooks_receive_last_response_tokens_in_run_loop(
        self,
        orch: PipelineOrchestrator,
    ) -> None:
        """The tokens passed to ``on_tool_end`` must be from the Claude
        response that emitted *this* tool call, not a stale prior value."""
        ends: list[tuple[str, int, int]] = []
        orch.on_tool_end = lambda name, _dur, ti, to: ends.append((name, ti, to))

        orch.client.messages.create = MagicMock(
            side_effect=[
                _response(
                    "tool_use",
                    [_tool_use_block("ingest_locations", {"file_path": "x.csv"}, "tu_1")],
                    in_tok=300,
                    out_tok=40,
                ),
                _response(
                    "tool_use",
                    [_tool_use_block("score_risk", {"enriched_locations_path": "e.p"}, "tu_2")],
                    in_tok=170,
                    out_tok=25,
                ),
                _response("end_turn", [_text_block("done")], in_tok=10, out_tok=5),
            ]
        )
        with (
            patch.object(orch, "_run_ingest_locations", return_value={"status": "ok"}),
            patch.object(orch, "_run_score_risk", return_value={"status": "ok"}),
        ):
            orch.run("x.csv", sample_size=None)

        # First tool's hook sees response 1's tokens; second tool's sees response 2's.
        assert ends == [("ingest_locations", 300, 40), ("score_risk", 170, 25)]


# ===========================================================================
# Phase 9 — generate_report
# ===========================================================================


def _scored_rows_for_report(
    n: int,
    state: str,
    county: str,
    tier: str,
    location_id_prefix: str,
) -> list[dict[str, Any]]:
    """Synthesise ``ScoredLocation``-shaped rows for a single tier+county.

    Tests in this section build a small set of (state, county, tier)
    cells so the partition store has enough rows for ``min_locations=25``
    in ``get_top_at_risk_counties`` to surface every county the test cares
    about.
    """
    tier_score_map = {TIER_HIGH: 0.85, TIER_MODERATE: 0.45, TIER_LOW: 0.15}
    return [
        {
            "location_id": f"{location_id_prefix}-{i}",
            "latitude": 35.5 + (i * 0.0001),
            "longitude": -80.0 - (i * 0.0001),
            "state": state,
            "county": county,
            "tcc_pct": 60 if tier == TIER_HIGH else (30 if tier == TIER_MODERATE else 5),
            "slope_deg": 25.0 if tier == TIER_HIGH else 5.0,
            "aspect_deg": 180.0,
            "land_cover_code": 42 if tier == TIER_HIGH else 81,
            "land_cover_class": "Evergreen Forest" if tier == TIER_HIGH else "Cultivated Crops",
            "risk_score": tier_score_map.get(tier, 0.15),
            "risk_tier": tier,
            "tcc_score": 1.0 if tier == TIER_HIGH else 0.0,
            "terrain_score": 1.0 if tier == TIER_HIGH else 0.0,
            "landcover_score": 1.0 if tier == TIER_HIGH else 0.0,
            "all_flags": [],
            "batch_id": "batch-000000",
        }
        for i in range(n)
    ]


def _populate_report_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, Path, Path]:
    """Wire up a tmp store + outputs dir, write a deterministic scored
    dataset (3 NC counties × multiple tiers), and return paths the
    report test can use.

    Returns
    -------
    (scored_locations_path, scored_dir, outputs_dir)
    """
    # The DuckDB queries read from ``config.SCORED_DIR`` via store.* — so
    # we patch it to a fresh tmp dir per test to avoid cross-pollination.
    scored_dir = tmp_path / "scored_store"
    scored_dir.mkdir()
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir()
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    monkeypatch.setattr(orch_mod.config, "SCORED_DIR", scored_dir)
    monkeypatch.setattr(orch_mod.config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(orch_mod, "_OUTPUTS_DIR", outputs_dir)
    monkeypatch.setattr(orch_mod, "_REPORT_MD", outputs_dir / "analysis_report.md")
    monkeypatch.setattr(orch_mod, "_MAP_HTML", outputs_dir / "risk_map.html")
    monkeypatch.setattr(
        orch_mod,
        "_STATE_SUMMARY_PARQUET",
        scored_dir / "risk_summary_by_state.parquet",
    )
    monkeypatch.setattr(
        orch_mod,
        "_COUNTY_SUMMARY_PARQUET",
        scored_dir / "risk_summary_by_county.parquet",
    )

    # Deterministic cell counts so the top-counties query returns
    # predictable ordering:
    #   - Asheville: 40 High + 10 Low  → 80% High share
    #   - Charlotte: 10 High + 20 Mod + 20 Low → 20% High share
    #   - Raleigh:    5 High + 15 Mod + 30 Low → 10% High share
    rows: list[dict[str, Any]] = []
    rows.extend(_scored_rows_for_report(40, "NC", "Asheville", TIER_HIGH, "ash-h"))
    rows.extend(_scored_rows_for_report(10, "NC", "Asheville", TIER_LOW, "ash-l"))
    rows.extend(_scored_rows_for_report(10, "NC", "Charlotte", TIER_HIGH, "chr-h"))
    rows.extend(_scored_rows_for_report(20, "NC", "Charlotte", TIER_MODERATE, "chr-m"))
    rows.extend(_scored_rows_for_report(20, "NC", "Charlotte", TIER_LOW, "chr-l"))
    rows.extend(_scored_rows_for_report(5, "NC", "Raleigh", TIER_HIGH, "ral-h"))
    rows.extend(_scored_rows_for_report(15, "NC", "Raleigh", TIER_MODERATE, "ral-m"))
    rows.extend(_scored_rows_for_report(30, "NC", "Raleigh", TIER_LOW, "ral-l"))

    df = pd.DataFrame(rows)
    # Write the single-file intermediate (the path Claude threads through)
    # and populate the Hive-partitioned store the DuckDB queries read.
    scored_path = processed_dir / "scored.parquet"
    df.to_parquet(scored_path, index=False)
    orch_mod.store.write_scored_locations(df, scored_dir=scored_dir)
    return str(scored_path), scored_dir, outputs_dir


class TestRemmemberSummary:
    """Every ``_run_*`` handler must stash its return value on
    ``self._tool_summaries`` so generate_report can read upstream
    summaries without re-doing their work.
    """

    def test_ingest_summary_is_remembered(
        self, orch: PipelineOrchestrator
    ) -> None:
        summary = {"status": "ok", "valid_rows": 3}
        returned = orch._remember_summary("ingest_locations", summary)
        assert returned is summary
        assert orch._tool_summaries["ingest_locations"] is summary

    def test_run_resets_tool_summary_cache(
        self, orch: PipelineOrchestrator
    ) -> None:
        """Back-to-back ``run`` calls must not leak summaries from a prior
        run into a new one — the cache is reset on entry."""
        orch._tool_summaries = {"ingest_locations": {"status": "leftover"}}
        orch.client.messages.create = MagicMock(
            return_value=_response("end_turn", [_text_block("done")])
        )
        orch.run("ignored.csv", sample_size=None)
        assert orch._tool_summaries == {}


class TestGenerateReport:
    """End-to-end ``_run_generate_report`` behavior — markdown contents,
    DuckDB-backed aggregations, parquet summaries, and key_findings.
    """

    def test_outputs_dict_points_at_real_files(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scored_path, scored_dir, outputs_dir = _populate_report_fixture(
            tmp_path, monkeypatch
        )
        validation_report = {
            "status": "passed",
            "checks": {},
            "total_warnings": 0,
            "recommendation": "proceed",
            "notes": "",
        }
        result = orch._run_generate_report(
            scored_locations_path=scored_path,
            validation_report=validation_report,
        )

        assert result["status"] == "ok"
        outputs = result["outputs"]
        assert Path(outputs["report"]).exists()
        assert Path(outputs["state_summary"]).exists()
        assert Path(outputs["county_summary"]).exists()
        # Map render may fail in a headless test env — only assert non-empty
        # when set, never that it had to render.
        if outputs["map"]:
            assert Path(outputs["map"]).exists()

    def test_key_findings_match_partition_store(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scored_path, scored_dir, outputs_dir = _populate_report_fixture(
            tmp_path, monkeypatch
        )
        validation_report = {"status": "passed", "recommendation": "proceed"}
        result = orch._run_generate_report(
            scored_locations_path=scored_path,
            validation_report=validation_report,
        )
        kf = result["key_findings"]
        # 40 + 10 + 10 + 20 + 20 + 5 + 15 + 30 = 150 total locations.
        assert kf["total_locations_analyzed"] == 150
        # High = 40 + 10 + 5 = 55  →  55 / 150 ≈ 0.3667
        assert kf["high_risk_pct"] == pytest.approx(55 / 150, rel=1e-3)
        # Asheville is highest-High share (80%) and clears the 25-loc floor.
        assert kf["top_at_risk_county"] == "Asheville"
        assert kf["state"] == "NC"

    def test_markdown_has_all_required_sections(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The Phase 9 STOP gate requires every named section to be
        present in the generated markdown."""
        scored_path, scored_dir, outputs_dir = _populate_report_fixture(
            tmp_path, monkeypatch
        )
        validation_report = {
            "status": "passed",
            "checks": {"distribution_sanity": {"passed": True}},
            "total_warnings": 0,
            "recommendation": "proceed",
            "notes": "",
        }
        # Pre-seed an ingest summary so the Data Quality section has real
        # numbers — the generate_report tool reads from _tool_summaries.
        orch._tool_summaries["ingest_locations"] = {
            "status": "ok",
            "total_rows": 200,
            "valid_rows": 150,
            "dropped_rows": 50,
            "valid_pct": 0.75,
            "drop_breakdown": {
                "NULL_COORDINATE": 30,
                "OUT_OF_BOUNDS": 20,
                "INVALID_STATE": 0,
            },
            "state_distribution": {"NC": 150},
        }
        orch._tool_summaries["sample_environment"] = {
            "status": "ok",
            "total_locations": 150,
            "enriched_locations": 148,
            "missing_rates": {
                "tcc_missing_pct": 0.01,
                "slope_missing_pct": 0.02,
                "landcover_missing_pct": 0.005,
                "elevation_missing_pct": 0.0,
            },
        }
        orch._tool_summaries["score_risk"] = {
            "status": "ok",
            "mean_composite_score": 0.42,
        }

        orch._run_generate_report(
            scored_locations_path=scored_path,
            validation_report=validation_report,
        )
        report = (outputs_dir / "analysis_report.md").read_text()
        for header in (
            "# LEO Satellite Coverage Risk — Analysis Report",
            "## Executive Summary",
            "## Risk Distribution",
            "## State-Level Breakdown",
            "## Top 10 At-Risk Counties",
            "## Data Quality Summary",
            "## Methodology",
            "## Known Limitations",
            "## Generated Artifacts",
        ):
            assert header in report, f"missing section: {header}"

    def test_executive_summary_uses_prescribed_opening(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The build plan requires the exec summary to open with the
        specific sentence pattern from the Phase 9 spec."""
        scored_path, scored_dir, outputs_dir = _populate_report_fixture(
            tmp_path, monkeypatch
        )
        validation_report = {"status": "passed", "recommendation": "proceed"}
        orch._tool_summaries["ingest_locations"] = {
            "state_distribution": {"NC": 150}
        }
        orch._run_generate_report(
            scored_locations_path=scored_path,
            validation_report=validation_report,
        )
        report = (outputs_dir / "analysis_report.md").read_text()
        # Spec wording: "Of the [N] locations committed for LEO satellite
        # service, approximately [X]% face elevated obstruction risk..."
        assert "locations committed for LEO satellite" in report
        assert "face elevated obstruction risk" in report
        # The region phrase should name the single state observed in the
        # ingest summary so the prose reads naturally.
        assert "in NC" in report

    def test_risk_distribution_table_counts_all_tiers(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scored_path, scored_dir, outputs_dir = _populate_report_fixture(
            tmp_path, monkeypatch
        )
        orch._run_generate_report(
            scored_locations_path=scored_path,
            validation_report={"status": "passed", "recommendation": "proceed"},
        )
        report = (outputs_dir / "analysis_report.md").read_text()
        # Every real tier shows up in the risk distribution table.
        for tier in (TIER_HIGH, TIER_MODERATE, TIER_LOW):
            assert f"| {tier} |" in report
        # The total row mirrors the dataset size (150 rows).
        assert "| **Total** | **150** | **100.00%** |" in report

    def test_top_counties_table_orders_by_high_share(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scored_path, scored_dir, outputs_dir = _populate_report_fixture(
            tmp_path, monkeypatch
        )
        orch._run_generate_report(
            scored_locations_path=scored_path,
            validation_report={"status": "passed", "recommendation": "proceed"},
        )
        report = (outputs_dir / "analysis_report.md").read_text()
        # The three NC counties should appear in descending High-share
        # order: Asheville (80%) → Charlotte (20%) → Raleigh (10%).
        ash_idx = report.find("Asheville")
        chr_idx = report.find("Charlotte")
        ral_idx = report.find("Raleigh")
        assert -1 < ash_idx < chr_idx < ral_idx
        # The leading county's high share is rendered as "80.00%" in
        # the same row.
        assert "Asheville" in report
        assert "80.00%" in report

    def test_methodology_section_pins_weights_and_dataset_versions(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scored_path, scored_dir, outputs_dir = _populate_report_fixture(
            tmp_path, monkeypatch
        )
        orch._run_generate_report(
            scored_locations_path=scored_path,
            validation_report={"status": "passed", "recommendation": "proceed"},
        )
        report = (outputs_dir / "analysis_report.md").read_text()
        # The weights from config must appear verbatim in the formula.
        assert f"× {orch_mod.config.TCC_WEIGHT:.2f}" in report
        assert f"× {orch_mod.config.TERRAIN_WEIGHT:.2f}" in report
        assert f"× {orch_mod.config.LANDCOVER_WEIGHT:.2f}" in report
        # And the dataset version pins.
        assert orch_mod.config.MRLC_TCC_COVERAGE_ID in report
        assert orch_mod.config.MRLC_LANDCOVER_COVERAGE_ID in report
        assert "USGS 3DEP" in report

    def test_data_quality_section_lists_drop_reasons(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scored_path, scored_dir, outputs_dir = _populate_report_fixture(
            tmp_path, monkeypatch
        )
        orch._tool_summaries["ingest_locations"] = {
            "status": "ok",
            "total_rows": 200,
            "valid_rows": 150,
            "dropped_rows": 50,
            "valid_pct": 0.75,
            "drop_breakdown": {
                "NULL_COORDINATE": 30,
                "OUT_OF_BOUNDS": 20,
                "INVALID_STATE": 0,
            },
            "state_distribution": {"NC": 150},
        }
        orch._tool_summaries["sample_environment"] = {
            "total_locations": 150,
            "enriched_locations": 148,
            "missing_rates": {
                "tcc_missing_pct": 0.01,
                "slope_missing_pct": 0.02,
                "landcover_missing_pct": 0.005,
                "elevation_missing_pct": 0.0,
            },
        }
        orch._run_generate_report(
            scored_locations_path=scored_path,
            validation_report={"status": "passed", "recommendation": "proceed"},
        )
        report = (outputs_dir / "analysis_report.md").read_text()
        # Non-zero exclusions show up; zero-counts are suppressed to keep
        # the report scannable.
        assert "`NULL_COORDINATE`" in report
        assert "| 30 |" in report
        assert "`OUT_OF_BOUNDS`" in report
        assert "| 20 |" in report
        assert "`INVALID_STATE`" not in report
        # The env-coverage table is also rendered.
        assert "Tree canopy cover (TCC)" in report
        assert "Terrain slope" in report

    def test_empty_dataset_returns_error_status(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty scored parquet should short-circuit with status=error
        — no markdown is written and the caller (Claude) sees the failure."""
        scored_dir = tmp_path / "scored"
        scored_dir.mkdir()
        outputs_dir = tmp_path / "outputs"
        outputs_dir.mkdir()
        empty_path = tmp_path / "empty.parquet"
        pd.DataFrame(
            columns=[
                "location_id",
                "latitude",
                "longitude",
                "state",
                "county",
                "risk_score",
                "risk_tier",
            ]
        ).to_parquet(empty_path, index=False)
        monkeypatch.setattr(orch_mod.config, "SCORED_DIR", scored_dir)
        monkeypatch.setattr(orch_mod, "_OUTPUTS_DIR", outputs_dir)
        monkeypatch.setattr(
            orch_mod, "_REPORT_MD", outputs_dir / "analysis_report.md"
        )

        result = orch._run_generate_report(
            scored_locations_path=str(empty_path),
            validation_report={"status": "passed"},
        )
        assert result["status"] == "error"
        assert not (outputs_dir / "analysis_report.md").exists()
        # Even on the error path the summary is remembered so a caller
        # reasoning over ``_tool_summaries`` sees the failure.
        assert orch._tool_summaries["generate_report"]["status"] == "error"

    def test_map_render_failure_does_not_break_report(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        logger: _CaptureLogger,
    ) -> None:
        """A folium failure must be logged but never crash the tool —
        the markdown + parquet summaries are the primary deliverables."""
        scored_path, scored_dir, outputs_dir = _populate_report_fixture(
            tmp_path, monkeypatch
        )

        with patch.object(
            orch, "_render_map", side_effect=RuntimeError("synthetic")
        ):
            result = orch._run_generate_report(
                scored_locations_path=scored_path,
                validation_report={"status": "passed"},
            )
        assert result["status"] == "ok"
        assert result["outputs"]["map"] == ""
        # The failure was logged with the structured event type.
        failures = [
            e for e in logger.events if e["event_type"] == "MAP_RENDER_FAILED"
        ]
        assert len(failures) == 1

    def test_summary_parquets_match_duckdb_aggregations(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The on-disk per-state and per-county summary parquets must be
        byte-for-byte the DuckDB aggregation results — the report's
        numbers and the parquet's numbers are guaranteed to agree."""
        scored_path, scored_dir, outputs_dir = _populate_report_fixture(
            tmp_path, monkeypatch
        )
        orch._run_generate_report(
            scored_locations_path=scored_path,
            validation_report={"status": "passed"},
        )

        on_disk_state = pd.read_parquet(
            scored_dir / "risk_summary_by_state.parquet"
        )
        ducked_state = orch_mod.store.get_state_breakdown(
            scored_dir=scored_dir
        )
        pd.testing.assert_frame_equal(on_disk_state, ducked_state)

        on_disk_county = pd.read_parquet(
            scored_dir / "risk_summary_by_county.parquet"
        )
        ducked_county = orch_mod.store.get_county_breakdown(
            scored_dir=scored_dir
        )
        pd.testing.assert_frame_equal(on_disk_county, ducked_county)


# ===========================================================================
# Phase 10 — interactive map
# ===========================================================================


def _scored_df_with_all_tiers(n_per_tier: int = 8) -> pd.DataFrame:
    """Synthesise a small scored dataset spanning every tier + one
    UNSCORED row, all at distinct lat/lon so MarkerCluster has real
    geographic distribution to work with."""
    rows: list[dict[str, Any]] = []
    tiers = [
        (TIER_HIGH, 0.85, 60, 25.0, 42, "Evergreen Forest"),
        (TIER_MODERATE, 0.45, 30, 12.0, 22, "Developed, Low Intensity"),
        (TIER_LOW, 0.10, 5, 2.0, 81, "Pasture/Hay"),
    ]
    for tier, score, tcc, slope, lc_code, lc_name in tiers:
        for i in range(n_per_tier):
            rows.append(
                {
                    "location_id": f"{tier}-{i}",
                    "latitude": 35.5 + (i * 0.01),
                    "longitude": -80.0 - (i * 0.01),
                    "state": "NC",
                    "county": f"37{str(i).zfill(3)}",
                    "tcc_pct": tcc,
                    "slope_deg": slope,
                    "aspect_deg": 180.0,
                    "land_cover_code": lc_code,
                    "land_cover_class": lc_name,
                    "risk_score": score,
                    "risk_tier": tier,
                    "tcc_score": 1.0 if tier == TIER_HIGH else 0.0,
                    "terrain_score": 1.0 if tier == TIER_HIGH else 0.0,
                    "landcover_score": 1.0 if tier == TIER_HIGH else 0.0,
                    "all_flags": [],
                    "batch_id": "batch-000000",
                }
            )
    # One UNSCORED row to confirm it is filtered out of the map.
    rows.append(
        {
            "location_id": "unscored-1",
            "latitude": 36.0,
            "longitude": -80.5,
            "state": "NC",
            "county": "37999",
            "tcc_pct": None,
            "slope_deg": None,
            "aspect_deg": None,
            "land_cover_code": None,
            "land_cover_class": None,
            "risk_score": None,
            "risk_tier": TIER_UNSCORED,
            "tcc_score": None,
            "terrain_score": None,
            "landcover_score": None,
            "all_flags": [],
            "batch_id": "batch-000000",
        }
    )
    return pd.DataFrame(rows)


class TestRenderMap:
    """Phase 10 STOP-gate verification — the rendered HTML must include
    every spec-required feature (base layer, hex colours, layer control,
    cluster, legend, tooltip fields)."""

    def test_map_html_contains_every_spec_feature(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        out = tmp_path / "risk_map.html"
        monkeypatch.setattr(orch_mod, "_MAP_HTML", out)
        df = _scored_df_with_all_tiers()
        orch._render_map(df)
        assert out.exists(), "map HTML was not written"

        html = out.read_text()
        # Base layer
        assert "openstreetmap" in html.lower()
        # Spec hex codes for the three tiers
        assert "#e74c3c" in html
        assert "#f39c12" in html
        assert "#2ecc71" in html
        # MarkerCluster with the spec'd zoom threshold
        assert "MarkerCluster" in html
        assert "disableClusteringAtZoom" in html and "8" in html
        # LayerControl present and not collapsed
        assert "LayerControl" in html or "layer_control" in html.lower()
        # Inline legend with the marker count summary in the toggle
        # labels (LayerControl shows them) and the legend title text.
        assert "LEO obstruction risk" in html
        assert "High risk (" in html  # FeatureGroup name carries the count
        assert "Moderate risk (" in html
        assert "Low risk (" in html
        # Every spec'd tooltip field label appears in the HTML.
        for label in (
            "Location",
            "State",
            "County",
            "Risk tier",
            "Risk score",
            "Tree canopy",
            "Slope",
            "Land cover",
        ):
            assert label in html, f"tooltip field missing: {label}"

    def test_unscored_rows_are_excluded(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """UNSCORED locations have no position on the risk spectrum,
        so painting them on the map (in any colour) would mislead
        the reader — they belong in the report's Data Quality table."""
        out = tmp_path / "risk_map.html"
        monkeypatch.setattr(orch_mod, "_MAP_HTML", out)
        df = _scored_df_with_all_tiers()
        # Sanity-check the fixture: an UNSCORED row IS in the input.
        assert (df["risk_tier"] == TIER_UNSCORED).any()
        orch._render_map(df)
        html = out.read_text()
        # The UNSCORED row's location_id must not appear anywhere
        # in the rendered HTML — neither tooltip nor coordinates.
        assert "unscored-1" not in html

    def test_empty_dataset_skips_render(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty input must not raise and must not write an empty
        HTML file — the report's status logic already handles the
        zero-data case before the map step runs."""
        out = tmp_path / "risk_map.html"
        monkeypatch.setattr(orch_mod, "_MAP_HTML", out)
        empty = pd.DataFrame(columns=["risk_tier", "latitude", "longitude"])
        orch._render_map(empty)
        assert not out.exists()

    def test_only_unscored_rows_skips_render(
        self,
        orch: PipelineOrchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An input where every row is UNSCORED has nothing to paint
        once the unscored rows are filtered out — must not raise."""
        out = tmp_path / "risk_map.html"
        monkeypatch.setattr(orch_mod, "_MAP_HTML", out)
        df = pd.DataFrame(
            [
                {
                    "location_id": "u1",
                    "latitude": 35.5,
                    "longitude": -80.0,
                    "state": "NC",
                    "county": "37001",
                    "risk_tier": TIER_UNSCORED,
                    "risk_score": None,
                    "tcc_pct": None,
                    "slope_deg": None,
                    "land_cover_class": None,
                }
            ]
        )
        orch._render_map(df)
        assert not out.exists()


class TestSampleForMap:
    """Tier-stratified subsampling — the cap must always be respected,
    every tier must keep proportional representation, and the spec'd
    UNSCORED filter must apply before the budget is computed."""

    def _df(self, h: int, m: int, l: int, u: int = 0) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for prefix, n, tier in (
            ("h", h, TIER_HIGH),
            ("m", m, TIER_MODERATE),
            ("l", l, TIER_LOW),
            ("u", u, TIER_UNSCORED),
        ):
            for i in range(n):
                rows.append(
                    {
                        "location_id": f"{prefix}-{i}",
                        "latitude": 35.0,
                        "longitude": -80.0,
                        "risk_tier": tier,
                    }
                )
        return pd.DataFrame(rows)

    def test_under_budget_returns_full_dataset(
        self, orch: PipelineOrchestrator
    ) -> None:
        """A scored dataset smaller than ``_MAP_MAX_POINTS`` must
        render every point — no information loss for the small case."""
        df = self._df(10, 20, 30)
        sample = orch._sample_for_map(df)
        assert len(sample) == 60
        assert set(sample["risk_tier"]) == {TIER_HIGH, TIER_MODERATE, TIER_LOW}

    def test_unscored_rows_filtered_before_budget(
        self, orch: PipelineOrchestrator
    ) -> None:
        df = self._df(5, 5, 5, u=5)
        sample = orch._sample_for_map(df)
        assert TIER_UNSCORED not in set(sample["risk_tier"])

    def test_cap_respected_on_large_dataset(
        self, orch: PipelineOrchestrator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Patch the cap to 100 and feed a 300-row dataset — the
        returned sample must be exactly 100 rows."""
        monkeypatch.setattr(orch_mod, "_MAP_MAX_POINTS", 100)
        df = self._df(120, 120, 120)
        sample = orch._sample_for_map(df)
        assert len(sample) == 100

    def test_spare_budget_flows_to_other_tiers(
        self, orch: PipelineOrchestrator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If a tier has fewer rows than its allocated slice, the
        unused budget must redistribute to tiers that still have room
        — a 100-budget cap with 5 High / 200 Moderate / 200 Low must
        still hit 100 sampled rows total."""
        monkeypatch.setattr(orch_mod, "_MAP_MAX_POINTS", 100)
        df = self._df(5, 200, 200)
        sample = orch._sample_for_map(df)
        assert len(sample) == 100
        # Every High row is included because High had headroom.
        assert (sample["risk_tier"] == TIER_HIGH).sum() == 5


class TestMarkerHtml:
    """The per-marker tooltip is the Phase 10 spec's hover surface;
    its field set and missing-value rendering are part of the contract."""

    def _row(self, **overrides: Any) -> Any:
        defaults = {
            "location_id": "L1",
            "state": "NC",
            "county": "37001",
            "risk_tier": "High",
            "risk_score": 0.7234,
            "tcc_pct": 55,
            "slope_deg": 12.4,
            "land_cover_class": "Evergreen Forest",
        }
        defaults.update(overrides)
        from types import SimpleNamespace
        return SimpleNamespace(**defaults)

    def test_marker_html_contains_every_field(
        self, orch: PipelineOrchestrator
    ) -> None:
        html = orch._marker_html(self._row())
        assert "L1" in html
        assert "NC" in html
        assert "37001" in html
        assert "High" in html
        # Score formatted to 3 decimals
        assert "0.723" in html
        # tcc_pct formatted as a percentage
        assert "55%" in html
        # slope formatted with degree symbol + 1 decimal
        assert "12.4°" in html
        # Land cover class verbatim
        assert "Evergreen Forest" in html

    def test_marker_html_handles_missing_fields(
        self, orch: PipelineOrchestrator
    ) -> None:
        """A null tcc_pct / slope_deg must render as the em-dash
        placeholder rather than the literal "NaN" / "None"."""
        import math
        html = orch._marker_html(
            self._row(tcc_pct=math.nan, slope_deg=None, land_cover_class=None)
        )
        # Three placeholder cells expected.
        assert html.count("—") >= 3
        # And the JavaScript-y strings must not leak through.
        assert "NaN" not in html
        assert ">None<" not in html
