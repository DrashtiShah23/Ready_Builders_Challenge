# AI Tools Used

This project uses AI assistance for both runtime pipeline logic (Claude as the orchestrator agent) and development assistance (Cursor in the IDE). Every use is disclosed here per the challenge requirements.

| Tool | Purpose | Version |
|---|---|---|
| Claude `claude-sonnet-4-6` (Anthropic API) | Runtime: agent orchestration via the `tool_use` API; reasons over `fetch_tcc`, `fetch_elevation`, `compute_risk_score` per batch and flags anomalies. | `claude-sonnet-4-6` |
| Cursor | Development: IDE with AI assistance for code generation, refactoring, and documentation drafting under explicit prompts and human review. | latest |

## Cases where I diverged from AI output

The two entries below are cases where Cursor's generated code deviated from the literal text of `MASTER_BUILD_PLAN.md`. I reviewed each change before accepting it; both are logged here for transparency rather than because either was rejected.

1. **Pydantic mutable-default syntax — `list[str] = []` vs `Field(default_factory=list)`** (Phase 1, `src/schemas/location.py`).
   - The build plan specified the four schema list fields as bare `list[str] = []`. Cursor implemented them with `Field(default_factory=list)`.
   - Both work in Pydantic v2 because v2 deep-copies mutable defaults per instance. `default_factory` is the idiomatic and unambiguous form, removes any ambiguity for a future Pydantic v1 reader, and is the pattern Pydantic's own docs recommend.
   - Independence is explicitly verified by `tests/test_schemas.py::test_validation_flags_default_is_independent_per_instance` (mutating one instance's list does not leak into another).
   - **Decision: kept Cursor's deviation.** Documented here because the source line differs from the literal build-plan text.

2. **`NLCD_CLASS_NAMES` lookup added to `src/config.py`** (Phase 1).
   - The build plan's Phase 1 spec for `src/config.py` did not include a code → class-name mapping. However, Phase 3's `fetch_land_cover` is required to return a `land_cover_class` string ("Evergreen Forest", "Developed, Low Intensity", etc.).
   - Cursor proactively added a `NLCD_CLASS_NAMES` dict covering all 16 NLCD codes to `src/config.py` in Phase 1, so Phase 3 has a single source of truth and reviewers can audit the full mapping in one place.
   - This is data, not logic — it belongs in config alongside the existing `FOREST_CODES` / `DEVELOPED_CODES` / `OPEN_CODES` lists. Putting it anywhere else would split the NLCD classification information across two files.
   - **Decision: kept Cursor's addition.** Flagged here because it is a content addition not requested by the build plan.

3. **`ds.sample(...)` over a cached file handle, instead of `src.read(1)[row, col]` on a fresh open per call** (Phase 3, `src/tools/tcc.py`, `src/tools/landcover.py`, and the slope read in `src/tools/elevation.py`).
   - The build plan's literal `fetch_tcc` body opens the GeoTIFF on every call and uses `src.read(1)[row, col]`, which loads the full ~3 GB NLCD band into memory for a single-pixel answer. That would be unusable at 1M points — both memory-blowout and I/O-bound — and it directly contradicts the build plan's own Phase 5 design note: "rasterio file handles are expensive to open. We open each raster file ONCE per batch."
   - Cursor implemented the equivalent corrected version: a module-level lazy-opened dataset cache, `ds.sample([(x, y)])` for the actual pixel read (which only reads the single tile that contains the point), and a public `_reset_cache()` helper so tests and pipeline shutdown can close handles cleanly.
   - The two changes are semantically identical at the per-call level (same input → same output for a single point) but make batch-scale execution feasible. Without this change, Phase 5's "open file handles once per batch" design has nothing to bind to.
   - **Decision: kept Cursor's deviation.** Flagged here because the source body materially differs from the literal Phase 3 code in `MASTER_BUILD_PLAN.md`, and because it's the largest implementation change I've made off-spec so far.

## Phase 2 — Data Downloader decisions

1. Streamed downloads write to a `.part` file and only get renamed to the final path once the byte stream finishes cleanly, because a Ctrl+C or dropped Wi-Fi mid-download otherwise leaves a half-written GeoTIFF that looks valid until rasterio tries to open it.
2. The `download_dem_tiles` loop catches per-state errors instead of bubbling them up, because one flaky USGS response shouldn't be allowed to kill a 49-state run when the other 48 are happily downloading.
3. Idempotency for DEM lives at the *tile* level rather than the state level, because USGS returns a dozen-plus 1°×1° tiles per state and a network blip mid-state should resume the missing tiles on the next run instead of re-downloading the whole state.
4. Boundary pixels in the pre-computed slope raster are written as `-9999` NoData, not zero, because Horn's method genuinely cannot compute slope at a raster edge — calling that "flat" would silently misclassify the entire rim as low-risk terrain.
5. I added `--skip-tcc`, `--skip-landcover`, and `--skip-slope` CLI flags beyond what the build plan asked for, because the bare `--states` flag still triggers ~6 GB of downloads, and I needed a way to smoke-test the CLI from pytest without burning bandwidth.
6. `STATE_FIPS` is CONUS-only (no AK/HI/territories), because the Starlink install guide and the Ready challenge brief both target the lower 48 + DC, and adding a state later is a one-line config change rather than a code rewrite.
7. DEM rasters are not downloaded during tests — the real integration test is the first manual `python -m src.data.downloader --states CA` run — because each tile is ~100 MB+ and pulling them on every test run would make the suite uselessly slow without testing anything that mocks + a synthetic GeoTIFF don't already cover.

## Phase 3 — Tools decisions

1. Each tool keeps its open rasterio dataset in a module-level cache and only reopens it when the underlying file path changes, because `rasterio.open` parses headers and allocates buffers — doing that on every call at 1M points would dominate runtime.
2. Aspect is computed per-point from a 3x3 windowed DEM read rather than pre-computed in Phase 2, because Phase 2 is already merged and the v3.0 scoring formula doesn't use aspect in the composite (only Claude's anomaly-reasoning does), so the per-point cost is acceptable and the alternative would have meant reopening Phase 2's downloader.
3. The DEM handle cache is LRU-capped at 16 open files instead of holding every tile open, because a 49-state CONUS run can produce hundreds of tiles and the OS file-descriptor table is not generous enough to hold them all simultaneously.
4. Unknown NLCD codes return `"Unknown (<code>)"` as the class name rather than raising or returning `None`, because dropping the raw integer would lose information that the analysis report and Claude's anomaly check both find useful — the value is still flagged via the class string.
5. The NoData sentinel for TCC is hard-coded as 255 *and* falls back to the dataset's declared `nodata` field, because the NLCD docs publish 255 as the canonical TCC NoData but the GeoTIFF metadata occasionally disagrees, and a missing-data classification should not depend on which source is correct.
6. TCC values outside [0, 100] are flagged as missing rather than passed through, because the NLCD legend caps canopy density at 100% and any out-of-range value indicates either a misinterpreted byte or corrupted sample — silently feeding garbage into the scoring formula would be worse than logging a clear flag.
7. Each tool exposes a `_reset_cache()` helper, because tests need to swap raster fixtures between cases without leaked file handles, and the pipeline's shutdown handler (Phase 8) needs a single clean exit point that releases every open dataset.

