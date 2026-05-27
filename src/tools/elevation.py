"""
``fetch_elevation``: point-in-raster lookup for USGS 3DEP elevation, plus
slope (from the pre-computed slope raster) and aspect (computed per-point
via a 3x3 Horn window from the DEM).

Returns
-------
``{"elevation_m": float | None,
   "slope_deg": float | None,
   "aspect_deg": float | None,
   "elevation_missing": bool,
   "reason"?: str}``

Design notes
------------
- **Slope comes from the pre-computed raster** at ``config.SLOPE_RASTER_PATH``
  (see ``src.data.downloader.precompute_slope_raster``). Per-point Horn at
  1M sites would mean 1M 3x3 windowed reads; pre-computing once is ~100x
  faster.
- **Aspect is computed per-point.** Phase 2 only pre-computes slope, not
  aspect, so we read a 3x3 elevation window from the underlying DEM tile
  and run Horn's method on it. Aspect doesn't feed the composite risk
  score in the v3.0 formula; it's persisted into ``EnrichedLocation``
  alongside slope so downstream consumers (the validation tool, the
  per-county summary, future hemisphere-aware scoring) have it on hand.
  If aspect becomes a bottleneck at full CONUS scale we can lift the
  same Horn kernel into the downloader's pre-compute step.
- **DEM tile lookup.** USGS 3DEP 1 arc-second products are distributed
  as 1°x1° tiles. We build a one-time in-process bounds index on first
  call and pick the tile whose footprint contains the request point.
  Open dataset handles are LRU-capped at ``_DEM_HANDLE_LIMIT`` so we
  don't exhaust the OS file-descriptor table on multi-state runs.
- **CRS handling.** Each dataset's transformer is derived from its own
  ``crs`` field on first open, so the tool stays correct if a tile
  arrives in something other than the expected EPSG:4326. WGS84 input is
  reprojected per-dataset just once.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Optional

import numpy as np
import rasterio
import rasterio.errors
from pyproj import Transformer
from rasterio.coords import BoundingBox
from rasterio.windows import Window

from src import config

# --- Slope raster cache ------------------------------------------------------

_slope_dataset: Optional["rasterio.io.DatasetReader"] = None
_slope_path: Optional[Path] = None
_slope_transformer: Optional[Transformer] = None

# --- DEM tile index + LRU handle cache ---------------------------------------

# Built once per process: list of (tile_path, bounds_in_native_crs, native_crs_str).
_dem_index: list[tuple[Path, BoundingBox, str]] = []
_dem_index_built: bool = False

# Reused transformer per native CRS so we don't rebuild it on every call.
_dem_transformers: dict[str, Transformer] = {}

# LRU cache of open DEM handles. Cap is generous enough for normal use
# (49 CONUS states ~ a few hundred tiles total) while still bounding fd use.
_DEM_HANDLE_LIMIT: int = 16
_dem_handles: "OrderedDict[Path, rasterio.io.DatasetReader]" = OrderedDict()


# ---------------------------------------------------------------------------
# Cache lifecycle
# ---------------------------------------------------------------------------


def _reset_cache() -> None:
    """Close all open handles and clear every module-level cache."""
    global _slope_dataset, _slope_path, _slope_transformer
    global _dem_index, _dem_index_built, _dem_transformers

    if _slope_dataset is not None:
        _slope_dataset.close()
    _slope_dataset = None
    _slope_path = None
    _slope_transformer = None

    for handle in _dem_handles.values():
        handle.close()
    _dem_handles.clear()
    _dem_index = []
    _dem_index_built = False
    _dem_transformers = {}


# ---------------------------------------------------------------------------
# Slope sampling
# ---------------------------------------------------------------------------


def _ensure_slope_dataset() -> Optional["rasterio.io.DatasetReader"]:
    """Lazily open ``SLOPE_RASTER_PATH`` and build its transformer."""
    global _slope_dataset, _slope_path, _slope_transformer

    if not config.SLOPE_RASTER_PATH.exists():
        return None
    if _slope_path != config.SLOPE_RASTER_PATH:
        if _slope_dataset is not None:
            _slope_dataset.close()
        _slope_dataset = rasterio.open(config.SLOPE_RASTER_PATH)
        _slope_path = config.SLOPE_RASTER_PATH
        native_crs = (
            _slope_dataset.crs.to_string() if _slope_dataset.crs else config.WGS84
        )
        _slope_transformer = Transformer.from_crs(
            config.WGS84, native_crs, always_xy=True
        )
    return _slope_dataset


def _sample_slope(latitude: float, longitude: float) -> Optional[float]:
    """Sample the pre-computed slope raster at (lat, lon). Returns ``None``
    if the raster is absent, the point is outside its bounds, or the pixel
    is NoData."""
    ds = _ensure_slope_dataset()
    if ds is None or _slope_transformer is None:
        return None
    try:
        x, y = _slope_transformer.transform(longitude, latitude)
        value_raw = next(ds.sample([(x, y)]))[0]
    except (StopIteration, ValueError, rasterio.errors.RasterioError):
        return None
    value = float(value_raw)
    if ds.nodata is not None and value == float(ds.nodata):
        return None
    if np.isnan(value):
        return None
    return value


# ---------------------------------------------------------------------------
# DEM tile index + handle cache
# ---------------------------------------------------------------------------


def _build_dem_index() -> None:
    """Scan ``config.DEM_DIR`` once and record each tile's bounds + CRS."""
    global _dem_index, _dem_index_built
    if _dem_index_built:
        return
    found: list[tuple[Path, BoundingBox, str]] = []
    for path in sorted(config.DEM_DIR.glob("*.tif")) + sorted(
        config.DEM_DIR.glob("*.tiff")
    ):
        try:
            with rasterio.open(path) as src:
                crs_str = src.crs.to_string() if src.crs else config.WGS84
                found.append((path, src.bounds, crs_str))
        except rasterio.errors.RasterioError:
            continue
    _dem_index = found
    _dem_index_built = True


