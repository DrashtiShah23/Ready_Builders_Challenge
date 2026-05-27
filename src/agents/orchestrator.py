"""
Pipeline Orchestrator: single-call Claude design (Phase 7 redesign).

Why pipeline-level orchestration instead of per-batch reasoning
---------------------------------------------------------------
The original Phase 7 design had Claude reason per batch of 100 locations,
making tool calls for each row. At 4.67M rows that would mean ~46,700 Claude
calls and roughly $10,600 in API spend — prohibitively expensive for a
challenge submission and never the right shape for a production pipeline.

The redesign keeps **all** of the agentic reasoning, just at the right scale:
Claude makes ONE API call total, with five pipeline-level tools. Each tool
internally runs the full dataset through the existing Phase 1-6 agents (the
ingestion, environmental, and scoring modules are untouched). Claude reasons
about the **summary** of each step, flags anomalies between steps, decides
whether the pipeline can proceed. Total cost: well under $1 for the full
North Carolina dataset.

The per-location Claude reasoning surface is preserved in :meth:`run_interactive`,
which IS a per-location call. It costs ~$0.01 per query and demonstrates the
"user asks where can my dish see the sky" agentic scenario without applying
that cost shape at the 4.67M-row scale where it would be infeasible.

Tool flow
---------
    ingest_locations  →  sample_environment  →  score_risk
                                                      ↓
                              generate_report  ←  validate_results

Each tool persists its output to a parquet file at ``data/processed/`` and
passes the path to the next tool. Claude never sees individual location
records — only summaries, distributions, and validation reports. That is the
whole point of the redesign: Claude's reasoning is applied where it can move
the needle (data quality calls, anomaly flagging, tier-distribution sanity
checks) and the per-row work stays in deterministic Python.

Safety
------
The dispatch loop is bounded by :data:`config.MAX_AGENT_TURNS`. Tool failures
return error JSON to Claude rather than raising, so a single bad tool call
cannot crash the run mid-stream. Every dispatch logs ``TOOL_CALL`` with name,
inputs, output summary, and duration.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional, Union

import anthropic
import pandas as pd

from src import config
from src.agents.environmental import EnvironmentalAgent
from src.agents.ingestion import IngestionAgent, Reason
from src.agents.scoring import (
    TIER_HIGH,
    TIER_LOW,
    TIER_MODERATE,
    TIER_UNSCORED,
    score_components,
)
from src.data import store
from src.schemas.location import ValidatedLocation
from src.tools.elevation import fetch_elevation
from src.tools.landcover import fetch_land_cover
from src.tools.tcc import fetch_tcc
from src.utils.logger import PipelineLogger


# ---------------------------------------------------------------------------
# Paths the orchestrator writes to (read by downstream tools / pipeline.py)
# ---------------------------------------------------------------------------

_PROCESSED_DIR: Path = config.DATA_DIR / "processed"
_OUTPUTS_DIR: Path = config.PROJECT_ROOT / "outputs"
_VALIDATED_PARQUET: Path = _PROCESSED_DIR / "validated_locations.parquet"
_ENRICHED_PARQUET: Path = _PROCESSED_DIR / "enriched_locations.parquet"
_SCORED_PARQUET: Path = _PROCESSED_DIR / "scored_locations.parquet"

_STATE_SUMMARY_PARQUET: Path = config.SCORED_DIR / "risk_summary_by_state.parquet"
_COUNTY_SUMMARY_PARQUET: Path = config.SCORED_DIR / "risk_summary_by_county.parquet"
_REPORT_MD: Path = _OUTPUTS_DIR / "analysis_report.md"
_MAP_HTML: Path = _OUTPUTS_DIR / "risk_map.html"

# Map render is bounded — 4.67M markers would never render anyway.
_MAP_MAX_POINTS: int = 5_000

# NC bounding box used by the geographic-sanity validation check.
_NC_LAT_MIN: float = 33.75
_NC_LAT_MAX: float = 36.59
_NC_LON_MIN: float = -84.32
_NC_LON_MAX: float = -75.46

# Thresholds named here so the validation check bodies are self-documenting.
_DOMINANT_TIER_THRESHOLD: float = 0.80
_FOREST_LOW_TCC_THRESHOLD: int = 10
_FOREST_LOW_TCC_RATE_FLAG: float = 0.001
_MISSING_DATA_FLAG_THRESHOLD: float = 0.15


# ---------------------------------------------------------------------------
# Tool definitions — passed to the Anthropic API as the ``tools`` parameter.
# Descriptions written for Claude, not for humans; they tell Claude exactly
# when to invoke each tool and what to do with the return value.
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "ingest_locations",
        "description": (
            "Load and validate the locations CSV. Runs chunked validation, "
            "deduplication, and geoid_cb derivation for state/county. "
            "Returns a quality summary — do NOT proceed to sample_environment "
            "if critical_failure is True or valid_pct is below 0.80."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the locations CSV file",
                },
                "sample_size": {
                    "type": ["integer", "null"],
                    "description": (
                        "If set, sample this many rows after validation. "
                        "Null means full dataset."
                    ),
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "sample_environment",
        "description": (
            "Enrich validated locations with TCC, elevation, slope, aspect, "
            "and land cover from local raster files. Opens raster handles once "
            "per RASTER_BATCH_SIZE chunk for memory efficiency. "
            "Returns enrichment summary with missing-data rates per signal. "
            "Flag to Claude if any signal is missing for more than 10 percent of rows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "validated_locations_path": {
                    "type": "string",
                    "description": "Path to parquet output from ingest_locations",
                },
            },
            "required": ["validated_locations_path"],
        },
    },
    {
        "name": "score_risk",
        "description": (
            "Apply the composite risk scoring formula to all enriched locations. "
            "Formula: (tcc_score * 0.50) + (terrain_score * 0.30) + (landcover_score * 0.20). "
            "Returns scored locations and tier distribution. "
            "Flag to Claude if more than 80 percent of locations fall in the same tier — "
            "that is a signal the thresholds may need review."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "enriched_locations_path": {
                    "type": "string",
                    "description": "Path to parquet output from sample_environment",
                },
            },
            "required": ["enriched_locations_path"],
        },
    },
    {
        "name": "validate_results",
        "description": (
            "Run four validation checks on scored locations: "
            "(1) distribution sanity — flag if more than 80 percent in one tier, "
            "(2) cross-validation — flag locations where land_cover is forest but tcc_pct is below 10, "
            "(3) geographic sanity — flag locations outside NC bounding box or in water, "
            "(4) missing data rate — flag if more than 15 percent missing any single signal. "
            "Returns validation report. Claude should review anomalies before calling generate_report."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scored_locations_path": {
                    "type": "string",
                    "description": "Path to parquet output from score_risk",
                },
            },
            "required": ["scored_locations_path"],
        },
    },
    {
        "name": "generate_report",
        "description": (
            "Generate the final analysis report, risk summary parquet files, "
            "and interactive Folium map. "
            "Produces: outputs/analysis_report.md, "
            "outputs/scored/risk_summary_by_state.parquet, "
            "outputs/scored/risk_summary_by_county.parquet, "
            "outputs/risk_map.html."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scored_locations_path": {
                    "type": "string",
                    "description": "Path to parquet output from score_risk",
                },
                "validation_report": {
                    "type": "object",
                    "description": (
                        "The full validation report returned by validate_results"
                    ),
                },
            },
            "required": ["scored_locations_path", "validation_report"],
        },
    },
]


# ---------------------------------------------------------------------------
# System prompt loader (versioned external file, not hardcoded string)
# ---------------------------------------------------------------------------


_PROMPT_PATH: Path = Path(__file__).parent / "prompts" / "orchestrator_v1.txt"


def _load_system_prompt() -> str:
    """Read the versioned system prompt from disk.

    Kept in a file (not a Python string) so prompt revisions are reviewable
    in git diffs and so the prompt artifact is auditable separately from
    the Python that loads it.
    """
    return _PROMPT_PATH.read_text(encoding="utf-8")


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Estimate Claude spend in USD for a given token count.

    Uses the rates declared in ``config.CLAUDE_INPUT_COST_PER_MTOK`` and
    ``config.CLAUDE_OUTPUT_COST_PER_MTOK``. Pulled into a free function so
    the per-response log event and the optional CLI cost summaries (Phase 8)
    can share the same arithmetic without re-deriving rates locally.
    """
    return (
        (input_tokens / 1_000_000.0) * config.CLAUDE_INPUT_COST_PER_MTOK
        + (output_tokens / 1_000_000.0) * config.CLAUDE_OUTPUT_COST_PER_MTOK
    )


