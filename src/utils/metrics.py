"""
Pipeline observability metrics (Phase 11).

Parses a pipeline run's JSONL log into a single ``dict`` of operational
metrics covering three concerns:

1. **Per-stage health** — event counts, success / warning / error rates,
   and latency percentiles (``mean / p50 / p95 / max``) for each stage
   (``ingestion``, ``environmental``, ``orchestrator``, ``scoring``,
   ``output``, ``pipeline``). Surfaces which subsystem regresses
   between runs without needing to grep raw logs.
2. **Claude agent cost & correctness** — cumulative input/output tokens,
   USD cost (computed via :func:`src.agents.orchestrator.estimate_cost_usd`
   so the math matches the per-response ``COST_ESTIMATE`` events on disk),
   per-response averages, and **tool call accuracy**: did the run fire
   every required tool exactly once in the documented order?
3. **Output quality** — % scored vs. % UNSCORED, tier distribution,
   mean / median risk score, and per-factor missing-data rates pulled
   from the most recent ``ENRICHMENT_SUMMARY`` event.

Why a single ``compute_pipeline_metrics`` entry point
-----------------------------------------------------
A reviewer or an on-call engineer can run::

    python -m src.utils.metrics logs/pipeline_run_pipeline-595beb5e.jsonl \
        outputs/scored

and get one block of plain text covering all three concerns without having
to remember three separate analysis scripts. The function also returns the
structured ``dict`` so downstream code (a future dashboard, drift watchdog,
CI metrics check) can consume the same numbers without re-parsing.

Why the tool order is derived from ``TOOLS``, not hardcoded
-----------------------------------------------------------
The "expected" tool sequence used by the accuracy check is read from
:data:`src.agents.orchestrator.TOOLS` at call time. If a future phase adds
or reorders tools the metrics module reflects the *current* spec without a
code change. The orchestrator is the single source of truth for what the
agent should be doing.

Why output quality also accepts a scored-parquet path
------------------------------------------------------
The JSONL never carries per-tier counts — Claude only ever sees summaries
and ``output_status`` codes. The persistent ground truth for "what did the
pipeline actually produce" is the scored parquet on disk. So
``compute_pipeline_metrics`` accepts an optional ``scored_path``:

* If pointed at the partitioned store dir (``outputs/scored/``) we go
  through DuckDB via :func:`src.data.store.get_risk_distribution` — the
  same query path the report generator uses, so metrics can never drift
  from the report.
* If pointed at the single-file workflow parquet
  (``data/processed/scored_locations.parquet``) we read it directly with
  pandas — cheap, identical schema.
* If omitted, the output-quality block degrades gracefully: per-tier %
  is filled in from the JSONL's ``cumulative_missing_rates`` and
  ``TOOL_CALL`` statuses where possible, and the rest is set to ``None``.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import pandas as pd

PathLike = Union[str, Path]


# ---------------------------------------------------------------------------
# Constants — kept module-local so this file has no dependency on the agent
# stack to load. The values match ``src.data.store`` and ``src.agents.scoring``.
# Drift between the two is caught by ``tests/test_metrics.py``.
# ---------------------------------------------------------------------------

_TIER_HIGH = "High"
_TIER_MODERATE = "Moderate"
_TIER_LOW = "Low"
_TIER_UNSCORED = "UNSCORED"

# Stages the pipeline logger emits today. Listed explicitly (rather than
# discovered from the log) so a stage that fires zero events still shows up
# in the metrics output with a clear ``event_count = 0`` — silence is itself
# a signal.
_KNOWN_STAGES: tuple[str, ...] = (
    "ingestion",
    "environmental",
    "orchestrator",
    "scoring",
    "output",
    "pipeline",
)


# ---------------------------------------------------------------------------
# Log loading
# ---------------------------------------------------------------------------


def _load_events(log_path: PathLike) -> tuple[list[dict[str, Any]], int]:
    """Parse a JSONL log file into ``(events, parse_errors)``.

    Each line is its own JSON document; malformed lines (rare in practice,
    but cheap to be defensive against an interrupted append) are skipped
    silently so a partially-flushed log from a still-running pipeline does
    not crash the metrics call. The skipped-line count is returned
    alongside the events so the top-level summary can surface it.
    """
    p = Path(log_path)
    if not p.is_file():
        raise FileNotFoundError(f"Log file not found: {p}")

    events: list[dict[str, Any]] = []
    parse_errors = 0
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                parse_errors += 1
    return events, parse_errors


# ---------------------------------------------------------------------------
# Per-stage metrics
# ---------------------------------------------------------------------------


def _percentile(values: list[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile suitable for small samples.

    ``statistics.quantiles`` requires at least two values and interpolates,
    which is the wrong shape for an "I have 5 TOOL_CALL durations, what's
    P95?" question — nearest-rank is the convention for operational
    latency dashboards and matches what every observability tool emits.
    """
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return float(s[k])


