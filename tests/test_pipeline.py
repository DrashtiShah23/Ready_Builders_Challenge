"""
Phase 7 follow-up tests: ``pipeline.py`` CLI surface.

These tests are intentionally narrow — pipeline.py is a thin shell, and the
heavy lifting is covered by ``tests/test_orchestrator.py``. The cases here
exist to lock in the user-facing CLI contract:

* ``--dry-run`` never calls Claude, threads output paths between tools,
  exits 0 on the happy path, and exits non-zero on a tool failure.
* The real-time progress hooks emit ``>>> Running ...`` / ``<<< ... complete``
  banners around every tool call.
* ``--interactive`` short-circuits without ``--lat``/``--lon``.
"""
from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import pipeline as pipeline_mod
from src.agents.orchestrator import PipelineOrchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_orchestrator_stub() -> MagicMock:
    """Return a MagicMock that quacks like a PipelineOrchestrator.

    Each of the five ``_run_*`` handlers returns a deterministic summary
    with an ``output_path`` (so dry-run can thread paths through), and
    ``run``/``run_interactive`` are mocked so the test never reaches the
    real Anthropic client.
    """
    stub = MagicMock(spec=PipelineOrchestrator)
    stub.on_tool_start = None
    stub.on_tool_end = None
    stub._run_ingest_locations = MagicMock(
        return_value={"status": "ok", "output_path": "/tmp/v.parquet"}
    )
    stub._run_sample_environment = MagicMock(
        return_value={"status": "ok", "output_path": "/tmp/e.parquet"}
    )
    stub._run_score_risk = MagicMock(
        return_value={"status": "ok", "output_path": "/tmp/s.parquet"}
    )
    stub._run_validate_results = MagicMock(
        return_value={"status": "passed", "recommendation": "proceed"}
    )
    stub._run_generate_report = MagicMock(
        return_value={"status": "ok", "outputs": {"report": "/tmp/r.md"}}
    )
    stub.run = MagicMock(
        return_value={
            "final_text": "Pipeline complete.",
            "tool_call_trace": [],
            "total_input_tokens": 100,
            "total_output_tokens": 200,
            "duration_ms": 1,
        }
    )
    stub.run_interactive = MagicMock(
        return_value={"risk_tier": "Low", "explanation": "OK"}
    )
    return stub


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dry_run_does_not_call_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--dry-run`` exits 0 and never invokes ``orchestrator.run``."""
    stub = _build_orchestrator_stub()
    monkeypatch.setattr(
        pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
    )

    rc = pipeline_mod.main(["--csv", "fake.csv", "--dry-run"])

    assert rc == 0
    stub.run.assert_not_called()
    # Every dry-run tool handler must be invoked exactly once, in order.
    for handler in (
        stub._run_ingest_locations,
        stub._run_sample_environment,
        stub._run_score_risk,
        stub._run_validate_results,
        stub._run_generate_report,
    ):
        assert handler.call_count == 1


def test_dry_run_forces_sample_size_100(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec: ``--dry-run`` always passes ``sample_size=100`` to ingest."""
    stub = _build_orchestrator_stub()
    monkeypatch.setattr(
        pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
    )
    pipeline_mod.main(["--csv", "fake.csv", "--dry-run"])
    stub._run_ingest_locations.assert_called_once_with(
        file_path="fake.csv", sample_size=100
    )


def test_dry_run_threads_output_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """The output_path from step N must arrive as the input of step N+1."""
    stub = _build_orchestrator_stub()
    monkeypatch.setattr(
        pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
    )
    pipeline_mod.main(["--csv", "fake.csv", "--dry-run"])

    stub._run_sample_environment.assert_called_once_with(
        validated_locations_path="/tmp/v.parquet"
    )
    stub._run_score_risk.assert_called_once_with(
        enriched_locations_path="/tmp/e.parquet"
    )
    stub._run_validate_results.assert_called_once_with(
        scored_locations_path="/tmp/s.parquet"
    )
    # generate_report receives both the scored path and the validation
    # report dict that step 4 produced.
    call_kwargs = stub._run_generate_report.call_args.kwargs
    assert call_kwargs["scored_locations_path"] == "/tmp/s.parquet"
    assert call_kwargs["validation_report"] == {
        "status": "passed",
        "recommendation": "proceed",
    }


