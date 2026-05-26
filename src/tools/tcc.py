"""
``fetch_tcc``: point-in-raster lookup for NLCD 2021 Tree Canopy Cover.

Returns a continuous canopy density (0–100 %) for a single WGS84 coordinate.
Used by the Environmental Data Agent (Phase 5) and exposed as the
``fetch_tcc`` Claude tool (Phase 7).

Design notes
------------
- **CRS-aware reprojection.** NLCD is stored in EPSG:5070 (Albers Equal
  Area for CONUS). Sampling at WGS84 coordinates without reprojection
  returns the *wrong* pixel. We build the transformer once from the
  dataset's own reported CRS so the tool works correctly even if the
  source CRS ever changes (e.g. a future MRLC release).
- **Cached file handle.** ``rasterio.open`` is genuinely expensive
  (parses headers, allocates buffers). At 1M call sites we cannot afford
  to reopen the GeoTIFF on every call. The handle is opened lazily on
  the first request and reused for the rest of the process; tests reset
  the cache via :func:`_reset_cache`.
- **``ds.sample`` instead of ``ds.read(1)[row, col]``.** ``read(1)``
  loads the entire band (~3 GB for NLCD CONUS) into memory. ``sample``
  reads only the single pixel that intersects the requested point.
  Documented as a deviation from the literal build-plan code in
  ``AI_TOOLS.md``.
- **NoData = 255.** Per NLCD's published documentation, the TCC layer
  uses 255 as its NoData sentinel. We also defer to the dataset's own
  declared ``nodata`` value if it differs.
- **Errors are returned, not raised.** A failure to sample one point
  must not crash a batch of 50. Every error path returns
  ``{"tcc_pct": None, "tcc_missing": True, "reason": ...}``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import rasterio
import rasterio.errors
from pyproj import Transformer

from src import config

# --- module-level cache (single open file handle per process) -----------------
_dataset: Optional["rasterio.io.DatasetReader"] = None
_dataset_path: Optional[Path] = None
_transformer: Optional[Transformer] = None

_TCC_NODATA_SENTINEL: int = 255   # NLCD-published TCC NoData value


def _find_tcc_raster() -> Optional[Path]:
    """Return the first NLCD TCC raster in ``config.TCC_DIR`` (or None)."""
    for pattern in ("*.tif", "*.tiff", "*.img"):
        for path in config.TCC_DIR.glob(pattern):
            return path
    return None


def _ensure_dataset() -> Optional["rasterio.io.DatasetReader"]:
    """Lazily open the TCC raster and the WGS84 → native-CRS transformer.

    Returns ``None`` if no raster is found (caller is expected to mark
    the location as missing).
    """
    global _dataset, _dataset_path, _transformer

    current = _find_tcc_raster()
    if current is None:
        return None

    if _dataset_path != current:
        # New file (or first call) — rebuild cache.
        if _dataset is not None:
            _dataset.close()
        _dataset = rasterio.open(current)
        _dataset_path = current
        native_crs = _dataset.crs.to_string() if _dataset.crs else config.ALBERS
        _transformer = Transformer.from_crs(config.WGS84, native_crs, always_xy=True)

    return _dataset


def _reset_cache() -> None:
    """Close any open dataset and clear the module cache.

    Tests use this to swap raster fixtures; the pipeline calls it from
    its shutdown handler so file descriptors don't leak.
    """
    global _dataset, _dataset_path, _transformer
    if _dataset is not None:
        _dataset.close()
    _dataset = None
    _dataset_path = None
    _transformer = None


def fetch_tcc(latitude: float, longitude: float) -> dict:
    """Return NLCD 2021 tree canopy cover (%) at the given WGS84 coordinate.

    Parameters
    ----------
    latitude, longitude:
        WGS84 (EPSG:4326) decimal degrees. CONUS bounds are not enforced
        here — that is the Ingestion Agent's job — but coordinates outside
        the raster footprint are returned as ``tcc_missing=True``.

    Returns
    -------
    dict
        Always contains ``tcc_pct`` and ``tcc_missing``. ``tcc_pct`` is an
        ``int`` in [0, 100] on success, ``None`` otherwise. ``reason`` is
        present whenever ``tcc_missing`` is ``True``.
    """
    dataset = _ensure_dataset()
    if dataset is None or _transformer is None:
        return {
            "tcc_pct": None,
            "tcc_missing": True,
            "reason": f"TCC raster not found in {config.TCC_DIR}",
        }

    try:
        x, y = _transformer.transform(longitude, latitude)
        sample = next(dataset.sample([(x, y)]))
        value_raw = sample[0]
    except (StopIteration, ValueError, rasterio.errors.RasterioError) as exc:
        return {"tcc_pct": None, "tcc_missing": True, "reason": str(exc)}

    # rasterio.sample returns numpy scalars; cast to native int for
    # downstream Pydantic / JSON serialisation. A sample read can also
    # return the raster's nodata value when the point falls outside any
    # tile, which would not raise.
    try:
        value = int(value_raw)
    except (TypeError, ValueError) as exc:
        return {"tcc_pct": None, "tcc_missing": True, "reason": f"non-numeric sample: {exc!r}"}

    nodata = dataset.nodata
    if value == _TCC_NODATA_SENTINEL or (nodata is not None and value == int(nodata)):
        return {"tcc_pct": None, "tcc_missing": True, "reason": "NoData pixel"}

    if not (0 <= value <= 100):
        return {
            "tcc_pct": None,
            "tcc_missing": True,
            "reason": f"TCC value {value} outside [0, 100]",
        }

    return {"tcc_pct": value, "tcc_missing": False}
