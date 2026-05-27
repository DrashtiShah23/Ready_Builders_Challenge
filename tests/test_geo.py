"""Tests for geospatial buffer search (Sample Agentic Scenario 3)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.agents.scoring import TIER_HIGH, TIER_LOW, TIER_MODERATE, TIER_UNSCORED
from src.utils.geo import find_better_alternatives, haversine_meters


def test_haversine_meters_same_point_is_zero() -> None:
    assert haversine_meters(35.0, -80.0, 35.0, -80.0) == 0.0


def test_find_better_alternatives_returns_lower_risk_within_buffer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scored.parquet"
    pd.DataFrame(
        [
            {
                "location_id": "near_low",
                "latitude": 35.001,
                "longitude": -80.001,
                "county": "37001",
                "risk_score": 0.3,
                "risk_tier": TIER_MODERATE,
            },
            {
                "location_id": "near_high",
                "latitude": 35.002,
                "longitude": -80.002,
                "county": "37001",
                "risk_score": 0.9,
                "risk_tier": TIER_HIGH,
            },
            {
                "location_id": "far_low",
                "latitude": 36.0,
                "longitude": -80.0,
                "county": "37001",
                "risk_score": 0.1,
                "risk_tier": TIER_LOW,
            },
        ]
    ).to_parquet(path)

    alts = find_better_alternatives(
        path,
        35.0,
        -80.0,
        0.8,
        buffer_meters=5_000.0,
        top_n=3,
    )
    assert len(alts) == 1
    assert alts[0]["location_id"] == "near_low"
    assert alts[0]["risk_score"] < 0.8
    assert alts[0]["distance_m"] > 0


def test_find_better_alternatives_respects_top_n(tmp_path: Path) -> None:
    path = tmp_path / "scored.parquet"
    rows = []
    for i in range(5):
        rows.append(
            {
                "location_id": f"alt-{i}",
                "latitude": 35.0 + (i + 1) * 0.0001,
                "longitude": -80.0,
                "county": "37001",
                "risk_score": 0.1 + i * 0.01,
                "risk_tier": TIER_LOW,
            }
        )
    pd.DataFrame(rows).to_parquet(path)

    alts = find_better_alternatives(
        path,
        35.0,
        -80.0,
        0.9,
        buffer_meters=5_000.0,
        top_n=3,
    )
    assert len(alts) == 3
    scores = [a["risk_score"] for a in alts]
    assert scores == sorted(scores)


def test_find_better_alternatives_missing_file_returns_empty(tmp_path: Path) -> None:
    assert (
        find_better_alternatives(
            tmp_path / "missing.parquet",
            35.0,
            -80.0,
            0.5,
        )
        == []
    )


def test_find_better_alternatives_excludes_unscored(tmp_path: Path) -> None:
    path = tmp_path / "scored.parquet"
    pd.DataFrame(
        [
            {
                "location_id": "unscored",
                "latitude": 35.001,
                "longitude": -80.0,
                "county": "37001",
                "risk_score": None,
                "risk_tier": TIER_UNSCORED,
            },
        ]
    ).to_parquet(path)
    assert find_better_alternatives(path, 35.0, -80.0, 0.5) == []


def test_find_better_alternatives_unscored_query_returns_any_scored(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scored.parquet"
    pd.DataFrame(
        [
            {
                "location_id": "low",
                "latitude": 35.001,
                "longitude": -80.0,
                "county": "37001",
                "risk_score": 0.2,
                "risk_tier": TIER_LOW,
            },
        ]
    ).to_parquet(path)
    alts = find_better_alternatives(path, 35.0, -80.0, None, buffer_meters=5_000.0)
    assert len(alts) == 1
    assert alts[0]["location_id"] == "low"