def _dem_transformer_for(crs_str: str) -> Transformer:
    if crs_str not in _dem_transformers:
        _dem_transformers[crs_str] = Transformer.from_crs(
            config.WGS84, crs_str, always_xy=True
        )
    return _dem_transformers[crs_str]


def _find_dem_tile(latitude: float, longitude: float) -> Optional[tuple[Path, str]]:
    """Return ``(path, native_crs_str)`` for the DEM tile that contains
    the given WGS84 point, or ``None``."""
    _build_dem_index()
    for path, bounds, crs_str in _dem_index:
        transformer = _dem_transformer_for(crs_str)
        x, y = transformer.transform(longitude, latitude)
        if bounds.left <= x <= bounds.right and bounds.bottom <= y <= bounds.top:
            return path, crs_str
    return None


def _get_dem_handle(path: Path) -> "rasterio.io.DatasetReader":
    """Return an LRU-cached DEM handle, evicting the oldest if at the cap."""
    if path in _dem_handles:
        _dem_handles.move_to_end(path)
        return _dem_handles[path]
    if len(_dem_handles) >= _DEM_HANDLE_LIMIT:
        oldest_path, oldest_handle = _dem_handles.popitem(last=False)
        oldest_handle.close()
    handle = rasterio.open(path)
    _dem_handles[path] = handle
    return handle


# ---------------------------------------------------------------------------
# Per-point Horn aspect
# ---------------------------------------------------------------------------


