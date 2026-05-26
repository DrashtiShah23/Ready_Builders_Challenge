"""
Phase 8 tests: Parquet state store + DuckDB query interface.

Tests use ``tmp_path`` as the store directory so every case starts from a
clean slate and nothing leaks between tests. ``config.SCORED_DIR`` is
monkeypatched per-test where the function under test reads the default.

Coverage:
* writer round-trips a DataFrame through Hive partitions
* reader reconstructs the ``state`` column from the partition path
* writer is overwrite-safe (default) and append-safe (overwrite=False)
* null state goes to the ``state=UNKNOWN/`` partition
* DuckDB query API: arbitrary SQL + the three pre-built aggregations
* empty store yields empty results, never raises
* sibling summary parquets in the same dir are not picked up as partitions
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src import config
from src.data import store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _scored_rows(n: int, state: str = "NC", tier: str = "Low") -> list[dict[str, Any]]:
    return [
        {
            "location_id": f"{state}-{tier}-{i}",
            "latitude": 35.5 + (i * 0.001),
            "longitude": -80.0 - (i * 0.001),
            "state": state,
            # Chunk in 50-row groups so a 30-50 row tier ends up in a
            # single county that passes the default top-counties
            # min_locations=25 filter.
            "county": f"{state[0]}_{tier[0]}{i // 50}",
            "tcc_pct": 50,
            "slope_deg": 5.0,
            "aspect_deg": 180.0,
            "land_cover_code": 42,
            "land_cover_class": "Evergreen Forest",
            "risk_score": {"High": 0.85, "Moderate": 0.45, "Low": 0.15}.get(tier, 0.15),
            "risk_tier": tier,
            "tcc_score": 1.0,
            "terrain_score": 0.0,
            "landcover_score": 1.0,
            "all_flags": [],
            "batch_id": "batch-000000",
        }
        for i in range(n)
    ]


@pytest.fixture
def store_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch ``config.SCORED_DIR`` to a fresh tmp dir and return it."""
    d = tmp_path / "scored"
    d.mkdir()
    monkeypatch.setattr(config, "SCORED_DIR", d)
    return d


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class TestWriter:
    def test_partitioned_layout_on_disk(self, store_dir: Path) -> None:
        rows = _scored_rows(5, state="NC", tier="High") + _scored_rows(
            3, state="CA", tier="Low"
        )
        df = pd.DataFrame(rows)

        store.write_scored_locations(df, scored_dir=store_dir)

        assert (store_dir / "state=NC").is_dir()
        assert (store_dir / "state=CA").is_dir()
        # Each partition contains at least one parquet file.
        assert any((store_dir / "state=NC").glob("*.parquet"))
        assert any((store_dir / "state=CA").glob("*.parquet"))

    def test_empty_df_writes_no_partitions(self, store_dir: Path) -> None:
        store.write_scored_locations(pd.DataFrame(), scored_dir=store_dir)
        assert not list(store_dir.glob("state=*"))

    def test_overwrite_default_clears_previous_partitions(
        self, store_dir: Path
    ) -> None:
        store.write_scored_locations(
            pd.DataFrame(_scored_rows(2, state="NC", tier="Low")),
            scored_dir=store_dir,
        )
        # Second write with a different state must remove the previous one.
        store.write_scored_locations(
            pd.DataFrame(_scored_rows(2, state="CA", tier="High")),
            scored_dir=store_dir,
        )
        assert not (store_dir / "state=NC").exists()
        assert (store_dir / "state=CA").exists()

    def test_append_keeps_previous_partitions(self, store_dir: Path) -> None:
        store.write_scored_locations(
            pd.DataFrame(_scored_rows(2, state="NC", tier="Low")),
            scored_dir=store_dir,
            overwrite=False,
        )
        store.write_scored_locations(
            pd.DataFrame(_scored_rows(2, state="CA", tier="High")),
            scored_dir=store_dir,
            overwrite=False,
        )
        assert (store_dir / "state=NC").exists()
        assert (store_dir / "state=CA").exists()

    def test_null_state_goes_to_unknown_partition(self, store_dir: Path) -> None:
        rows = _scored_rows(3, state="NC", tier="Low")
        rows[0]["state"] = None
        store.write_scored_locations(pd.DataFrame(rows), scored_dir=store_dir)
        assert (store_dir / "state=UNKNOWN").is_dir()

    def test_returns_target_path(self, store_dir: Path) -> None:
        ret = store.write_scored_locations(
            pd.DataFrame(_scored_rows(1)), scored_dir=store_dir
        )
        assert ret == store_dir


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class TestReader:
    def test_round_trip(self, store_dir: Path) -> None:
        rows = _scored_rows(5, state="NC", tier="High") + _scored_rows(
            3, state="CA", tier="Moderate"
        )
        store.write_scored_locations(pd.DataFrame(rows), scored_dir=store_dir)
        back = store.read_scored_locations(scored_dir=store_dir)

        assert len(back) == 8
        assert set(back["state"]) == {"NC", "CA"}
        # ``state`` is reconstructed from the partition path on read, so it
        # survives even when the writer dropped it from the row data.
        assert "state" in back.columns

    def test_empty_store_returns_empty_df(self, store_dir: Path) -> None:
        out = store.read_scored_locations(scored_dir=store_dir)
        assert out.empty

    def test_sibling_summary_parquets_are_ignored(self, store_dir: Path) -> None:
        # Place a non-partitioned parquet at the store root — DuckDB's glob
        # in store.py should not consider it part of the dataset.
        pd.DataFrame({"x": [1, 2]}).to_parquet(
            store_dir / "risk_summary_by_state.parquet", index=False
        )
        store.write_scored_locations(
            pd.DataFrame(_scored_rows(4, state="NC", tier="High")),
            scored_dir=store_dir,
        )

        back = store.read_scored_locations(scored_dir=store_dir)
        # 4 rows from the partition only — the 2 sibling rows must not appear.
        assert len(back) == 4
        assert "x" not in back.columns