## Phase 4 — Ingestion Agent decisions

1. Validation runs in a deliberate order — `location_id` → dedup → null coords → Pydantic → CONUS bbox → state — because each check is positioned to give the most precise reason code possible (e.g. an obviously-null coordinate gets `NULL_COORDINATE` instead of the more generic `PARSE_ERROR` that a Pydantic-first ordering would emit).
2. An invalid `state` value drops the row rather than nulling the field, because the build plan lists `INVALID_STATE` in its drop-reasons list and silently coercing a bad state would let upstream data-quality issues hide instead of surfacing in the per-reason summary.
3. A single `_is_null` helper centralises the "what counts as missing" definition across `None`, `pandas.NA`, `float("nan")`, and whitespace-only strings, because CSVs ingested from different sources express missingness differently and inconsistent handling would let some null variants leak through unflagged.
4. Each chunk is converted to dicts via `chunk.to_dict(orient="records")` rather than `itertuples`, because `to_dict` is slightly slower but eliminates the column-presence edge cases that `itertuples` named-tuple access introduces when an optional column is absent.
5. The `run` generator wraps its body in `try/finally` so the `INGESTION_SUMMARY` event is logged even when a downstream consumer abandons iteration partway through, because a half-finished pipeline run is exactly when you most want to know the partial drop counts.
6. Batch IDs are monotonic `batch-NNNNNN` strings stamped at validation time, because tracing one bad record from the input CSV through the orchestrator log requires a stable identifier that all rows in the same batch share and the build plan's spec for `ValidatedLocation.batch_id` is a `str`.
7. State values are normalised to canonical upper-case before being written into `ValidatedLocation`, because the USGS API and the downstream Parquet partition key both expect `"CA"`-style abbreviations and a mix of `"CA"` / `"Ca"` / `"ca"` would fragment partitions and break joins.
8. Optional `state` and `county` columns are backfilled with null if absent from the CSV (instead of raising), because the build plan's `EXPECTED_CSV_COLUMNS` is documented as "expected" not "required" and a CSV that only ships `location_id, latitude, longitude` should still ingest cleanly — every other field is enrichment, not validation.
