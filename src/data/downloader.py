"""
Reproducible, idempotent downloader for every environmental dataset the
pipeline depends on.

Datasets
--------
- **NLCD 2021 Tree Canopy Cover** — fetched as a per-state GeoTIFF subset
  from MRLC's WCS service (``config.MRLC_TCC_COVERAGE_ID``) →
  ``data/raw/tcc/``.
- **NLCD 2021 Land Cover** — fetched as a per-state GeoTIFF subset from
  MRLC's WCS service (``config.MRLC_LANDCOVER_COVERAGE_ID``) →
  ``data/raw/landcover/``.
- **USGS 3DEP 1 arc-second elevation tiles** — fetched per-state via the
  USGS National Map (TNM) ``products?bbox=...`` endpoint → ``data/raw/dem/``.
- **Pre-computed slope raster** (Horn's method, degrees) at
  ``config.SLOPE_RASTER_PATH`` derived from the downloaded DEM tiles.

Why WCS for NLCD instead of bulk S3 zips
----------------------------------------
The original implementation pinned ``CANOPY_RASTER_URL`` and
``LANDCOVER_RASTER_URL`` to MRLC's S3 bucket
(``s3-us-west-2.amazonaws.com/mrlc/...``). Those URLs returned HTTP 403
``AccessDenied`` in May 2026 — the bucket no longer permits anonymous bulk
zip downloads. Rather than chase another zip mirror that could break in the
same way, the downloader was switched to MRLC's WCS service. Three
practical wins from the migration:

1. **State-shaped payloads instead of a national 3 GB pull.** A WCS
   GetCoverage with the NC bbox returns ~50-150 MB per coverage instead of
   the 3 GB national TIFF. For a single-state run we now download 4 % of
   the data we used to.
2. **No zip-extraction step.** WCS returns a TIFF directly; the
   ``_extract_zip`` helper is gone.
3. **No URL-on-S3 fragility.** WCS is the documented OGC interface MRLC's
   own viewer is built on, so it's the least-likely-to-disappear path.

Why bbox for DEM instead of polyCode
------------------------------------
The TNM API's ``polyType=state&polyCode=<FIPS>`` filter is currently
non-functional — ``polyCode=37`` (NC) returns ~200 tiles, all in
Oregon/Idaho. The bbox filter still works and is what TNM's own viewer
uses internally. State-to-bbox mapping lives in
``config.STATE_BBOX_WGS84``; the downloader transforms the bbox once per
call and queries the same first-page-and-dedupe path the TNM viewer uses.

Design decisions
----------------
- **Idempotent.** Every function checks for its output before doing any
  work. Reruns are safe (and cheap).
- **Streaming downloads.** GeoTIFF subsets can still be hundreds of
  megabytes, so we stream chunks with ``httpx`` instead of buffering whole
  responses in memory, and we drive a ``tqdm`` progress bar from the
  ``Content-Length`` header when present (WCS often omits it under
  chunked encoding; tqdm degrades to "unknown total" gracefully).
- **Atomic writes.** Files are written to ``<name>.part`` and renamed only
  after a successful download to avoid leaving truncated artifacts on
  Ctrl+C or network failure.
- **Pre-computed slope raster.** Horn's-method slope per-point at 1M
  locations would mean 1M 3x3 windowed rasterio reads. Pre-computing once
  collapses that to 1 GDAL operation + 1M cheap pixel lookups. ~100x
  faster, and it eliminates an entire class of raster-boundary edge cases
  that the per-point version would have to handle.
- **No hardcoded URLs / state codes / dataset names.** Everything is
  imported from :mod:`src.config`.

CLI
---
::

    python -m src.data.downloader --states NC          # NC only (default scope)
    python -m src.data.downloader --states NC TX       # multiple states
    python -m src.data.downloader --skip-dem           # smoke-test raster downloads only

All operations log structured JSONL events via :class:`src.utils.logger.PipelineLogger`.
"""
from __future__ import annotations

import argparse
import time
import uuid
from pathlib import Path
from typing import Iterable, Optional

import httpx
import numpy as np
from tqdm import tqdm

from src import config
from src.utils.logger import PipelineLogger

# ---------------------------------------------------------------------------
# Shared HTTP helpers
# ---------------------------------------------------------------------------

