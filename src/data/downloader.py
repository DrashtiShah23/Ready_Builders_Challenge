"""
Reproducible, idempotent downloader for every environmental dataset the
pipeline depends on.

Datasets
--------
- NLCD 2021 Tree Canopy Cover (national GeoTIFF, ~3 GB unzipped) via
  ``config.CANOPY_RASTER_URL`` → ``data/raw/tcc/``.
- NLCD 2021 Land Cover (national GeoTIFF, ~3 GB unzipped) via
  ``config.LANDCOVER_RASTER_URL`` → ``data/raw/landcover/``.
- USGS 3DEP 1 arc-second elevation tiles (per CONUS state) via the USGS
  National Map (TNM) Access API → ``data/raw/dem/``.
- A single pre-computed slope raster (Horn's method, degrees) at
  ``config.SLOPE_RASTER_PATH`` derived from the downloaded DEM tiles.

Design decisions
----------------
- **Idempotent.** Every function checks for its output before doing any
  work. Reruns are safe (and cheap).
- **Streaming downloads.** GeoTIFFs are multi-GB, so we stream chunks with
  ``httpx`` instead of buffering whole responses in memory, and we drive a
  ``tqdm`` progress bar from the ``Content-Length`` header when present.
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

    python -m src.data.downloader --states CA TX
    python -m src.data.downloader                # CONUS-wide
    python -m src.data.downloader --skip-dem     # smoke-test the raster downloads only

All operations log structured JSONL events via :class:`src.utils.logger.PipelineLogger`.
"""
from __future__ import annotations

import argparse
import shutil
import time
import uuid
import zipfile
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


def _stream_download(url: str, dest: Path, logger: PipelineLogger, desc: str) -> Path:
    """Stream ``url`` to ``dest`` with a tqdm progress bar.

    Writes to ``dest.with_suffix(dest.suffix + ".part")`` and atomically
    renames on success so a partial file never appears at ``dest``.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest.with_suffix(dest.suffix + ".part")
    if part_path.exists():
        part_path.unlink()

    start = time.monotonic()
    bytes_written = 0
    with httpx.stream("GET", url, timeout=_HTTP_TIMEOUT, follow_redirects=True) as resp:
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


def _extract_zip(zip_path: Path, dest_dir: Path, logger: PipelineLogger) -> list[Path]:
    """Extract every member of ``zip_path`` to ``dest_dir`` and return the
    extracted file paths."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    extracted: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            target = dest_dir / Path(member).name  # flatten any nested paths
            if member.endswith("/"):
                continue
            with zf.open(member) as src_f, open(target, "wb") as out_f:
                shutil.copyfileobj(src_f, out_f)
            extracted.append(target)
    elapsed_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        stage="downloader",
        event_type="ZIP_EXTRACTED",
        detail={"zip": str(zip_path), "files": [p.name for p in extracted]},
        duration_ms=elapsed_ms,
    )
    return extracted


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
# NLCD Tree Canopy Cover
# ---------------------------------------------------------------------------


def download_tcc(logger: Optional[PipelineLogger] = None) -> Path:
    """Download and unzip the NLCD 2021 Tree Canopy Cover national GeoTIFF.

    Idempotent: returns the existing raster path if it already exists.
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

    logger.info(stage="downloader", event_type="TCC_DOWNLOAD_START",
                detail={"url": config.CANOPY_RASTER_URL})
    zip_path = config.TCC_DIR / Path(config.CANOPY_RASTER_URL).name
    _stream_download(config.CANOPY_RASTER_URL, zip_path, logger, desc="NLCD TCC")
    extracted = _extract_zip(zip_path, config.TCC_DIR, logger)
    zip_path.unlink(missing_ok=True)

    raster = _raster_already_present(config.TCC_DIR)
    if raster is None:
        raise RuntimeError(
            f"TCC download succeeded but no .tif/.img found in {config.TCC_DIR}. "
            f"Extracted members: {[p.name for p in extracted]}"
        )
    logger.info(stage="downloader", event_type="TCC_DOWNLOAD_DONE",
                detail={"path": str(raster)})
    return raster


# ---------------------------------------------------------------------------
# NLCD Land Cover
# ---------------------------------------------------------------------------


def download_landcover(logger: Optional[PipelineLogger] = None) -> Path:
    """Download and unzip the NLCD 2021 Land Cover national GeoTIFF.

    Same source family as TCC (USGS / MRLC), same CRS (EPSG:5070), same
    resolution (30m). Idempotent.
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

    logger.info(stage="downloader", event_type="LANDCOVER_DOWNLOAD_START",
                detail={"url": config.LANDCOVER_RASTER_URL})
    zip_path = config.LC_DIR / Path(config.LANDCOVER_RASTER_URL).name
    _stream_download(config.LANDCOVER_RASTER_URL, zip_path, logger, desc="NLCD LC")
    extracted = _extract_zip(zip_path, config.LC_DIR, logger)
    zip_path.unlink(missing_ok=True)

    raster = _raster_already_present(config.LC_DIR)
    if raster is None:
        raise RuntimeError(
            f"Land Cover download succeeded but no .tif/.img found in {config.LC_DIR}. "
            f"Extracted members: {[p.name for p in extracted]}"
        )
    logger.info(stage="downloader", event_type="LANDCOVER_DOWNLOAD_DONE",
                detail={"path": str(raster)})
    return raster


