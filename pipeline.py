"""
Top-level pipeline entrypoint.

Usage
-----
Batch mode (full pipeline run):

    python pipeline.py --csv data/locations.csv
    python pipeline.py --csv data/locations.csv --sample 10000
    python pipeline.py --csv data/locations.csv --states NC CA
    python pipeline.py --csv data/locations.csv --resume

Dry-run mode (smoke-test pipeline structure with NO Claude API spend):

    python pipeline.py --csv data/locations.csv --dry-run
    python pipeline.py --csv data/locations.csv --mode dry-run

Interactive mode (single coordinate):

    python pipeline.py --mode interactive --lat 35.06 --lon -80.66

Design notes
------------
This file is intentionally thin: every meaningful piece of behavior lives in
``src/agents/orchestrator.py``. Phase 7's redesign moved the agent loop down
into the orchestrator (one orchestration session of ~6 turns, five
pipeline-level tools), so
``pipeline.py``'s only jobs are:

* parse CLI args,
* instantiate the structured logger,
* construct the orchestrator with the API key,
* install Ctrl+C handling and real-time progress hooks, and
* dispatch into ``run`` (batch), ``run_interactive`` (single point), or
  ``run_dry`` (no Claude, just execute the five tool internals).

Phase 8 additions: ``--states`` filter (post-ingestion), ``--resume`` (each
tool short-circuits when its output parquet already exists), ``--mode`` as
the canonical mode selector (``--interactive`` / ``--dry-run`` stay as
aliases for backward compatibility with Phase 7), and a clean ``SIGINT``
handler that emits a ``PIPELINE_INTERRUPTED`` log event before exit.
"""
from __future__ import annotations

import argparse
import csv
import json
import signal
import subprocess
import sys
import time
import uuid
from typing import Any, Optional

import httpx

from src import config
from src.agents.orchestrator import PipelineOrchestrator, estimate_cost_usd
from src.utils.logger import PipelineLogger


# ---------------------------------------------------------------------------
# Real-time progress hooks
# ---------------------------------------------------------------------------


def _on_tool_start(tool_name: str, _tool_input: dict[str, Any]) -> None:
    """Print a banner immediately before a tool handler runs.

    Uses ``print(..., flush=True)`` so the banner appears in the terminal
    while the tool is still working, not buffered until the pipeline
    finishes. This is the whole point of the real-time progress output —
    the structured JSONL log is already capturing the same events for
    post-run audit.
    """
    print(f">>> Running {tool_name}...", flush=True)


