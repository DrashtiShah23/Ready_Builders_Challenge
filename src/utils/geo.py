"""Geospatial helpers for distance queries and CRS work.

Haversine distance is used for interactive-mode buffer search (Sample
Agentic Scenario 3): find nearby scored locations with lower obstruction
risk than a customer-supplied coordinate.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from src.agents.scoring import TIER_UNSCORED

# Mean Earth radius in metres (WGS84 spherical approximation).
_EARTH_RADIUS_M: float = 6_371_000.0

# Rows closer than this to the query point are treated as the same site.
_MIN_SEPARATION_M: float = 1.0


def haversine_meters(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    """Great-circle distance between two WGS84 points, in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _bbox_degrees(latitude: float, buffer_meters: float) -> tuple[float, float]:
    """Return ``(lat_min, lat_max)`` for a square buffer pre-filter.

    Longitude bounds are computed separately from the query longitude
    because they depend on latitude via ``cos(lat)``.
    """
    lat_delta = buffer_meters / 111_320.0
    return latitude - lat_delta, latitude + lat_delta


def find_better_alternatives(
    scored_path: Path,
    latitude: float,
    longitude: float,
    queried_risk_score: Optional[float],
    *,
    buffer_meters: float = 5_000.0,
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """Return up to ``top_n`` nearby scored locations with lower risk.

    Implements Sample Agentic Scenario 3: after scoring a customer
    coordinate, search the persisted scored parquet for alternatives
    within ``buffer_meters`` that have a strictly lower ``risk_score``
    (or any scored row if the query point itself is UNSCORED / has no
    score).

    Parameters
    ----------
    scored_path:
        Path to ``scored_locations.parquet`` (or any parquet with the
        standard scored columns).
    latitude, longitude:
        Customer query point in WGS84.
    queried_risk_score:
        Composite risk score at the query point. ``None`` or NaN means
        the query is UNSCORED — every scored neighbour qualifies as
        "better" and results are ranked by risk score then distance.
    buffer_meters:
        Search radius in metres (default 5 000 from ``config``).
    top_n:
        Maximum alternatives to return (default 3).

    Returns
    -------
    list[dict]
        Each dict has ``location_id``, ``latitude``, ``longitude``,
        ``county``, ``risk_score``, ``risk_tier``, ``distance_m``.
        Empty when the parquet is missing, empty, or no qualifying rows
        exist in the buffer.
    """
    if not scored_path.is_file():
        return []

    df = pd.read_parquet(
        scored_path,
        columns=[
            "location_id",
            "latitude",
            "longitude",
            "county",
            "risk_score",
            "risk_tier",
        ],
    )
    if df.empty:
        return []

    scored = df[
        (df["risk_tier"] != TIER_UNSCORED)
        & df["risk_score"].notna()
    ].copy()
    if scored.empty:
        return []

    lat_min, lat_max = _bbox_degrees(latitude, buffer_meters)
    cos_lat = max(math.cos(math.radians(latitude)), 1e-6)
    lon_delta = buffer_meters / (111_320.0 * cos_lat)
    lon_min = longitude - lon_delta
    lon_max = longitude + lon_delta

    candidates = scored[
        (scored["latitude"] >= lat_min)
        & (scored["latitude"] <= lat_max)
        & (scored["longitude"] >= lon_min)
        & (scored["longitude"] <= lon_max)
    ]
    if candidates.empty:
        return []

    lat_arr = candidates["latitude"].to_numpy(dtype=float)
    lon_arr = candidates["longitude"].to_numpy(dtype=float)
    dlat = np.radians(lat_arr - latitude)
    dlon = np.radians(lon_arr - longitude)
    phi1 = math.radians(latitude)
    phi2 = np.radians(lat_arr)
    a = (
        np.sin(dlat / 2) ** 2
        + np.cos(phi1) * np.cos(phi2) * np.sin(dlon / 2) ** 2
    )
    distances = 2 * _EARTH_RADIUS_M * np.arcsin(np.minimum(1.0, np.sqrt(a)))
    candidates = candidates.assign(distance_m=distances)

    candidates = candidates[candidates["distance_m"] <= buffer_meters]
    candidates = candidates[candidates["distance_m"] > _MIN_SEPARATION_M]
    if candidates.empty:
        return []

    if queried_risk_score is not None and not (
        isinstance(queried_risk_score, float) and math.isnan(queried_risk_score)
    ):
        candidates = candidates[candidates["risk_score"] < queried_risk_score]
        if candidates.empty:
            return []

    candidates = candidates.sort_values(
        ["risk_score", "distance_m"],
        ascending=[True, True],
    ).head(top_n)

    out: list[dict[str, Any]] = []
    for row in candidates.itertuples(index=False):
        county = row.county
        out.append(
            {
                "location_id": str(row.location_id),
                "latitude": float(row.latitude),
                "longitude": float(row.longitude),
                "county": None if pd.isna(county) else str(county),
                "risk_score": round(float(row.risk_score), 4),
                "risk_tier": str(row.risk_tier),
                "distance_m": round(float(row.distance_m), 1),
            }
        )
    return out
