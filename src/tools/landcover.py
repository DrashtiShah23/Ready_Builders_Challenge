"""
``fetch_land_cover``: point-in-raster lookup for NLCD 2021 Land Cover.

Returns the raw NLCD class code (integer) plus a human-readable class
name (e.g. "Evergreen Forest", "Developed, Low Intensity"). Used by the
Environmental Data Agent (Phase 5) alongside ``fetch_tcc``. Not exposed
as a Claude tool — the Phase 7 redesign moved Claude's tool surface up
to five pipeline-level tools, so ``fetch_land_cover`` is now called
inside ``sample_environment``'s Python loop and the resulting
``land_cover_code`` flows downstream as a plain integer column on the
``EnrichedLocation`` parquet that ``score_risk`` consumes.

Design notes
------------
- **Same CRS family as TCC** (EPSG:5070 Albers). Reusing the same
  reprojection pattern keeps the two tools symmetric and means no extra
  pyproj configuration.
- **Class-name lookup lives in ``config.NLCD_CLASS_NAMES``** so the full
  16-code mapping is auditable in one place. Unknown codes fall back to
  the string ``"Unknown (<code>)"`` rather than ``None`` — downstream
  code can still log the raw value.
- **Same cached-dataset + ``ds.sample`` pattern as ``tcc.py``** for the
  same reasons (file-handle reuse, single-pixel reads instead of
  full-band loads).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import rasterio
import rasterio.errors
from pyproj import Transformer

from src import config

_dataset: Optional["rasterio.io.DatasetReader"] = None
_dataset_path: Optional[Path] = None
_transformer: Optional[Transformer] = None


def _find_landcover_raster() -> Optional[Path]:
    """Return the first NLCD Land Cover raster in ``config.LC_DIR`` (or None)."""
    for pattern in ("*.tif", "*.tiff", "*.img"):
        for path in config.LC_DIR.glob(pattern):
            return path
    return None


def _ensure_dataset() -> Optional["rasterio.io.DatasetReader"]:
    global _dataset, _dataset_path, _transformer

    current = _find_landcover_raster()
    if current is None:
        return None

    if _dataset_path != current:
        if _dataset is not None:
            _dataset.close()
        _dataset = rasterio.open(current)
        _dataset_path = current
        native_crs = _dataset.crs.to_string() if _dataset.crs else config.ALBERS
        _transformer = Transformer.from_crs(config.WGS84, native_crs, always_xy=True)

    return _dataset


def _reset_cache() -> None:
    global _dataset, _dataset_path, _transformer
    if _dataset is not None:
        _dataset.close()
    _dataset = None
    _dataset_path = None
    _transformer = None


def _classify(code: int) -> str:
    """Map an NLCD code to a human-readable class name."""
    return config.NLCD_CLASS_NAMES.get(code, f"Unknown ({code})")


def fetch_land_cover(latitude: float, longitude: float) -> dict:
    """Return the NLCD 2021 land cover code and class name at a WGS84 point.

    Returns
    -------
    dict
        ``{"land_cover_code": int | None, "land_cover_class": str,
           "lc_missing": bool, "reason"?: str}``. ``land_cover_class`` is
        always populated — either the resolved class name or a
        placeholder so the field is never null in JSON.
    """
    dataset = _ensure_dataset()
    if dataset is None or _transformer is None:
        return {
            "land_cover_code": None,
            "land_cover_class": "UNKNOWN",
            "lc_missing": True,
            "reason": f"Land Cover raster not found in {config.LC_DIR}",
        }

    try:
        x, y = _transformer.transform(longitude, latitude)
        sample = next(dataset.sample([(x, y)]))
        value_raw = sample[0]
    except (StopIteration, ValueError, rasterio.errors.RasterioError) as exc:
        return {
            "land_cover_code": None,
            "land_cover_class": "UNKNOWN",
            "lc_missing": True,
            "reason": str(exc),
        }

    try:
        code = int(value_raw)
    except (TypeError, ValueError) as exc:
        return {
            "land_cover_code": None,
            "land_cover_class": "UNKNOWN",
            "lc_missing": True,
            "reason": f"non-numeric sample: {exc!r}",
        }

    nodata = dataset.nodata
    if nodata is not None and code == int(nodata):
        return {
            "land_cover_code": None,
            "land_cover_class": "UNKNOWN",
            "lc_missing": True,
            "reason": "NoData pixel",
        }

    # NLCD codes are in the published legend (11, 12, 21–24, 31, 41–43,
    # 52, 71, 81–82, 90, 95). Codes outside that set are still returned
    # raw — with an "Unknown (<code>)" label — so the data isn't dropped
    # silently, but downstream scoring can ignore them safely.
    return {
        "land_cover_code": code,
        "land_cover_class": _classify(code),
        "lc_missing": False,
    }