def _on_tool_end(
    tool_name: str,
    duration_s: float,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Print the completion banner with per-tool duration + Claude tokens.

    ``input_tokens`` / ``output_tokens`` come from the Claude response that
    *emitted* this tool call — the orchestrator tracks them as the most
    recent response's totals. They're displayed alongside the tool name so
    a reviewer skimming the terminal can spot the calls that drove the
    biggest token bills without leaving the running pipeline.
    """
    print(
        f"<<< {tool_name} complete in {duration_s:.1f}s | "
        f"tokens: {input_tokens}in {output_tokens}out",
        flush=True,
    )


# ---------------------------------------------------------------------------
# Dry-run executor — no Claude, just the five tool internals
# ---------------------------------------------------------------------------


_DRY_RUN_TOOL_ORDER: tuple[str, ...] = (
    "ingest_locations",
    "sample_environment",
    "score_risk",
    "validate_results",
    "generate_report",
)


def _run_dry(
    orchestrator: PipelineOrchestrator,
    csv_path: str,
    sample_size: int,
) -> dict[str, Any]:
    """Run the five tools end-to-end with NO Claude API call.

    ``--dry-run`` is the "verify pipeline structure without paying for
    tokens" mode. We call each handler directly in spec order, threading
    the output_path of step N into the input of step N+1 — same shape
    Claude would orchestrate, just without an LLM in the loop.

    Print what each tool *receives* (input dict) and what it *returns*
    (summary dict, truncated for terminal readability). The structured
    JSONL log captures the full payload either way.
    """
    print(
        f"[DRY RUN] Claude API call skipped. Running 5 tool internals "
        f"directly with sample_size={sample_size}.\n",
        flush=True,
    )

    state: dict[str, Any] = {}
    summaries: list[dict[str, Any]] = []

    for tool_name in _DRY_RUN_TOOL_ORDER:
        tool_input = _dry_run_input_for(tool_name, csv_path, sample_size, state)

        print(f">>> Running {tool_name}...", flush=True)
        print(
            f"    input  = {json.dumps(tool_input, indent=2, default=str)}",
            flush=True,
        )

        start = time.monotonic()
        try:
            handler = getattr(orchestrator, f"_run_{tool_name}")
            result = handler(**tool_input)
        except Exception as exc:
            duration_s = time.monotonic() - start
            print(
                f"<<< {tool_name} FAILED in {duration_s:.1f}s | "
                f"tokens: 0in 0out\n    error = {type(exc).__name__}: {exc}",
                flush=True,
            )
            return {
                "status": "dry_run_failed",
                "failed_tool": tool_name,
                "error": str(exc),
                "exception_type": type(exc).__name__,
                "summaries": summaries,
            }
        duration_s = time.monotonic() - start

        # Mutate ``state`` so the next tool can read this one's outputs.
        if isinstance(result, dict):
            if "output_path" in result:
                state[f"{tool_name}_output_path"] = result["output_path"]
            if tool_name == "validate_results":
                state["validation_report"] = result

        summaries.append({"tool": tool_name, "input": tool_input, "result": result})

        print(
            f"<<< {tool_name} complete in {duration_s:.1f}s | "
            f"tokens: 0in 0out",
            flush=True,
        )
        print(
            f"    result = {_truncate(json.dumps(result, indent=2, default=str), 600)}\n",
            flush=True,
        )

    print(
        "[DRY RUN] Pipeline structure verified end-to-end. "
        "No Claude tokens consumed.",
        flush=True,
    )
    return {"status": "dry_run_ok", "summaries": summaries}


def _dry_run_input_for(
    tool_name: str,
    csv_path: str,
    sample_size: int,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Build the input dict each tool would receive from Claude.

    Mirrors the input schemas declared in ``orchestrator.TOOLS`` so a
    reviewer can compare the printed call to the live Claude call shape.
    """
    if tool_name == "ingest_locations":
        return {"file_path": csv_path, "sample_size": sample_size}
    if tool_name == "sample_environment":
        return {
            "validated_locations_path": state["ingest_locations_output_path"]
        }
    if tool_name == "score_risk":
        return {
            "enriched_locations_path": state["sample_environment_output_path"]
        }
    if tool_name == "validate_results":
        return {
            "scored_locations_path": state["score_risk_output_path"]
        }
    if tool_name == "generate_report":
        return {
            "scored_locations_path": state["score_risk_output_path"],
            "validation_report": state.get("validation_report", {}),
        }
    raise ValueError(f"No dry-run input mapping for tool: {tool_name}")


def _truncate(text: str, max_chars: int) -> str:
    """Truncate a multi-line JSON dump so the terminal output stays scannable."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n    ... (+{len(text) - max_chars} more chars)"


# ---------------------------------------------------------------------------
# Ctrl+C handling
# ---------------------------------------------------------------------------


def _install_sigint_handler(logger: PipelineLogger) -> None:
    """Install a SIGINT handler that logs a checkpoint before exiting.

    Without this, Ctrl+C during a long enrichment leaves no breadcrumb
    in the JSONL log for the metrics module (Phase 11) to discover. The
    handler converts the signal into a clean ``PIPELINE_INTERRUPTED``
    event, then re-raises ``KeyboardInterrupt`` so the main loop's
    ``except`` block in :func:`main` can finalise the exit code.
    """
    def _handle(signum: int, _frame: Any) -> None:
        logger.warning(
            stage="pipeline",
            event_type="PIPELINE_INTERRUPTED",
            detail={"signal": signum, "note": "SIGINT received"},
        )
        # Re-raise as KeyboardInterrupt so ``main`` can branch on it.
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, _handle)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


_MODES = ("batch", "interactive", "dry-run")


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Build the argparse Namespace for the pipeline CLI.

    ``argv`` defaults to ``sys.argv[1:]`` when called from a terminal;
    tests pass an explicit list to drive the parser deterministically.
    The returned Namespace holds every flag described in the module
    docstring.
    """
    parser = argparse.ArgumentParser(
        description=(
            "LEO satellite coverage risk pipeline. Batch mode runs the "
            "full ingestion→enrichment→scoring→validation→report flow. "
            "Interactive mode scores a single coordinate with a "
            "plain-English explanation. Dry-run mode verifies pipeline "
            "structure with no Claude API spend."
        )
    )
    parser.add_argument(
        "--csv",
        type=str,
        default=str(config.LOCATIONS_CSV),
        help="Path to locations CSV (default: data/locations.csv).",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help=(
            "If set, sample this many validated rows before enrichment. "
            "Omit to run the full dataset."
        ),
    )
    parser.add_argument(
        "--states",
        nargs="+",
        default=None,
        metavar="STATE",
        help=(
            "Limit ingestion to one or more state abbreviations "
            "(e.g. --states NC CA). Case-insensitive."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Skip any pipeline step whose output parquet already exists "
            "on disk. Use to recover from a crashed run without re-paying "
            "the raster-enrichment cost."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=_MODES,
        default="batch",
        help=(
            "Pipeline mode. 'batch' (default) runs the full flow. "
            "'interactive' scores one lat/lon. 'dry-run' executes the "
            "5 tools without calling Claude."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Alias for --mode dry-run (kept for backward compatibility).",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Alias for --mode interactive (kept for backward compatibility).",
    )
    parser.add_argument("--lat", type=float, help="Latitude for interactive mode.")
    parser.add_argument("--lon", type=float, help="Longitude for interactive mode.")
    parser.add_argument(
        "--address",
        type=str,
        default=None,
        help="Interactive mode only: resolve an address to coordinates via Nominatim.",
    )
    parser.add_argument(
        "--county",
        type=str,
        default=None,
        help="Interactive mode only: assess a county by GEOID or county name.",
    )
    parser.add_argument(
        "--buffer",
        type=float,
        default=None,
        help=(
            "Interactive mode only: search radius in metres for better "
            f"alternatives (default {config.INTERACTIVE_BUFFER_METERS:.0f} m)."
        ),
    )
    parser.add_argument(
        "--regenerate-map",
        action="store_true",
        help=(
            "Skip all pipeline steps and re-run report/map generation using the "
            "existing scored parquet (data/processed/scored_locations.parquet)."
        ),
    )
    return parser.parse_args(argv)


def _resolve_mode(args: argparse.Namespace) -> str:
    """Reconcile ``--mode`` with the legacy ``--interactive`` / ``--dry-run``
    boolean flags. The legacy switches win when they're set, mirroring the
    Phase 7 behaviour, but ``--mode`` is what new docs reference."""
    if args.dry_run:
        return "dry-run"
    if args.interactive:
        return "interactive"
    return args.mode


def _require_api_key(allow_missing: bool) -> Optional[str]:
    """Return the API key, or None if the current mode tolerates its absence."""
    api_key = config.ANTHROPIC_API_KEY
    if api_key:
        return api_key
    if allow_missing:
        # Dry-run never calls Claude, so it can construct the client with a
        # dummy string. The anthropic SDK does not validate the key at
        # construction time — only at request time, which dry-run never reaches.
        return "dry-run-no-api-call"
    print(
        "ERROR: ANTHROPIC_API_KEY is not set. Copy .env.example to .env "
        "and add your key, or use --dry-run.",
        file=sys.stderr,
    )
    return None


def _raster_files_present() -> bool:
    """Return True if required raster artifacts exist on disk."""
    has_tcc = any(config.TCC_DIR.glob("*.tif")) or any(config.TCC_DIR.glob("*.tiff")) or any(
        config.TCC_DIR.glob("*.img")
    )
    has_lc = any(config.LC_DIR.glob("*.tif")) or any(config.LC_DIR.glob("*.tiff")) or any(
        config.LC_DIR.glob("*.img")
    )
    has_slope = config.SLOPE_RASTER_PATH.exists()
    return bool(has_tcc and has_lc and has_slope)


def _states_in_csv(csv_path: str, *, max_rows: int = 20_000) -> list[str]:
    """Best effort discovery of states present in the input CSV."""
    states: set[str] = set()
    try:
        with open(csv_path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                return ["NC"]
            fieldnames = {f.strip() for f in reader.fieldnames if f}
            has_state = "state" in fieldnames
            has_geoid = "geoid_cb" in fieldnames
            for i, row in enumerate(reader):
                if i >= max_rows:
                    break
                if has_state:
                    s = (row.get("state") or "").strip().upper()
                    if s:
                        states.add(s)
                        continue
                if has_geoid:
                    g = (row.get("geoid_cb") or "").strip()
                    if len(g) == 15 and g.isdigit():
                        abbr = config.STATE_FIPS_TO_ABBR.get(g[:2])
                        if abbr:
                            states.add(abbr)
    except Exception:
        return ["NC"]

    return sorted(states) if states else ["NC"]


def _download_required_rasters(states: list[str]) -> int:
    """Invoke the downloader CLI for the given states."""
    cmd = [sys.executable, "-m", "src.data.downloader", "--states", *states]
    return subprocess.call(cmd)


def _ensure_rasters(csv_path: str) -> bool:
    """Ensure rasters exist, optionally downloading them."""
    if _raster_files_present():
        return True

    answer = input(
        "Required rasters not found. Download now? This will take approximately 5 minutes. (yes/no) "
    ).strip().lower()
    if answer in {"y", "yes"}:
        states = _states_in_csv(csv_path)
        rc = _download_required_rasters(states)
        if rc != 0:
            print("ERROR: downloader failed.", file=sys.stderr)
            return False
        return _raster_files_present()

    print(
        "Raster files not found. Run: python -m src.data.downloader --states NC",
        file=sys.stderr,
    )
    return False


def _geocode_address(address: str) -> Optional[tuple[float, float]]:
    """Resolve an address to (lat, lon) using Nominatim."""
    if not address.strip():
        return None
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": address, "format": "json", "limit": 1}
    headers = {
        "User-Agent": "leo-satellite-coverage-risk/1.0 (educational challenge submission)",
    }
    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=20.0)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        lat = float(data[0]["lat"])
        lon = float(data[0]["lon"])
        return lat, lon
    except Exception:
        return None


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint — parse args, dispatch into batch / interactive / dry-run.

    Returns an integer exit code (``0`` success, ``1`` dry-run failure,
    ``2`` invalid args / missing API key, ``130`` ``SIGINT``). The
    function is testable: pass an explicit ``argv`` list to bypass
    ``sys.argv``. The module's ``if __name__ == "__main__"`` block
    wraps the result in ``SystemExit`` for POSIX-compliant exit
    propagation.
    """
    args = _parse_args(argv)
    mode = _resolve_mode(args)

    api_key = _require_api_key(
        allow_missing=(mode == "dry-run" or args.regenerate_map or args.county is not None)
    )
    if api_key is None:
        return 2

    logger = PipelineLogger(run_id=f"pipeline-{uuid.uuid4().hex[:8]}")
    _install_sigint_handler(logger)
    orchestrator = PipelineOrchestrator(api_key=api_key, logger=logger)

    # Wire the real-time progress hooks. The orchestrator invokes them
    # around every ``_dispatch_tool`` call (both live and inside dry-run's
    # direct handler calls would normally bypass them, which is why dry-run
    # prints its own banners).
    orchestrator.on_tool_start = _on_tool_start
    orchestrator.on_tool_end = _on_tool_end

    try:
        if args.regenerate_map:
            scored_path = config.DATA_DIR / "processed" / "scored_locations.parquet"
            if not scored_path.exists():
                print(
                    f"ERROR: scored parquet not found at {scored_path}. "
                    "Run the full pipeline first.",
                    file=sys.stderr,
                )
                return 2
            validation = orchestrator._run_validate_results(str(scored_path))
            result = orchestrator._run_generate_report(
                scored_locations_path=str(scored_path),
                validation_report=validation,
            )
            print(json.dumps(result, indent=2, default=str))
            return 0

        if mode == "dry-run":
            result = _run_dry(orchestrator, args.csv, sample_size=100)
            print(
                "\nDry-run summary:\n"
                f"{json.dumps({'status': result['status']}, indent=2)}"
            )
            return 0 if result["status"] == "dry_run_ok" else 1

        if mode == "interactive":
            if args.county:
                result = orchestrator.run_interactive_county(args.county)
                print(json.dumps(result, indent=2, default=str))
                return 0

            if not _ensure_rasters(args.csv):
                return 2

            lat = args.lat
            lon = args.lon
            if (lat is None or lon is None) and args.address:
                resolved = _geocode_address(args.address)
                if resolved is None:
                    print(
                        "ERROR: address geocoding failed. Try a more specific address.",
                        file=sys.stderr,
                    )
                    return 2
                lat, lon = resolved
                print(f"Resolved coordinates: lat={lat:.6f}, lon={lon:.6f}", flush=True)

            if lat is None or lon is None:
                print(
                    "ERROR: interactive mode requires --lat and --lon, or --address, or --county.",
                    file=sys.stderr,
                )
                return 2

            result = orchestrator.run_interactive(
                lat,
                lon,
                buffer_meters=args.buffer,
            )
            print(json.dumps(result, indent=2, default=str))
            return 0

        # mode == "batch"
        if not _ensure_rasters(args.csv):
            return 2
        sample_size = (
            args.sample if args.sample is not None else config.DEMO_SAMPLE_SIZE
        )
        result = orchestrator.run(
            args.csv,
            sample_size=sample_size,
            states=args.states,
            resume=args.resume,
        )
        print(result.get("final_text", ""))
        print(
            "\nRun cost: $"
            f"{estimate_cost_usd(result['total_input_tokens'], result['total_output_tokens']):.4f} "
            f"({result['total_input_tokens']}in {result['total_output_tokens']}out tokens)"
        )
        return 0
    except KeyboardInterrupt:
        # The SIGINT handler already wrote ``PIPELINE_INTERRUPTED`` to the
        # JSONL log. Surface a one-line user-facing summary too — distinct
        # from the regular completion banner so a tail of stdout makes the
        # cause of exit obvious.
        print(
            "\n[INTERRUPTED] Pipeline halted by SIGINT. Re-run with "
            "--resume to continue from the last completed step.",
            flush=True,
        )
        return 130  # POSIX convention: 128 + SIGINT (2)


if __name__ == "__main__":
    raise SystemExit(main())