# ---------------------------------------------------------------------------
# Scored ID retrieval (resumability)
# ---------------------------------------------------------------------------


class TestScoredLocationIds:
    def test_returns_distinct_ids(self, store_dir: Path) -> None:
        rows = _scored_rows(3, state="NC", tier="Low") + _scored_rows(
            2, state="CA", tier="High"
        )
        store.write_scored_locations(pd.DataFrame(rows), scored_dir=store_dir)

        ids = store.get_scored_location_ids(scored_dir=store_dir)
        assert isinstance(ids, set)
        assert len(ids) == 5
        assert "NC-Low-0" in ids
        assert "CA-High-1" in ids

    def test_empty_store_returns_empty_set(self, store_dir: Path) -> None:
        assert store.get_scored_location_ids(scored_dir=store_dir) == set()


# ---------------------------------------------------------------------------
# DuckDB query API
# ---------------------------------------------------------------------------


class TestQueries:
    @pytest.fixture
    def populated_store(self, store_dir: Path) -> Path:
        rows = (
            _scored_rows(30, state="NC", tier="High")
            + _scored_rows(50, state="NC", tier="Moderate")
            + _scored_rows(20, state="NC", tier="Low")
            + _scored_rows(10, state="CA", tier="High")
            + _scored_rows(40, state="CA", tier="Low")
        )
        store.write_scored_locations(pd.DataFrame(rows), scored_dir=store_dir)
        return store_dir

    def test_query_arbitrary_sql(self, populated_store: Path) -> None:
        df = store.query(
            "SELECT COUNT(*) AS n FROM scored",
            scored_dir=populated_store,
        )
        assert df.iloc[0]["n"] == 150

    def test_get_risk_distribution(self, populated_store: Path) -> None:
        dist = store.get_risk_distribution(scored_dir=populated_store)
        assert set(dist["risk_tier"]) == {"High", "Moderate", "Low"}
        by_tier = {row["risk_tier"]: row for _, row in dist.iterrows()}
        assert by_tier["High"]["count"] == 40
        assert by_tier["Moderate"]["count"] == 50
        assert by_tier["Low"]["count"] == 60
        # Ordering: High first.
        assert dist.iloc[0]["risk_tier"] == "High"
        # Pct sums to ~1.0 across all tiers.
        assert dist["pct"].sum() == pytest.approx(1.0, abs=1e-6)

    def test_get_state_breakdown(self, populated_store: Path) -> None:
        br = store.get_state_breakdown(scored_dir=populated_store)
        assert set(br["state"]) == {"NC", "CA"}
        by_state = {row["state"]: row for _, row in br.iterrows()}
        assert by_state["NC"]["high_count"] == 30
        assert by_state["NC"]["moderate_count"] == 50
        assert by_state["NC"]["low_count"] == 20
        assert by_state["NC"]["total"] == 100
        assert by_state["NC"]["high_pct"] == pytest.approx(0.30, abs=1e-4)
        assert by_state["CA"]["high_count"] == 10
        assert by_state["CA"]["total"] == 50
        # Ordering: highest high_pct first.
        assert br.iloc[0]["state"] == "NC"

    def test_get_top_at_risk_counties(self, populated_store: Path) -> None:
        # Default min_locations=25 — each of our 5 county slots has 30+ rows.
        top = store.get_top_at_risk_counties(scored_dir=populated_store, n=5)
        assert len(top) > 0
        assert {"state", "county", "high_count", "total", "high_pct"} <= set(top.columns)
        # Sorted descending by high_pct.
        assert top["high_pct"].is_monotonic_decreasing or len(top) == 1

    def test_top_counties_min_locations_filter(self, store_dir: Path) -> None:
        # Tiny county (2 rows, both High) must be filtered out when
        # min_locations defaults to 25.
        store.write_scored_locations(
            pd.DataFrame(_scored_rows(2, state="NC", tier="High")),
            scored_dir=store_dir,
        )
        top = store.get_top_at_risk_counties(scored_dir=store_dir, n=5)
        assert top.empty

    def test_top_counties_min_locations_param_override(
        self, store_dir: Path
    ) -> None:
        store.write_scored_locations(
            pd.DataFrame(_scored_rows(2, state="NC", tier="High")),
            scored_dir=store_dir,
        )
        top = store.get_top_at_risk_counties(
            scored_dir=store_dir, n=5, min_locations=1
        )
        assert len(top) >= 1

    def test_query_on_empty_store_returns_empty_df(self, store_dir: Path) -> None:
        # Empty-store path must not raise — report generator should be able
        # to call this on a clean repo without guarding.
        out = store.query("SELECT COUNT(*) AS n FROM scored", scored_dir=store_dir)
        assert out.iloc[0]["n"] == 0

    def test_risk_distribution_on_empty_store(self, store_dir: Path) -> None:
        out = store.get_risk_distribution(scored_dir=store_dir)
        assert out.empty


# ---------------------------------------------------------------------------
# Default-dir resolution
# ---------------------------------------------------------------------------


class TestDefaultDir:
    def test_default_dir_uses_config_scored_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        new_dir = tmp_path / "alt"
        new_dir.mkdir()
        monkeypatch.setattr(config, "SCORED_DIR", new_dir)
        store.write_scored_locations(
            pd.DataFrame(_scored_rows(2, state="NC"))
        )  # no scored_dir arg
        assert (new_dir / "state=NC").exists()
