"""
Top-level pipeline entrypoint.

Usage
-----
Batch mode (full pipeline run):

    python pipeline.py --csv data/locations.csv
    python pipeline.py --csv data/locations.csv --sample 10000

Dry-run mode (smoke-test pipeline structure with NO Claude API spend):

    python pipeline.py --csv data/locations.csv --dry-run

Interactive mode (single coordinate):

    python pipeline.py --interactive --lat 35.06 --lon -80.66

Design notes
------------
This file is intentionally thin: every meaningful piece of behavior lives in
``src/agents/orchestrator.py``. Phase 7's redesign moved the agent loop down
into the orchestrator (single Claude call, five pipeline-level tools), so
``pipeline.py``'s only jobs are:

* parse CLI args,
* instantiate the structured logger,
* construct the orchestrator with the API key,
* wire real-time progress prints to the orchestrator's tool hooks, and
* dispatch into ``run`` (batch), ``run_interactive`` (single point), or
  ``run_dry`` (no Claude, just execute the five tool internals).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any, Optional

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
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
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
        "--dry-run",
        action="store_true",
        help=(
            "Run the 5 pipeline tools directly without calling Claude. "
            "Forces sample_size=100. Use to verify pipeline structure "
            "before spending any tokens."
        ),
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Run interactive mode for a single coordinate.",
    )
    parser.add_argument("--lat", type=float, help="Latitude for interactive mode.")
    parser.add_argument("--lon", type=float, help="Longitude for interactive mode.")
    return parser.parse_args(argv)


def _require_api_key(allow_missing: bool) -> Optional[str]:
    """Return the API key, or None if dry-run mode tolerates its absence."""
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


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)

    api_key = _require_api_key(allow_missing=args.dry_run)
    if api_key is None:
        return 2

    logger = PipelineLogger(run_id=f"pipeline-{uuid.uuid4().hex[:8]}")
    orchestrator = PipelineOrchestrator(api_key=api_key, logger=logger)

    # Wire the real-time progress hooks. The orchestrator invokes them
    # around every ``_dispatch_tool`` call (both live and inside dry-run's
    # direct handler calls would normally bypass them, which is why dry-run
    # prints its own banners).
    orchestrator.on_tool_start = _on_tool_start
    orchestrator.on_tool_end = _on_tool_end

    if args.dry_run:
        # Dry-run never hits Claude. Force sample_size=100 (per spec) so a
        # 4.67M-row CSV doesn't get fully ingested just to smoke-test the
        # call chain.
        result = _run_dry(orchestrator, args.csv, sample_size=100)
        print(
            "\nDry-run summary:\n"
            f"{json.dumps({'status': result['status']}, indent=2)}"
        )
        return 0 if result["status"] == "dry_run_ok" else 1

    if args.interactive:
        if args.lat is None or args.lon is None:
            print(
                "ERROR: --interactive requires --lat and --lon.",
                file=sys.stderr,
            )
            return 2
        result = orchestrator.run_interactive(args.lat, args.lon)
        print(json.dumps(result, indent=2, default=str))
        return 0

    sample_size = args.sample if args.sample is not None else config.DEMO_SAMPLE_SIZE
    result = orchestrator.run(args.csv, sample_size=sample_size)
    print(result.get("final_text", ""))
    print(
        "\nRun cost: $"
        f"{estimate_cost_usd(result['total_input_tokens'], result['total_output_tokens']):.4f} "
        f"({result['total_input_tokens']}in {result['total_output_tokens']}out tokens)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
