"""
Phase 2 tests: data downloader.

Covers every public function in ``src.data.downloader``:
    download_tcc, download_landcover, download_dem_tiles, precompute_slope_raster
plus the Horn's-method numerical kernel and the CLI argument parser.

Network is fully mocked. The single slope-pre-computation test creates a
real (but tiny) GeoTIFF in a tmp dir using rasterio so the rasterio code
path is exercised end-to-end without touching the live USGS endpoints.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock

import httpx
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from src import config
from src.data import downloader as dl


# ---------------------------------------------------------------------------
# Shared fixtures: redirect every config path to a tmp dir so tests cannot
# pollute the developer's local data/ tree.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point every config.*_DIR at a fresh tmp_path subdirectory."""
    paths = {
        "TCC_DIR": tmp_path / "tcc",
        "LC_DIR": tmp_path / "landcover",
        "DEM_DIR": tmp_path / "dem",
        "PROCESSED_DIR": tmp_path / "processed",
        "SCORED_DIR": tmp_path / "scored",
        "LOG_DIR": tmp_path / "logs",
    }
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    for name, p in paths.items():
        monkeypatch.setattr(config, name, p)
    monkeypatch.setattr(
        config, "SLOPE_RASTER_PATH", paths["PROCESSED_DIR"] / "slope_degrees.tif"
    )
    monkeypatch.setattr(dl, "_default_logger", lambda: _MutedLogger(paths["LOG_DIR"]))
    return paths


class _MutedLogger:
    """A logger that captures events in memory instead of writing JSONL,
    so tests can assert on what was logged without filesystem coupling."""

    def __init__(self, log_dir: Path) -> None:
        self.log_path = log_dir / "test.jsonl"
        self.events: list[dict[str, Any]] = []

    def _record(self, level: str, stage: str, event_type: str, **kwargs: Any) -> None:
        self.events.append(
            {"level": level, "stage": stage, "event_type": event_type, **kwargs}
        )

    def info(self, stage: str, event_type: str, **kwargs: Any) -> None:
        self._record("INFO", stage, event_type, **kwargs)

    def warning(self, stage: str, event_type: str, **kwargs: Any) -> None:
        self._record("WARNING", stage, event_type, **kwargs)

    def error(self, stage: str, event_type: str, **kwargs: Any) -> None:
        self._record("ERROR", stage, event_type, **kwargs)


# ---------------------------------------------------------------------------
# httpx.stream mocking helpers
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    """Minimal context manager that mimics httpx.stream's response object."""

    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self._content = content
        self.status_code = status_code
        self.headers = {"Content-Length": str(len(content))}

    def __enter__(self) -> "_FakeStreamResponse":
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=MagicMock(),
                response=MagicMock(status_code=self.status_code),
            )

    def iter_bytes(self, chunk_size: int = 1 << 16) -> Iterator[bytes]:
        buf = io.BytesIO(self._content)
        while True:
            chunk = buf.read(chunk_size)
            if not chunk:
                return
            yield chunk