_HTTP_TIMEOUT = httpx.Timeout(connect=30.0, read=600.0, write=60.0, pool=30.0)
_CHUNK_BYTES = 1 << 20  # 1 MiB chunks for streamed downloads


def _stream_download(
    url: str,
    dest: Path,
    logger: PipelineLogger,
    desc: str,
    params: Optional[dict] = None,
) -> Path:
    """Stream ``url`` (optionally with ``params``) to ``dest``.

    Writes to ``dest.with_suffix(dest.suffix + ".part")`` and atomically
    renames on success so a partial file never appears at ``dest``. The
    ``params`` kwarg is what lets the WCS callers issue a GetCoverage
    against the same primitive without rebuilding the URL.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest.with_suffix(dest.suffix + ".part")
    if part_path.exists():
        part_path.unlink()

    start = time.monotonic()
    bytes_written = 0
    with httpx.stream(
        "GET", url, params=params, timeout=_HTTP_TIMEOUT, follow_redirects=True
    ) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length", "0")) or None
        with open(part_path, "wb") as out_f, tqdm(
            total=total, unit="B", unit_scale=True, desc=desc, leave=False
        ) as pbar:
            for chunk in resp.iter_bytes(chunk_size=_CHUNK_BYTES):
                if not chunk:
                    continue
                out_f.write(chunk)
                bytes_written += len(chunk)
                pbar.update(len(chunk))

    part_path.replace(dest)
    elapsed_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        stage="downloader",
        event_type="HTTP_DOWNLOAD_DONE",
        detail={"url": url, "dest": str(dest), "bytes": bytes_written},
        duration_ms=elapsed_ms,
    )
    return dest


def _raster_already_present(dest_dir: Path) -> Optional[Path]:
    """Return the first GeoTIFF/IMG in ``dest_dir``, or None.

    Used as the idempotency sentinel: if the destination directory already
    contains a raster the dataset is considered fully downloaded.
    """
    for ext in ("*.tif", "*.tiff", "*.img"):
        for path in dest_dir.glob(ext):
            return path
    return None


# ---------------------------------------------------------------------------
# Bbox helpers (WGS84 ↔ EPSG:5070)
# ---------------------------------------------------------------------------


def _states_to_bbox_wgs84(
    states: Iterable[str],
) -> tuple[float, float, float, float]:
    """Return the union of state bboxes in WGS84 (lon_min, lat_min, lon_max,
    lat_max).

    Used to size a single WCS GetCoverage / TNM bbox request that covers
    every requested state. NC-only is the default; multi-state callers get
    a single bounding rectangle around the union.
    """
    bboxes: list[tuple[float, float, float, float]] = []
    for s in states:
        bbox = config.STATE_BBOX_WGS84.get(s.upper())
        if bbox is None:
            raise ValueError(
                f"No bounding box configured for state '{s}'. "
                f"Add it to config.STATE_BBOX_WGS84 first. "
                f"Currently configured: {sorted(config.STATE_BBOX_WGS84.keys())}"
            )
        bboxes.append(bbox)
    if not bboxes:
        raise ValueError("At least one state must be supplied to derive a bbox.")
    lon_min = min(b[0] for b in bboxes)
    lat_min = min(b[1] for b in bboxes)
    lon_max = max(b[2] for b in bboxes)
    lat_max = max(b[3] for b in bboxes)
    return lon_min, lat_min, lon_max, lat_max


def _project_bbox_to_5070(
    bbox_wgs84: tuple[float, float, float, float],
    buffer_m: float = 60.0,
) -> tuple[float, float, float, float]:
    """Project a WGS84 (lon_min, lat_min, lon_max, lat_max) bbox to
    EPSG:5070 (Conus Albers, metres).

    Conus Albers is *not* axis-aligned with the WGS84 graticule, so we
    project all four corners and take the min/max in each axis to get the
    smallest axis-aligned rectangle in 5070 that fully contains the WGS84
    rectangle. A small buffer (default one 30m NLCD pixel) is added so
    edge pixels are never lost to off-by-one boundary effects.
    """
    from pyproj import Transformer  # lazy import — pyproj is heavy

    tfm = Transformer.from_crs("EPSG:4326", config.NLCD_RASTER_CRS, always_xy=True)
    lon_min, lat_min, lon_max, lat_max = bbox_wgs84
    corners = [
        (lon_min, lat_min),
        (lon_max, lat_min),
        (lon_max, lat_max),
        (lon_min, lat_max),
    ]
    projected = [tfm.transform(lon, lat) for lon, lat in corners]
    xs = [p[0] for p in projected]
    ys = [p[1] for p in projected]
    return (
        min(xs) - buffer_m,
        min(ys) - buffer_m,
        max(xs) + buffer_m,
        max(ys) + buffer_m,
    )


# ---------------------------------------------------------------------------
# WCS coverage fetch (TCC + Land Cover)
# ---------------------------------------------------------------------------


def _download_wcs_coverage(
    coverage_id: str,
    bbox_wgs84: tuple[float, float, float, float],
    dest: Path,
    logger: PipelineLogger,
    desc: str,
) -> Path:
    """Issue a WCS 2.0.1 GetCoverage and stream the response to ``dest``.

    Builds the EPSG:5070 subset from the supplied WGS84 bbox, then calls
    the shared ``_stream_download`` helper. Returns the final TIFF path.
    """
    x_min, y_min, x_max, y_max = _project_bbox_to_5070(bbox_wgs84)
    # WCS 2.0.1 accepts multiple ``subset`` parameters in the same query.
    # httpx serialises a list value as repeated keys, which is what the
    # spec calls for.
    params = {
        "service": "WCS",
        "version": config.MRLC_WCS_VERSION,
        "request": "GetCoverage",
        "coverageid": coverage_id,
        "subset": [f"X({x_min:.0f},{x_max:.0f})", f"Y({y_min:.0f},{y_max:.0f})"],
        "format": "image/tiff",
    }
    logger.info(
        stage="downloader",
        event_type="WCS_GETCOVERAGE_START",
        detail={
            "coverage_id": coverage_id,
            "bbox_wgs84": list(bbox_wgs84),
            "bbox_5070": [round(v, 1) for v in (x_min, y_min, x_max, y_max)],
        },
    )
    return _stream_download(
        config.MRLC_WCS_BASE, dest, logger, desc=desc, params=params
    )


# ---------------------------------------------------------------------------
# NLCD Tree Canopy Cover
# ---------------------------------------------------------------------------


def download_tcc(
    states: Optional[Iterable[str]] = None,
    logger: Optional[PipelineLogger] = None,
) -> Path:
    """Download a per-state TCC subset via MRLC WCS.

    Idempotent: returns the existing raster path if any ``.tif`` is already
    present under ``config.TCC_DIR``. Use ``states`` to control the bbox;
    defaults to every state in ``config.STATE_BBOX_WGS84`` (NC today).
    """
    logger = logger or _default_logger()
    existing = _raster_already_present(config.TCC_DIR)
    if existing is not None:
        logger.info(
            stage="downloader",
            event_type="TCC_SKIP_EXISTS",
            detail={"path": str(existing)},
        )
        return existing

    state_list = list(states) if states else sorted(config.STATE_BBOX_WGS84.keys())
    bbox = _states_to_bbox_wgs84(state_list)
    config.TCC_DIR.mkdir(parents=True, exist_ok=True)
    # File name encodes the coverage id + the requested states so a future
    # reviewer can tell at a glance which subset is on disk. Single-state
    # files are titled ``...NC.tif``; multi-state ones get ``..._NC_TX.tif``.
    suffix = "_".join(state_list)
    dest = config.TCC_DIR / f"nlcd_tcc_conus_2021_v2021-4__{suffix}.tif"

    logger.info(
        stage="downloader",
        event_type="TCC_DOWNLOAD_START",
        detail={"coverage_id": config.MRLC_TCC_COVERAGE_ID, "states": state_list},
    )
    _download_wcs_coverage(
        config.MRLC_TCC_COVERAGE_ID,
        bbox,
        dest,
        logger,
        desc="NLCD TCC (WCS)",
    )
    logger.info(
        stage="downloader",
        event_type="TCC_DOWNLOAD_DONE",
        detail={"path": str(dest)},
    )
    return dest


# ---------------------------------------------------------------------------
# NLCD Land Cover
# ---------------------------------------------------------------------------


def download_landcover(
    states: Optional[Iterable[str]] = None,
    logger: Optional[PipelineLogger] = None,
) -> Path:
    """Download a per-state Land Cover subset via MRLC WCS.

    Same source family as TCC (USGS / MRLC), same CRS (EPSG:5070), same
    resolution (30 m), same WCS endpoint. Idempotent.
    """
    logger = logger or _default_logger()
    existing = _raster_already_present(config.LC_DIR)
    if existing is not None:
        logger.info(
            stage="downloader",
            event_type="LANDCOVER_SKIP_EXISTS",
            detail={"path": str(existing)},
        )
        return existing

    state_list = list(states) if states else sorted(config.STATE_BBOX_WGS84.keys())
    bbox = _states_to_bbox_wgs84(state_list)
    config.LC_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "_".join(state_list)
    dest = config.LC_DIR / f"NLCD_2021_Land_Cover_L48__{suffix}.tif"

    logger.info(
        stage="downloader",
        event_type="LANDCOVER_DOWNLOAD_START",
        detail={
            "coverage_id": config.MRLC_LANDCOVER_COVERAGE_ID,
            "states": state_list,
        },
    )
    _download_wcs_coverage(
        config.MRLC_LANDCOVER_COVERAGE_ID,
        bbox,
        dest,
        logger,
        desc="NLCD LC (WCS)",
    )
    logger.info(
        stage="downloader",
        event_type="LANDCOVER_DOWNLOAD_DONE",
        detail={"path": str(dest)},
    )
    return dest


# ---------------------------------------------------------------------------
# USGS 3DEP DEM tiles (bbox query)
# ---------------------------------------------------------------------------


_TNM_QUERY_MAX_ATTEMPTS: int = 5
_TNM_QUERY_BACKOFF_SECONDS: float = 3.0


def _tnm_query_bbox(
    bbox_wgs84: tuple[float, float, float, float],
    *,
    _client: Optional[httpx.Client] = None,
    _max_attempts: int = _TNM_QUERY_MAX_ATTEMPTS,
    _backoff_seconds: float = _TNM_QUERY_BACKOFF_SECONDS,
    _treat_empty_as_transient: bool = True,
) -> list[dict]:
    """Query the USGS TNM API for all 3DEP 1 arc-second tiles that
    intersect ``bbox_wgs84`` (lon_min, lat_min, lon_max, lat_max).

    Replaces the broken ``polyType=state`` path. The downloader requests
    ``max=config.TNM_PAGE_SIZE`` (default 200) in a single shot —
    server-side pagination via ``offset`` is currently broken
    (``total=0`` after the first page), and TNM's bbox endpoint will
    happily serve a couple hundred items in one response, which is enough
    to cover any single CONUS state's ~30-50 1° quads with their multiple
    vintages. The caller dedupes by 1° quad below.

    TNM has three observed flavors of transient failure:
      1. HTTP 5xx (gateway timeouts, Lambda errors) — caught by
         ``raise_for_status``.
      2. HTTP 200 with a non-JSON body (Python repr, HTML error pages) —
         caught by ``ValueError`` (``json.JSONDecodeError`` is a
         ``ValueError``).
      3. HTTP 200 with valid JSON but ``items: []`` — *also* transient
         in our experience: the same bbox immediately returns 50 results
         on a retry. This is what ``_treat_empty_as_transient`` covers.
         If a caller has a legitimately empty bbox they can disable it.

    All three retry up to ``_max_attempts`` times with a backoff between
    attempts. Empty-result retries log + return `[]` if they exhaust the
    budget, since "TNM still says no results" is the only signal we have.

    Returns the raw ``items`` list from the JSON response.
    """
    lon_min, lat_min, lon_max, lat_max = bbox_wgs84
    params = {
        "datasets": config.TNM_DEM_DATASET,
        "bbox": f"{lon_min},{lat_min},{lon_max},{lat_max}",
        "prodFormats": "GeoTIFF",
        "outputFormat": "JSON",
        "max": config.TNM_PAGE_SIZE,
    }
    client = _client or httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True)
    try:
        last_exc: Optional[BaseException] = None
        for attempt in range(1, _max_attempts + 1):
            try:
                resp = client.get(config.TNM_API_BASE, params=params)
                resp.raise_for_status()
                payload = resp.json()
            except (ValueError, httpx.HTTPError) as exc:
                last_exc = exc
                if attempt < _max_attempts:
                    # Linear backoff is enough — TNM glitches are
                    # second-scale, not minute-scale.
                    time.sleep(_backoff_seconds)
                    continue
                raise
            items = payload.get("items", []) or []
            if items or not _treat_empty_as_transient or attempt == _max_attempts:
                return items
            # Empty result on a transient-retry attempt — wait and retry.
            time.sleep(_backoff_seconds)
        # Loop exited via the empty-list path on the final attempt.
        return []
    finally:
        if _client is None:
            client.close()


def _dedupe_tiles_latest_vintage(items: list[dict]) -> list[dict]:
    """USGS 3DEP serves multiple vintages for the same 1° quad
    (e.g. ``n34w079 20250507`` and ``n34w079 20260320``). Pick the latest
    vintage per (round(min_lat), round(min_lon)) so the mosaic doesn't
    double-stack on overlapping cells.

    Tile titles follow ``USGS 1 Arc Second n34w079 YYYYMMDD``; the date
    is the last whitespace-separated token. We compare those tokens
    lexicographically — sufficient because they're ISO-8601 dates.
    """
    by_quad: dict[tuple[int, int], dict] = {}
    for it in items:
        bb = it.get("boundingBox", {}) or {}
        try:
            key = (int(round(bb["minY"])), int(round(bb["minX"])))
        except (KeyError, TypeError, ValueError):
            continue
        title = it.get("title", "") or ""
        new_date_token = title.split()[-1] if title else ""
        existing = by_quad.get(key)
        if existing is None:
            by_quad[key] = it
            continue
        existing_title = existing.get("title", "") or ""
        existing_date_token = existing_title.split()[-1] if existing_title else ""
        if new_date_token > existing_date_token:
            by_quad[key] = it
    return list(by_quad.values())


def download_dem_tiles(
    states: Optional[Iterable[str]] = None,
    logger: Optional[PipelineLogger] = None,
) -> list[Path]:
    """Download USGS 3DEP 1 arc-second DEM tiles for the requested states.

    Idempotent at the *tile* level: each tile is skipped if its file
    already exists in ``config.DEM_DIR``. The bbox query that drives the
    catalogue lookup is the union of the states' bboxes from
    ``config.STATE_BBOX_WGS84``.

    Parameters
    ----------
    states:
        Iterable of state abbreviations (e.g. ``["NC"]``). If ``None``,
        defaults to every state configured in ``config.STATE_BBOX_WGS84``.

    Returns
    -------
    list[Path]
        All tile paths now present on disk for the requested states (both
        newly downloaded and previously cached).
    """
    logger = logger or _default_logger()
    state_list = list(states) if states else sorted(config.STATE_BBOX_WGS84.keys())
    bbox = _states_to_bbox_wgs84(state_list)
    logger.info(
        stage="downloader",
        event_type="DEM_DOWNLOAD_START",
        detail={"states": state_list, "bbox_wgs84": list(bbox)},
    )

    tiles: list[Path] = []
    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        try:
            items = _tnm_query_bbox(bbox, _client=client)
        except (httpx.HTTPError, ValueError) as exc:
            logger.error(
                stage="downloader",
                event_type="DEM_BBOX_QUERY_FAILED",
                detail={"bbox_wgs84": list(bbox), "error": str(exc)},
            )
            return tiles

        deduped = _dedupe_tiles_latest_vintage(items)
        logger.info(
            stage="downloader",
            event_type="DEM_BBOX_QUERY_OK",
            detail={
                "bbox_wgs84": list(bbox),
                "raw_count": len(items),
                "deduped_count": len(deduped),
            },
        )

        for item in deduped:
            url = item.get("downloadURL") or (item.get("urls") or {}).get("TIFF")
            if not url:
                logger.warning(
                    stage="downloader",
                    event_type="DEM_TILE_NO_URL",
                    detail={"title": item.get("title")},
                )
                continue
            dest = config.DEM_DIR / Path(url).name
            if dest.exists():
                logger.info(
                    stage="downloader",
                    event_type="DEM_TILE_SKIP_EXISTS",
                    detail={"path": str(dest)},
                )
                tiles.append(dest)
                continue
            try:
                _stream_download(url, dest, logger, desc=f"DEM {dest.name}")
                tiles.append(dest)
            except httpx.HTTPError as exc:
                logger.error(
                    stage="downloader",
                    event_type="DEM_TILE_DOWNLOAD_FAILED",
                    detail={"url": url, "error": str(exc)},
                )

    logger.info(
        stage="downloader",
        event_type="DEM_DOWNLOAD_DONE",
        detail={"states": state_list, "tile_count": len(tiles)},
    )
    return tiles


# ---------------------------------------------------------------------------
# Slope pre-computation (Horn's method)
# ---------------------------------------------------------------------------


def _horn_slope_degrees(elev: np.ndarray, cellsize_x: float, cellsize_y: float) -> np.ndarray:
    """Compute slope in degrees from an elevation array via Horn (1981).

    The standard 8-neighbour weighted gradient — same algorithm GDAL's
    ``gdaldem slope`` uses by default. Boundary pixels are filled with
    NaN so downstream NoData handling stays explicit.
    """
    if elev.ndim != 2:
        raise ValueError(f"Expected a 2D elevation array; got shape {elev.shape}")

    z = elev.astype(np.float64, copy=False)
    h, w = z.shape
    slope = np.full((h, w), np.nan, dtype=np.float64)
    if h < 3 or w < 3:
        return slope

    a = z[0:-2, 0:-2]
    b = z[0:-2, 1:-1]
    c = z[0:-2, 2:]
    d = z[1:-1, 0:-2]
    f = z[1:-1, 2:]
    g = z[2:, 0:-2]
    hh = z[2:, 1:-1]
    i = z[2:, 2:]

    dzdx = ((c + 2.0 * f + i) - (a + 2.0 * d + g)) / (8.0 * cellsize_x)
    dzdy = ((g + 2.0 * hh + i) - (a + 2.0 * b + c)) / (8.0 * cellsize_y)

    slope[1:-1, 1:-1] = np.degrees(np.arctan(np.sqrt(dzdx * dzdx + dzdy * dzdy)))
    return slope


def precompute_slope_raster(logger: Optional[PipelineLogger] = None) -> Path:
    """Compute the full slope raster (degrees) from all DEM tiles in
    ``config.DEM_DIR`` and write it to ``config.SLOPE_RASTER_PATH``.

    Why pre-compute:
        At 1M locations, computing slope per-point would require 1M 3x3
        windowed rasterio reads — and would have to handle raster-boundary
        edge cases for every individual sample. Pre-computing once turns
        the per-point cost into a single fast pixel lookup. ~100x faster
        in practice, and it consolidates Horn's-method handling in a
        single, testable function.

    Idempotent: returns the existing path if the slope raster already
    exists.
    """
    import rasterio
    from rasterio.merge import merge as rio_merge

    logger = logger or _default_logger()

    if config.SLOPE_RASTER_PATH.exists():
        logger.info(
            stage="downloader",
            event_type="SLOPE_SKIP_EXISTS",
            detail={"path": str(config.SLOPE_RASTER_PATH)},
        )
        return config.SLOPE_RASTER_PATH

    tiles = sorted(
        list(config.DEM_DIR.glob("*.tif")) + list(config.DEM_DIR.glob("*.tiff"))
    )
    if not tiles:
        raise FileNotFoundError(
            f"No DEM tiles found in {config.DEM_DIR}. "
            f"Run download_dem_tiles(...) first."
        )

    logger.info(
        stage="downloader",
        event_type="SLOPE_COMPUTE_START",
        detail={"tile_count": len(tiles)},
    )
    start = time.monotonic()

    sources = [rasterio.open(t) for t in tiles]
    try:
        mosaic, mosaic_transform = rio_merge(sources)
        ref = sources[0]
        crs = ref.crs
        nodata = ref.nodata
        mosaic_bounds = rasterio.transform.array_bounds(
            mosaic.shape[1], mosaic.shape[2], mosaic_transform
        )
    finally:
        for s in sources:
            s.close()

    elev = mosaic[0]
    if nodata is not None:
        elev = np.where(elev == nodata, np.nan, elev)

    # Horn's slope wants cell size in the SAME units as the elevation values.
    # USGS 3DEP tiles are in EPSG:4269 (NAD83 lat/lon) but elevation is in
    # metres, so the raw pixel size (0.000277° ≈ 30 m) needs converting
    # before we divide elevation by it. For projected CRSes (e.g. UTM,
    # EPSG:5070) the transform is already in metres and no conversion is
    # needed.
    raw_cellsize_x = float(abs(mosaic_transform.a))
    raw_cellsize_y = float(abs(mosaic_transform.e))
    if crs is not None and crs.is_geographic:
        # Approximate the centre latitude of the mosaic and convert
        # degree-cells to metres there. 111_320 m/deg is the standard
        # geographic factor (1 minute of latitude = 1 nautical mile by
        # historical definition); longitudinal degrees shrink by cos(lat).
        # The error from picking the centre latitude over per-row
        # latitude is <3% across NC's 3° height — safely below the
        # accuracy of any downstream slope threshold in the scoring tools.
        lon_min, lat_min, lon_max, lat_max = mosaic_bounds
        centre_lat_rad = np.deg2rad((lat_min + lat_max) / 2.0)
        deg_to_m = 111_320.0
        cellsize_x_m = raw_cellsize_x * deg_to_m * np.cos(centre_lat_rad)
        cellsize_y_m = raw_cellsize_y * deg_to_m
    else:
        cellsize_x_m = raw_cellsize_x
        cellsize_y_m = raw_cellsize_y

    slope = _horn_slope_degrees(elev, cellsize_x_m, cellsize_y_m)
    slope_out = np.where(np.isnan(slope), -9999.0, slope).astype(np.float32)

    config.SLOPE_RASTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": slope_out.shape[0],
        "width": slope_out.shape[1],
        "transform": mosaic_transform,
        "crs": crs,
        "nodata": -9999.0,
        "compress": "deflate",
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
    }
    with rasterio.open(config.SLOPE_RASTER_PATH, "w", **profile) as dst:
        dst.write(slope_out, 1)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        stage="downloader",
        event_type="SLOPE_COMPUTE_DONE",
        detail={
            "path": str(config.SLOPE_RASTER_PATH),
            "shape": list(slope_out.shape),
            "raw_cellsize_x": raw_cellsize_x,
            "raw_cellsize_y": raw_cellsize_y,
            "cellsize_x_m": cellsize_x_m,
            "cellsize_y_m": cellsize_y_m,
            "crs": str(crs),
        },
        duration_ms=elapsed_ms,
    )
    return config.SLOPE_RASTER_PATH


# ---------------------------------------------------------------------------
# Default logger + CLI
# ---------------------------------------------------------------------------


def _default_logger() -> PipelineLogger:
    return PipelineLogger(run_id=f"downloader-{uuid.uuid4().hex[:8]}")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="src.data.downloader",
        description="Download NLCD TCC + NLCD Land Cover (via MRLC WCS) and "
                    "USGS 3DEP DEM tiles (via TNM bbox), then pre-compute "
                    "the slope raster.",
    )
    parser.add_argument(
        "--states",
        nargs="+",
        default=None,
        help="State abbreviations to download for (e.g. NC). If omitted, "
             "uses every state configured in config.STATE_BBOX_WGS84 (NC "
             "is the only configured state at this time).",
    )
    parser.add_argument(
        "--skip-tcc", action="store_true", help="Skip the NLCD TCC download.",
    )
    parser.add_argument(
        "--skip-landcover", action="store_true",
        help="Skip the NLCD Land Cover download.",
    )
    parser.add_argument(
        "--skip-dem", action="store_true",
        help="Skip the DEM download and the slope pre-computation.",
    )
    parser.add_argument(
        "--skip-slope", action="store_true",
        help="Skip only the slope pre-computation (still download DEM tiles).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point. Returns process exit code (0 = success)."""
    args = _build_arg_parser().parse_args(argv)
    logger = _default_logger()
    logger.info(stage="downloader", event_type="CLI_START",
                detail={"args": vars(args)})

    try:
        if not args.skip_tcc:
            download_tcc(args.states, logger)
        if not args.skip_landcover:
            download_landcover(args.states, logger)
        if not args.skip_dem:
            download_dem_tiles(args.states, logger)
            if not args.skip_slope:
                precompute_slope_raster(logger)
    except Exception as exc:
        logger.error(
            stage="downloader",
            event_type="CLI_FAILED",
            detail={"error": str(exc), "type": type(exc).__name__},
        )
        raise

    logger.info(stage="downloader", event_type="CLI_DONE")
    return 0


if __name__ == "__main__":   # pragma: no cover - exercised via the CLI smoke test
    raise SystemExit(main())
