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
