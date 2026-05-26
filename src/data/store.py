"""
Parquet state store + DuckDB analytics interface.

The state store is the canonical analytical home for scored locations. The
orchestrator's ``_run_score_risk`` writes to it in addition to the single-file
intermediate at ``data/processed/scored_locations.parquet``; the single file
remains the workflow-internal artifact the downstream tools read, while this
store is the queryable surface for analysts, the report generator, and the
``--resume`` logic in ``pipeline.py``.

Layout
------
Hive-partitioned by ``state``::

    outputs/scored/
    ├── state=NC/part-0.parquet
    ├── state=CA/part-0.parquet
    └── ...

Why partitioned-by-state:

- **DuckDB partition pruning** — a `WHERE state = 'NC'` scan reads only the NC
  parquet, not the full 4.67M-row dataset. At one-state-per-run today this is
  zero-cost; at the 49-state scale this is the difference between a 200 ms
  query and a 10 s one.
- **Spark / Athena / BigQuery compatibility** — every other ecosystem
  partition reader treats ``state={value}/`` as a column, so the same files
  can be queried from anywhere without an explicit catalog.
- **Per-state regeneration** — re-running the pipeline for just California
  rewrites ``state=CA/`` and leaves the other states untouched.

DuckDB queries always go through an explicit glob (``state=*/*.parquet``) so
sibling summary parquet files (``risk_summary_by_state.parquet`` etc.)
co-located in ``outputs/scored/`` are never accidentally picked up as part of
the partitioned dataset.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd

from src import config


# ---------------------------------------------------------------------------
# Constants — kept module-private to keep the public surface narrow.
# ---------------------------------------------------------------------------


# Tier strings duplicated here (rather than imported from src.agents.scoring)
# so the store has no dependency on the agent stack. ``read_scored_locations``
# returns whatever tier strings were written; the helper queries below filter
# on these literals.
_TIER_HIGH = "High"
_TIER_MODERATE = "Moderate"
_TIER_LOW = "Low"
_TIER_UNSCORED = "UNSCORED"

# A leading underscore is the convention for "skip this in Hive readers", but
# DuckDB/pyarrow only honor it when the writer didn't manually add a partition
# value of that name. Stored here so the read-glob below stays the single
# source of truth for the partition layout.
_PARTITION_GLOB = "state=*/*.parquet"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _default_dir() -> Path:
    """Return the default state-store directory.

    Resolved lazily so a test that monkeypatches ``config.SCORED_DIR`` to a
    tmp path gets the patched value without needing to re-import this module.
    """
    return config.SCORED_DIR


def _glob_path(scored_dir: Path) -> str:
    """Build the DuckDB ``read_parquet`` glob for the partitioned dataset."""
    return str(scored_dir / _PARTITION_GLOB)


def _has_any_partitions(scored_dir: Path) -> bool:
    """True when at least one ``state=*/*.parquet`` file exists."""
    return any(scored_dir.glob(_PARTITION_GLOB))


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def write_scored_locations(
    df: pd.DataFrame,
    scored_dir: Optional[Path] = None,
    overwrite: bool = True,
) -> Path:
    """Write a scored-locations DataFrame as a Hive-partitioned parquet store.

    Parameters
    ----------
    df:
        DataFrame matching the ``ScoredLocation`` schema. Must include a
        ``state`` column — rows with ``state`` null are written into the
        ``state=UNKNOWN/`` partition so they remain queryable.
    scored_dir:
        Output root. Defaults to ``config.SCORED_DIR``.
    overwrite:
        When True (default), deletes existing ``state=*/`` partitions
        beneath ``scored_dir`` before writing. When False, new files are
        added alongside existing partitions — useful for incremental writes
        (e.g. one state at a time on a multi-state run).

    Returns
    -------
    Path
        The root directory of the partitioned dataset.
    """
    target = Path(scored_dir) if scored_dir is not None else _default_dir()
    target.mkdir(parents=True, exist_ok=True)

    if overwrite:
        # Only blast away ``state=*`` subdirs — sibling summary parquets
        # (e.g. ``risk_summary_by_state.parquet``) stay untouched.
        for partition_dir in target.glob("state=*"):
            if partition_dir.is_dir():
                shutil.rmtree(partition_dir)

    if df.empty:
        # Empty dataset is a legal outcome (zero scored locations on a
        # failed run). Leave the directory empty rather than write a
        # spurious ``state=UNKNOWN/`` partition.
        return target

    # Fill null states with the literal ``"UNKNOWN"`` so pyarrow's partition
    # writer doesn't drop those rows silently. The Phase 4 ingestion step
    # only emits ``state=None`` when geoid_cb derivation failed, so the
    # surfaced partition is meaningful for the data-quality story.
    df = df.copy()
    df["state"] = df["state"].where(df["state"].notna(), "UNKNOWN")

    # ``list``-dtype columns (notably ``all_flags``) round-trip through
    # parquet fine when the engine is pyarrow. Some pandas builds default
    # to fastparquet; force pyarrow for reliability.
    df.to_parquet(
        target,
        engine="pyarrow",
        partition_cols=["state"],
        index=False,
    )
    return target


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def read_scored_locations(
    scored_dir: Optional[Path] = None,
) -> pd.DataFrame:
    """Read every state partition back into a single DataFrame.

    Returns an empty DataFrame when the store is empty, so callers don't
    have to special-case missing data. Goes through DuckDB's explicit
    glob rather than ``pd.read_parquet(dir)`` — pyarrow's dataset reader
    would otherwise include sibling top-level parquets (e.g. the per-state
    summary file) as part of the partitioned dataset, which is exactly the
    "no accidental pickups" guarantee documented at the top of this module.
    """
    target = Path(scored_dir) if scored_dir is not None else _default_dir()
    if not _has_any_partitions(target):
        return pd.DataFrame()
    return duckdb.sql(
        f"SELECT * FROM read_parquet("
        f"'{_glob_path(target)}', hive_partitioning = true)"
    ).fetchdf()


def get_scored_location_ids(
    scored_dir: Optional[Path] = None,
) -> set[str]:
    """Return every ``location_id`` already in the store.

    Used by ``pipeline.py --resume`` to short-circuit re-running steps that
    have already produced output. DuckDB pulls only the ``location_id``
    column, so this is cheap even on the full 4.67M-row store.
    """
    target = Path(scored_dir) if scored_dir is not None else _default_dir()
    if not _has_any_partitions(target):
        return set()
    rows = duckdb.sql(
        f"SELECT DISTINCT location_id FROM read_parquet('{_glob_path(target)}')"
    ).fetchall()
    return {str(r[0]) for r in rows}


# ---------------------------------------------------------------------------
# DuckDB query API
# ---------------------------------------------------------------------------


def query(
    sql: str,
    scored_dir: Optional[Path] = None,
) -> pd.DataFrame:
    """Run an arbitrary SQL query over the partitioned store.

    The query receives a view named ``scored`` that points at every
    partition under ``scored_dir``. Callers write SQL against that view::

        store.query("SELECT risk_tier, COUNT(*) FROM scored GROUP BY 1")

    Returns an empty DataFrame when the store is empty (rather than raise),
    so a single-state report generator on a freshly-initialised repo
    doesn't need to guard for the no-data case.
    """
    target = Path(scored_dir) if scored_dir is not None else _default_dir()
    con = duckdb.connect(database=":memory:")
    try:
        if _has_any_partitions(target):
            # Register the partitioned dataset as a view. The ``hive_partitioning``
            # flag is the explicit form — DuckDB infers it for paths matching
            # ``key=value`` segments, but stating it removes the inference
            # cost and makes the call self-documenting.
            con.execute(
                f"CREATE VIEW scored AS "
                f"SELECT * FROM read_parquet('{_glob_path(target)}', "
                f"hive_partitioning = true)"
            )
        else:
            # Empty-store path: register a typed empty view so queries
            # against ``scored`` succeed with zero rows rather than raise.
            con.execute(
                "CREATE VIEW scored AS SELECT "
                "CAST(NULL AS VARCHAR) AS location_id, "
                "CAST(NULL AS DOUBLE) AS latitude, "
                "CAST(NULL AS DOUBLE) AS longitude, "
                "CAST(NULL AS VARCHAR) AS state, "
                "CAST(NULL AS VARCHAR) AS county, "
                "CAST(NULL AS INTEGER) AS tcc_pct, "
                "CAST(NULL AS DOUBLE) AS slope_deg, "
                "CAST(NULL AS INTEGER) AS land_cover_code, "
                "CAST(NULL AS DOUBLE) AS risk_score, "
                "CAST(NULL AS VARCHAR) AS risk_tier "
                "WHERE 1 = 0"
            )
        return con.execute(sql).fetchdf()
    finally:
        con.close()


def get_risk_distribution(
    scored_dir: Optional[Path] = None,
) -> pd.DataFrame:
    """Tier counts and pcts across the whole store.

    Columns: ``risk_tier``, ``count``, ``pct``. Ordered with High first so
    a report writer that takes ``.iloc[0]`` lands on the most-at-risk tier.
    """
    return query(
        """
        WITH tier_counts AS (
            SELECT risk_tier, COUNT(*) AS count
            FROM scored
            GROUP BY risk_tier
        ),
        totals AS (SELECT SUM(count) AS total FROM tier_counts)
        SELECT
            risk_tier,
            count,
            ROUND(count / NULLIF((SELECT total FROM totals), 0), 4) AS pct
        FROM tier_counts
        ORDER BY
            CASE risk_tier
                WHEN 'High' THEN 0
                WHEN 'Moderate' THEN 1
                WHEN 'Low' THEN 2
                ELSE 3
            END
        """,
        scored_dir=scored_dir,
    )


def get_state_breakdown(
    scored_dir: Optional[Path] = None,
) -> pd.DataFrame:
    """Per-state tier counts.

    Columns: ``state``, ``high_count``, ``moderate_count``, ``low_count``,
    ``unscored_count``, ``total``, ``high_pct``. ``high_pct`` is rounded to
    4 dp so a downstream consumer can sort on it deterministically.
    """
    return query(
        f"""
        SELECT
            state,
            SUM(CASE WHEN risk_tier = '{_TIER_HIGH}' THEN 1 ELSE 0 END) AS high_count,
            SUM(CASE WHEN risk_tier = '{_TIER_MODERATE}' THEN 1 ELSE 0 END) AS moderate_count,
            SUM(CASE WHEN risk_tier = '{_TIER_LOW}' THEN 1 ELSE 0 END) AS low_count,
            SUM(CASE WHEN risk_tier = '{_TIER_UNSCORED}' THEN 1 ELSE 0 END) AS unscored_count,
            COUNT(*) AS total,
            ROUND(
                SUM(CASE WHEN risk_tier = '{_TIER_HIGH}' THEN 1 ELSE 0 END)
                / NULLIF(COUNT(*), 0),
                4
            ) AS high_pct
        FROM scored
        GROUP BY state
        ORDER BY high_pct DESC NULLS LAST, state
        """,
        scored_dir=scored_dir,
    )


def get_top_at_risk_counties(
    n: int = 10,
    scored_dir: Optional[Path] = None,
    min_locations: int = 25,
) -> pd.DataFrame:
    """Top ``n`` counties by share of locations in the ``High`` tier.

    ``min_locations`` filters out tiny counties whose ``high_pct`` is
    statistically meaningless (one High row out of two locations is not a
    "high-risk county"). Defaults to 25 — small enough to keep most real
    rural counties, large enough to suppress the noise.
    """
    return query(
        f"""
        WITH per_county AS (
            SELECT
                state,
                county,
                SUM(CASE WHEN risk_tier = '{_TIER_HIGH}' THEN 1 ELSE 0 END) AS high_count,
                COUNT(*) AS total
            FROM scored
            WHERE county IS NOT NULL
            GROUP BY state, county
            HAVING COUNT(*) >= {int(min_locations)}
        )
        SELECT
            state,
            county,
            high_count,
            total,
            ROUND(high_count / NULLIF(total, 0), 4) AS high_pct
        FROM per_county
        ORDER BY high_pct DESC, total DESC
        LIMIT {int(n)}
        """,
        scored_dir=scored_dir,
    )
