"""
Phase 3 tests: fetch_tcc, fetch_land_cover, fetch_elevation.

Synthetic GeoTIFFs are written in EPSG:4326 so the lat/lon → pixel math
stays trivial. A raster created with ``from_origin(west=0, north=10,
xsize=1, ysize=1)`` places its pixel ``(row, col)`` centre at
``(lon=col+0.5, lat=10-row-0.5)``. Pixel (5, 5) is therefore at
(lon=5.5, lat=4.5). Every test that targets pixel (5, 5) uses those
coordinates.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from src import config
from src.tools import elevation, landcover, tcc


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_tool_caches() -> None:
    """Make every test start with empty module-level caches.

    Cleanup runs after the test to free file handles regardless of result.
    """
    yield
    tcc._reset_cache()
    landcover._reset_cache()
    elevation._reset_cache()


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = {
        "TCC_DIR": tmp_path / "tcc",
        "LC_DIR": tmp_path / "landcover",
        "DEM_DIR": tmp_path / "dem",
        "PROCESSED_DIR": tmp_path / "processed",
    }
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    for name, p in paths.items():
        monkeypatch.setattr(config, name, p)
    monkeypatch.setattr(
        config, "SLOPE_RASTER_PATH", paths["PROCESSED_DIR"] / "slope.tif"
    )
    return paths


def _make_geotiff(
    path: Path,
    data: np.ndarray,
    *,
    crs: str = "EPSG:4326",
    west: float = 0.0,
    north: float = 10.0,
    cellsize: float = 1.0,
    nodata: Optional[float] = None,
    dtype: str = "uint8",
) -> Path:
    """Write a synthetic single-band GeoTIFF and return its path."""
    height, width = data.shape
    transform = from_origin(west, north, cellsize, cellsize)
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": dtype,
        "transform": transform,
        "crs": crs,
        "nodata": nodata,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data.astype(dtype), 1)
    return path


# ---------------------------------------------------------------------------
# fetch_tcc
# ---------------------------------------------------------------------------


class TestFetchTCC:
    def test_returns_value_for_known_pixel(self, isolated_paths: dict[str, Path]) -> None:
        data = np.full((10, 10), 50, dtype="uint8")
        data[5, 5] = 73
        _make_geotiff(isolated_paths["TCC_DIR"] / "tcc.tif", data, nodata=255)
        assert tcc.fetch_tcc(latitude=4.5, longitude=5.5) == {
            "tcc_pct": 73, "tcc_missing": False,
        }

    def test_returns_missing_for_nodata_pixel(self, isolated_paths: dict[str, Path]) -> None:
        data = np.full((10, 10), 50, dtype="uint8")
        data[5, 5] = 255
        _make_geotiff(isolated_paths["TCC_DIR"] / "tcc.tif", data, nodata=255)
        result = tcc.fetch_tcc(latitude=4.5, longitude=5.5)
        assert result["tcc_pct"] is None
        assert result["tcc_missing"] is True
        assert "NoData" in result["reason"]

    def test_returns_missing_when_no_raster_present(self, isolated_paths: dict[str, Path]) -> None:
        result = tcc.fetch_tcc(latitude=4.5, longitude=5.5)
        assert result["tcc_pct"] is None
        assert result["tcc_missing"] is True
        assert "TCC raster not found" in result["reason"]

    def test_value_outside_0_100_flagged_missing(self, isolated_paths: dict[str, Path]) -> None:
        data = np.full((10, 10), 50, dtype="uint8")
        data[5, 5] = 200  # not NoData (255) but still invalid
        _make_geotiff(isolated_paths["TCC_DIR"] / "tcc.tif", data, nodata=255)
        result = tcc.fetch_tcc(latitude=4.5, longitude=5.5)
        assert result["tcc_pct"] is None
        assert "outside [0, 100]" in result["reason"]

    def test_point_outside_raster_returns_missing(self, isolated_paths: dict[str, Path]) -> None:
        data = np.full((10, 10), 50, dtype="uint8")
        _make_geotiff(isolated_paths["TCC_DIR"] / "tcc.tif", data, nodata=255)
        result = tcc.fetch_tcc(latitude=999.0, longitude=999.0)
        assert result["tcc_missing"] is True

    def test_dataset_handle_reused_across_calls(
        self, isolated_paths: dict[str, Path]
    ) -> None:
        data = np.full((10, 10), 42, dtype="uint8")
        _make_geotiff(isolated_paths["TCC_DIR"] / "tcc.tif", data, nodata=255)
        tcc.fetch_tcc(latitude=4.5, longitude=5.5)
        first_handle = tcc._dataset
        assert first_handle is not None
        tcc.fetch_tcc(latitude=3.5, longitude=4.5)
        # The cached handle is the SAME object — proves we are not reopening.
        assert tcc._dataset is first_handle

    def test_reset_cache_closes_handle(self, isolated_paths: dict[str, Path]) -> None:
        data = np.full((10, 10), 42, dtype="uint8")
        _make_geotiff(isolated_paths["TCC_DIR"] / "tcc.tif", data, nodata=255)
        tcc.fetch_tcc(latitude=4.5, longitude=5.5)
        assert tcc._dataset is not None
        tcc._reset_cache()
        assert tcc._dataset is None


# ---------------------------------------------------------------------------
# fetch_land_cover
# ---------------------------------------------------------------------------


_NLCD_CODE_EXPECTATIONS = [
    (41, "Deciduous Forest"),
    (42, "Evergreen Forest"),
    (43, "Mixed Forest"),
    (21, "Developed, Open Space"),
    (22, "Developed, Low Intensity"),
    (23, "Developed, Medium Intensity"),
    (24, "Developed, High Intensity"),
    (31, "Barren Land"),
    (52, "Shrub/Scrub"),
    (71, "Grassland/Herbaceous"),
    (81, "Pasture/Hay"),
    (82, "Cultivated Crops"),
    (90, "Woody Wetlands"),
    (95, "Emergent Herbaceous Wetlands"),
]


class TestFetchLandCover:
    @pytest.mark.parametrize("code,expected_class", _NLCD_CODE_EXPECTATIONS)
    def test_known_codes_map_to_class(
        self,
        isolated_paths: dict[str, Path],
        code: int,
        expected_class: str,
    ) -> None:
        data = np.full((10, 10), code, dtype="uint8")
        _make_geotiff(isolated_paths["LC_DIR"] / "lc.tif", data, nodata=0)
        result = landcover.fetch_land_cover(latitude=4.5, longitude=5.5)
        assert result["land_cover_code"] == code
        assert result["land_cover_class"] == expected_class
        assert result["lc_missing"] is False

    def test_unknown_code_returns_placeholder_class(
        self, isolated_paths: dict[str, Path]
    ) -> None:
        data = np.full((10, 10), 99, dtype="uint8")
        _make_geotiff(isolated_paths["LC_DIR"] / "lc.tif", data, nodata=0)
        result = landcover.fetch_land_cover(latitude=4.5, longitude=5.5)
        assert result["land_cover_code"] == 99
        assert result["land_cover_class"] == "Unknown (99)"
        assert result["lc_missing"] is False

    def test_nodata_pixel_returns_missing(self, isolated_paths: dict[str, Path]) -> None:
        data = np.zeros((10, 10), dtype="uint8")  # entire raster is NoData
        _make_geotiff(isolated_paths["LC_DIR"] / "lc.tif", data, nodata=0)
        result = landcover.fetch_land_cover(latitude=4.5, longitude=5.5)
        assert result["lc_missing"] is True
        assert result["land_cover_code"] is None
        assert result["land_cover_class"] == "UNKNOWN"

    def test_no_raster_returns_missing(self, isolated_paths: dict[str, Path]) -> None:
        result = landcover.fetch_land_cover(latitude=4.5, longitude=5.5)
        assert result["lc_missing"] is True
        assert "Land Cover raster not found" in result["reason"]

    def test_dataset_handle_reused_across_calls(
        self, isolated_paths: dict[str, Path]
    ) -> None:
        data = np.full((10, 10), 41, dtype="uint8")
        _make_geotiff(isolated_paths["LC_DIR"] / "lc.tif", data, nodata=0)
        landcover.fetch_land_cover(latitude=4.5, longitude=5.5)
        first_handle = landcover._dataset
        assert first_handle is not None
        landcover.fetch_land_cover(latitude=3.5, longitude=4.5)
        assert landcover._dataset is first_handle


# ---------------------------------------------------------------------------
# fetch_elevation
# ---------------------------------------------------------------------------


def _make_dem(dem_dir: Path, elev: np.ndarray, *, nodata: float = -9999.0) -> Path:
    return _make_geotiff(
        dem_dir / "dem.tif",
        elev.astype("float32"),
        nodata=nodata,
        dtype="float32",
    )


def _make_slope(slope_path: Path, slope: np.ndarray, *, nodata: float = -9999.0) -> Path:
    return _make_geotiff(
        slope_path, slope.astype("float32"), nodata=nodata, dtype="float32",
    )


class TestFetchElevation:
    def test_returns_elevation_only_when_no_slope_raster(
        self, isolated_paths: dict[str, Path]
    ) -> None:
        _make_dem(isolated_paths["DEM_DIR"], np.full((10, 10), 100.0))
        result = elevation.fetch_elevation(latitude=4.5, longitude=5.5)
        assert result["elevation_m"] == pytest.approx(100.0)
        assert result["slope_deg"] is None
        assert result["elevation_missing"] is False

    def test_returns_slope_only_when_no_dem_tile(
        self, isolated_paths: dict[str, Path]
    ) -> None:
        _make_slope(config.SLOPE_RASTER_PATH, np.full((10, 10), 15.5))
        result = elevation.fetch_elevation(latitude=4.5, longitude=5.5)
        assert result["elevation_m"] is None
        assert result["slope_deg"] == pytest.approx(15.5)
        assert result["aspect_deg"] is None
        assert result["elevation_missing"] is True

    def test_full_elevation_and_slope_when_both_present(
        self, isolated_paths: dict[str, Path]
    ) -> None:
        _make_dem(isolated_paths["DEM_DIR"], np.full((10, 10), 100.0))
        _make_slope(config.SLOPE_RASTER_PATH, np.full((10, 10), 15.5))
        result = elevation.fetch_elevation(latitude=4.5, longitude=5.5)
        assert result["elevation_m"] == pytest.approx(100.0)
        assert result["slope_deg"] == pytest.approx(15.5)
        assert result["elevation_missing"] is False

    def test_aspect_north_for_southward_rising_elevation(
        self, isolated_paths: dict[str, Path]
    ) -> None:
        # In rasterio, row=0 is the northernmost row. z[row, col] = row means
        # elevation rises as row increases, i.e. southward. Downhill is north,
        # so compass aspect == 0°.
        elev = np.tile(np.arange(10, dtype=np.float32).reshape(-1, 1), (1, 10))
        _make_dem(isolated_paths["DEM_DIR"], elev)
        result = elevation.fetch_elevation(latitude=4.5, longitude=5.5)
        assert result["aspect_deg"] == pytest.approx(0.0, abs=1e-6)

    def test_aspect_west_for_eastward_rising_elevation(
        self, isolated_paths: dict[str, Path]
    ) -> None:
        # z[row, col] = col -> elevation rises eastward -> downhill is west,
        # so compass aspect == 270°.
        elev = np.tile(np.arange(10, dtype=np.float32).reshape(1, -1), (10, 1))
        _make_dem(isolated_paths["DEM_DIR"], elev)
        result = elevation.fetch_elevation(latitude=4.5, longitude=5.5)
        assert result["aspect_deg"] == pytest.approx(270.0, abs=1e-6)

    def test_aspect_none_for_flat_dem(self, isolated_paths: dict[str, Path]) -> None:
        _make_dem(isolated_paths["DEM_DIR"], np.full((10, 10), 100.0))
        result = elevation.fetch_elevation(latitude=4.5, longitude=5.5)
        assert result["aspect_deg"] is None

    def test_dem_nodata_pixel_marked_missing(self, isolated_paths: dict[str, Path]) -> None:
        elev = np.full((10, 10), -9999.0)
        _make_dem(isolated_paths["DEM_DIR"], elev, nodata=-9999.0)
        result = elevation.fetch_elevation(latitude=4.5, longitude=5.5)
        assert result["elevation_m"] is None
        assert result["elevation_missing"] is True
        assert "NoData" in result["reason"]

    def test_slope_nodata_returns_none(self, isolated_paths: dict[str, Path]) -> None:
        _make_dem(isolated_paths["DEM_DIR"], np.full((10, 10), 100.0))
        _make_slope(config.SLOPE_RASTER_PATH, np.full((10, 10), -9999.0))
        result = elevation.fetch_elevation(latitude=4.5, longitude=5.5)
        assert result["elevation_m"] == pytest.approx(100.0)
        assert result["slope_deg"] is None

    def test_no_data_at_all_returns_all_none(self, isolated_paths: dict[str, Path]) -> None:
        result = elevation.fetch_elevation(latitude=4.5, longitude=5.5)
        assert result["elevation_m"] is None
        assert result["slope_deg"] is None
        assert result["aspect_deg"] is None
        assert result["elevation_missing"] is True

    def test_dem_index_built_only_once(self, isolated_paths: dict[str, Path]) -> None:
        _make_dem(isolated_paths["DEM_DIR"], np.full((10, 10), 100.0))
        elevation.fetch_elevation(latitude=4.5, longitude=5.5)
        assert elevation._dem_index_built is True
        assert len(elevation._dem_index) == 1
        elevation.fetch_elevation(latitude=3.5, longitude=4.5)
        # Still exactly 1 tile in the index — not rebuilt.
        assert len(elevation._dem_index) == 1

    def test_dem_handle_lru_cache_respected(
        self, isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force a tiny LRU cap, write two non-overlapping DEM tiles, and
        # confirm only the cap is held open at any time.
        monkeypatch.setattr(elevation, "_DEM_HANDLE_LIMIT", 1)
        # Tile A covers x:[0,10], y:[0,10]
        _make_geotiff(
            isolated_paths["DEM_DIR"] / "a.tif",
            np.full((10, 10), 100.0, dtype="float32"),
            west=0.0, north=10.0, cellsize=1.0, nodata=-9999.0, dtype="float32",
        )
        # Tile B covers x:[20,30], y:[0,10]
        _make_geotiff(
            isolated_paths["DEM_DIR"] / "b.tif",
            np.full((10, 10), 200.0, dtype="float32"),
            west=20.0, north=10.0, cellsize=1.0, nodata=-9999.0, dtype="float32",
        )
        a = elevation.fetch_elevation(latitude=4.5, longitude=5.5)
        b = elevation.fetch_elevation(latitude=4.5, longitude=25.5)
        assert a["elevation_m"] == pytest.approx(100.0)
        assert b["elevation_m"] == pytest.approx(200.0)
        # LRU cap honoured — only one handle open at a time.
        assert len(elevation._dem_handles) == 1
