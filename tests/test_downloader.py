"""
Phase 2 tests: data downloader.

Covers every public function in ``src.data.downloader``:
    download_tcc, download_landcover, download_dem_tiles, precompute_slope_raster
plus the Horn's-method numerical kernel and the CLI argument parser.

Post-Phase-8 the downloader was migrated from S3 bulk zips to MRLC's WCS
service (TCC + Land Cover) and from the broken TNM ``polyType=state``
filter to the working TNM ``bbox`` filter (DEM). These tests cover the
new code paths end-to-end with mocked HTTP — network is fully mocked. The
slope-pre-computation test creates a real (but tiny) GeoTIFF in a tmp
dir using rasterio so the rasterio code path is exercised end-to-end
without touching the live USGS endpoints.
"""
from __future__ import annotations

import io
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


def _patch_stream_capture(monkeypatch: pytest.MonkeyPatch, content: bytes) -> list[dict[str, Any]]:
    """Replace ``httpx.stream`` with a recorder that returns ``content``.

    Returns the list each call appends to so the test can assert on the
    URL, params, method, etc.
    """
    calls: list[dict[str, Any]] = []

    def fake_stream(method: str, url: str, **kwargs: Any) -> _FakeStreamResponse:
        calls.append({"method": method, "url": url, **kwargs})
        return _FakeStreamResponse(content)

    monkeypatch.setattr(dl.httpx, "stream", fake_stream)
    return calls


# ---------------------------------------------------------------------------
# Bbox helpers
# ---------------------------------------------------------------------------