# ---------------------------------------------------------------------------
# USGS 3DEP DEM tiles
# ---------------------------------------------------------------------------


def _tnm_query_state(state_abbr: str, *, _client: Optional[httpx.Client] = None) -> list[dict]:
    """Query the USGS National Map API for all 3DEP 1 arc-second GeoTIFF
    products that intersect ``state_abbr`` (e.g. "CA").

    Returns the raw ``items`` list from the JSON response. A separate function
    so tests can mock the network layer in isolation.
    """
    fips = config.STATE_FIPS.get(state_abbr.upper())
    if fips is None:
        raise ValueError(
            f"Unknown CONUS state '{state_abbr}'. Expected one of "
            f"{sorted(config.STATE_FIPS.keys())}"
        )
    params = {
        "datasets": config.TNM_DEM_DATASET,
        "polyType": "state",
        "polyCode": fips,
        "prodFormats": "GeoTIFF",
        "outputFormat": "JSON",
    }
    client = _client or httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True)
    try:
        resp = client.get(config.TNM_API_BASE, params=params)
        resp.raise_for_status()
        payload = resp.json()
    finally:
        if _client is None:
            client.close()
    items = payload.get("items", []) or []
    return items


def download_dem_tiles(
    states: Optional[Iterable[str]] = None,
    logger: Optional[PipelineLogger] = None,
) -> list[Path]:
    """Download USGS 3DEP 1 arc-second DEM tiles for the given CONUS states.

    Idempotent at the *tile* level: each individual tile is skipped if its
    file already exists in ``config.DEM_DIR``. Re-running for the same set
    of states is therefore safe.

    Parameters
    ----------
    states:
        Iterable of state abbreviations (e.g. ``["CA", "TX"]``). If ``None``,
        downloads tiles for every CONUS state in ``config.STATE_FIPS``.

    Returns
    -------
    list[Path]
        All tile paths now present on disk for the requested states (both
        newly downloaded and previously cached).
    """
    logger = logger or _default_logger()
    state_list = (
        [s.upper() for s in states] if states else sorted(config.STATE_FIPS.keys())
    )
    logger.info(
        stage="downloader",
        event_type="DEM_DOWNLOAD_START",
        detail={"states": state_list},
    )

    tiles: list[Path] = []
    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        for state in state_list:
            try:
                items = _tnm_query_state(state, _client=client)
            except (httpx.HTTPError, ValueError) as exc:
                logger.error(
                    stage="downloader",
                    event_type="DEM_STATE_QUERY_FAILED",
                    detail={"state": state, "error": str(exc)},
                )
                continue

            logger.info(
                stage="downloader",
                event_type="DEM_STATE_QUERY_OK",
                detail={"state": state, "tile_count": len(items)},
            )

            for item in items:
                url = item.get("downloadURL") or item.get("urls", {}).get("TIFF")
                if not url:
                    logger.warning(
                        stage="downloader",
                        event_type="DEM_TILE_NO_URL",
                        detail={"state": state, "item": item.get("title")},
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

    # 8-neighbour Horn weights:
    # dz/dx = ((c+2f+i) - (a+2d+g)) / (8 * cellsize_x)
    # dz/dy = ((g+2h+i) - (a+2b+c)) / (8 * cellsize_y)
    # where positions are:  a b c
    #                       d e f
    #                       g h i
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
    # Lazy rasterio import so importing this module is cheap.
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

    # Merge tiles into a single in-memory mosaic. For CONUS this can be
    # large; production scale should switch to a windowed read loop, but
    # for the per-state subsets the build plan supports this fits in memory
    # comfortably.
    sources = [rasterio.open(t) for t in tiles]
    try:
        mosaic, mosaic_transform = rio_merge(sources)
        ref = sources[0]
        crs = ref.crs
        nodata = ref.nodata
    finally:
        for s in sources:
            s.close()

    elev = mosaic[0]
    if nodata is not None:
        elev = np.where(elev == nodata, np.nan, elev)

    cellsize_x = float(abs(mosaic_transform.a))
    cellsize_y = float(abs(mosaic_transform.e))
    slope = _horn_slope_degrees(elev, cellsize_x, cellsize_y)
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
            "cellsize_x": cellsize_x,
            "cellsize_y": cellsize_y,
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
        description="Download NLCD TCC + NLCD Land Cover + USGS 3DEP DEM tiles "
                    "and pre-compute the slope raster.",
    )
    parser.add_argument(
        "--states",
        nargs="+",
        default=None,
        help="CONUS state abbreviations to download DEM for (e.g. CA TX). "
             "If omitted, downloads tiles for all 48 + DC.",
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
            download_tcc(logger)
        if not args.skip_landcover:
            download_landcover(logger)
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