def _build_zip_bytes(members: dict[str, bytes]) -> bytes:
    """Build an in-memory ZIP containing ``members`` (name -> bytes)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# download_tcc
# ---------------------------------------------------------------------------


class TestDownloadTCC:
    def test_skips_when_raster_already_present(
        self, isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        existing = isolated_paths["TCC_DIR"] / "nlcd_tcc_already_here.tif"
        existing.write_bytes(b"not-a-real-geotiff-but-the-sentinel-suffices")

        # If we accidentally hit the network the test fails loudly.
        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("download_tcc should not network when raster exists")

        monkeypatch.setattr(dl.httpx, "stream", boom)

        result = dl.download_tcc()
        assert result == existing

    def test_downloads_and_extracts_zip(
        self, isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fake NLCD-TCC zip containing a single placeholder .tif.
        fake_tif = b"GeoTIFF placeholder for TCC"
        zip_bytes = _build_zip_bytes({"nlcd_tcc_conus_2021.tif": fake_tif})

        def fake_stream(method: str, url: str, **_kwargs: Any) -> _FakeStreamResponse:
            assert method == "GET"
            assert url == config.CANOPY_RASTER_URL
            return _FakeStreamResponse(zip_bytes)

        monkeypatch.setattr(dl.httpx, "stream", fake_stream)

        result = dl.download_tcc()
        assert result.suffix == ".tif"
        assert result.read_bytes() == fake_tif
        # ZIP itself is cleaned up after extraction.
        assert not list(isolated_paths["TCC_DIR"].glob("*.zip"))

    def test_raises_if_zip_contains_no_raster(
        self, isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        zip_bytes = _build_zip_bytes({"README.txt": b"no rasters here"})
        monkeypatch.setattr(
            dl.httpx, "stream", lambda *_a, **_k: _FakeStreamResponse(zip_bytes)
        )
        with pytest.raises(RuntimeError, match="no .tif/.img"):
            dl.download_tcc()


# ---------------------------------------------------------------------------
# download_landcover
# ---------------------------------------------------------------------------


class TestDownloadLandcover:
    def test_skips_when_raster_already_present(
        self, isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        existing = isolated_paths["LC_DIR"] / "nlcd_lc.tif"
        existing.write_bytes(b"placeholder")
        monkeypatch.setattr(
            dl.httpx, "stream",
            lambda *_a, **_k: pytest.fail("should not network when raster exists"),
        )
        assert dl.download_landcover() == existing

    def test_downloads_and_extracts(
        self, isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_tif = b"GeoTIFF placeholder for LC"
        zip_bytes = _build_zip_bytes({"nlcd_land_cover_l48_2021.tif": fake_tif})
        monkeypatch.setattr(
            dl.httpx, "stream", lambda *_a, **_k: _FakeStreamResponse(zip_bytes)
        )
        result = dl.download_landcover()
        assert result.read_bytes() == fake_tif


# ---------------------------------------------------------------------------
# USGS National Map API
# ---------------------------------------------------------------------------


class _FakeHTTPClient:
    """Minimal stand-in for ``httpx.Client`` that returns a canned JSON
    response and records the request params for assertions."""

    def __init__(self, payload: dict[str, Any], record: list[dict[str, Any]]) -> None:
        self._payload = payload
        self._record = record

    def __enter__(self) -> "_FakeHTTPClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def close(self) -> None:
        return None

    def get(self, url: str, params: dict[str, Any]) -> "_FakeHTTPResponse":
        self._record.append({"url": url, "params": dict(params)})
        return _FakeHTTPResponse(self._payload)


class _FakeHTTPResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class TestTNMQuery:
    def test_builds_correct_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record: list[dict[str, Any]] = []
        fake_client = _FakeHTTPClient({"items": []}, record)
        items = dl._tnm_query_state("CA", _client=fake_client)  # type: ignore[arg-type]
        assert items == []
        assert len(record) == 1
        call = record[0]
        assert call["url"] == config.TNM_API_BASE
        assert call["params"]["polyType"] == "state"
        assert call["params"]["polyCode"] == config.STATE_FIPS["CA"]
        assert call["params"]["prodFormats"] == "GeoTIFF"
        assert call["params"]["datasets"] == config.TNM_DEM_DATASET

    def test_unknown_state_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown CONUS state"):
            dl._tnm_query_state("ZZ")

    def test_lowercase_state_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        record: list[dict[str, Any]] = []
        fake_client = _FakeHTTPClient({"items": []}, record)
        dl._tnm_query_state("ca", _client=fake_client)  # type: ignore[arg-type]
        assert record[0]["params"]["polyCode"] == config.STATE_FIPS["CA"]


# ---------------------------------------------------------------------------
# download_dem_tiles
# ---------------------------------------------------------------------------


class TestDownloadDEMTiles:
    @pytest.fixture
    def stub_tnm_and_stream(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_paths: dict[str, Path],
    ) -> dict[str, Any]:
        """Stub the TNM query + the streaming downloader.

        Returns a dict so tests can introspect what was 'downloaded'.
        """
        downloads: list[str] = []

        def fake_query(state_abbr: str, **_kw: Any) -> list[dict[str, Any]]:
            return [
                {
                    "downloadURL": f"https://example.test/{state_abbr}_tile1.tif",
                    "title": f"{state_abbr} tile 1",
                },
                {
                    "downloadURL": f"https://example.test/{state_abbr}_tile2.tif",
                    "title": f"{state_abbr} tile 2",
                },
            ]

        def fake_stream(url: str, dest: Path, *_a: Any, **_kw: Any) -> Path:
            downloads.append(url)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake-dem")
            return dest

        monkeypatch.setattr(dl, "_tnm_query_state", fake_query)
        monkeypatch.setattr(dl, "_stream_download", fake_stream)

        # Also stub httpx.Client so the outer 'with' in download_dem_tiles works.
        class _NoopClient:
            def __init__(self, *_a: Any, **_kw: Any) -> None: ...
            def __enter__(self) -> "_NoopClient": return self
            def __exit__(self, *_exc: Any) -> None: ...
            def close(self) -> None: ...

        monkeypatch.setattr(dl.httpx, "Client", _NoopClient)
        return {"downloads": downloads, "paths": isolated_paths}

    def test_downloads_tiles_for_requested_states(self, stub_tnm_and_stream: dict[str, Any]) -> None:
        tiles = dl.download_dem_tiles(["CA", "TX"])
        assert len(tiles) == 4
        assert len(stub_tnm_and_stream["downloads"]) == 4

    def test_skips_already_present_tiles(self, stub_tnm_and_stream: dict[str, Any]) -> None:
        dem_dir = stub_tnm_and_stream["paths"]["DEM_DIR"]
        (dem_dir / "CA_tile1.tif").write_bytes(b"existing")
        tiles = dl.download_dem_tiles(["CA"])
        urls = stub_tnm_and_stream["downloads"]
        # Tile 1 should NOT have been re-downloaded; tile 2 should have been.
        assert "https://example.test/CA_tile1.tif" not in urls
        assert "https://example.test/CA_tile2.tif" in urls
        assert len(tiles) == 2  # both tiles present on disk regardless

    def test_unknown_state_logged_not_raised(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_paths: dict[str, Path],
    ) -> None:
        # The fixture isn't used here; we want the real _tnm_query_state to
        # raise ValueError so the outer function catches it.
        class _NoopClient:
            def __init__(self, *_a: Any, **_kw: Any) -> None: ...
            def __enter__(self) -> "_NoopClient": return self
            def __exit__(self, *_exc: Any) -> None: ...
            def close(self) -> None: ...

        monkeypatch.setattr(dl.httpx, "Client", _NoopClient)
        tiles = dl.download_dem_tiles(["ZZ"])  # invalid state
        assert tiles == []


# ---------------------------------------------------------------------------
# Horn's-method slope kernel
# ---------------------------------------------------------------------------


class TestHornSlope:
    def test_flat_surface_zero_slope(self) -> None:
        z = np.full((10, 10), 100.0)
        slope = dl._horn_slope_degrees(z, 1.0, 1.0)
        interior = slope[1:-1, 1:-1]
        assert np.allclose(interior, 0.0, atol=1e-9)

    def test_constant_east_slope_is_45_degrees(self) -> None:
        # z[i, j] = j, unit cellsize -> Horn dzdx = 1, dzdy = 0 -> slope = 45 deg.
        j = np.arange(10)
        z = np.tile(j.astype(float), (10, 1))
        slope = dl._horn_slope_degrees(z, 1.0, 1.0)
        interior = slope[1:-1, 1:-1]
        assert np.allclose(interior, 45.0, atol=1e-9)

    def test_constant_north_slope_is_45_degrees(self) -> None:
        # z[i, j] = i -> Horn dzdy = 1, dzdx = 0 -> slope = 45 deg.
        i = np.arange(10).reshape(-1, 1)
        z = np.tile(i.astype(float), (1, 10))
        slope = dl._horn_slope_degrees(z, 1.0, 1.0)
        interior = slope[1:-1, 1:-1]
        assert np.allclose(interior, 45.0, atol=1e-9)

    def test_boundary_filled_with_nan(self) -> None:
        z = np.full((5, 5), 100.0)
        slope = dl._horn_slope_degrees(z, 1.0, 1.0)
        assert np.isnan(slope[0, :]).all()
        assert np.isnan(slope[-1, :]).all()
        assert np.isnan(slope[:, 0]).all()
        assert np.isnan(slope[:, -1]).all()

    def test_smaller_cellsize_means_steeper_slope(self) -> None:
        j = np.arange(10).astype(float)
        z = np.tile(j, (10, 1))
        coarse = dl._horn_slope_degrees(z, 10.0, 10.0)
        fine = dl._horn_slope_degrees(z, 1.0, 1.0)
        # Same elevation change over a smaller cell = steeper apparent slope.
        assert np.nanmean(fine[1:-1, 1:-1]) > np.nanmean(coarse[1:-1, 1:-1])

    def test_1d_input_raises(self) -> None:
        with pytest.raises(ValueError, match="2D"):
            dl._horn_slope_degrees(np.arange(10).astype(float), 1.0, 1.0)


# ---------------------------------------------------------------------------
# precompute_slope_raster (real rasterio I/O on a synthetic DEM)
# ---------------------------------------------------------------------------


def _write_synthetic_dem(path: Path, elev: np.ndarray) -> None:
    """Write a tiny GeoTIFF DEM with a known elevation array."""
    height, width = elev.shape
    transform = from_origin(west=0.0, north=float(height), xsize=1.0, ysize=1.0)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": height,
        "width": width,
        "transform": transform,
        "crs": "EPSG:4326",
        "nodata": -9999.0,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(elev.astype(np.float32), 1)


class TestPrecomputeSlope:
    def test_raises_when_no_dem_tiles(self, isolated_paths: dict[str, Path]) -> None:
        with pytest.raises(FileNotFoundError, match="No DEM tiles"):
            dl.precompute_slope_raster()

    def test_skips_when_slope_raster_exists(self, isolated_paths: dict[str, Path]) -> None:
        config.SLOPE_RASTER_PATH.write_bytes(b"sentinel")
        # Also drop a DEM tile so we'd otherwise have computed.
        _write_synthetic_dem(
            isolated_paths["DEM_DIR"] / "tile.tif",
            np.tile(np.arange(10, dtype=float), (10, 1)),
        )
        result = dl.precompute_slope_raster()
        assert result == config.SLOPE_RASTER_PATH
        # File should be untouched.
        assert config.SLOPE_RASTER_PATH.read_bytes() == b"sentinel"

    def test_computes_45_degree_slope_for_constant_east_gradient(
        self, isolated_paths: dict[str, Path]
    ) -> None:
        # z[i, j] = j with unit cellsize -> 45 deg slope everywhere in the interior.
        elev = np.tile(np.arange(20, dtype=float), (20, 1))
        _write_synthetic_dem(isolated_paths["DEM_DIR"] / "tile.tif", elev)

        out = dl.precompute_slope_raster()
        assert out.exists()
        with rasterio.open(out) as src:
            data = src.read(1)
            assert src.nodata == -9999.0
            assert src.crs.to_string() == "EPSG:4326"
        interior = data[1:-1, 1:-1]
        assert np.allclose(interior, 45.0, atol=1e-4)
        # Boundaries are NoData.
        assert (data[0, :] == -9999.0).all()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_argparse_states(self) -> None:
        parser = dl._build_arg_parser()
        args = parser.parse_args(["--states", "CA", "TX"])
        assert args.states == ["CA", "TX"]
        assert args.skip_dem is False

    def test_argparse_skip_flags(self) -> None:
        parser = dl._build_arg_parser()
        args = parser.parse_args(["--skip-tcc", "--skip-landcover", "--skip-dem"])
        assert args.skip_tcc and args.skip_landcover and args.skip_dem

    def test_main_with_all_skip_flags_does_no_work(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With every step skipped, ``main`` must complete cleanly without
        touching the network or rasterio."""
        # Belt-and-suspenders: explode if any download function is called.
        for name in ("download_tcc", "download_landcover", "download_dem_tiles",
                     "precompute_slope_raster"):
            monkeypatch.setattr(
                dl, name,
                lambda *_a, **_kw: pytest.fail(f"{name} should be skipped"),  # noqa: B023
            )
        # Quiet the per-run JSONL log into tmp.
        monkeypatch.setattr(config, "LOG_DIR", tmp_path)
        rc = dl.main(["--skip-tcc", "--skip-landcover", "--skip-dem"])
        assert rc == 0
