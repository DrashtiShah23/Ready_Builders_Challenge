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

3. _[To be filled as further deviations occur during the build]_

## Phase 2 — Data Downloader decisions

1. Streamed downloads write to a `.part` file and only get renamed to the final path once the byte stream finishes cleanly, because a Ctrl+C or dropped Wi-Fi mid-download otherwise leaves a half-written GeoTIFF that looks valid until rasterio tries to open it.
2. The `download_dem_tiles` loop catches per-state errors instead of bubbling them up, because one flaky USGS response shouldn't be allowed to kill a 49-state run when the other 48 are happily downloading.
3. Idempotency for DEM lives at the *tile* level rather than the state level, because USGS returns a dozen-plus 1°×1° tiles per state and a network blip mid-state should resume the missing tiles on the next run instead of re-downloading the whole state.
4. Boundary pixels in the pre-computed slope raster are written as `-9999` NoData, not zero, because Horn's method genuinely cannot compute slope at a raster edge — calling that "flat" would silently misclassify the entire rim as low-risk terrain.
5. I added `--skip-tcc`, `--skip-landcover`, and `--skip-slope` CLI flags beyond what the build plan asked for, because the bare `--states` flag still triggers ~6 GB of downloads, and I needed a way to smoke-test the CLI from pytest without burning bandwidth.
6. `STATE_FIPS` is CONUS-only (no AK/HI/territories), because the Starlink install guide and the Ready challenge brief both target the lower 48 + DC, and adding a state later is a one-line config change rather than a code rewrite.
7. DEM rasters are not downloaded during tests — the real integration test is the first manual `python -m src.data.downloader --states CA` run — because each tile is ~100 MB+ and pulling them on every test run would make the suite uselessly slow without testing anything that mocks + a synthetic GeoTIFF don't already cover.
