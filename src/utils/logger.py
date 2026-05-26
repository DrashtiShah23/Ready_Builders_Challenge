"""
Structured JSONL logger for pipeline observability.

Every agent writes events through this logger. Each line in the log file is
a single valid JSON object — easy to parse with `jq`, `pandas.read_json`, or
DuckDB's `read_json_auto` for post-run analysis.

Log files land at `logs/pipeline_run_{run_id}.jsonl`.

Why JSONL not plain text:
- Structured per-event fields (level, stage, batch_id, location_id, duration,
  token usage) make it trivial to compute per-agent metrics (Phase 11).
- A reviewer or on-call engineer can `jq 'select(.level=="ERROR")'` to triage
  failures without writing a parser.
- Token usage and latency live alongside the events that generated them, so a
  single log file is a complete cost + performance audit trail.

Schema for each event (typed kwargs in `_write`):
    timestamp      ISO 8601 UTC
    level          INFO | WARNING | ERROR
    stage          ingestion | environmental | orchestrator | scoring | output | pipeline
    batch_id       optional, propagates from ValidatedLocation onward
    location_id    optional, present for per-record events
    event_type     short uppercase code (e.g. BATCH_START, TCC_MISSING, CLAUDE_CALL_DONE)
    detail         arbitrary structured dict for event-specific context
    duration_ms    optional latency
    token_input    optional Claude prompt-token usage
    token_output   optional Claude completion-token usage
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.config import LOG_DIR

LOG_DIR.mkdir(parents=True, exist_ok=True)


class PipelineLogger:
    """Append-only JSONL writer scoped to a single pipeline run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.log_path: Path = LOG_DIR / f"pipeline_run_{run_id}.jsonl"

    def _write(
        self,
        level: str,
        stage: str,
        event_type: str,
        batch_id: Optional[str] = None,
        location_id: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
        duration_ms: Optional[int] = None,
        token_input: Optional[int] = None,
        token_output: Optional[int] = None,
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "stage": stage,
            "batch_id": batch_id,
            "location_id": location_id,
            "event_type": event_type,
            "detail": detail or {},
            "duration_ms": duration_ms,
            "token_input": token_input,
            "token_output": token_output,
        }
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def info(self, stage: str, event_type: str, **kwargs: Any) -> None:
        self._write("INFO", stage, event_type, **kwargs)

    def warning(self, stage: str, event_type: str, **kwargs: Any) -> None:
        self._write("WARNING", stage, event_type, **kwargs)

    def error(self, stage: str, event_type: str, **kwargs: Any) -> None:
        self._write("ERROR", stage, event_type, **kwargs)
