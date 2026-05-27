"""
Phase 11 tests: pipeline observability metrics.

Every test builds its own JSONL log inside ``tmp_path`` so cases are
independent and never depend on a real run. The synthetic logs use the
same event shape as :class:`src.utils.logger.PipelineLogger` writes —
tested in :func:`test_load_events_matches_pipeline_logger_shape` so a
schema drift on the logger side is caught here.

Coverage:
* JSONL parser skips blank / malformed lines and reports the count
* Per-stage metrics: event counts, success/warning/error rates, p50/p95
  latency, and empty-stage handling
* Claude metrics: cumulative tokens from the last COST_ESTIMATE event,
  cost re-computed via ``estimate_cost_usd``, per-response averages
* Tool call accuracy: happy path, missing tools, wrong order, duplicates,
  and the case where ``TOOLS`` is empty / unmockable
* Output quality from the scored parquet: tier distribution, scored/
  unscored split, mean/median over the scored rows only, missing rates
* End-to-end ``compute_pipeline_metrics`` on a multi-event log with a
  matching scored parquet
* ``format_metrics_text`` is non-empty and contains every section header
* CLI ``python -m src.utils.metrics`` happy-path
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import pytest

from src.utils import metrics as M


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    stage: str = "orchestrator",
    level: str = "INFO",
    event_type: str = "TOOL_CALL",
    detail: dict[str, Any] | None = None,
    duration_ms: int | None = None,
    token_input: int | None = None,
    token_output: int | None = None,
    timestamp: str = "2026-05-27T00:00:00+00:00",
) -> dict[str, Any]:
    """Build a single JSONL event matching ``PipelineLogger`` schema."""
    return {
        "timestamp": timestamp,
        "level": level,
        "stage": stage,
        "batch_id": None,
        "location_id": None,
        "event_type": event_type,
        "detail": detail or {},
        "duration_ms": duration_ms,
        "token_input": token_input,
        "token_output": token_output,
    }


def _write_log(tmp_path: Path, events: Iterable[dict[str, Any]], name: str = "pipeline_run_test-abc123.jsonl") -> Path:
    p = tmp_path / name
    with p.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    return p


def _write_scored_parquet(tmp_path: Path) -> Path:
    """Build a 6-row scored parquet covering all four tiers + nulls."""
    df = pd.DataFrame(
        [
            # 2 High, 1 Moderate, 2 Low, 1 UNSCORED
            {"location_id": "h1", "risk_tier": "High", "risk_score": 0.85, "state": "NC"},
            {"location_id": "h2", "risk_tier": "High", "risk_score": 0.80, "state": "NC"},
            {"location_id": "m1", "risk_tier": "Moderate", "risk_score": 0.50, "state": "NC"},
            {"location_id": "l1", "risk_tier": "Low", "risk_score": 0.10, "state": "NC"},
            {"location_id": "l2", "risk_tier": "Low", "risk_score": 0.20, "state": "NC"},
            {"location_id": "u1", "risk_tier": "UNSCORED", "risk_score": float("nan"), "state": "NC"},
        ]
    )
    out = tmp_path / "scored_locations.parquet"
    df.to_parquet(out, index=False)
    return out


# ---------------------------------------------------------------------------
# Tests: _load_events
# ---------------------------------------------------------------------------


class TestLoadEvents:
    def test_parses_one_event_per_line(self, tmp_path: Path) -> None:
        events_in = [_make_event(event_type="ORCHESTRATOR_START"), _make_event(event_type="TOOL_CALL")]
        log_path = _write_log(tmp_path, events_in)
        events, errs = M._load_events(log_path)
        assert len(events) == 2
        assert errs == 0
        assert events[0]["event_type"] == "ORCHESTRATOR_START"

    def test_skips_blank_lines_and_counts_parse_errors(self, tmp_path: Path) -> None:
        # Two valid events, one blank line (skipped, not counted), one
        # malformed line (counted as parse error). The malformed line
        # mimics a half-flushed write from a still-running pipeline.
        log_path = tmp_path / "pipeline_run_partial.jsonl"
        log_path.write_text(
            json.dumps(_make_event(event_type="A")) + "\n"
            + "\n"  # blank — silently skipped
            + "{not valid json\n"  # malformed — counted
            + json.dumps(_make_event(event_type="B")) + "\n",
            encoding="utf-8",
        )
        events, errs = M._load_events(log_path)
        assert [e["event_type"] for e in events] == ["A", "B"]
        assert errs == 1

    def test_raises_on_missing_log(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            M._load_events(tmp_path / "does-not-exist.jsonl")

    def test_load_events_matches_pipeline_logger_shape(self, tmp_path: Path) -> None:
        """Schema drift check — every key our metrics module reads must
        exist on a real PipelineLogger event."""
        from src.utils.logger import PipelineLogger

        log = PipelineLogger("metrics-shape-check")
        log.log_path = tmp_path / "pipeline_run_metrics-shape-check.jsonl"
        log.info("orchestrator", "TOOL_CALL", detail={"tool_name": "ingest_locations", "output_status": "ok"}, duration_ms=42)
        events, _ = M._load_events(log.log_path)
        evt = events[0]
        for key in ("timestamp", "level", "stage", "event_type", "detail", "duration_ms"):
            assert key in evt, f"PipelineLogger emits {key}; metrics module relies on it"


# ---------------------------------------------------------------------------
# Tests: percentile
# ---------------------------------------------------------------------------


class TestPercentile:
    def test_empty_returns_none(self) -> None:
        assert M._percentile([], 50) is None

    def test_single_value(self) -> None:
        assert M._percentile([42.0], 50) == 42.0
        assert M._percentile([42.0], 95) == 42.0

    def test_p50_and_p95_nearest_rank(self) -> None:
        # 10 values 1..10. P50 = 5 or 6 (nearest-rank lands on index 4 or 5).
        vals = [float(i) for i in range(1, 11)]
        assert M._percentile(vals, 50) in (5.0, 6.0)
        assert M._percentile(vals, 95) in (9.0, 10.0)
        assert M._percentile(vals, 100) == 10.0
        assert M._percentile(vals, 0) == 1.0


# ---------------------------------------------------------------------------
# Tests: per-stage metrics
# ---------------------------------------------------------------------------


class TestPerStageMetrics:
    def test_known_stages_always_present(self) -> None:
        out = M._per_stage_metrics([])
        for stage in M._KNOWN_STAGES:
            assert stage in out
            assert out[stage]["event_count"] == 0
            assert out[stage]["latency_ms"] is None

    def test_counts_by_level(self) -> None:
        events = [
            _make_event(stage="ingestion", level="INFO", event_type="A"),
            _make_event(stage="ingestion", level="WARNING", event_type="B"),
            _make_event(stage="ingestion", level="ERROR", event_type="C"),
            _make_event(stage="ingestion", level="INFO", event_type="D"),
        ]
        out = M._per_stage_metrics(events)
        ing = out["ingestion"]
        assert ing["event_count"] == 4
        assert ing["success_events"] == 2
        assert ing["warning_events"] == 1
        assert ing["error_events"] == 1
        assert ing["error_rate"] == 0.25
        assert ing["success_rate"] == 0.5

    def test_latency_rollup(self) -> None:
        events = [
            _make_event(stage="environmental", duration_ms=100),
            _make_event(stage="environmental", duration_ms=200),
            _make_event(stage="environmental", duration_ms=300),
            _make_event(stage="environmental", duration_ms=None),  # ignored
        ]
        lat = M._per_stage_metrics(events)["environmental"]["latency_ms"]
        assert lat is not None
        assert lat["count"] == 3
        assert lat["mean"] == 200.0
        assert lat["max"] == 300.0
        assert lat["p50"] in (200.0, 300.0)


# ---------------------------------------------------------------------------
# Tests: Claude metrics & tool call accuracy
# ---------------------------------------------------------------------------


class TestClaudeMetrics:
    def test_takes_last_cost_estimate_not_sum(self) -> None:
        # Three cumulative COST_ESTIMATE events. The metrics module must
        # take the LAST one, not sum them — they are cumulative on the
        # writer side and summing would triple-count.
        events = [
            _make_event(event_type="CLAUDE_RESPONSE"),
            _make_event(
                event_type="COST_ESTIMATE",
                detail={"total_input_tokens": 100, "total_output_tokens": 10, "estimated_cost_usd": 0.001},
            ),
            _make_event(event_type="CLAUDE_RESPONSE"),
            _make_event(
                event_type="COST_ESTIMATE",
                detail={"total_input_tokens": 250, "total_output_tokens": 30, "estimated_cost_usd": 0.005},
            ),
        ]
        out = M._claude_metrics(events)
        assert out["total_input_tokens"] == 250
        assert out["total_output_tokens"] == 30
        assert out["response_count"] == 2
        assert out["avg_input_per_response"] == 125.0
        assert out["avg_output_per_response"] == 15.0
        # Cost re-derived from totals via estimate_cost_usd, not the
        # logged value.
        from src.agents.orchestrator import estimate_cost_usd
        assert out["estimated_cost_usd"] == round(estimate_cost_usd(250, 30), 6)

    def test_empty_log_gives_zero_tokens(self) -> None:
        out = M._claude_metrics([])
        assert out["total_input_tokens"] == 0
        assert out["total_output_tokens"] == 0
        assert out["response_count"] == 0
        assert out["avg_input_per_response"] is None


class TestToolCallAccuracy:
    def test_happy_path_all_in_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(M, "_expected_tool_order", lambda: ["a", "b", "c"])
        events = [
            _make_event(event_type="TOOL_CALL", detail={"tool_name": "a"}),
            _make_event(event_type="TOOL_CALL", detail={"tool_name": "b"}),
            _make_event(event_type="TOOL_CALL", detail={"tool_name": "c"}),
        ]
        acc = M._tool_call_accuracy(events)
        assert acc["all_present"] is True
        assert acc["in_expected_order"] is True
        assert acc["missing"] == []
        assert acc["extra_or_repeated"] == []

    def test_missing_tool_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(M, "_expected_tool_order", lambda: ["a", "b", "c"])
        events = [
            _make_event(event_type="TOOL_CALL", detail={"tool_name": "a"}),
            _make_event(event_type="TOOL_CALL", detail={"tool_name": "c"}),
        ]
        acc = M._tool_call_accuracy(events)
        assert acc["all_present"] is False
        assert acc["in_expected_order"] is False
        assert acc["missing"] == ["b"]

    def test_wrong_order_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(M, "_expected_tool_order", lambda: ["a", "b", "c"])
        events = [
            _make_event(event_type="TOOL_CALL", detail={"tool_name": "a"}),
            _make_event(event_type="TOOL_CALL", detail={"tool_name": "c"}),
            _make_event(event_type="TOOL_CALL", detail={"tool_name": "b"}),
        ]
        acc = M._tool_call_accuracy(events)
        assert acc["all_present"] is True
        assert acc["in_expected_order"] is False

    def test_duplicate_flagged_as_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(M, "_expected_tool_order", lambda: ["a", "b"])
        events = [
            _make_event(event_type="TOOL_CALL", detail={"tool_name": "a"}),
            _make_event(event_type="TOOL_CALL", detail={"tool_name": "b"}),
            _make_event(event_type="TOOL_CALL", detail={"tool_name": "b"}),
        ]
        acc = M._tool_call_accuracy(events)
        assert acc["in_expected_order"] is False
        assert "b" in acc["extra_or_repeated"]

    def test_unknown_tool_flagged_as_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(M, "_expected_tool_order", lambda: ["a", "b"])
        events = [
            _make_event(event_type="TOOL_CALL", detail={"tool_name": "a"}),
            _make_event(event_type="TOOL_CALL", detail={"tool_name": "b"}),
            _make_event(event_type="TOOL_CALL", detail={"tool_name": "rogue"}),
        ]
        acc = M._tool_call_accuracy(events)
        assert "rogue" in acc["extra_or_repeated"]

    def test_expected_order_reads_live_TOOLS(self) -> None:
        """The expected order must match the real orchestrator TOOLS so
        the metrics module can't drift from the production spec."""
        from src.agents.orchestrator import TOOLS
        assert M._expected_tool_order() == [t["name"] for t in TOOLS]