class TestBboxHelpers:
    def test_states_to_bbox_single_state(self) -> None:
        bbox = dl._states_to_bbox_wgs84(["NC"])
        assert bbox == config.STATE_BBOX_WGS84["NC"]

    def test_states_to_bbox_case_insensitive(self) -> None:
        bbox = dl._states_to_bbox_wgs84(["nc"])
        assert bbox == config.STATE_BBOX_WGS84["NC"]

    def test_states_to_bbox_unknown_state_raises(self) -> None:
        with pytest.raises(ValueError, match="No bounding box configured"):
            dl._states_to_bbox_wgs84(["ZZ"])

    def test_states_to_bbox_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="At least one state"):
            dl._states_to_bbox_wgs84([])

    def test_states_to_bbox_union_of_multiple_states(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A multi-state request returns the union (smallest enclosing rect)."""
        monkeypatch.setattr(
            config,
            "STATE_BBOX_WGS84",
            {
                "NC": (-84.32, 33.75, -75.46, 36.59),
                "VA": (-83.68, 36.54, -75.24, 39.47),
            },
        )
        lon_min, lat_min, lon_max, lat_max = dl._states_to_bbox_wgs84(["NC", "VA"])
        # Union: most-west lon, most-south lat, most-east lon, most-north lat.
        assert lon_min == -84.32
        assert lat_min == 33.75
        assert lon_max == -75.24
        assert lat_max == 39.47

    def test_project_bbox_to_5070_buffer_applied(self) -> None:
        """The 5070 projection adds a small pixel-buffer to all four sides."""
        nc = config.STATE_BBOX_WGS84["NC"]
        x_min, y_min, x_max, y_max = dl._project_bbox_to_5070(nc, buffer_m=60.0)
        # NC in 5070 sits roughly in the (1.0e6, 1.25e6) -> (1.87e6, 1.70e6) range.
        assert 1_000_000 < x_min < 1_100_000
        assert 1_800_000 < x_max < 1_950_000
        assert 1_200_000 < y_min < 1_300_000
        assert 1_650_000 < y_max < 1_750_000
        # Buffer expands the box slightly relative to the un-buffered version.
        x_min_nb, _, x_max_nb, _ = dl._project_bbox_to_5070(nc, buffer_m=0.0)
        assert x_min < x_min_nb < x_max_nb < x_max


# ---------------------------------------------------------------------------
# download_tcc (WCS-based)
# ---------------------------------------------------------------------------


class TestDownloadTCC:
    def test_skips_when_raster_already_present(
        self, isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        existing = isolated_paths["TCC_DIR"] / "nlcd_tcc_already_here.tif"
        existing.write_bytes(b"not-a-real-geotiff-but-the-sentinel-suffices")

        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("download_tcc should not network when raster exists")

        monkeypatch.setattr(dl.httpx, "stream", boom)

        result = dl.download_tcc(["NC"])
        assert result == existing

    def test_wcs_request_built_correctly(
        self, isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """download_tcc must hit the WCS base URL with the right coverage
        id, format, and EPSG:5070-projected X/Y subsets."""
        fake_tif = b"GeoTIFF\x00placeholder for TCC"
        calls = _patch_stream_capture(monkeypatch, fake_tif)

        result = dl.download_tcc(["NC"])

        assert result.suffix == ".tif"
        assert result.read_bytes() == fake_tif
        # No .part file lingers after the atomic rename.
        assert not list(isolated_paths["TCC_DIR"].glob("*.part"))

        assert len(calls) == 1
        call = calls[0]
        assert call["method"] == "GET"
        assert call["url"] == config.MRLC_WCS_BASE
        params = call["params"]
        assert params["service"] == "WCS"
        assert params["version"] == config.MRLC_WCS_VERSION
        assert params["request"] == "GetCoverage"
        assert params["coverageid"] == config.MRLC_TCC_COVERAGE_ID
        assert params["format"] == "image/tiff"
        # Two subset parameters, one for X and one for Y in EPSG:5070.
        subsets = params["subset"]
        assert len(subsets) == 2
        assert subsets[0].startswith("X(")
        assert subsets[1].startswith("Y(")

    def test_default_states_uses_config_bbox_dict(
        self, isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Omitting ``states`` should use every configured state — today
        that's NC, so the resulting file name encodes 'NC'."""
        calls = _patch_stream_capture(monkeypatch, b"tiff")
        result = dl.download_tcc()
        assert "NC" in result.name
        assert len(calls) == 1

    def test_filename_encodes_requested_states(
        self, isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_stream_capture(monkeypatch, b"tiff")
        result = dl.download_tcc(["NC"])
        # Single-state filename: ..._NC.tif
        assert result.name.endswith("__NC.tif")


# ---------------------------------------------------------------------------
# download_landcover (WCS-based)
# ---------------------------------------------------------------------------


class TestDownloadLandcover:
    def test_skips_when_raster_already_present(
        self, isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        existing = isolated_paths["LC_DIR"] / "nlcd_lc.tif"
        existing.write_bytes(b"placeholder")

        def boom(*_a: Any, **_k: Any) -> None:
            pytest.fail("should not network when raster exists")

        monkeypatch.setattr(dl.httpx, "stream", boom)
        assert dl.download_landcover(["NC"]) == existing

    def test_wcs_request_uses_landcover_coverage_id(
        self, isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_tif = b"GeoTIFF placeholder for LC"
        calls = _patch_stream_capture(monkeypatch, fake_tif)
        result = dl.download_landcover(["NC"])
        assert result.read_bytes() == fake_tif
        assert calls[0]["params"]["coverageid"] == config.MRLC_LANDCOVER_COVERAGE_ID


# ---------------------------------------------------------------------------
# TNM bbox query (replaces former polyCode-based query)
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


class _FakeHTTPResponse_NonJSON:
    """Mimics TNM's flaky "HTML instead of JSON" responses."""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        # json.JSONDecodeError is a ValueError — same exception the live
        # API surfaces when it returns HTML/garbage.
        import json
        raise json.JSONDecodeError("Expecting property name", "<html>...</html>", 1)


class TestTNMBboxQuery:
    def test_builds_bbox_request(self) -> None:
        """The bbox query must send the bbox in `lon_min,lat_min,lon_max,lat_max`
        order with the configured dataset name."""
        record: list[dict[str, Any]] = []
        fake_client = _FakeHTTPClient({"items": []}, record)
        items = dl._tnm_query_bbox(
            (-84.32, 33.75, -75.46, 36.59),
            _client=fake_client,  # type: ignore[arg-type]
        )
        assert items == []
        call = record[0]
        assert call["url"] == config.TNM_API_BASE
        assert call["params"]["bbox"] == "-84.32,33.75,-75.46,36.59"
        assert call["params"]["prodFormats"] == "GeoTIFF"
        assert call["params"]["datasets"] == config.TNM_DEM_DATASET
        # Pagination size is read from config so the 50-cap stays auditable.
        assert call["params"]["max"] == config.TNM_PAGE_SIZE

    def test_returns_raw_items_unmodified(self) -> None:
        record: list[dict[str, Any]] = []
        fake_payload = {
            "items": [
                {"title": "USGS 1 Arc Second n34w078 20250507", "downloadURL": "x"},
                {"title": "USGS 1 Arc Second n34w079 20260320", "downloadURL": "y"},
            ]
        }
        fake_client = _FakeHTTPClient(fake_payload, record)
        items = dl._tnm_query_bbox(
            (-84.32, 33.75, -75.46, 36.59),
            _client=fake_client,  # type: ignore[arg-type]
        )
        assert len(items) == 2
        assert items[0]["title"].startswith("USGS 1 Arc Second n34w078")

    def test_retries_on_transient_non_json(self) -> None:
        """TNM occasionally returns HTML instead of JSON under load. The
        downloader must retry up to ``_max_attempts`` before giving up."""

        class _FlakyClient:
            """Returns non-JSON on the first call, valid JSON on the second."""
            def __init__(self) -> None:
                self.calls = 0

            def __enter__(self) -> "_FlakyClient":
                return self

            def __exit__(self, *_exc: Any) -> None:
                return None

            def close(self) -> None:
                return None

            def get(self, url: str, params: dict[str, Any]) -> Any:
                self.calls += 1
                if self.calls == 1:
                    return _FakeHTTPResponse_NonJSON()
                return _FakeHTTPResponse({"items": [{"title": "USGS 1 Arc Second n34w078 20250507"}]})

        client = _FlakyClient()
        items = dl._tnm_query_bbox(
            (-84.32, 33.75, -75.46, 36.59),
            _client=client,  # type: ignore[arg-type]
            _backoff_seconds=0.0,  # no real wait in tests
        )
        assert client.calls == 2
        assert len(items) == 1

    def test_retries_on_empty_items_then_recovers(self) -> None:
        """TNM sometimes returns valid JSON with ``items: []`` for a bbox
        that actually has tiles. Treat empty as transient and retry."""

        class _FlakyEmptyClient:
            def __init__(self) -> None:
                self.calls = 0

            def __enter__(self) -> "_FlakyEmptyClient":
                return self

            def __exit__(self, *_exc: Any) -> None:
                return None

            def close(self) -> None:
                return None

            def get(self, url: str, params: dict[str, Any]) -> Any:
                self.calls += 1
                if self.calls < 3:
                    return _FakeHTTPResponse({"items": []})
                return _FakeHTTPResponse({"items": [{"title": "tile"}]})

        client = _FlakyEmptyClient()
        items = dl._tnm_query_bbox(
            (-84.32, 33.75, -75.46, 36.59),
            _client=client,  # type: ignore[arg-type]
            _backoff_seconds=0.0,
        )
        assert client.calls == 3
        assert items == [{"title": "tile"}]

    def test_empty_items_returned_when_flag_disabled(self) -> None:
        """If the caller disables the empty-as-transient heuristic, the
        first empty result must be returned without retries."""

        class _AlwaysEmpty:
            def __init__(self) -> None:
                self.calls = 0

            def __enter__(self) -> "_AlwaysEmpty":
                return self

            def __exit__(self, *_exc: Any) -> None:
                return None

            def close(self) -> None:
                return None

            def get(self, url: str, params: dict[str, Any]) -> Any:
                self.calls += 1
                return _FakeHTTPResponse({"items": []})

        client = _AlwaysEmpty()
        items = dl._tnm_query_bbox(
            (-84.32, 33.75, -75.46, 36.59),
            _client=client,  # type: ignore[arg-type]
            _backoff_seconds=0.0,
            _treat_empty_as_transient=False,
        )
        assert client.calls == 1
        assert items == []

    def test_gives_up_after_max_attempts(self) -> None:
        """Persistent non-JSON should raise after the retry budget is
        exhausted — the outer ``download_dem_tiles`` catches the ValueError."""

        class _AlwaysFlakyClient:
            def __init__(self) -> None:
                self.calls = 0

            def __enter__(self) -> "_AlwaysFlakyClient":
                return self

            def __exit__(self, *_exc: Any) -> None:
                return None

            def close(self) -> None:
                return None

            def get(self, url: str, params: dict[str, Any]) -> Any:
                self.calls += 1
                return _FakeHTTPResponse_NonJSON()

        client = _AlwaysFlakyClient()
        with pytest.raises(ValueError):
            dl._tnm_query_bbox(
                (-84.32, 33.75, -75.46, 36.59),
                _client=client,  # type: ignore[arg-type]
                _max_attempts=3,
                _backoff_seconds=0.0,
            )
        assert client.calls == 3


class TestDedupeTilesLatestVintage:
    def test_picks_latest_vintage_per_quad(self) -> None:
        items = [
            {
                "title": "USGS 1 Arc Second n34w079 20250507",
                "boundingBox": {"minY": 33.0, "minX": -79.0},
            },
            {
                "title": "USGS 1 Arc Second n34w079 20260320",  # newer
                "boundingBox": {"minY": 33.0, "minX": -79.0},
            },
            {
                "title": "USGS 1 Arc Second n35w080 20220504",
                "boundingBox": {"minY": 34.5, "minX": -80.4},  # different quad
            },
        ]
        deduped = dl._dedupe_tiles_latest_vintage(items)
        # Two quads -> two deduped tiles.
        assert len(deduped) == 2
        # The n34w079 quad picked the 20260320 vintage.
        n34w079 = next(d for d in deduped if "n34w079" in d["title"])
        assert "20260320" in n34w079["title"]

    def test_skips_items_with_no_bbox(self) -> None:
        items = [
            {"title": "no-bbox", "boundingBox": {}},
            {"title": "x", "boundingBox": {"minY": 1.0, "minX": 1.0}},
        ]
        deduped = dl._dedupe_tiles_latest_vintage(items)
        # The no-bbox item is dropped; the well-formed item survives.
        assert len(deduped) == 1
        assert deduped[0]["title"] == "x"

    def test_empty_input_returns_empty(self) -> None:
        assert dl._dedupe_tiles_latest_vintage([]) == []


# ---------------------------------------------------------------------------
# download_dem_tiles (bbox-driven)
# ---------------------------------------------------------------------------


class TestDownloadDEMTiles:
    @pytest.fixture
    def stub_tnm_and_stream(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_paths: dict[str, Path],
    ) -> dict[str, Any]:
        """Stub the TNM bbox query + the streaming downloader."""
        downloads: list[str] = []

        def fake_query(bbox: Any, **_kw: Any) -> list[dict[str, Any]]:
            return [
                {
                    "downloadURL": "https://example.test/nc_tile1.tif",
                    "title": "USGS 1 Arc Second n34w078 20250507",
                    "boundingBox": {"minY": 33.0, "minX": -78.0},
                },
                {
                    "downloadURL": "https://example.test/nc_tile2.tif",
                    "title": "USGS 1 Arc Second n35w080 20260320",
                    "boundingBox": {"minY": 34.5, "minX": -80.4},
                },
            ]

        def fake_stream(url: str, dest: Path, *_a: Any, **_kw: Any) -> Path:
            downloads.append(url)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake-dem")
            return dest

        monkeypatch.setattr(dl, "_tnm_query_bbox", fake_query)
        monkeypatch.setattr(dl, "_stream_download", fake_stream)

        class _NoopClient:
            def __init__(self, *_a: Any, **_kw: Any) -> None: ...
            def __enter__(self) -> "_NoopClient": return self
            def __exit__(self, *_exc: Any) -> None: ...
            def close(self) -> None: ...

        monkeypatch.setattr(dl.httpx, "Client", _NoopClient)
        return {"downloads": downloads, "paths": isolated_paths}

    def test_downloads_tiles_for_state_bbox(
        self, stub_tnm_and_stream: dict[str, Any]
    ) -> None:
        tiles = dl.download_dem_tiles(["NC"])
        # Two stubbed tiles, both downloaded.
        assert len(tiles) == 2
        assert len(stub_tnm_and_stream["downloads"]) == 2
        assert "https://example.test/nc_tile1.tif" in stub_tnm_and_stream["downloads"]

    def test_skips_already_present_tiles(
        self, stub_tnm_and_stream: dict[str, Any]
    ) -> None:
        dem_dir = stub_tnm_and_stream["paths"]["DEM_DIR"]
        (dem_dir / "nc_tile1.tif").write_bytes(b"existing")
        tiles = dl.download_dem_tiles(["NC"])
        urls = stub_tnm_and_stream["downloads"]
        assert "https://example.test/nc_tile1.tif" not in urls
        assert "https://example.test/nc_tile2.tif" in urls
        # Both tiles end up present on disk regardless.
        assert len(tiles) == 2

    def test_unknown_state_raises(self) -> None:
        """A state without a configured bbox can't be downloaded — surface
        the error before issuing any HTTP."""
        with pytest.raises(ValueError, match="No bounding box configured"):
            dl.download_dem_tiles(["ZZ"])

    def test_dedupe_collapses_repeated_vintages(
        self, monkeypatch: pytest.MonkeyPatch, isolated_paths: dict[str, Path]
    ) -> None:
        """Two TNM results for the same 1° quad must produce one tile
        download (the latest vintage)."""
        def fake_query(bbox: Any, **_kw: Any) -> list[dict[str, Any]]:
            return [
                {
                    "downloadURL": "https://example.test/n34w079_v1.tif",
                    "title": "USGS 1 Arc Second n34w079 20250507",
                    "boundingBox": {"minY": 33.0, "minX": -79.0},
                },
                {
                    "downloadURL": "https://example.test/n34w079_v2.tif",
                    "title": "USGS 1 Arc Second n34w079 20260320",
                    "boundingBox": {"minY": 33.0, "minX": -79.0},
                },
            ]

        downloads: list[str] = []

        def fake_stream(url: str, dest: Path, *_a: Any, **_kw: Any) -> Path:
            downloads.append(url)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake-dem")
            return dest

        monkeypatch.setattr(dl, "_tnm_query_bbox", fake_query)
        monkeypatch.setattr(dl, "_stream_download", fake_stream)

        class _NoopClient:
            def __init__(self, *_a: Any, **_kw: Any) -> None: ...
            def __enter__(self) -> "_NoopClient": return self
            def __exit__(self, *_exc: Any) -> None: ...
            def close(self) -> None: ...

        monkeypatch.setattr(dl.httpx, "Client", _NoopClient)

        tiles = dl.download_dem_tiles(["NC"])
        assert len(downloads) == 1
        # The v2 (latest) URL is the one that was downloaded.
        assert downloads[0].endswith("n34w079_v2.tif")
        assert len(tiles) == 1


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
        j = np.arange(10)
        z = np.tile(j.astype(float), (10, 1))
        slope = dl._horn_slope_degrees(z, 1.0, 1.0)
        interior = slope[1:-1, 1:-1]
        assert np.allclose(interior, 45.0, atol=1e-9)

    def test_constant_north_slope_is_45_degrees(self) -> None:
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
        assert np.nanmean(fine[1:-1, 1:-1]) > np.nanmean(coarse[1:-1, 1:-1])

    def test_1d_input_raises(self) -> None:
        with pytest.raises(ValueError, match="2D"):
            dl._horn_slope_degrees(np.arange(10).astype(float), 1.0, 1.0)


# ---------------------------------------------------------------------------
# precompute_slope_raster (real rasterio I/O on a synthetic DEM)
# ---------------------------------------------------------------------------


def _write_synthetic_dem(
    path: Path, elev: np.ndarray, crs: str = "EPSG:5070"
) -> None:
    """Write a tiny GeoTIFF DEM with a known elevation array.

    Defaults to EPSG:5070 (Conus Albers, metres) so that a unit cellsize
    corresponds to 1 m and the kernel sees the same units it'd see on a
    real projected DEM. Tests that need a geographic CRS (to exercise
    the degrees→metres conversion code path) pass ``crs="EPSG:4326"``
    explicitly.
    """
    height, width = elev.shape
    transform = from_origin(west=0.0, north=float(height), xsize=1.0, ysize=1.0)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": height,
        "width": width,
        "transform": transform,
        "crs": crs,
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
        _write_synthetic_dem(
            isolated_paths["DEM_DIR"] / "tile.tif",
            np.tile(np.arange(10, dtype=float), (10, 1)),
        )
        result = dl.precompute_slope_raster()
        assert result == config.SLOPE_RASTER_PATH
        assert config.SLOPE_RASTER_PATH.read_bytes() == b"sentinel"

    def test_computes_45_degree_slope_for_constant_east_gradient(
        self, isolated_paths: dict[str, Path]
    ) -> None:
        """Projected-CRS path: 1 m cellsize × 1 m elevation step → 45°."""
        elev = np.tile(np.arange(20, dtype=float), (20, 1))
        _write_synthetic_dem(
            isolated_paths["DEM_DIR"] / "tile.tif", elev, crs="EPSG:5070"
        )

        out = dl.precompute_slope_raster()
        assert out.exists()
        with rasterio.open(out) as src:
            data = src.read(1)
            assert src.nodata == -9999.0
            # Slope raster inherits the input DEM's CRS.
            assert src.crs.to_string() == "EPSG:5070"
        interior = data[1:-1, 1:-1]
        assert np.allclose(interior, 45.0, atol=1e-4)
        assert (data[0, :] == -9999.0).all()

    def test_geographic_crs_converts_degrees_to_meters(
        self, isolated_paths: dict[str, Path]
    ) -> None:
        """Regression test for the original "slope == 90° everywhere" bug.

        A 30 m × 30 m DEM in EPSG:4326 with a 1 m east gradient should
        produce slope ≈ atan(1/30) ≈ 1.91°. If the slope code mistakes
        the 0.000277° (≈30 m) cellsize for being in metres, slope blows
        up to ~atan(1/0.000277) ≈ 89.98°.
        """
        elev = np.tile(np.arange(20, dtype=float), (20, 1))
        # 1/3600° ≈ 30 m at the equator; pick a tile spanning lat 35° so the
        # downloader's cos(lat) conversion kicks in.
        pixel_deg = 1.0 / 3600.0
        transform = from_origin(
            west=-79.0, north=36.0, xsize=pixel_deg, ysize=pixel_deg
        )
        path = isolated_paths["DEM_DIR"] / "geographic_dem.tif"
        profile = {
            "driver": "GTiff",
            "dtype": "float32",
            "count": 1,
            "height": elev.shape[0],
            "width": elev.shape[1],
            "transform": transform,
            "crs": "EPSG:4326",  # geographic — degrees, not metres
            "nodata": -9999.0,
        }
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(elev.astype(np.float32), 1)

        out = dl.precompute_slope_raster()
        with rasterio.open(out) as src:
            data = src.read(1)
        interior = data[1:-1, 1:-1]
        finite = interior[interior != -9999.0]
        # 1 m elevation rise per pixel; pixel ≈ 30 m × cos(35°) ≈ 25 m east-west.
        # So slope ≈ atan(1/25) ≈ 2.3°. Wide tolerance (1.5–4°) so the test
        # is sturdy against the exact ``deg_to_m`` constant we chose.
        median = float(np.median(finite))
        assert 1.5 < median < 4.0, (
            f"Expected NC-latitude geographic slope median ≈ 2°, got {median:.2f}°. "
            f"This usually means the downloader is treating degrees as metres."
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_argparse_states(self) -> None:
        parser = dl._build_arg_parser()
        args = parser.parse_args(["--states", "NC"])
        assert args.states == ["NC"]
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
        for name in ("download_tcc", "download_landcover", "download_dem_tiles",
                     "precompute_slope_raster"):
            monkeypatch.setattr(
                dl, name,
                lambda *_a, **_kw: pytest.fail(f"{name} should be skipped"),  # noqa: B023
            )
        monkeypatch.setattr(config, "LOG_DIR", tmp_path)
        rc = dl.main(["--skip-tcc", "--skip-landcover", "--skip-dem"])
        assert rc == 0

    def test_main_threads_states_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`main --states NC --skip-dem` must reach download_tcc / download_landcover
        with ``["NC"]`` as the positional ``states`` argument."""
        seen: dict[str, Any] = {}

        def fake_tcc(states: Any, _logger: Any) -> Path:
            seen["tcc_states"] = list(states) if states else None
            return tmp_path / "fake_tcc.tif"

        def fake_lc(states: Any, _logger: Any) -> Path:
            seen["lc_states"] = list(states) if states else None
            return tmp_path / "fake_lc.tif"

        monkeypatch.setattr(dl, "download_tcc", fake_tcc)
        monkeypatch.setattr(dl, "download_landcover", fake_lc)
        monkeypatch.setattr(config, "LOG_DIR", tmp_path)

        rc = dl.main(["--states", "NC", "--skip-dem"])
        assert rc == 0
        assert seen["tcc_states"] == ["NC"]
        assert seen["lc_states"] == ["NC"]