def _stage_durations(events: Iterable[dict[str, Any]]) -> list[float]:
    """Pull non-null ``duration_ms`` values for a stage's events."""
    return [
        float(e["duration_ms"])
        for e in events
        if isinstance(e.get("duration_ms"), (int, float))
    ]


def _per_stage_metrics(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Bucket events by ``stage`` and summarize each bucket.

    For every known stage we report:

    * ``event_count`` — total events emitted (success + warning + error)
    * ``success_events``, ``warning_events``, ``error_events`` — by ``level``
    * ``success_rate`` / ``error_rate`` — out of total events, rounded to 4 dp
    * ``latency_ms`` — ``{count, mean, p50, p95, max}`` over events that
      carried a ``duration_ms``. ``None`` when no events in the stage
      carried a duration.

    A stage with zero events emits all-zero counts plus ``latency_ms: None``
    so downstream consumers can iterate over ``_KNOWN_STAGES`` and always
    find the key — easier than a defaultdict in the renderer.
    """
    out: dict[str, dict[str, Any]] = {}
    by_stage: dict[str, list[dict[str, Any]]] = {s: [] for s in _KNOWN_STAGES}
    for e in events:
        by_stage.setdefault(e.get("stage", "unknown"), []).append(e)

    for stage, stage_events in by_stage.items():
        total = len(stage_events)
        levels = {"INFO": 0, "WARNING": 0, "ERROR": 0}
        for e in stage_events:
            lvl = e.get("level", "INFO")
            levels[lvl] = levels.get(lvl, 0) + 1

        durations = _stage_durations(stage_events)
        latency: Optional[dict[str, Any]] = None
        if durations:
            latency = {
                "count": len(durations),
                "mean": round(statistics.fmean(durations), 2),
                "p50": _percentile(durations, 50),
                "p95": _percentile(durations, 95),
                "max": float(max(durations)),
            }

        out[stage] = {
            "event_count": total,
            "success_events": levels.get("INFO", 0),
            "warning_events": levels.get("WARNING", 0),
            "error_events": levels.get("ERROR", 0),
            "success_rate": (
                round(levels.get("INFO", 0) / total, 4) if total else None
            ),
            "error_rate": (
                round(levels.get("ERROR", 0) / total, 4) if total else None
            ),
            "latency_ms": latency,
        }
    return out


# ---------------------------------------------------------------------------
# Claude metrics
# ---------------------------------------------------------------------------


def _expected_tool_order() -> list[str]:
    """Return the canonical tool sequence the orchestrator should execute.

    Reads the live ``TOOLS`` definition rather than hardcoding so this stays
    correct as tools are added or reordered. Imported lazily — the metrics
    module otherwise has no agent-stack dependencies, which keeps test
    setup cheap.
    """
    from src.agents.orchestrator import TOOLS

    return [t["name"] for t in TOOLS]


def _tool_call_accuracy(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Did Claude fire every required tool exactly once in the expected order?

    Operates on ``TOOL_CALL`` events only. The "expected order" is whatever
    :func:`_expected_tool_order` returns. The observed order is the sequence
    of ``detail.tool_name`` values in the JSONL.

    The pass criterion is **exact equality** of the observed and expected
    lists. That is stricter than "all present + in order" because the
    pipeline-level design only fires each tool once in a happy-path run;
    a duplicate is a red flag (a tool error caused Claude to retry, or
    a tool was called twice for the same input). We surface those
    deviations explicitly in ``missing`` / ``extra``.
    """
    expected = _expected_tool_order()
    observed = [
        e.get("detail", {}).get("tool_name", "")
        for e in events
        if e.get("event_type") == "TOOL_CALL"
    ]
    observed_set = set(observed)
    expected_set = set(expected)

    return {
        "expected": expected,
        "observed": observed,
        "all_present": expected_set.issubset(observed_set),
        "in_expected_order": observed == expected,
        "missing": [t for t in expected if t not in observed_set],
        "extra_or_repeated": (
            [t for t in observed if t not in expected_set]
            + [t for t in expected if observed.count(t) > 1]
        ),
    }


def _claude_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate Claude token usage, cost, and tool-call accuracy.

    Token totals are read from the **last** ``COST_ESTIMATE`` event rather
    than summed over all events because the orchestrator writes cumulative
    totals on every Claude response — summing would double-count. Cost is
    re-computed from those totals via
    :func:`src.agents.orchestrator.estimate_cost_usd` so the metrics
    module is the source of truth even if a future log writer forgets
    to include the ``estimated_cost_usd`` field.

    ``avg_input_per_response`` and ``avg_output_per_response`` are simple
    cumulative-over-response-count quotients; when there are zero Claude
    responses they are ``None``.
    """
    cost_events = [e for e in events if e.get("event_type") == "COST_ESTIMATE"]
    response_events = [e for e in events if e.get("event_type") == "CLAUDE_RESPONSE"]
    last_cost: dict[str, Any] = (
        cost_events[-1].get("detail", {}) if cost_events else {}
    )

    total_input = int(last_cost.get("total_input_tokens") or 0)
    total_output = int(last_cost.get("total_output_tokens") or 0)
    response_count = len(response_events) or len(cost_events)

    try:
        from src.agents.orchestrator import estimate_cost_usd

        cost_usd = estimate_cost_usd(total_input, total_output)
    except Exception:
        # Fall back to the cost the orchestrator already logged so the
        # metrics module remains useful even if the agent stack fails to
        # import (e.g. in a stripped-down analysis environment).
        cost_usd = float(last_cost.get("estimated_cost_usd") or 0.0)

    try:
        from src import config as _config

        claude_model = _config.CLAUDE_MODEL
    except Exception:
        claude_model = None

    return {
        "model": claude_model,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "estimated_cost_usd": round(cost_usd, 6),
        "response_count": response_count,
        "avg_input_per_response": (
            round(total_input / response_count, 2) if response_count else None
        ),
        "avg_output_per_response": (
            round(total_output / response_count, 2) if response_count else None
        ),
        "tool_call_accuracy": _tool_call_accuracy(events),
    }


# ---------------------------------------------------------------------------
# Output-quality metrics
# ---------------------------------------------------------------------------


def _read_scored_df(scored_path: Path) -> pd.DataFrame:
    """Load the scored locations dataframe from disk.

    Accepts either the partitioned store directory or the single-file
    workflow parquet. The two paths are intentionally co-equal: a partial
    run that wrote the single file but not the partition store is still
    queryable, and a fully-merged run's partition store is the more
    authoritative source.
    """
    if scored_path.is_dir():
        # Route through ``src.data.store`` so the partition-glob and
        # empty-store handling stays in one place.
        from src.data.store import read_scored_locations

        return read_scored_locations(scored_dir=scored_path)
    return pd.read_parquet(scored_path)


def _enrichment_missing_rates(events: list[dict[str, Any]]) -> dict[str, float]:
    """Pull the most recent per-factor missing-data rates from the log.

    The environmental agent emits ``ENRICHMENT_SUMMARY`` on completion of
    each batch with ``cumulative_missing_rates`` covering tcc, slope,
    aspect, land_cover. We take the **last** one rather than averaging
    because the rates are already cumulative across all batches in the
    run; the last event is the run-final view.
    """
    summary_events = [
        e for e in events if e.get("event_type") == "ENRICHMENT_SUMMARY"
    ]
    if not summary_events:
        return {}
    detail = summary_events[-1].get("detail", {}) or {}
    rates = detail.get("cumulative_missing_rates", {}) or {}
    return {k: float(v) for k, v in rates.items()}


def _validation_status(events: list[dict[str, Any]]) -> Optional[str]:
    """Return the ``output_status`` from the ``validate_results`` TOOL_CALL.

    Possible values today: ``ok``, ``warnings``, ``error``. ``None`` when
    the tool was never called (a truncated or failed run).
    """
    for e in events:
        if (
            e.get("event_type") == "TOOL_CALL"
            and e.get("detail", {}).get("tool_name") == "validate_results"
        ):
            return e.get("detail", {}).get("output_status")
    return None


def _output_quality(
    events: list[dict[str, Any]],
    scored_path: Optional[Path],
) -> dict[str, Any]:
    """Tier distribution, mean risk score, missing rates, validation status.

    Numbers come from the scored parquet when available (the ground truth)
    and from the JSONL otherwise. Both surfaces are populated so a reader
    of the metrics dict can spot disagreement between "what the run logged"
    and "what is on disk now".
    """
    missing_rates = _enrichment_missing_rates(events)
    validation_status = _validation_status(events)

    # Output quality anomaly detection note
    #
    # Anomaly detection accuracy cannot be computed without ground truth labels.
    # Current checks are rule based statistical flags based on distribution sanity,
    # cross validation, geographic bounds, and missing data rates.
    # Accuracy becomes measurable when installer field visit outcomes are collected
    # and matched back to location_ids.

    base: dict[str, Any] = {
        "scored_path": str(scored_path) if scored_path else None,
        "total_locations": None,
        "scored_count": None,
        "scored_pct": None,
        "unscored_count": None,
        "unscored_pct": None,
        "tier_distribution": None,
        "mean_risk_score": None,
        "median_risk_score": None,
        "missing_rates": missing_rates,
        "validation_status": validation_status,
    }

    if scored_path is None or not scored_path.exists():
        return base

    df = _read_scored_df(scored_path)
    total = int(len(df))
    base["total_locations"] = total
    if total == 0:
        return base

    tier_col = df["risk_tier"]
    unscored = int((tier_col == _TIER_UNSCORED).sum())
    scored = total - unscored

    tier_counts: dict[str, int] = {
        _TIER_HIGH: int((tier_col == _TIER_HIGH).sum()),
        _TIER_MODERATE: int((tier_col == _TIER_MODERATE).sum()),
        _TIER_LOW: int((tier_col == _TIER_LOW).sum()),
        _TIER_UNSCORED: unscored,
    }
    tier_distribution = {
        tier: {
            "count": count,
            "pct": round(count / total, 4) if total else None,
        }
        for tier, count in tier_counts.items()
    }

    # ``risk_score`` is NaN for UNSCORED rows by design (see
    # ``src.agents.scoring.score_components``). Take the mean/median over
    # scored rows only — the "average risk score of scored locations" is
    # the metric a reader expects, not "average risk score including 0s
    # for the unscored bucket".
    scored_mask = tier_col != _TIER_UNSCORED
    scored_scores = df.loc[scored_mask, "risk_score"].dropna()
    mean_score = (
        round(float(scored_scores.mean()), 4) if not scored_scores.empty else None
    )
    median_score = (
        round(float(scored_scores.median()), 4) if not scored_scores.empty else None
    )

    base.update(
        scored_count=scored,
        scored_pct=round(scored / total, 4),
        unscored_count=unscored,
        unscored_pct=round(unscored / total, 4),
        tier_distribution=tier_distribution,
        mean_risk_score=mean_score,
        median_risk_score=median_score,
    )
    return base


# ---------------------------------------------------------------------------
# Run-level metadata
# ---------------------------------------------------------------------------


def _run_metadata(
    events: list[dict[str, Any]],
    log_path: Path,
    parse_errors: int = 0,
) -> dict[str, Any]:
    """Extract run-id, start/end timestamps, and total wall-clock duration.

    Wall-clock duration comes from the ``ORCHESTRATOR_DONE`` event's
    ``duration_ms`` when present (the orchestrator writes its own
    end-to-end elapsed there). Otherwise we fall back to the timestamp
    delta between the first and last log line — less accurate (depends
    on clock granularity and write buffering) but always available.
    """
    run_id = log_path.stem.replace("pipeline_run_", "")
    start_ts: Optional[str] = events[0].get("timestamp") if events else None
    end_ts: Optional[str] = events[-1].get("timestamp") if events else None

    duration_ms: Optional[int] = None
    for e in events:
        if e.get("event_type") == "ORCHESTRATOR_DONE" and e.get("duration_ms"):
            duration_ms = int(e["duration_ms"])
            break

    if duration_ms is None and start_ts and end_ts:
        try:
            from datetime import datetime

            duration_ms = int(
                (
                    datetime.fromisoformat(end_ts) - datetime.fromisoformat(start_ts)
                ).total_seconds()
                * 1000
            )
        except ValueError:
            duration_ms = None

    return {
        "run_id": run_id,
        "log_path": str(log_path),
        "event_count": len(events),
        "parse_errors": parse_errors,
        "start_timestamp": start_ts,
        "end_timestamp": end_ts,
        "duration_ms": duration_ms,
    }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def compute_pipeline_metrics(
    log_path: PathLike,
    scored_path: Optional[PathLike] = None,
) -> dict[str, Any]:
    """Compute observability metrics for a pipeline run.

    Parameters
    ----------
    log_path:
        Path to the JSONL log file (typically under ``logs/``).
    scored_path:
        Optional path to the scored locations parquet. May be either
        the partitioned store directory (``outputs/scored/``) or the
        single-file workflow parquet
        (``data/processed/scored_locations.parquet``). If omitted, the
        ``output_quality`` block is partially populated from the log
        alone — tier counts and risk-score stats will be ``None``.

    Returns
    -------
    dict
        Nested dict with keys ``run``, ``per_stage``, ``claude``, and
        ``output_quality``. See module docstring for shape. The dict
        is JSON-serializable (no numpy / pandas objects leak through).
    """
    log_p = Path(log_path)
    scored_p = Path(scored_path) if scored_path is not None else None
    events, parse_errors = _load_events(log_p)

    return {
        "run": _run_metadata(events, log_p, parse_errors=parse_errors),
        "per_stage": _per_stage_metrics(events),
        "claude": _claude_metrics(events),
        "output_quality": _output_quality(events, scored_p),
    }


def format_metrics_text(metrics: dict[str, Any]) -> str:
    """Render a metrics dict as a human-readable plain-text report.

    Optimized for a terminal — no markdown, no color, just aligned columns
    and section headers. The shape mirrors the dict's structure so a
    reviewer can cross-reference any line back to its source key.
    """
    out: list[str] = []
    run = metrics.get("run", {})
    out.append("=" * 70)
    out.append("PIPELINE METRICS — " + (run.get("run_id") or "(unknown run)"))
    out.append("=" * 70)
    out.append(f"  Log file:        {run.get('log_path')}")
    out.append(f"  Event count:     {run.get('event_count')}")
    if run.get("parse_errors"):
        out.append(f"  Parse errors:    {run['parse_errors']} (lines skipped)")
    out.append(f"  Started:         {run.get('start_timestamp')}")
    out.append(f"  Finished:        {run.get('end_timestamp')}")
    dur = run.get("duration_ms")
    if dur is not None:
        out.append(f"  Wall-clock:      {dur:,} ms ({dur / 1000:.1f} s)")
    out.append("")

    out.append("-- Per-stage health -------------------------------------------------")
    out.append(
        f"  {'stage':<14} {'events':>7} {'ok':>5} {'warn':>5} {'err':>4} "
        f"{'err_rate':>9} {'p50_ms':>8} {'p95_ms':>8}"
    )
    for stage, m in metrics.get("per_stage", {}).items():
        lat = m.get("latency_ms") or {}
        out.append(
            f"  {stage:<14} {m['event_count']:>7} "
            f"{m['success_events']:>5} {m['warning_events']:>5} "
            f"{m['error_events']:>4} "
            f"{(_fmt_pct(m['error_rate'])):>9} "
            f"{_fmt_num(lat.get('p50')):>8} {_fmt_num(lat.get('p95')):>8}"
        )
    out.append("")

    claude = metrics.get("claude", {})
    out.append("-- Claude agent -----------------------------------------------------")
    out.append(f"  Model:                  {claude.get('model')}")
    out.append(f"  Responses:              {claude.get('response_count')}")
    out.append(f"  Total input tokens:     {claude.get('total_input_tokens'):,}")
    out.append(f"  Total output tokens:    {claude.get('total_output_tokens'):,}")
    out.append(f"  Avg input/response:     {claude.get('avg_input_per_response')}")
    out.append(f"  Avg output/response:    {claude.get('avg_output_per_response')}")
    out.append(f"  Estimated cost (USD):   ${claude.get('estimated_cost_usd'):.6f}")

    acc = claude.get("tool_call_accuracy", {})
    out.append("  Tool call accuracy:")
    out.append(f"      expected: {acc.get('expected')}")
    out.append(f"      observed: {acc.get('observed')}")
    out.append(f"      all present:        {acc.get('all_present')}")
    out.append(f"      in expected order:  {acc.get('in_expected_order')}")
    if acc.get("missing"):
        out.append(f"      missing:            {acc['missing']}")
    if acc.get("extra_or_repeated"):
        out.append(f"      extra/repeated:     {acc['extra_or_repeated']}")
    out.append("")

    quality = metrics.get("output_quality", {})
    out.append("-- Output quality ---------------------------------------------------")
    out.append(f"  Scored parquet:      {quality.get('scored_path')}")
    out.append(f"  Total locations:     {_fmt_num(quality.get('total_locations'))}")
    out.append(
        f"  Scored:              {_fmt_num(quality.get('scored_count'))} "
        f"({_fmt_pct(quality.get('scored_pct'))})"
    )
    out.append(
        f"  UNSCORED:            {_fmt_num(quality.get('unscored_count'))} "
        f"({_fmt_pct(quality.get('unscored_pct'))})"
    )
    out.append(f"  Mean risk score:     {quality.get('mean_risk_score')}")
    out.append(f"  Median risk score:   {quality.get('median_risk_score')}")
    out.append(f"  Validation status:   {quality.get('validation_status')}")

    tier_dist = quality.get("tier_distribution")
    if tier_dist:
        out.append("  Tier distribution:")
        for tier in (_TIER_HIGH, _TIER_MODERATE, _TIER_LOW, _TIER_UNSCORED):
            row = tier_dist.get(tier, {})
            out.append(
                f"      {tier:<10}  {_fmt_num(row.get('count')):>8}  "
                f"({_fmt_pct(row.get('pct'))})"
            )

    missing = quality.get("missing_rates") or {}
    if missing:
        out.append("  Missing-data rates (per environmental factor):")
        for factor, rate in missing.items():
            out.append(f"      {factor:<10}  {_fmt_pct(rate)}")

    out.append("=" * 70)
    return "\n".join(out)


def _fmt_pct(v: Optional[float]) -> str:
    """Format a fractional rate as a percentage string, or ``-`` if ``None``."""
    if v is None:
        return "-"
    return f"{v * 100:.2f}%"


def _fmt_num(v: Optional[Union[int, float]]) -> str:
    """Format a numeric value with thousands separators, or ``-`` if ``None``."""
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return f"{v:,}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str]) -> int:
    """Entry point for ``python -m src.utils.metrics LOG_PATH [SCORED_PATH]``.

    Prints the formatted metrics block and exits 0. Exits 2 with a usage
    line if the log path is missing — matching the convention used by
    the rest of the CLIs in this project.
    """
    if not argv or argv[0] in {"-h", "--help"}:
        print(
            "Usage: python -m src.utils.metrics LOG_PATH [SCORED_PATH]\n"
            "  LOG_PATH:     logs/pipeline_run_<run_id>.jsonl\n"
            "  SCORED_PATH:  outputs/scored/  OR  "
            "data/processed/scored_locations.parquet  (optional)",
            file=sys.stderr,
        )
        return 2
    log_path = argv[0]
    scored_path = argv[1] if len(argv) > 1 else None
    metrics = compute_pipeline_metrics(log_path, scored_path)
    print(format_metrics_text(metrics))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main(sys.argv[1:]))