# ---------------------------------------------------------------------------
# Tests: output quality
# ---------------------------------------------------------------------------


class TestOutputQuality:
    def test_no_scored_path_returns_log_only_block(self) -> None:
        events = [
            _make_event(
                stage="environmental",
                event_type="ENRICHMENT_SUMMARY",
                detail={"cumulative_missing_rates": {"tcc": 0.33}},
            ),
            _make_event(
                event_type="TOOL_CALL",
                detail={"tool_name": "validate_results", "output_status": "warnings"},
            ),
        ]
        out = M._output_quality(events, None)
        assert out["scored_path"] is None
        assert out["total_locations"] is None
        assert out["missing_rates"] == {"tcc": 0.33}
        assert out["validation_status"] == "warnings"

    def test_reads_tier_distribution_from_parquet(self, tmp_path: Path) -> None:
        scored = _write_scored_parquet(tmp_path)
        out = M._output_quality([], scored)
        assert out["total_locations"] == 6
        assert out["scored_count"] == 5
        assert out["unscored_count"] == 1
        assert out["tier_distribution"]["High"]["count"] == 2
        assert out["tier_distribution"]["Moderate"]["count"] == 1
        assert out["tier_distribution"]["Low"]["count"] == 2
        assert out["tier_distribution"]["UNSCORED"]["count"] == 1
        # Mean/median use scored rows only — should be over 0.10..0.85
        # NOT include the NaN row.
        assert out["mean_risk_score"] is not None
        assert 0.10 <= out["mean_risk_score"] <= 0.85

    def test_handles_empty_parquet(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.parquet"
        pd.DataFrame(
            {"location_id": [], "risk_tier": [], "risk_score": [], "state": []}
        ).astype({"risk_tier": "string"}).to_parquet(empty, index=False)
        out = M._output_quality([], empty)
        assert out["total_locations"] == 0
        assert out["tier_distribution"] is None

    def test_missing_path_returns_base_block(self, tmp_path: Path) -> None:
        out = M._output_quality([], tmp_path / "nope.parquet")
        # Path string is recorded so the renderer can show "(missing)" cleanly.
        assert out["scored_path"] == str(tmp_path / "nope.parquet")
        assert out["total_locations"] is None


# ---------------------------------------------------------------------------
# Tests: full compute_pipeline_metrics + formatter + CLI
# ---------------------------------------------------------------------------


def _full_log_events() -> list[dict[str, Any]]:
    """Synthetic happy-path log used by the integration tests below."""
    return [
        _make_event(
            event_type="ORCHESTRATOR_START",
            detail={"csv_path": "data/x.csv", "sample_size": 100},
            timestamp="2026-05-27T01:00:00+00:00",
        ),
        _make_event(stage="ingestion", event_type="INGESTION_START"),
        _make_event(stage="ingestion", event_type="INGESTION_SUMMARY", duration_ms=80),
        _make_event(
            event_type="TOOL_CALL",
            detail={"tool_name": "ingest_locations", "output_status": "ok"},
            duration_ms=120,
        ),
        _make_event(
            stage="environmental",
            event_type="ENRICHMENT_SUMMARY",
            detail={
                "cumulative_missing": {"tcc": 33},
                "cumulative_missing_rates": {"tcc": 0.33},
            },
        ),
        _make_event(
            event_type="TOOL_CALL",
            detail={"tool_name": "sample_environment", "output_status": "ok"},
            duration_ms=200,
        ),
        _make_event(event_type="TOOL_CALL", detail={"tool_name": "score_risk", "output_status": "ok"}, duration_ms=60),
        _make_event(
            event_type="TOOL_CALL",
            detail={"tool_name": "validate_results", "output_status": "warnings"},
            duration_ms=40,
        ),
        _make_event(
            event_type="TOOL_CALL",
            detail={"tool_name": "generate_report", "output_status": "ok"},
            duration_ms=300,
        ),
        _make_event(event_type="CLAUDE_RESPONSE"),
        _make_event(
            event_type="COST_ESTIMATE",
            detail={"total_input_tokens": 1500, "total_output_tokens": 200, "estimated_cost_usd": 0.005},
        ),
        _make_event(
            event_type="ORCHESTRATOR_DONE",
            duration_ms=50000,
            detail={"tool_calls": 5, "total_input_tokens": 1500, "total_output_tokens": 200},
            timestamp="2026-05-27T01:00:50+00:00",
        ),
    ]


class TestComputePipelineMetrics:
    def test_end_to_end_shape(self, tmp_path: Path) -> None:
        log_path = _write_log(tmp_path, _full_log_events())
        scored = _write_scored_parquet(tmp_path)
        metrics = M.compute_pipeline_metrics(log_path, scored)

        assert set(metrics.keys()) == {"run", "per_stage", "claude", "output_quality"}
        assert metrics["run"]["duration_ms"] == 50000
        assert metrics["run"]["run_id"] == "test-abc123"
        assert metrics["run"]["event_count"] == 12

        assert metrics["claude"]["total_input_tokens"] == 1500
        assert metrics["claude"]["tool_call_accuracy"]["in_expected_order"] is True

        assert metrics["output_quality"]["total_locations"] == 6
        assert metrics["output_quality"]["scored_pct"] == round(5 / 6, 4)
        assert metrics["output_quality"]["validation_status"] == "warnings"

    def test_run_id_extracted_from_filename(self, tmp_path: Path) -> None:
        log_path = _write_log(
            tmp_path,
            _full_log_events(),
            name="pipeline_run_real-id-9f21660b.jsonl",
        )
        metrics = M.compute_pipeline_metrics(log_path)
        assert metrics["run"]["run_id"] == "real-id-9f21660b"


class TestFormatMetricsText:
    def test_contains_all_section_headers(self, tmp_path: Path) -> None:
        log_path = _write_log(tmp_path, _full_log_events())
        scored = _write_scored_parquet(tmp_path)
        text = M.format_metrics_text(M.compute_pipeline_metrics(log_path, scored))
        for needle in (
            "PIPELINE METRICS",
            "Per-stage health",
            "Claude agent",
            "Output quality",
            "Tool call accuracy",
            "Missing-data rates",
            "Tier distribution",
        ):
            assert needle in text, f"section header {needle!r} missing"

    def test_handles_missing_values(self) -> None:
        # All-None / empty metrics dict — formatter must not crash.
        empty = {
            "run": {"run_id": "empty", "log_path": "/x", "event_count": 0, "parse_errors": 0,
                    "start_timestamp": None, "end_timestamp": None, "duration_ms": None},
            "per_stage": M._per_stage_metrics([]),
            "claude": M._claude_metrics([]),
            "output_quality": M._output_quality([], None),
        }
        text = M.format_metrics_text(empty)
        assert "PIPELINE METRICS" in text
        # No tier dist, no missing rates — section headers absent, no
        # KeyError.
        assert "Tier distribution" not in text
        assert "Missing-data rates" not in text


class TestCLI:
    def test_help_exits_with_code_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = M._main(["--help"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "Usage:" in err

    def test_runs_on_log_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log_path = _write_log(tmp_path, _full_log_events())
        scored = _write_scored_parquet(tmp_path)
        rc = M._main([str(log_path), str(scored)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "PIPELINE METRICS" in out
        assert "Tool call accuracy" in out