def _horn_aspect_from_window(
    window: np.ndarray, cellsize_x: float, cellsize_y: float
) -> Optional[float]:
    """Compute aspect (degrees, 0=N clockwise) for the centre of a 3x3
    elevation window via Horn (1981).

    Returns ``None`` for a flat 3x3 window (gradient is exactly zero
    everywhere — aspect is undefined) or if the window is not 3x3 (e.g.
    the requested point is at the raster's edge).
    """
    if window.shape != (3, 3):
        return None
    a, b, c = window[0]
    d, _, f = window[1]
    g, h, i = window[2]

    dzdx = ((c + 2.0 * f + i) - (a + 2.0 * d + g)) / (8.0 * cellsize_x)
    dzdy = ((g + 2.0 * h + i) - (a + 2.0 * b + c)) / (8.0 * cellsize_y)

    if dzdx == 0.0 and dzdy == 0.0:
        return None

    # Horn (1981) compass aspect: 0=N, 90=E, 180=S, 270=W. The math
    # convention from arctan2 gives 0=east counterclockwise — we rotate
    # and flip so the result is a familiar compass bearing.
    aspect_math_deg = float(np.degrees(np.arctan2(dzdy, -dzdx)))
    aspect_compass = 90.0 - aspect_math_deg
    if aspect_compass < 0.0:
        aspect_compass += 360.0
    if aspect_compass >= 360.0:
        aspect_compass -= 360.0
    return aspect_compass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_elevation(latitude: float, longitude: float) -> dict:
    """Return elevation (m), slope (deg), and aspect (deg) at a WGS84 point.

    Any field may be ``None`` independently:
        - ``elevation_m`` is ``None`` if no DEM tile contains the point.
        - ``slope_deg`` is ``None`` if the pre-computed slope raster is
          missing or the point lies on its NoData ring.
        - ``aspect_deg`` is ``None`` for a flat 3x3 window, when the
          point is in the DEM's 1-pixel boundary, or when the DEM tile
          itself is missing.

    ``elevation_missing`` is ``True`` only when the elevation field is
    ``None`` — slope is treated as an independent secondary signal.
    """
    slope_deg = _sample_slope(latitude, longitude)

    tile = _find_dem_tile(latitude, longitude)
    if tile is None:
        return {
            "elevation_m": None,
            "slope_deg": slope_deg,
            "aspect_deg": None,
            "elevation_missing": True,
            "reason": "No DEM tile contains the coordinate",
        }

    path, crs_str = tile
    transformer = _dem_transformer_for(crs_str)

    try:
        ds = _get_dem_handle(path)
        x, y = transformer.transform(longitude, latitude)

        # Elevation: single-pixel sample (fast, low-memory).
        elev_raw = next(ds.sample([(x, y)]))[0]
        elev = float(elev_raw)
        if ds.nodata is not None and elev == float(ds.nodata):
            elev = float("nan")

        # Aspect: 3x3 windowed read around the same pixel.
        row, col = ds.index(x, y)
        win = Window(col_off=col - 1, row_off=row - 1, width=3, height=3)
        window_data = ds.read(1, window=win, boundless=False).astype(np.float64)
        if ds.nodata is not None:
            window_data = np.where(
                window_data == float(ds.nodata), np.nan, window_data
            )
        if np.isnan(window_data).any():
            aspect_deg: Optional[float] = None
        else:
            cellsize_x = float(abs(ds.transform.a))
            cellsize_y = float(abs(ds.transform.e))
            aspect_deg = _horn_aspect_from_window(window_data, cellsize_x, cellsize_y)
    except (StopIteration, ValueError, rasterio.errors.RasterioError) as exc:
        return {
            "elevation_m": None,
            "slope_deg": slope_deg,
            "aspect_deg": None,
            "elevation_missing": True,
            "reason": str(exc),
        }

    if np.isnan(elev):
        return {
            "elevation_m": None,
            "slope_deg": slope_deg,
            "aspect_deg": aspect_deg,
            "elevation_missing": True,
            "reason": "NoData pixel",
        }

    return {
        "elevation_m": elev,
        "slope_deg": slope_deg,
        "aspect_deg": aspect_deg,
        "elevation_missing": False,
    }