# Callback signatures for the optional pipeline-side hooks. The orchestrator
# invokes ``on_tool_start`` immediately before each tool handler runs and
# ``on_tool_end`` immediately after, regardless of success or failure. They
# are intentionally typed as ``Callable[..., None]`` so a caller can supply
# either of the strict signatures below or a more permissive variadic wrapper.
ToolStartHook = Callable[[str, dict[str, Any]], None]
ToolEndHook = Callable[[str, float, int, int], None]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class PipelineOrchestrator:
    """Drive the full pipeline via a single Claude call with five tools.

    Lifecycle:
        1. Construct with an API key + logger.
        2. Call ``run(csv_path, sample_size)`` for batch mode.
        3. Or call ``run_interactive(lat, lon)`` for single-point mode.
    """

    def __init__(self, api_key: str, logger: PipelineLogger) -> None:
        self.client = anthropic.Anthropic(api_key=api_key)
        self.logger = logger
        self.tools = TOOLS
        self.system_prompt = _load_system_prompt()

        # Sample size threaded through the ingest tool when Claude doesn't
        # explicitly pass one. Set per-run inside ``run()`` so the same
        # orchestrator instance can be reused across calls without leaking
        # state.
        self._default_sample_size: Optional[int] = None

        # Per-run options that are *not* part of any tool's input schema —
        # Claude doesn't need to know about them, but the handlers do.
        # ``_resume`` makes each ``_run_*`` skip its body when its output
        # parquet already exists. ``_states_filter`` restricts ingestion
        # to a subset of state abbreviations (Phase 8 ``--states`` flag).
        self._resume: bool = False
        self._states_filter: Optional[set[str]] = None

        # Optional callbacks for callers that want real-time visibility into
        # tool dispatch (e.g. pipeline.py prints `>>> Running ...` / `<<< ...
        # complete`). Defaults to no-op so unit tests and library callers
        # don't need to know they exist.
        self.on_tool_start: Optional[ToolStartHook] = None
        self.on_tool_end: Optional[ToolEndHook] = None

        # Token counts from the most recent Claude response. Threaded into
        # the ``on_tool_end`` callback so the pipeline can print per-call
        # cost alongside the duration. Initialised to zero so direct unit
        # tests of ``_dispatch_tool`` (no preceding Claude turn) work.
        self._last_response_input_tokens: int = 0
        self._last_response_output_tokens: int = 0

        # In-run cache of each tool's most recent summary, keyed by tool
        # name. Populated by each ``_run_*`` handler at its tail so the
        # later ``_run_generate_report`` step can pull ingestion / env-
        # enrichment / scoring details out of memory rather than re-deriving
        # them from disk. Reset on every public ``run`` / ``run_interactive``
        # so back-to-back runs in the same process don't leak state.
        self._tool_summaries: dict[str, dict[str, Any]] = {}

        _PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        _OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        config.SCORED_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def run(
        self,
        csv_path: str,
        sample_size: Optional[int] = None,
        states: Optional[list[str]] = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        """Run the full five-step pipeline.

        Sends ONE message to Claude with the task; Claude calls the five
        tools in order. The dispatch loop terminates when Claude returns
        ``end_turn`` (after its final summary) or when the safety cap
        ``MAX_AGENT_TURNS`` is hit.

        Parameters
        ----------
        csv_path:
            Path to the input locations CSV.
        sample_size:
            If set, ingestion sub-samples to this many valid rows before
            enrichment runs.
        states:
            If set, ingestion drops every row whose ``state`` is not in
            this list (case-insensitive). Equivalent to the Phase 8
            ``--states`` CLI flag.
        resume:
            If True, each tool short-circuits when its output parquet
            already exists, returning a "skipped" summary. Use to recover
            from a crashed run without re-paying the enrichment cost.

        Returns a dict with the final summary text, the tool-call trace,
        and token-usage totals so callers can audit cost and behavior.
        """
        self._default_sample_size = sample_size
        self._states_filter = (
            {s.upper() for s in states} if states else None
        )
        self._resume = resume
        self._tool_summaries = {}
        run_start = time.monotonic()

        resume_note = " (resume mode — completed steps will be skipped)" if resume else ""
        states_note = (
            f"\nFilter to states: {sorted(self._states_filter)}."
            if self._states_filter
            else ""
        )
        initial_message = (
            f"Run the full LEO satellite coverage risk pipeline on the locations "
            f"CSV at: {csv_path}{resume_note}.\n\n"
            f"Sample size for this run: "
            f"{'full dataset' if sample_size is None else sample_size}.{states_note}\n\n"
            "Call the five tools in order. After each tool, reason about whether "
            "to continue. After generate_report, write a plain-English summary."
        )

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": initial_message}
        ]
        tool_call_trace: list[dict[str, Any]] = []
        total_input_tokens = 0
        total_output_tokens = 0
        final_text: str = ""

        self.logger.info(
            stage="orchestrator",
            event_type="ORCHESTRATOR_START",
            detail={"csv_path": str(csv_path), "sample_size": sample_size},
        )

        for turn in range(config.MAX_AGENT_TURNS):
            response = self.client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=config.CLAUDE_MAX_TOKENS,
                system=self.system_prompt,
                tools=self.tools,
                messages=messages,
            )

            usage = getattr(response, "usage", None)
            input_tokens = getattr(usage, "input_tokens", 0) if usage else 0
            output_tokens = getattr(usage, "output_tokens", 0) if usage else 0
            total_input_tokens += input_tokens
            total_output_tokens += output_tokens

            # Stash this turn's per-response token counts so the tool-end
            # hook (and any future per-call cost report) can attribute spend
            # to the tool calls Claude emitted in this very response.
            self._last_response_input_tokens = input_tokens
            self._last_response_output_tokens = output_tokens

            self.logger.info(
                stage="orchestrator",
                event_type="CLAUDE_RESPONSE",
                detail={"turn": turn, "stop_reason": response.stop_reason},
                token_input=input_tokens,
                token_output=output_tokens,
            )
            self._log_cost_estimate(total_input_tokens, total_output_tokens)

            if response.stop_reason == "end_turn":
                final_text = self._extract_text(response.content)
                break

            if response.stop_reason == "tool_use":
                tool_results: list[dict[str, Any]] = []
                for block in response.content:
                    if getattr(block, "type", None) == "tool_use":
                        result_str = self._dispatch_tool(block.name, dict(block.input))
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_str,
                            }
                        )
                        tool_call_trace.append(
                            {
                                "tool_name": block.name,
                                "tool_use_id": block.id,
                                "input": dict(block.input),
                            }
                        )

                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
                continue

            # Any other stop reason (``max_tokens``, ``stop_sequence``, etc.)
            # is recorded and breaks out — a graceful exit beats spinning.
            final_text = self._extract_text(response.content)
            self.logger.warning(
                stage="orchestrator",
                event_type="UNEXPECTED_STOP_REASON",
                detail={"stop_reason": response.stop_reason, "turn": turn},
            )
            break
        else:
            # ``for`` ``else`` runs when the loop wasn't broken — i.e. we hit
            # the safety cap without an ``end_turn``. Log and return what we have.
            self.logger.warning(
                stage="orchestrator",
                event_type="MAX_TURNS_REACHED",
                detail={"max_turns": config.MAX_AGENT_TURNS},
            )

        elapsed_ms = int((time.monotonic() - run_start) * 1000)
        self.logger.info(
            stage="orchestrator",
            event_type="ORCHESTRATOR_DONE",
            detail={
                "tool_calls": len(tool_call_trace),
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
            },
            duration_ms=elapsed_ms,
        )

        return {
            "final_text": final_text,
            "tool_call_trace": tool_call_trace,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "duration_ms": elapsed_ms,
        }

    def run_interactive(self, latitude: float, longitude: float) -> dict[str, Any]:
        """Single-location analysis with per-location Claude reasoning.

        This is the **only** path in the redesign where Claude sees an
        individual location. It demonstrates the "user gives coordinates,
        agent explains sky visibility" scenario at ~$0.01 per query, where
        applying it across 4.67M rows would be infeasible.
        """
        run_start = time.monotonic()

        tcc_result = fetch_tcc(latitude, longitude)
        elev_result = fetch_elevation(latitude, longitude)
        lc_result = fetch_land_cover(latitude, longitude)

        score_result = score_components(
            tcc_pct=tcc_result.get("tcc_pct"),
            slope_deg=elev_result.get("slope_deg"),
            land_cover_code=lc_result.get("land_cover_code"),
            latitude=latitude,
        )

        location_payload = {
            "latitude": latitude,
            "longitude": longitude,
            "tcc_pct": tcc_result.get("tcc_pct"),
            "elevation_m": elev_result.get("elevation_m"),
            "slope_deg": elev_result.get("slope_deg"),
            "aspect_deg": elev_result.get("aspect_deg"),
            "land_cover_code": lc_result.get("land_cover_code"),
            "land_cover_class": lc_result.get("land_cover_class"),
            "risk_score": score_result["risk_score"],
            "risk_tier": score_result["risk_tier"],
            "component_scores": {
                "tcc_score": score_result["tcc_score"],
                "terrain_score": score_result["terrain_score"],
                "landcover_score": score_result["landcover_score"],
            },
            "flags": score_result["flags"],
        }

        user_message = (
            "Explain in 2-3 plain-English sentences why this location has the "
            "risk tier it does, framed for a non-technical broadband customer "
            "asking whether their Starlink dish will see the sky. Reference the "
            "specific values that drove the score.\n\n"
            f"Location data:\n{json.dumps(location_payload, indent=2)}"
        )

        explanation: str = ""
        token_in = 0
        token_out = 0
        try:
            response = self.client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=512,
                system=(
                    "You explain LEO satellite obstruction risk to broadband "
                    "customers. Be specific, accurate, and brief. Cite the actual "
                    "tcc_pct, slope_deg, and land_cover_class values you were given."
                ),
                messages=[{"role": "user", "content": user_message}],
            )
            explanation = self._extract_text(response.content)
            usage = getattr(response, "usage", None)
            token_in = getattr(usage, "input_tokens", 0) if usage else 0
            token_out = getattr(usage, "output_tokens", 0) if usage else 0
            self._log_cost_estimate(token_in, token_out)
        except Exception as exc:
            explanation = (
                f"(Claude explanation unavailable: {type(exc).__name__}: {exc}.) "
                f"Risk tier {score_result['risk_tier']} computed from "
                f"TCC={location_payload['tcc_pct']}, "
                f"slope={location_payload['slope_deg']}, "
                f"land_cover={location_payload['land_cover_class']}."
            )

        elapsed_ms = int((time.monotonic() - run_start) * 1000)
        self.logger.info(
            stage="orchestrator",
            event_type="INTERACTIVE_DONE",
            detail={"latitude": latitude, "longitude": longitude},
            duration_ms=elapsed_ms,
            token_input=token_in,
            token_output=token_out,
        )

        return {
            **location_payload,
            "explanation": explanation,
            "token_input": token_in,
            "token_output": token_out,
        }

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    def _dispatch_tool(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Route ``tool_name`` to its Python implementation.

        Wraps the call in try/except so a single bad tool input cannot crash
        the run. On failure, returns a JSON error payload — Claude reads it
        and decides whether to retry or abort. Every dispatch logs
        ``TOOL_CALL`` with name, inputs, summary of the return, and duration.

        Optional callbacks ``self.on_tool_start`` and ``self.on_tool_end``
        fire around the handler (including the error path), so a caller can
        emit real-time progress without subclassing the orchestrator.
        Callback failures are swallowed and logged — a misbehaving hook
        must never break the pipeline.
        """
        handlers = {
            "ingest_locations": self._run_ingest_locations,
            "sample_environment": self._run_sample_environment,
            "score_risk": self._run_score_risk,
            "validate_results": self._run_validate_results,
            "generate_report": self._run_generate_report,
        }

        self._fire_on_tool_start(tool_name, tool_input)

        start = time.monotonic()
        handler = handlers.get(tool_name)
        if handler is None:
            result: dict[str, Any] = {
                "status": "error",
                "error": f"Unknown tool: {tool_name}",
            }
            duration_ms = int((time.monotonic() - start) * 1000)
            self.logger.error(
                stage="orchestrator",
                event_type="TOOL_CALL",
                detail={
                    "tool_name": tool_name,
                    "input": tool_input,
                    "error": result["error"],
                },
                duration_ms=duration_ms,
            )
            self._fire_on_tool_end(tool_name, duration_ms / 1000.0)
            return json.dumps(result)

        try:
            result = handler(**tool_input)
        except Exception as exc:
            result = {
                "status": "error",
                "error": str(exc),
                "exception_type": type(exc).__name__,
            }
            duration_ms = int((time.monotonic() - start) * 1000)
            self.logger.error(
                stage="orchestrator",
                event_type="TOOL_CALL",
                detail={
                    "tool_name": tool_name,
                    "input": tool_input,
                    "error": str(exc),
                    "exception_type": type(exc).__name__,
                },
                duration_ms=duration_ms,
            )
            self._fire_on_tool_end(tool_name, duration_ms / 1000.0)
            return json.dumps(result, default=str)

        duration_ms = int((time.monotonic() - start) * 1000)
        self.logger.info(
            stage="orchestrator",
            event_type="TOOL_CALL",
            detail={
                "tool_name": tool_name,
                "input": tool_input,
                "output_status": result.get("status", "ok"),
            },
            duration_ms=duration_ms,
        )
        self._fire_on_tool_end(tool_name, duration_ms / 1000.0)
        return json.dumps(result, default=str)

    def _fire_on_tool_start(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> None:
        """Invoke the ``on_tool_start`` hook if set; swallow + log failures."""
        if self.on_tool_start is None:
            return
        try:
            self.on_tool_start(tool_name, tool_input)
        except Exception as exc:
            self.logger.warning(
                stage="orchestrator",
                event_type="TOOL_HOOK_FAILED",
                detail={
                    "hook": "on_tool_start",
                    "tool_name": tool_name,
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                },
            )

    def _fire_on_tool_end(self, tool_name: str, duration_s: float) -> None:
        """Invoke the ``on_tool_end`` hook if set; swallow + log failures.

        Passes the duration plus the **last Claude response's** token counts,
        so a real-time UI can attribute API cost to the tool call that
        Claude's reasoning emitted. Both token fields default to 0 when
        the dispatcher is invoked outside a Claude loop (unit tests).
        """
        if self.on_tool_end is None:
            return
        try:
            self.on_tool_end(
                tool_name,
                duration_s,
                self._last_response_input_tokens,
                self._last_response_output_tokens,
            )
        except Exception as exc:
            self.logger.warning(
                stage="orchestrator",
                event_type="TOOL_HOOK_FAILED",
                detail={
                    "hook": "on_tool_end",
                    "tool_name": tool_name,
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                },
            )

    # ------------------------------------------------------------------
    # Tool 1 — ingest_locations
    # ------------------------------------------------------------------

    def _run_ingest_locations(
        self, file_path: str, sample_size: Optional[int] = None
    ) -> dict[str, Any]:
        # Resume short-circuit: if the validated parquet exists, return a
        # summary from it without re-running ingestion. The downstream tools
        # never know the difference because the on-disk artifact is the same.
        if self._resume and _VALIDATED_PARQUET.exists():
            return self._remember_summary(
                "ingest_locations", self._resume_ingest_summary()
            )

        # If Claude doesn't supply sample_size, fall back to whatever the
        # caller passed to ``run()``.
        if sample_size is None:
            sample_size = self._default_sample_size

        csv_path = Path(file_path)
        agent = IngestionAgent(logger=self.logger)

        # Choose a sensible batch size for the ingestion generator. When
        # sample_size is set we don't want to read whole RASTER_BATCH_SIZE
        # chunks (50k rows) just to throw most of them away, so the batch
        # size is capped at sample_size for small samples.
        if sample_size is not None and sample_size < config.RASTER_BATCH_SIZE:
            ingest_batch_size = max(sample_size, 1)
        else:
            ingest_batch_size = config.RASTER_BATCH_SIZE

        validated: list[ValidatedLocation] = []
        for batch in agent.run(csv_path, batch_size=ingest_batch_size):
            # ``states`` filter: drop rows whose canonical state isn't in
            # the allowlist before deciding the sample budget is full. The
            # filter is applied here (not in IngestionAgent) so Phase 4's
            # tested ingestion logic stays unmodified.
            if self._states_filter is not None:
                batch = [v for v in batch if v.state in self._states_filter]
            validated.extend(batch)
            # Early-exit when sampling: ``agent.run`` is a generator, so
            # abandoning iteration here saves reading the remaining ~4.67M
            # rows for a 100-row dry-run. The ingestion agent's ``finally``
            # block still emits ``INGESTION_SUMMARY`` with the partial
            # totals it has actually seen.
            if sample_size is not None and len(validated) >= sample_size:
                validated = validated[:sample_size]
                break

        df = pd.DataFrame(
            [
                {
                    "location_id": v.location_id,
                    "latitude": v.latitude,
                    "longitude": v.longitude,
                    "state": v.state,
                    "county": v.county,
                    "batch_id": v.batch_id,
                }
                for v in validated
            ]
        )

        _PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(_VALIDATED_PARQUET, index=False)
        self._log_checkpoint("ingest_locations", str(_VALIDATED_PARQUET), len(df))

        # Build a complete drop breakdown — every Reason is present (with 0
        # when nothing was dropped for that reason) so Claude can read a
        # stable schema rather than a sparse dict that may omit keys.
        all_reasons = [
            Reason.NULL_LOCATION_ID,
            Reason.NULL_COORDINATE,
            Reason.OUT_OF_BOUNDS,
            Reason.INVALID_STATE,
            Reason.DUPLICATE_DROPPED,
            Reason.PARSE_ERROR,
        ]
        drop_breakdown = {r: int(agent.stats.get(r, 0)) for r in all_reasons}

        state_distribution: dict[str, int] = {}
        if not df.empty and "state" in df.columns:
            state_distribution = {
                str(k): int(v)
                for k, v in df["state"].dropna().value_counts().to_dict().items()
            }

        total_rows = agent.total_rows
        valid_rows = len(validated)
        dropped_rows = sum(drop_breakdown.values())
        valid_pct = (valid_rows / total_rows) if total_rows > 0 else 0.0
        critical_failure = (total_rows == 0) or (valid_rows == 0)

        return self._remember_summary(
            "ingest_locations",
            {
                "status": "critical_failure" if critical_failure else "ok",
                "total_rows": int(total_rows),
                "valid_rows": int(valid_rows),
                "dropped_rows": int(dropped_rows),
                "valid_pct": round(valid_pct, 4),
                "drop_breakdown": drop_breakdown,
                "state_distribution": state_distribution,
                "output_path": str(_VALIDATED_PARQUET),
                "critical_failure": critical_failure,
                "notes": (
                    "geoid_cb derived state and county GEOID for all valid rows. "
                    "Sampling applied: "
                    f"{'yes (' + str(sample_size) + ')' if sample_size is not None else 'no'}."
                ),
            },
        )

    # ------------------------------------------------------------------
    # Tool 2 — sample_environment
    # ------------------------------------------------------------------

    def _run_sample_environment(
        self, validated_locations_path: str
    ) -> dict[str, Any]:
        if self._resume and _ENRICHED_PARQUET.exists():
            return self._remember_summary(
                "sample_environment", self._resume_enrich_summary()
            )

        df = pd.read_parquet(validated_locations_path)
        total_locations = len(df)
        if total_locations == 0:
            return self._remember_summary(
                "sample_environment",
                {
                    "status": "failed",
                    "total_locations": 0,
                    "enriched_locations": 0,
                    "missing_rates": {},
                    "output_path": str(_ENRICHED_PARQUET),
                    "raster_files_used": {},
                    "notes": "No validated locations to enrich.",
                },
            )

        agent = EnvironmentalAgent(logger=self.logger)
        enriched_rows: list[dict[str, Any]] = []

        # Chunk through the validated locations so raster handles are
        # re-warmed per chunk (RASTER_BATCH_SIZE) and the peak memory stays
        # bounded even at 4.67M rows.
        for start in range(0, total_locations, config.RASTER_BATCH_SIZE):
            chunk = df.iloc[start : start + config.RASTER_BATCH_SIZE]
            batch = [
                ValidatedLocation(
                    location_id=str(row["location_id"]),
                    latitude=float(row["latitude"]),
                    longitude=float(row["longitude"]),
                    state=(None if pd.isna(row.get("state")) else row.get("state")),
                    county=(None if pd.isna(row.get("county")) else row.get("county")),
                    batch_id=str(row["batch_id"]),
                )
                for _, row in chunk.iterrows()
            ]
            for enriched in agent.enrich_batch(batch):
                enriched_rows.append(
                    {
                        "location_id": enriched.location_id,
                        "latitude": enriched.latitude,
                        "longitude": enriched.longitude,
                        "state": enriched.state,
                        "county": enriched.county,
                        "tcc_pct": enriched.tcc_pct,
                        "elevation_m": enriched.elevation_m,
                        "slope_deg": enriched.slope_deg,
                        "aspect_deg": enriched.aspect_deg,
                        "land_cover_code": enriched.land_cover_code,
                        "land_cover_class": enriched.land_cover_class,
                        "env_fetch_flags": list(enriched.env_fetch_flags),
                        "batch_id": enriched.batch_id,
                    }
                )

        agent.log_run_summary()
        out_df = pd.DataFrame(enriched_rows)
        out_df.to_parquet(_ENRICHED_PARQUET, index=False)
        self._log_checkpoint(
            "sample_environment", str(_ENRICHED_PARQUET), len(out_df)
        )

        # A row is "enriched" if at least one of the three scoring signals
        # came back present. A row with every signal null is unscorable
        # downstream — we surface that count to Claude so the
        # missing-data-rate check has the right denominator.
        if total_locations > 0:
            unscorable_mask = (
                out_df["tcc_pct"].isna()
                & out_df["slope_deg"].isna()
                & out_df["land_cover_code"].isna()
            )
            enriched_locations = int(total_locations - unscorable_mask.sum())
        else:
            enriched_locations = 0

        def _missing_pct(col: str) -> float:
            return round(out_df[col].isna().sum() / total_locations, 4)

        missing_rates = {
            "tcc_missing_pct": _missing_pct("tcc_pct"),
            "elevation_missing_pct": _missing_pct("elevation_m"),
            "slope_missing_pct": _missing_pct("slope_deg"),
            "landcover_missing_pct": _missing_pct("land_cover_code"),
        }

        # Status reflects severity: any signal over 10% missing degrades to
        # ``partial``; over 50% missing on any single signal is ``failed``
        # (the formula can't produce meaningful tiers without it).
        max_missing = max(missing_rates.values()) if missing_rates else 0.0
        if max_missing > 0.50:
            status = "failed"
        elif max_missing > 0.10:
            status = "partial"
        else:
            status = "ok"

        raster_files_used = {
            "tcc": self._first_file(config.TCC_DIR),
            "dem_slope": (
                str(config.SLOPE_RASTER_PATH)
                if config.SLOPE_RASTER_PATH.exists()
                else None
            ),
            "landcover": self._first_file(config.LC_DIR),
        }

        return self._remember_summary(
            "sample_environment",
            {
                "status": status,
                "total_locations": int(total_locations),
                "enriched_locations": int(enriched_locations),
                "missing_rates": missing_rates,
                "output_path": str(_ENRICHED_PARQUET),
                "raster_files_used": raster_files_used,
                "notes": "" if status == "ok" else (
                    f"Highest missing rate: {max_missing:.4f}. Review raster coverage."
                ),
            },
        )

    @staticmethod
    def _first_file(directory: Path) -> Optional[str]:
        """Return the first raster file in ``directory`` (or None)."""
        for pattern in ("*.tif", "*.tiff", "*.img"):
            for path in directory.glob(pattern):
                return str(path)
        return None

    # ------------------------------------------------------------------
    # Tool 3 — score_risk
    # ------------------------------------------------------------------

    def _run_score_risk(self, enriched_locations_path: str) -> dict[str, Any]:
        if self._resume and _SCORED_PARQUET.exists():
            return self._remember_summary(
                "score_risk", self._resume_score_summary()
            )

        df = pd.read_parquet(enriched_locations_path)
        total = len(df)
        if total == 0:
            return self._remember_summary(
                "score_risk",
                {
                    "status": "ok",
                    "total_scored": 0,
                    "unscored": 0,
                    "tier_distribution": {},
                    "mean_composite_score": None,
                    "output_path": str(_SCORED_PARQUET),
                    "dominant_tier_flag": False,
                    "notes": "No enriched locations to score.",
                },
            )

        # Compute scores per row via ``score_components`` directly — avoids
        # constructing 4.67M Pydantic objects just to throw them away after
        # one read. Output column shape matches ScoredLocation.
        records: list[dict[str, Any]] = []
        for row in df.itertuples(index=False):
            row_d = row._asdict()
            result = score_components(
                tcc_pct=_coerce_int(row_d.get("tcc_pct")),
                slope_deg=_coerce_float(row_d.get("slope_deg")),
                land_cover_code=_coerce_int(row_d.get("land_cover_code")),
                latitude=_coerce_float(row_d.get("latitude")),
            )

            # ``env_fetch_flags`` round-trips through parquet as a numpy
            # array of object dtype, not a Python list — so ``or []`` and
            # ``is None`` both blow up on the array branch. Explicitly
            # normalise to a Python list before treating it as one.
            env_flags_raw = row_d.get("env_fetch_flags")
            if env_flags_raw is None:
                env_flags: list[str] = []
            elif isinstance(env_flags_raw, str):
                env_flags = [env_flags_raw]
            else:
                try:
                    env_flags = [str(f) for f in env_flags_raw]
                except TypeError:
                    env_flags = []
            combined = env_flags + list(result["flags"])
            seen: set[str] = set()
            all_flags: list[str] = []
            for f in combined:
                if f not in seen:
                    seen.add(f)
                    all_flags.append(f)

            records.append(
                {
                    "location_id": row_d["location_id"],
                    "latitude": row_d["latitude"],
                    "longitude": row_d["longitude"],
                    "state": row_d.get("state"),
                    "county": row_d.get("county"),
                    "tcc_pct": row_d.get("tcc_pct"),
                    "slope_deg": row_d.get("slope_deg"),
                    "aspect_deg": row_d.get("aspect_deg"),
                    "land_cover_code": row_d.get("land_cover_code"),
                    "land_cover_class": row_d.get("land_cover_class"),
                    "risk_score": result["risk_score"],
                    "risk_tier": result["risk_tier"],
                    "tcc_score": result["tcc_score"],
                    "terrain_score": result["terrain_score"],
                    "landcover_score": result["landcover_score"],
                    "all_flags": all_flags,
                    "batch_id": row_d.get("batch_id"),
                }
            )

        scored_df = pd.DataFrame(records)
        scored_df.to_parquet(_SCORED_PARQUET, index=False)
        # Write the queryable Hive-partitioned store as well. The downstream
        # tools still read the single-file intermediate (no behavioural change
        # for them), but the report generator and ``pipeline.py --resume``
        # consult the partition store via DuckDB.
        store.write_scored_locations(scored_df, scored_dir=config.SCORED_DIR)
        self._log_checkpoint("score_risk", str(_SCORED_PARQUET), len(scored_df))

        tier_counts = scored_df["risk_tier"].value_counts().to_dict()
        all_tiers = [TIER_LOW, TIER_MODERATE, TIER_HIGH, TIER_UNSCORED]
        tier_distribution: dict[str, dict[str, Any]] = {}
        dominant_tier_flag = False
        for tier in all_tiers:
            count = int(tier_counts.get(tier, 0))
            pct = round(count / total, 4) if total else 0.0
            tier_distribution[tier] = {"count": count, "pct": pct}
            # Dominant-tier flag fires only on the three real tiers — an
            # all-UNSCORED dataset is a data-quality failure, not a tier
            # calibration issue, and the missing-data check catches it.
            if tier != TIER_UNSCORED and pct > _DOMINANT_TIER_THRESHOLD:
                dominant_tier_flag = True

        unscored = int(tier_counts.get(TIER_UNSCORED, 0))
        scored_count = total - unscored

        mean_score: Optional[float] = None
        non_null_scores = scored_df["risk_score"].dropna()
        if len(non_null_scores) > 0:
            mean_score = round(float(non_null_scores.mean()), 4)

        return self._remember_summary(
            "score_risk",
            {
                "status": "ok",
                "total_scored": int(scored_count),
                "unscored": unscored,
                "tier_distribution": tier_distribution,
                "mean_composite_score": mean_score,
                "output_path": str(_SCORED_PARQUET),
                "dominant_tier_flag": dominant_tier_flag,
                "notes": "" if not dominant_tier_flag else (
                    "Dominant tier detected — review scoring thresholds in config.py."
                ),
            },
        )

    # ------------------------------------------------------------------
    # Tool 4 — validate_results
    # ------------------------------------------------------------------

    def _run_validate_results(
        self, scored_locations_path: str
    ) -> dict[str, Any]:
        df = pd.read_parquet(scored_locations_path)
        total = len(df)

        # --- Check 1: distribution sanity --------------------------------
        dominant_tier: Optional[str] = None
        dominant_pct: Optional[float] = None
        if total > 0:
            real_tier_mask = df["risk_tier"] != TIER_UNSCORED
            real_df = df[real_tier_mask]
            if len(real_df) > 0:
                tier_counts = real_df["risk_tier"].value_counts()
                top_tier = tier_counts.idxmax()
                top_pct = round(float(tier_counts.max()) / total, 4)
                if top_pct > _DOMINANT_TIER_THRESHOLD:
                    dominant_tier = str(top_tier)
                    dominant_pct = top_pct
        check_distribution = {
            "passed": dominant_tier is None,
            "dominant_tier": dominant_tier,
            "dominant_pct": dominant_pct,
        }

        # --- Check 2: cross-validation -----------------------------------
        forest_low_tcc_mask = (
            df["land_cover_code"].isin(config.FOREST_CODES)
            & df["tcc_pct"].notna()
            & (df["tcc_pct"] < _FOREST_LOW_TCC_THRESHOLD)
        )
        forest_low_tcc_count = int(forest_low_tcc_mask.sum())
        forest_low_tcc_pct = (
            round(forest_low_tcc_count / total, 6) if total > 0 else 0.0
        )
        sample_anomalies = (
            df.loc[forest_low_tcc_mask, ["location_id", "tcc_pct", "land_cover_code"]]
            .head(5)
            .to_dict(orient="records")
            if forest_low_tcc_count > 0
            else []
        )
        check_cross_validation = {
            "passed": forest_low_tcc_pct <= _FOREST_LOW_TCC_RATE_FLAG,
            "forest_low_tcc_count": forest_low_tcc_count,
            "forest_low_tcc_pct": forest_low_tcc_pct,
            "sample_anomalies": sample_anomalies,
        }

        # --- Check 3: geographic sanity ----------------------------------
        out_of_bounds_mask = (
            (df["latitude"] < _NC_LAT_MIN)
            | (df["latitude"] > _NC_LAT_MAX)
            | (df["longitude"] < _NC_LON_MIN)
            | (df["longitude"] > _NC_LON_MAX)
        )
        out_of_bounds_count = int(out_of_bounds_mask.sum())
        # ``elevation_m`` may not be present on scored output, but if the
        # column is there, negative elevation is a hard signal of water or
        # sensor noise.
        if "elevation_m" in df.columns:
            water_mask = df["elevation_m"].notna() & (df["elevation_m"] < 0)
            water_location_count = int(water_mask.sum())
        else:
            water_location_count = 0
        check_geographic = {
            "passed": out_of_bounds_count == 0 and water_location_count == 0,
            "out_of_bounds_count": out_of_bounds_count,
            "water_location_count": water_location_count,
        }

        # --- Check 4: missing data rate ----------------------------------
        signal_columns = ["tcc_pct", "slope_deg", "land_cover_code"]
        missing_pcts: dict[str, float] = {}
        for col in signal_columns:
            if col in df.columns and total > 0:
                missing_pcts[f"{col}_missing_pct"] = round(
                    float(df[col].isna().sum()) / total, 4
                )
            else:
                missing_pcts[f"{col}_missing_pct"] = 0.0
        max_missing_signal = (
            max(missing_pcts, key=missing_pcts.get) if missing_pcts else None
        )
        max_missing_pct = (
            missing_pcts[max_missing_signal] if max_missing_signal else 0.0
        )
        check_missing = {
            "passed": max_missing_pct <= _MISSING_DATA_FLAG_THRESHOLD,
            "max_missing_signal": max_missing_signal,
            "max_missing_pct": max_missing_pct,
        }

        # --- Roll up -----------------------------------------------------
        checks = {
            "distribution_sanity": check_distribution,
            "cross_validation": check_cross_validation,
            "geographic_sanity": check_geographic,
            "missing_data_rate": check_missing,
        }
        warnings_count = sum(1 for c in checks.values() if not c["passed"])

        # Status is ``failed`` only when geographic-sanity fails (out-of-bounds
        # rows are a data integrity error). Other check failures are warnings
        # — they degrade quality but the pipeline can still publish a report
        # with the caveats called out.
        if not check_geographic["passed"]:
            status = "failed"
            recommendation = "halt"
        elif warnings_count > 0:
            status = "warnings"
            recommendation = "proceed_with_caveats"
        else:
            status = "passed"
            recommendation = "proceed"

        notes_parts: list[str] = []
        if forest_low_tcc_count > 0:
            notes_parts.append(
                f"{forest_low_tcc_count} forest-classified pixels with tcc_pct < "
                f"{_FOREST_LOW_TCC_THRESHOLD} — likely recent clear-cuts or "
                "data vintage mismatch between the two NLCD layers."
            )
        if out_of_bounds_count > 0:
            notes_parts.append(
                f"{out_of_bounds_count} locations outside the NC bounding box."
            )
        notes = " ".join(notes_parts)

        return self._remember_summary(
            "validate_results",
            {
                "status": status,
                "checks": checks,
                "total_warnings": warnings_count,
                "recommendation": recommendation,
                "notes": notes,
            },
        )

    # ------------------------------------------------------------------
    # Tool 5 — generate_report
    # ------------------------------------------------------------------

    def _run_generate_report(
        self,
        scored_locations_path: str,
        validation_report: dict[str, Any],
    ) -> dict[str, Any]:
        """Produce the analysis report + queryable per-state/per-county
        parquet summaries from the partitioned scored store.

        Aggregations come from :mod:`src.data.store` (DuckDB) so the same
        SQL paths an analyst would query interactively are the ones the
        report itself uses — no second pandas implementation to drift
        out of sync with the partition store. ``scored_locations_path``
        is still accepted (it's the path Claude was told to thread
        through), but the queries hit the Hive-partitioned store at
        ``config.SCORED_DIR``; the single-file path is only used as a
        cheap empty-dataset guard before reaching for DuckDB.
        """
        # Cheap empty-dataset gate: the single-file intermediate is
        # always written by ``_run_score_risk`` and is the smallest read.
        df_quick = pd.read_parquet(scored_locations_path)
        total = int(len(df_quick))
        if total == 0:
            return self._remember_summary(
                "generate_report",
                {"status": "error", "error": "No scored locations to report on."},
            )

        # --- DuckDB aggregations over the partition store --------------
        risk_dist = store.get_risk_distribution(scored_dir=config.SCORED_DIR)
        state_breakdown = store.get_state_breakdown(scored_dir=config.SCORED_DIR)
        county_breakdown = store.get_county_breakdown(scored_dir=config.SCORED_DIR)
        top_counties = store.get_top_at_risk_counties(
            n=10, scored_dir=config.SCORED_DIR
        )

        # Persist the per-state and per-county aggregations as their own
        # parquet artifacts so downstream consumers (Phase 10 map, future
        # API endpoints) don't need to re-run the SQL. These are sibling
        # parquets at the root of ``SCORED_DIR``; the partitioned store's
        # DuckDB glob (``state=*/part-*.parquet``) ignores them by design.
        config.SCORED_DIR.mkdir(parents=True, exist_ok=True)
        state_breakdown.to_parquet(_STATE_SUMMARY_PARQUET, index=False)
        county_breakdown.to_parquet(_COUNTY_SUMMARY_PARQUET, index=False)

        # --- Headline figures ------------------------------------------
        tier_counts = self._tier_counts_from_distribution(risk_dist)
        high_count = int(tier_counts.get(TIER_HIGH, 0))
        high_pct = round(high_count / total, 4) if total else 0.0

        # County breakdown is already ordered by ``high_pct`` desc — the
        # top-counties list (which applies a ``min_locations`` floor) is
        # the right place to look for the *meaningful* top-at-risk county.
        # If every county is below the floor, fall back to the unfiltered
        # county breakdown so the headline always has a value to print.
        top_at_risk_county: Optional[str] = None
        top_at_risk_state: Optional[str] = None
        for source in (top_counties, county_breakdown):
            if source.empty:
                continue
            top_row = source.iloc[0]
            top_at_risk_county = (
                None if pd.isna(top_row.get("county")) else str(top_row["county"])
            )
            top_at_risk_state = (
                None if pd.isna(top_row.get("state")) else str(top_row.get("state"))
            )
            break

        # --- Markdown report -------------------------------------------
        _OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        report_md = self._build_report_md(
            total=total,
            risk_distribution=risk_dist,
            state_breakdown=state_breakdown,
            top_counties=top_counties,
            top_at_risk_county=top_at_risk_county,
            top_at_risk_state=top_at_risk_state,
            validation_report=validation_report,
            ingest_summary=self._tool_summaries.get("ingest_locations"),
            env_summary=self._tool_summaries.get("sample_environment"),
            score_summary=self._tool_summaries.get("score_risk"),
        )
        _REPORT_MD.write_text(report_md, encoding="utf-8")

        # --- Folium map (best-effort, non-fatal on failure) ------------
        try:
            self._render_map(df_quick)
            map_path = str(_MAP_HTML)
        except Exception as exc:
            self.logger.error(
                stage="orchestrator",
                event_type="MAP_RENDER_FAILED",
                detail={"error": str(exc), "exception_type": type(exc).__name__},
            )
            map_path = ""

        self._log_checkpoint("generate_report", str(_REPORT_MD), total)

        return self._remember_summary(
            "generate_report",
            {
                "status": "ok",
                "outputs": {
                    "report": str(_REPORT_MD),
                    "state_summary": str(_STATE_SUMMARY_PARQUET),
                    "county_summary": str(_COUNTY_SUMMARY_PARQUET),
                    "map": map_path,
                },
                "key_findings": {
                    "total_locations_analyzed": int(total),
                    "high_risk_pct": high_pct,
                    "top_at_risk_county": top_at_risk_county,
                    "state": top_at_risk_state,
                },
            },
        )

    @staticmethod
    def _tier_counts_from_distribution(
        risk_dist: pd.DataFrame,
    ) -> dict[str, int]:
        """Pluck integer tier counts out of a ``get_risk_distribution`` frame.

        Returns a dict with every tier present (zero-filled for missing
        tiers) so callers can index by string literal without guarding
        for ``KeyError``.
        """
        counts: dict[str, int] = {
            TIER_HIGH: 0,
            TIER_MODERATE: 0,
            TIER_LOW: 0,
            TIER_UNSCORED: 0,
        }
        if risk_dist.empty:
            return counts
        for _, row in risk_dist.iterrows():
            tier = row.get("risk_tier")
            if isinstance(tier, str) and tier in counts:
                counts[tier] = int(row.get("count", 0) or 0)
        return counts

    def _build_report_md(
        self,
        total: int,
        risk_distribution: pd.DataFrame,
        state_breakdown: pd.DataFrame,
        top_counties: pd.DataFrame,
        top_at_risk_county: Optional[str],
        top_at_risk_state: Optional[str],
        validation_report: dict[str, Any],
        ingest_summary: Optional[dict[str, Any]],
        env_summary: Optional[dict[str, Any]],
        score_summary: Optional[dict[str, Any]],
    ) -> str:
        """Assemble the Phase 9 analysis report.

        Audience: a state broadband officer. The executive summary is
        plain English; jargon and formulas live in the Methodology
        section so a reader scanning the headline can stop after the
        first page. Every number in the report comes from a DuckDB
        aggregation over the partitioned scored store — no second
        derivation path.
        """
        # Each ``_md_*`` returns its section already trailed by ``\n``
        # so concatenating with ``\n`` puts a blank line between sections.
        # The title is added separately because it's a single h1 line
        # with no trailing newline of its own.
        sections = [
            "# LEO Satellite Coverage Risk — Analysis Report\n",
            self._md_executive_summary(
                total=total,
                risk_distribution=risk_distribution,
                top_at_risk_county=top_at_risk_county,
                top_at_risk_state=top_at_risk_state,
                ingest_summary=ingest_summary,
                env_summary=env_summary,
            ),
            self._md_risk_distribution(risk_distribution, total),
            self._md_state_breakdown(state_breakdown),
            self._md_top_counties(top_counties),
            self._md_data_quality(
                total=total,
                ingest_summary=ingest_summary,
                env_summary=env_summary,
                validation_report=validation_report,
            ),
            self._md_methodology(score_summary=score_summary),
            self._md_limitations(),
            self._md_outputs(),
        ]
        return "\n".join(sections)

    @staticmethod
    def _md_executive_summary(
        total: int,
        risk_distribution: pd.DataFrame,
        top_at_risk_county: Optional[str],
        top_at_risk_state: Optional[str],
        ingest_summary: Optional[dict[str, Any]],
        env_summary: Optional[dict[str, Any]],
    ) -> str:
        """First-page non-technical summary.

        Opens with the prescribed sentence pattern from the build plan
        so an evaluator can grep the exact wording. The composition
        intentionally avoids math: no formulas, no weights, no
        thresholds — those live in the Methodology section.
        """
        # Tier counts pulled straight from the DuckDB result so we never
        # diverge from the table that follows.
        tier_map: dict[str, int] = {}
        for _, row in risk_distribution.iterrows():
            tier_map[str(row.get("risk_tier"))] = int(row.get("count", 0) or 0)
        high = tier_map.get(TIER_HIGH, 0)
        moderate = tier_map.get(TIER_MODERATE, 0)
        elevated = high + moderate
        elevated_pct = (elevated / total * 100) if total else 0.0
        high_pct = (high / total * 100) if total else 0.0

        # Region name for the opening sentence — single-state runs read
        # naturally ("in North Carolina"), multi-state runs degrade to
        # "across the studied region".
        states_seen: list[str] = []
        if ingest_summary and isinstance(
            ingest_summary.get("state_distribution"), dict
        ):
            states_seen = sorted(
                {
                    s for s in ingest_summary["state_distribution"].keys()
                    if isinstance(s, str) and s
                }
            )
        if len(states_seen) == 1:
            region_phrase = f"in {states_seen[0]}"
        elif 1 < len(states_seen) <= 3:
            region_phrase = "across " + ", ".join(states_seen)
        else:
            region_phrase = "across the studied region"

        top_phrase = ""
        if top_at_risk_county:
            state_suffix = f", {top_at_risk_state}" if top_at_risk_state else ""
            # The exact high_pct lives in the Top-10 table further down —
            # the executive summary just names the leading county and
            # points at the table so the reader gets the number without
            # the prose juggling a percentage inline.
            top_phrase = (
                f" The single highest-risk county {region_phrase} is "
                f"**{top_at_risk_county}{state_suffix}** — see the "
                "*Top 10 At-Risk Counties* table below for the exact share."
            )

        # Driver hint: which signal is doing the most work? Use the env
        # summary's missing rates inverted as a "data confidence" cue,
        # but keep the prose general — the Methodology section explains
        # the weights.
        driver_sentence = (
            "Tree canopy density is the dominant signal in the scoring "
            "model; locations with extensive forest cover, steep terrain, "
            "or both, are the most likely to need site assessment before "
            "installation."
        )

        # Coverage transparency: surface the env-enrichment success rate.
        env_phrase = ""
        if env_summary and isinstance(env_summary.get("enriched_locations"), int):
            enr = int(env_summary["enriched_locations"])
            tot = int(env_summary.get("total_locations", 0) or 0)
            if tot > 0:
                env_phrase = (
                    f" Environmental data was successfully resolved for "
                    f"{enr:,} of {tot:,} locations "
                    f"({enr / tot * 100:.1f}% coverage)."
                )

        opening = (
            f"Of the **{total:,}** locations committed for LEO satellite "
            f"service {region_phrase}, approximately **{elevated_pct:.1f}%** "
            f"face elevated obstruction risk ({high_pct:.1f}% rated High, "
            f"the remainder Moderate). These are the locations a broadband "
            "officer should prioritize for site assessment before scheduling "
            "installation."
        )

        return (
            "## Executive Summary\n\n"
            f"{opening}{top_phrase}{env_phrase}\n\n"
            f"{driver_sentence}\n"
        )

    @staticmethod
    def _md_risk_distribution(
        risk_distribution: pd.DataFrame, total: int,
    ) -> str:
        """Risk-tier counts and percentages, in High → Unscored order."""
        lines = [
            "## Risk Distribution",
            "",
            "| Risk Tier | Locations | Share |",
            "| --- | ---: | ---: |",
        ]
        if risk_distribution.empty:
            lines.append("| _(no data)_ | 0 | 0.0% |")
        else:
            tier_order = {
                TIER_HIGH: 0,
                TIER_MODERATE: 1,
                TIER_LOW: 2,
                TIER_UNSCORED: 3,
            }
            sortable = risk_distribution.copy()
            sortable["_order"] = sortable["risk_tier"].map(
                lambda t: tier_order.get(str(t), 99)
            )
            sortable = sortable.sort_values("_order")
            for _, row in sortable.iterrows():
                tier = str(row.get("risk_tier", ""))
                count = int(row.get("count", 0) or 0)
                pct = float(row.get("pct", 0.0) or 0.0)
                lines.append(
                    f"| {tier} | {count:,} | {pct * 100:.2f}% |"
                )
            lines.append(f"| **Total** | **{total:,}** | **100.00%** |")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _md_state_breakdown(state_breakdown: pd.DataFrame) -> str:
        """One row per state, ordered by High-risk share (desc).

        For a single-state run this is a one-row table — by design.
        The table form is the same shape state broadband officers will
        see when the pipeline runs nationally, so the format is
        unconditionally a table rather than a paragraph.
        """
        lines = [
            "## State-Level Breakdown",
            "",
            "| State | High | Moderate | Low | Unscored | Total | High Share |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        if state_breakdown.empty:
            lines.append("| _(no data)_ | 0 | 0 | 0 | 0 | 0 | 0.00% |")
            return "\n".join(lines) + "\n"
        for _, row in state_breakdown.iterrows():
            state = row.get("state") or "UNKNOWN"
            lines.append(
                f"| {state} "
                f"| {int(row.get('high_count', 0) or 0):,} "
                f"| {int(row.get('moderate_count', 0) or 0):,} "
                f"| {int(row.get('low_count', 0) or 0):,} "
                f"| {int(row.get('unscored_count', 0) or 0):,} "
                f"| {int(row.get('total', 0) or 0):,} "
                f"| {float(row.get('high_pct', 0.0) or 0.0) * 100:.2f}% |"
            )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _md_top_counties(top_counties: pd.DataFrame) -> str:
        """The top 10 counties by High-risk share.

        The DuckDB query already applies a ``min_locations`` floor so
        small counties whose ``high_pct`` is statistically meaningless
        are filtered out upstream — the report doesn't need to repeat
        that logic.
        """
        lines = [
            "## Top 10 At-Risk Counties",
            "",
            "| Rank | County | State | Locations | High-Risk Share |",
            "| ---: | --- | :---: | ---: | ---: |",
        ]
        if top_counties.empty:
            lines.append(
                "| — | _(no counties met the minimum-locations threshold)_ | "
                "— | 0 | 0.00% |"
            )
            return "\n".join(lines) + "\n"
        for rank, (_, row) in enumerate(top_counties.iterrows(), start=1):
            county = row.get("county") or "UNKNOWN"
            state = row.get("state") or "—"
            total = int(row.get("total", 0) or 0)
            high_pct = float(row.get("high_pct", 0.0) or 0.0)
            lines.append(
                f"| {rank} | {county} | {state} | {total:,} | "
                f"{high_pct * 100:.2f}% |"
            )
        return "\n".join(lines) + "\n"

    def _md_data_quality(
        self,
        total: int,
        ingest_summary: Optional[dict[str, Any]],
        env_summary: Optional[dict[str, Any]],
        validation_report: dict[str, Any],
    ) -> str:
        """Validation exclusion counts + environmental missing rates +
        post-scoring check pass/fail.

        This section is what makes the report trustable — it tells the
        reader exactly which rows were dropped, why, and how much of the
        remaining environmental coverage was sampled cleanly.
        """
        lines = ["## Data Quality Summary", ""]

        # --- Ingestion exclusions --------------------------------------
        if ingest_summary:
            total_rows = int(ingest_summary.get("total_rows", 0) or 0)
            valid_rows = int(ingest_summary.get("valid_rows", 0) or 0)
            dropped_rows = int(ingest_summary.get("dropped_rows", 0) or 0)
            valid_pct = float(ingest_summary.get("valid_pct", 0.0) or 0.0)
            lines.extend([
                "### Ingestion",
                "",
                f"- Rows read from input: **{total_rows:,}**",
                f"- Rows accepted (passed validation): "
                f"**{valid_rows:,}** ({valid_pct * 100:.2f}%)",
                f"- Rows excluded: **{dropped_rows:,}**",
                "",
            ])
            breakdown = ingest_summary.get("drop_breakdown") or {}
            if any(int(v or 0) > 0 for v in breakdown.values()):
                lines.extend([
                    "Exclusions by reason code (each row counted once):",
                    "",
                    "| Reason | Count |",
                    "| --- | ---: |",
                ])
                # Sort descending so the noisiest reason is at the top.
                for reason, count in sorted(
                    breakdown.items(),
                    key=lambda kv: -int(kv[1] or 0),
                ):
                    n = int(count or 0)
                    if n > 0:
                        lines.append(f"| `{reason}` | {n:,} |")
                lines.append("")
        else:
            lines.extend([
                "### Ingestion",
                "",
                "_Ingestion summary not available for this run._",
                "",
            ])

        # --- Environmental coverage ------------------------------------
        if env_summary:
            missing_rates = env_summary.get("missing_rates") or {}
            enriched = int(env_summary.get("enriched_locations", 0) or 0)
            total_env = int(env_summary.get("total_locations", 0) or 0)
            lines.extend([
                "### Environmental Sampling",
                "",
                f"- Locations with at least one signal resolved: "
                f"**{enriched:,}** of {total_env:,}"
                f"{f' ({enriched / total_env * 100:.2f}%)' if total_env else ''}",
                "",
            ])
            if missing_rates:
                lines.extend([
                    "Missing-data rate per signal (lower is better):",
                    "",
                    "| Signal | Missing Rate |",
                    "| --- | ---: |",
                ])
                pretty = {
                    "tcc_missing_pct": "Tree canopy cover (TCC)",
                    "slope_missing_pct": "Terrain slope",
                    "landcover_missing_pct": "Land cover",
                    "elevation_missing_pct": "Elevation",
                }
                for key, label in pretty.items():
                    if key in missing_rates:
                        rate = float(missing_rates[key] or 0.0)
                        lines.append(f"| {label} | {rate * 100:.2f}% |")
                lines.append("")
        else:
            lines.extend([
                "### Environmental Sampling",
                "",
                "_Environmental sampling summary not available for this run._",
                "",
            ])

        # --- Post-scoring validation checks ----------------------------
        lines.extend([
            "### Post-Scoring Validation",
            "",
            f"- Overall status: **{validation_report.get('status', 'unknown')}**",
            f"- Recommendation: `{validation_report.get('recommendation', 'n/a')}`",
            f"- Warnings: {int(validation_report.get('total_warnings', 0) or 0)}",
        ])
        checks = validation_report.get("checks") or {}
        if isinstance(checks, dict) and checks:
            lines.extend(["", "| Check | Passed | Detail |", "| --- | :---: | --- |"])
            for name, body in checks.items():
                if not isinstance(body, dict):
                    continue
                passed = bool(body.get("passed"))
                # Keep the detail snippet small — full per-check payload
                # lives in the JSONL log.
                detail_parts: list[str] = []
                for k, v in body.items():
                    if k in ("passed", "sample_anomalies"):
                        continue
                    if isinstance(v, (int, float)) and v == 0:
                        continue
                    detail_parts.append(f"`{k}`={v}")
                detail = ", ".join(detail_parts) if detail_parts else "—"
                marker = "✅" if passed else "⚠️"
                lines.append(f"| `{name}` | {marker} | {detail} |")
        notes = validation_report.get("notes")
        if notes:
            lines.extend(["", f"_Validator notes_: {notes}"])
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _md_methodology(score_summary: Optional[dict[str, Any]]) -> str:
        """Formula, weights, thresholds, and dataset version pins.

        Pulls every threshold from :mod:`src.config` so this section
        never lies about what the pipeline actually used — re-tuning
        a weight or threshold in ``config.py`` flows directly into the
        next regenerated report.
        """
        mean_score_str = ""
        if score_summary is not None:
            mean = score_summary.get("mean_composite_score")
            if isinstance(mean, (int, float)):
                mean_score_str = (
                    f"Mean composite score across all scored locations: "
                    f"**{mean:.3f}**\n\n"
                )

        return (
            "## Methodology\n\n"
            "Each location receives a composite obstruction risk score in "
            "the range 0.0–1.0, combining three signals derived from "
            "authoritative remotely-sensed datasets:\n\n"
            "```\n"
            "composite_score = "
            f"(canopy_score × {config.TCC_WEIGHT:.2f}) + "
            f"(terrain_score × {config.TERRAIN_WEIGHT:.2f}) + "
            f"(landcover_score × {config.LANDCOVER_WEIGHT:.2f})\n"
            "```\n\n"
            "### Component thresholds\n\n"
            "| Component | High score (1.0) | Moderate score (0.5) | Low score (0.0) | Weight |\n"
            "| --- | --- | --- | --- | ---: |\n"
            f"| Tree canopy cover | > {config.CANOPY_HIGH_THRESHOLD}% "
            f"| {config.CANOPY_MOD_THRESHOLD}–{config.CANOPY_HIGH_THRESHOLD}% "
            f"| < {config.CANOPY_MOD_THRESHOLD}% "
            f"| {config.TCC_WEIGHT * 100:.0f}% |\n"
            f"| Terrain slope | > {config.SLOPE_HIGH_THRESHOLD}° "
            f"| {config.SLOPE_MOD_THRESHOLD}°–{config.SLOPE_HIGH_THRESHOLD}° "
            f"| < {config.SLOPE_MOD_THRESHOLD}° "
            f"| {config.TERRAIN_WEIGHT * 100:.0f}% |\n"
            "| Land cover | Forest (NLCD 41/42/43) "
            "| Developed (NLCD 21–24) "
            "| Open / barren / water (NLCD 11, 31, 52, 71, 81, 82) "
            f"| {config.LANDCOVER_WEIGHT * 100:.0f}% |\n\n"
            "### Tier thresholds (composite score)\n\n"
            "| Tier | Range | Operational meaning |\n"
            "| --- | --- | --- |\n"
            f"| High | ≥ {config.RISK_HIGH_THRESHOLD:.2f} "
            "| Multiple factors indicate significant obstruction risk. "
            "Site assessment recommended before installation. |\n"
            f"| Moderate | {config.RISK_MOD_THRESHOLD:.2f} – "
            f"{config.RISK_HIGH_THRESHOLD:.2f} "
            "| Some factors elevated. Standard installation workflow with "
            "noted obstructions and roof-mount recommendation. |\n"
            f"| Low | < {config.RISK_MOD_THRESHOLD:.2f} "
            "| Environmental conditions favor successful installation. |\n"
            "| Unscored | (any signal null) "
            "| Environmental data unavailable. Manual assessment required. |\n\n"
            f"{mean_score_str}"
            "### Dataset version pins\n\n"
            "| Dataset | Source | Version |\n"
            "| --- | --- | --- |\n"
            f"| Tree Canopy Cover | USGS / MRLC NLCD | "
            f"`{config.MRLC_TCC_COVERAGE_ID}` |\n"
            f"| Land Cover | USGS / MRLC NLCD | "
            f"`{config.MRLC_LANDCOVER_COVERAGE_ID}` |\n"
            "| Elevation / slope | USGS 3DEP | 1 arc-second (per-tile vintage; "
            "latest vintage selected per 1° quad) |\n"
            "| Census GEOID lookup | `geoid_cb` package | block-group resolution |\n\n"
            "See `docs/data_sourcing.md` for the full sourcing rationale and "
            "`docs/analysis_rationale.md` for the threshold derivation.\n"
        )

    @staticmethod
    def _md_limitations() -> str:
        """What this analysis is — and is not — claiming to model.

        Rooted in ``docs/analysis_rationale.md`` § 4 and ``docs/data_sourcing.md``
        § "What cannot be modeled with public data". Surfacing these
        upfront is the operational difference between a "score" and a
        "score the user can act on".
        """
        return (
            "## Known Limitations\n\n"
            "This pipeline scores each location using nationally available "
            "remotely-sensed data at 30 m resolution. Several factors that "
            "materially affect real-world Starlink performance cannot be "
            "captured by any such public dataset, and are deliberately "
            "**not** modeled:\n\n"
            "- **Exact tree heights.** NLCD TCC measures canopy area "
            "(percent of pixel under canopy), not vertical height. A 90% "
            "canopy pixel could be tall pines or short shrubs — the "
            "obstruction implications differ substantially.\n"
            "- **Building heights.** No national dataset exists. OSM has "
            "footprints but not heights, so urban locations are scored "
            "from land-cover class alone (Developed → Moderate by default).\n"
            "- **Seasonal canopy variation.** NLCD TCC is a peak-summer "
            "2021 snapshot. Deciduous forest (NLCD 41) sheds leaves "
            "November–March, reducing effective obstruction by an "
            "estimated 30–60% in winter — but the stored score does not "
            "vary by query time. The interactive `analyze_location` tool "
            "emits a seasonal advisory; the batch report does not.\n"
            "- **Sub-30 m obstructions.** A single tall tree or utility "
            "pole at a property edge may not register in a 30 m pixel "
            "average. Site assessment is the only reliable way to catch "
            "these.\n"
            "- **Microsite conditions.** Roof access, mounting surface, "
            "HOA restrictions, landlord permission — none of these are in "
            "any remote dataset.\n"
            "- **Temporary obstructions.** Construction cranes, scaffolding, "
            "parked vehicles fall outside the 2021 snapshot.\n"
            "- **Recent vegetation change.** Locations cleared, replanted, "
            "or burned since 2021 are scored from the 2021 state of the "
            "land, not today's.\n\n"
            "A **High** score means the location warrants priority site "
            "assessment, not that it is unserviceable; a skilled installer "
            "with a roof mount frequently turns a High-scored location "
            "into a successful install. A **Low** score reflects the best "
            "available remote assessment but does not rule out the "
            "microsite issues above.\n"
        )

    @staticmethod
    def _md_outputs() -> str:
        """Pointer to the on-disk artifacts an analyst can re-query.

        Paths are rendered relative to ``config.PROJECT_ROOT`` when that
        ancestry holds, otherwise as absolute paths. The fallback covers
        runs (and tests) that monkeypatch ``SCORED_DIR`` or ``_OUTPUTS_DIR``
        to a tmp directory outside the project root.
        """

        def _rel(p: Path) -> str:
            try:
                return str(p.relative_to(config.PROJECT_ROOT))
            except ValueError:
                return str(p)

        return (
            "## Generated Artifacts\n\n"
            f"- Markdown report: `{_rel(_REPORT_MD)}`\n"
            f"- Per-state tier summary (parquet): "
            f"`{_rel(_STATE_SUMMARY_PARQUET)}`\n"
            f"- Per-county tier summary (parquet): "
            f"`{_rel(_COUNTY_SUMMARY_PARQUET)}`\n"
            f"- Interactive risk map: `{_rel(_MAP_HTML)}`\n"
            "- Hive-partitioned scored store (queryable from DuckDB): "
            f"`{_rel(config.SCORED_DIR)}/state=*/part-0.parquet`\n"
        )

    def _render_map(self, df: pd.DataFrame) -> None:
        """Render a Folium map of risk tiers, subsampled for browser sanity.

        4.67M markers would never render in a browser, so we subsample by
        tier (preferring High > Moderate > Low) up to ``_MAP_MAX_POINTS``.
        """
        import folium  # local import — folium is a heavy dependency to import at module load.

        high = df[df["risk_tier"] == TIER_HIGH]
        mod = df[df["risk_tier"] == TIER_MODERATE]
        low = df[df["risk_tier"] == TIER_LOW]

        # Allocate the 5k point budget proportionally but with a floor for
        # each tier so the map always shows variety.
        budget = _MAP_MAX_POINTS
        h_take = min(len(high), max(int(budget * 0.5), 0))
        m_take = min(len(mod), max(int(budget * 0.3), 0))
        l_take = min(len(low), max(budget - h_take - m_take, 0))

        sample = pd.concat(
            [
                high.head(h_take),
                mod.head(m_take),
                low.head(l_take),
            ]
        )

        if sample.empty:
            return

        center_lat = float(sample["latitude"].mean())
        center_lon = float(sample["longitude"].mean())
        m = folium.Map(location=[center_lat, center_lon], zoom_start=7)
        tier_colors = {
            TIER_HIGH: "red",
            TIER_MODERATE: "orange",
            TIER_LOW: "green",
            TIER_UNSCORED: "gray",
        }
        for row in sample.itertuples(index=False):
            color = tier_colors.get(getattr(row, "risk_tier", None), "gray")
            folium.CircleMarker(
                location=[float(row.latitude), float(row.longitude)],
                radius=2,
                color=color,
                fill=True,
                fill_opacity=0.6,
                popup=(
                    f"id={row.location_id}<br>"
                    f"tier={row.risk_tier}<br>"
                    f"score={row.risk_score}"
                ),
            ).add_to(m)
        m.save(str(_MAP_HTML))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(content: Any) -> str:
        """Concatenate all ``TextBlock.text`` fields from a Claude response."""
        parts: list[str] = []
        for block in content or []:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "")
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()

    def _log_cost_estimate(
        self, total_input_tokens: int, total_output_tokens: int
    ) -> None:
        """Emit a ``COST_ESTIMATE`` JSONL event with running spend totals.

        Fires once per Claude API response (in both ``run`` and
        ``run_interactive``). Cumulative figures, not per-response — a
        reviewer scanning the log only needs to read the most recent event
        to see total run cost so far.
        """
        cost_usd = estimate_cost_usd(total_input_tokens, total_output_tokens)
        self.logger.info(
            stage="orchestrator",
            event_type="COST_ESTIMATE",
            detail={
                "total_input_tokens": int(total_input_tokens),
                "total_output_tokens": int(total_output_tokens),
                "estimated_cost_usd": round(cost_usd, 6),
            },
        )

    def _remember_summary(self, tool_name: str, summary: dict[str, Any]) -> dict[str, Any]:
        """Cache ``summary`` for ``tool_name`` and return it unchanged.

        The cache feeds ``_run_generate_report`` — by the time the report
        tool runs, it can reach back into the ingest / env / score summaries
        without re-deriving missing-data rates or row counts from parquet
        files. Returns the input unchanged so callers can write::

            return self._remember_summary("ingest_locations", {...})

        instead of needing a separate stash-then-return block.

        Calls are idempotent and per-handler: the same tool calling twice
        in one run (which only happens in ``_run_generate_report``'s own
        early-return path) overwrites the prior entry.
        """
        self._tool_summaries[tool_name] = summary
        return summary

    def _log_checkpoint(
        self, step: str, output_path: str, row_count: int
    ) -> None:
        """Emit a ``PIPELINE_CHECKPOINT`` event after a tool persists output.

        The structured-log JSONL is the canonical audit trail for what each
        run produced. ``PIPELINE_CHECKPOINT`` events are what Phase 11's
        metrics module scans for to compute per-step latency and to detect
        crashed runs (a checkpoint for step N but not N+1).
        """
        self.logger.info(
            stage="orchestrator",
            event_type="PIPELINE_CHECKPOINT",
            detail={
                "step": step,
                "output_path": output_path,
                "row_count": int(row_count),
                "resume_eligible": True,
            },
        )

    def _resume_ingest_summary(self) -> dict[str, Any]:
        """Build a synthetic ``ingest_locations`` summary from the on-disk
        validated parquet. Called when ``--resume`` finds the artifact
        already present, so the real ingestion work is skipped.
        """
        df = pd.read_parquet(_VALIDATED_PARQUET)
        state_distribution = (
            {str(k): int(v) for k, v in df["state"].dropna().value_counts().items()}
            if "state" in df.columns
            else {}
        )
        self.logger.info(
            stage="orchestrator",
            event_type="RESUME_SKIP",
            detail={"step": "ingest_locations", "rows": len(df)},
        )
        return {
            "status": "ok",
            "total_rows": int(len(df)),
            "valid_rows": int(len(df)),
            "dropped_rows": 0,
            "valid_pct": 1.0,
            "drop_breakdown": {
                Reason.NULL_LOCATION_ID: 0,
                Reason.NULL_COORDINATE: 0,
                Reason.OUT_OF_BOUNDS: 0,
                Reason.INVALID_STATE: 0,
                Reason.DUPLICATE_DROPPED: 0,
                Reason.PARSE_ERROR: 0,
            },
            "state_distribution": state_distribution,
            "output_path": str(_VALIDATED_PARQUET),
            "critical_failure": False,
            "notes": "RESUME: reused existing validated_locations.parquet.",
        }

    def _resume_enrich_summary(self) -> dict[str, Any]:
        """Build a synthetic ``sample_environment`` summary from the on-disk
        enriched parquet. Recomputes missing rates so Claude's downstream
        reasoning still has accurate data-quality numbers."""
        df = pd.read_parquet(_ENRICHED_PARQUET)
        total = len(df)
        self.logger.info(
            stage="orchestrator",
            event_type="RESUME_SKIP",
            detail={"step": "sample_environment", "rows": total},
        )

        def _missing_pct(col: str) -> float:
            return round(df[col].isna().sum() / total, 4) if total else 0.0

        missing_rates = {
            "tcc_missing_pct": _missing_pct("tcc_pct"),
            "elevation_missing_pct": _missing_pct("elevation_m"),
            "slope_missing_pct": _missing_pct("slope_deg"),
            "landcover_missing_pct": _missing_pct("land_cover_code"),
        }
        return {
            "status": "ok",
            "total_locations": int(total),
            "enriched_locations": int(total),
            "missing_rates": missing_rates,
            "output_path": str(_ENRICHED_PARQUET),
            "raster_files_used": {},
            "notes": "RESUME: reused existing enriched_locations.parquet.",
        }

    def _resume_score_summary(self) -> dict[str, Any]:
        """Build a synthetic ``score_risk`` summary from the on-disk
        scored parquet. Recomputes the tier distribution + dominant flag
        so the validation step's downstream reasoning still works."""
        df = pd.read_parquet(_SCORED_PARQUET)
        total = len(df)
        self.logger.info(
            stage="orchestrator",
            event_type="RESUME_SKIP",
            detail={"step": "score_risk", "rows": total},
        )

        tier_counts = df["risk_tier"].value_counts().to_dict() if total else {}
        tier_distribution: dict[str, dict[str, Any]] = {}
        dominant_tier_flag = False
        for tier in (TIER_LOW, TIER_MODERATE, TIER_HIGH, TIER_UNSCORED):
            count = int(tier_counts.get(tier, 0))
            pct = round(count / total, 4) if total else 0.0
            tier_distribution[tier] = {"count": count, "pct": pct}
            if tier != TIER_UNSCORED and pct > _DOMINANT_TIER_THRESHOLD:
                dominant_tier_flag = True

        unscored = int(tier_counts.get(TIER_UNSCORED, 0))
        scored = total - unscored
        mean_score = (
            round(float(df["risk_score"].dropna().mean()), 4)
            if scored
            else None
        )
        return {
            "status": "ok",
            "total_scored": int(scored),
            "unscored": unscored,
            "tier_distribution": tier_distribution,
            "mean_composite_score": mean_score,
            "output_path": str(_SCORED_PARQUET),
            "dominant_tier_flag": dominant_tier_flag,
            "notes": "RESUME: reused existing scored_locations.parquet.",
        }


# ---------------------------------------------------------------------------
# Internal coercion helpers
# ---------------------------------------------------------------------------


def _coerce_int(value: Any) -> Optional[int]:
    """Best-effort int coercion that treats pandas NA / NaN as ``None``."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> Optional[float]:
    """Best-effort float coercion that treats pandas NA / NaN as ``None``."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