def test_dry_run_prints_progress_banners(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user-facing banners are the whole point of dry-run output."""
    stub = _build_orchestrator_stub()
    monkeypatch.setattr(
        pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        pipeline_mod.main(["--csv", "fake.csv", "--dry-run"])
    out = buf.getvalue()

    assert "[DRY RUN]" in out
    for name in (
        "ingest_locations",
        "sample_environment",
        "score_risk",
        "validate_results",
        "generate_report",
    ):
        assert f">>> Running {name}..." in out
        assert f"<<< {name} complete" in out


def test_dry_run_reports_failed_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a tool raises mid-dry-run we exit non-zero with the failure name."""
    stub = _build_orchestrator_stub()
    stub._run_score_risk.side_effect = RuntimeError("synthetic")
    monkeypatch.setattr(
        pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
    )

    rc = pipeline_mod.main(["--csv", "fake.csv", "--dry-run"])
    assert rc == 1
    # Tools after the failure must NOT run.
    stub._run_validate_results.assert_not_called()
    stub._run_generate_report.assert_not_called()


def test_real_run_wires_progress_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Batch mode must install both progress hooks on the orchestrator."""
    stub = _build_orchestrator_stub()
    monkeypatch.setattr(
        pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
    )
    monkeypatch.setattr(pipeline_mod.config, "ANTHROPIC_API_KEY", "test-key")

    pipeline_mod.main(["--csv", "fake.csv", "--sample", "10"])

    assert stub.on_tool_start is pipeline_mod._on_tool_start
    assert stub.on_tool_end is pipeline_mod._on_tool_end
    # Batch mode now threads the Phase 8 ``states`` and ``resume`` knobs
    # through to ``run()``, even when the CLI defaults were taken.
    stub.run.assert_called_once_with(
        "fake.csv", sample_size=10, states=None, resume=False
    )


def test_interactive_requires_lat_lon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Interactive without both coordinates exits 2 before any Claude call."""
    stub = _build_orchestrator_stub()
    monkeypatch.setattr(
        pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
    )
    monkeypatch.setattr(pipeline_mod.config, "ANTHROPIC_API_KEY", "test-key")

    rc = pipeline_mod.main(["--interactive"])
    assert rc == 2
    stub.run_interactive.assert_not_called()


def test_real_run_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-dry-run with no API key exits 2 and prints a clear error."""
    monkeypatch.setattr(pipeline_mod.config, "ANTHROPIC_API_KEY", None)

    buf = io.StringIO()
    # Print should go to stderr, but we just want to ensure the rc.
    rc = pipeline_mod.main(["--csv", "fake.csv"])
    assert rc == 2


def test_dry_run_tolerates_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dry-run never calls Claude, so the missing API key must not block it."""
    stub = _build_orchestrator_stub()
    monkeypatch.setattr(
        pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
    )
    monkeypatch.setattr(pipeline_mod.config, "ANTHROPIC_API_KEY", None)

    rc = pipeline_mod.main(["--csv", "fake.csv", "--dry-run"])
    assert rc == 0


def test_progress_hooks_format(capfd: pytest.CaptureFixture[str]) -> None:
    """``_on_tool_start`` and ``_on_tool_end`` print the exact spec'd format."""
    pipeline_mod._on_tool_start("ingest_locations", {"file_path": "x.csv"})
    pipeline_mod._on_tool_end("ingest_locations", 1.23, 100, 50)
    out, _err = capfd.readouterr()
    assert ">>> Running ingest_locations..." in out
    assert "<<< ingest_locations complete in 1.2s | tokens: 100in 50out" in out


# ---------------------------------------------------------------------------
# Phase 8: --states, --resume, --mode, Ctrl+C handling
# ---------------------------------------------------------------------------


class TestStatesFlag:
    def test_states_threaded_to_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _build_orchestrator_stub()
        monkeypatch.setattr(
            pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
        )
        monkeypatch.setattr(pipeline_mod.config, "ANTHROPIC_API_KEY", "test-key")

        pipeline_mod.main(["--csv", "fake.csv", "--states", "NC", "CA"])
        stub.run.assert_called_once_with(
            "fake.csv", sample_size=None, states=["NC", "CA"], resume=False
        )

    def test_states_default_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _build_orchestrator_stub()
        monkeypatch.setattr(
            pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
        )
        monkeypatch.setattr(pipeline_mod.config, "ANTHROPIC_API_KEY", "test-key")

        pipeline_mod.main(["--csv", "fake.csv"])
        _, kwargs = stub.run.call_args
        assert kwargs["states"] is None


class TestResumeFlag:
    def test_resume_threaded_to_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _build_orchestrator_stub()
        monkeypatch.setattr(
            pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
        )
        monkeypatch.setattr(pipeline_mod.config, "ANTHROPIC_API_KEY", "test-key")

        pipeline_mod.main(["--csv", "fake.csv", "--resume"])
        stub.run.assert_called_once_with(
            "fake.csv", sample_size=None, states=None, resume=True
        )

    def test_resume_default_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _build_orchestrator_stub()
        monkeypatch.setattr(
            pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
        )
        monkeypatch.setattr(pipeline_mod.config, "ANTHROPIC_API_KEY", "test-key")

        pipeline_mod.main(["--csv", "fake.csv"])
        _, kwargs = stub.run.call_args
        assert kwargs["resume"] is False


class TestModeFlag:
    def test_mode_dry_run_takes_dry_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _build_orchestrator_stub()
        monkeypatch.setattr(
            pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
        )
        rc = pipeline_mod.main(["--csv", "fake.csv", "--mode", "dry-run"])
        assert rc == 0
        stub.run.assert_not_called()
        stub._run_ingest_locations.assert_called_once()

    def test_mode_interactive_takes_interactive_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _build_orchestrator_stub()
        monkeypatch.setattr(
            pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
        )
        monkeypatch.setattr(pipeline_mod.config, "ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(pipeline_mod, "_ensure_rasters", lambda _csv: True)

        rc = pipeline_mod.main(
            ["--mode", "interactive", "--lat", "35.5", "--lon", "-80.0"]
        )
        assert rc == 0
        stub.run_interactive.assert_called_once_with(
            35.5, -80.0, buffer_meters=None
        )

    def test_interactive_buffer_flag_is_forwarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _build_orchestrator_stub()
        monkeypatch.setattr(
            pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
        )
        monkeypatch.setattr(pipeline_mod.config, "ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(pipeline_mod, "_ensure_rasters", lambda _csv: True)

        rc = pipeline_mod.main(
            [
                "--mode",
                "interactive",
                "--lat",
                "35.5",
                "--lon",
                "-80.0",
                "--buffer",
                "2500",
            ]
        )
        assert rc == 0
        stub.run_interactive.assert_called_once_with(
            35.5, -80.0, buffer_meters=2500.0
        )

    def test_mode_batch_is_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _build_orchestrator_stub()
        monkeypatch.setattr(
            pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
        )
        monkeypatch.setattr(pipeline_mod.config, "ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(pipeline_mod, "_ensure_rasters", lambda _csv: True)

        pipeline_mod.main(["--csv", "fake.csv"])
        stub.run.assert_called_once()

    def test_legacy_interactive_flag_still_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _build_orchestrator_stub()
        monkeypatch.setattr(
            pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
        )
        monkeypatch.setattr(pipeline_mod.config, "ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(pipeline_mod, "_ensure_rasters", lambda _csv: True)

        rc = pipeline_mod.main(
            ["--interactive", "--lat", "35.5", "--lon", "-80.0"]
        )
        assert rc == 0
        stub.run_interactive.assert_called_once_with(
            35.5, -80.0, buffer_meters=None
        )

    def test_regenerate_map_skips_pipeline_and_calls_generate_report(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stub = _build_orchestrator_stub()
        monkeypatch.setattr(
            pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
        )
        # regenerate-map allows missing API key, but still needs the scored parquet present
        monkeypatch.setattr(pipeline_mod.config, "ANTHROPIC_API_KEY", None)
        processed = tmp_path / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        scored = processed / "scored_locations.parquet"
        scored.write_bytes(b"PAR1")  # sentinel; stubbed methods never read it
        monkeypatch.setattr(pipeline_mod.config, "DATA_DIR", tmp_path)

        rc = pipeline_mod.main(["--regenerate-map"])
        assert rc == 0
        stub._run_validate_results.assert_called_once()
        stub._run_generate_report.assert_called_once()

    def test_legacy_dry_run_flag_still_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _build_orchestrator_stub()
        monkeypatch.setattr(
            pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
        )
        rc = pipeline_mod.main(["--csv", "fake.csv", "--dry-run"])
        assert rc == 0
        stub.run.assert_not_called()

    def test_interactive_address_geocoding_resolves_coordinates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _build_orchestrator_stub()
        monkeypatch.setattr(
            pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
        )
        monkeypatch.setattr(pipeline_mod.config, "ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(pipeline_mod, "_ensure_rasters", lambda _csv: True)
        monkeypatch.setattr(pipeline_mod, "_geocode_address", lambda _addr: (35.0, -80.0))

        rc = pipeline_mod.main(["--mode", "interactive", "--address", "Charlotte, NC"])
        assert rc == 0
        stub.run_interactive.assert_called_once_with(35.0, -80.0, buffer_meters=None)

    def test_interactive_county_short_circuits_without_rasters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _build_orchestrator_stub()
        monkeypatch.setattr(
            pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
        )
        monkeypatch.setattr(pipeline_mod.config, "ANTHROPIC_API_KEY", None)
        # If county mode accidentally touches rasters, this will fail the test.
        monkeypatch.setattr(pipeline_mod, "_ensure_rasters", lambda _csv: False)

        rc = pipeline_mod.main(["--mode", "interactive", "--county", "37135"])
        assert rc == 0
        stub.run_interactive_county.assert_called_once_with("37135")


class TestSigintHandler:
    def test_keyboard_interrupt_exit_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``KeyboardInterrupt`` raised mid-run must exit 130, not crash."""
        stub = _build_orchestrator_stub()
        stub.run.side_effect = KeyboardInterrupt()
        monkeypatch.setattr(
            pipeline_mod, "PipelineOrchestrator", MagicMock(return_value=stub)
        )
        monkeypatch.setattr(pipeline_mod.config, "ANTHROPIC_API_KEY", "test-key")

        rc = pipeline_mod.main(["--csv", "fake.csv"])
        assert rc == 130


class TestResolveMode:
    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["--csv", "x"], "batch"),
            (["--csv", "x", "--mode", "batch"], "batch"),
            (["--csv", "x", "--mode", "dry-run"], "dry-run"),
            (["--csv", "x", "--mode", "interactive"], "interactive"),
            (["--csv", "x", "--dry-run"], "dry-run"),
            (["--csv", "x", "--interactive"], "interactive"),
            # Legacy flag wins over an opposite explicit mode (Phase 7 behaviour).
            (["--csv", "x", "--mode", "batch", "--dry-run"], "dry-run"),
        ],
    )
    def test_resolve_mode(self, argv: list[str], expected: str) -> None:
        args = pipeline_mod._parse_args(argv)
        assert pipeline_mod._resolve_mode(args) == expected
