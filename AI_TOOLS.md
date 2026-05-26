# AI Tools Used

This project uses AI assistance for both runtime pipeline logic (Claude as the orchestrator agent) and development assistance (Cursor in the IDE). Every use is disclosed here per the challenge requirements.

| Tool | Purpose | Version |
|---|---|---|
| Claude `claude-sonnet-4-6` (Anthropic API) | Runtime: pipeline orchestration via the `tool_use` API. **One** API call per run, five pipeline-level tools (`ingest_locations`, `sample_environment`, `score_risk`, `validate_results`, `generate_report`). Reasons about each step's summary (data quality, missing-data rates, tier distribution, validation anomalies), decides whether to proceed, and writes the plain-English end-of-run summary. Per-location reasoning is preserved in `run_interactive` for single-coordinate queries. | `claude-sonnet-4-6` |
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

## Phase 5 — Environmental Agent decisions

1. The agent calls each tool module's private `_ensure_dataset` / `_build_dem_index` once per batch via a `_warm_caches` step instead of letting the tools open lazily on the first row, because explicit cache-warming surfaces a `CACHE_WARMED` event in the JSONL log with the per-dataset open status — which makes it observable that the build plan's "open file handles ONCE per batch" intent is actually being enforced, not just happening accidentally.
2. `SLOPE_MISSING` is logged as a separate flag from `ELEVATION_MISSING` even though both come from the same `fetch_elevation` call, because the v3.0 scoring formula uses slope (terrain weight = 30%) but does not use raw elevation, so the two missing-data populations have different downstream blast radius and a reviewer auditing scoring drop-outs needs to filter on the slope flag specifically.
3. `ASPECT_MISSING` is emitted alongside the other missing flags despite being ambiguous (null can mean "fetch failed" or "genuinely flat terrain"), because the alternative — silently nulling aspect — would lose the fact that the agent did attempt to read it, and the ambiguity is documented in the agent's module docstring so a reviewer sees the caveat next to the flag.
4. A `FETCH_EXCEPTION` flag is added on top of the per-tool missing flag whenever a tool raises (not returns) an error, because the Phase 3 tools are written never to raise, so if one ever does it's a real bug and the flag is what makes that bug visible in a 1M-row run instead of being swallowed by the per-row try/except.
5. The agent imports the tool modules (`tcc_mod`, `landcover_mod`, `elevation_mod`) by alias and references their private `_ensure_*` helpers directly, because the alternative — adding a `warm()` public method to each tool — would mean three more API surfaces to keep stable when the only legitimate caller is this one agent. The coupling is explicit and one-directional, and `tests/test_environmental.py` proves the patch target is stable.
6. `enrich_batch` returns the empty list (and logs an `EMPTY_BATCH` event) without warming caches when handed `[]`, because the orchestrator may legitimately emit an empty trailing partial batch from ingestion, and warming + an empty `ENRICH_BATCH_DONE` event would just be noise in the log.
7. Cumulative per-signal missing counts and rates are tracked on the agent instance and exposed via `log_run_summary`, because the orchestrator (Phase 7) needs an end-of-run snapshot of data-quality across every batch — and pushing that responsibility into the orchestrator would mean either re-walking every batch's log entry or duplicating the counter logic, both of which are worse than letting the agent own the rollup of its own work.

## Phase 6 — Risk Scoring decisions

1. The composite is rounded to 4 decimal places *inside* `score_components` *before* being handed to `tier_for`, because the natural outputs of the formula (combinations of weights × bucket scores) hit clean values like 0.6 exactly in math but can drift to 0.6000000000000001 in IEEE 754 — and an un-rounded 0.6 + 1e-16 would flip a borderline location into "High" by an invisible epsilon.
2. `tier_for` itself does NOT round its input, because the STOP-gate boundary test (0.599 → Moderate, 0.600 → High) calls `tier_for` directly with hand-crafted boundary values and rounding inside the comparator would corrupt that test surface — the rounding belongs at the composite-computation step, not the tier-mapping step.
3. Non-classified NLCD codes (11 Open Water, 12 Ice/Snow, 90/95 Wetlands, and any unknown integer) score 0.0 AND emit a `LANDCOVER_UNHANDLED_CODE` flag, because silently lumping them with `OPEN_CODES` would lose the audit trail and silently raising would break partial scoring — TCC already captures any canopy density at wetland sites, so the 0.0 default isn't lying so much as deferring to TCC for that signal.
4. Slope-missing uses the flag name `SLOPE_MISSING` (matching Phase 5's `EnvFlag.SLOPE_MISSING`) rather than the build plan's literal `ELEVATION_MISSING` text in the Phase 6 spec, because the two phases need a single coherent flag vocabulary for `ScoredLocation.all_flags` and using two different names for the same condition would force every downstream consumer to alias them anyway.
5. The agent exposes both `score_components(...)` (returns a plain dict) and `compute_risk_score(enriched)` (returns a full `ScoredLocation`), because Phase 7's Claude tool dispatcher needs a JSON-serialisable return value to hand back as `tool_result` content, but the orchestrator's non-Claude fallback path needs the full Pydantic object straight away — and a single function couldn't cleanly serve both.
6. `score_components` accepts a `latitude` keyword argument that the v3.0 formula ignores, because Phase 7's Claude tool schema declares latitude as a required input for forward compatibility with hemisphere-aware scoring (south-facing CONUS dishes need slope/aspect orientation context) and the agent and Claude tool surfaces must match.
7. The three bucket-output scores are named `_SCORE_HIGH / _SCORE_MODERATE / _SCORE_LOW` even though they're just `1.0 / 0.5 / 0.0`, because (a) naming the buckets disambiguates them from the configurable weights in the source (otherwise `0.5` could mean either `TCC_WEIGHT` or the moderate bucket) and (b) it lets the regression test `test_no_hardcoded_thresholds_in_scoring_module` scan the executable code for accidental weight literals without false-positives on the bucket values.
8. `ScoredLocation.all_flags` is order-preserved-deduplicated with env flags BEFORE scoring flags, because a reviewer reading the flag list cares first about what the upstream data-quality state was (env_fetch_flags) and only then about what the scoring engine itself flagged — and `set()` + sorting would scramble that storyline.

## Phase 7 — Claude Orchestrator architectural redesign

Before implementing Phase 7 I changed the agent design that was in the build plan. The change is large enough to warrant its own section.

**Original design (replaced):** Claude reasons per batch of ~100 locations, calling `fetch_tcc`, `fetch_elevation`, and `compute_risk_score` once per row. At 4.67M rows that would mean ~46,700 Claude calls and roughly $10,600 in API spend, which is incompatible with running the full North Carolina dataset that ships in `data/locations.csv`.

**New design (implemented):** Claude makes ONE API call per pipeline run. It is given five pipeline-level tools — `ingest_locations`, `sample_environment`, `score_risk`, `validate_results`, `generate_report` — and each tool internally runs the full dataset through the existing Phase 1-6 agents (none of `ingestion.py`, `environmental.py`, `scoring.py`, `tcc.py`, `elevation.py`, or `landcover.py` was modified). Claude reasons about each step's summary (valid_pct, missing-data rates, tier distribution, validation anomalies), decides whether to proceed, and writes the plain-English end-of-run summary. Total cost: under $1 for the full 4.67M-row dataset.

Decisions tied to the redesign:

1. **Per-location Claude reasoning is preserved in interactive mode only.** `PipelineOrchestrator.run_interactive(lat, lon)` is the one path where Claude sees an individual location — it costs ~$0.01 per query and demonstrates the "user gives coordinates, agent explains sky visibility" agentic scenario from the challenge brief at a cost shape that makes sense for one-off queries. Applying that same per-location call pattern at 4.67M rows is what the redesign is replacing.

2. **The five tools each persist their output to parquet** (`data/processed/validated_locations.parquet`, `enriched_locations.parquet`, `scored_locations.parquet`). The next tool reads the previous tool's parquet, so Claude only ever sees JSON summaries — never individual rows. That is the whole point: Claude's reasoning lives at the level where it can move the needle (data quality calls, anomaly flagging, distribution sanity checks), and the per-row work stays in deterministic Python.

3. **Four validation checks fire between scoring and reporting**: distribution sanity (>80% in any one tier flags a threshold calibration issue), cross-validation (forest-classified pixels with `tcc_pct < 10` flagged as likely clear-cut / data-vintage mismatch), geographic sanity (rows outside the NC bounding box halt the pipeline), and missing-data rate (any single signal >15% missing flags as a warning). Geographic-sanity failure is the only check that halts the pipeline; the other three degrade status to "warnings" and surface in the report.

4. **`MAX_AGENT_TURNS = 20` bounds the tool call loop.** If Claude never returns `end_turn`, the loop exits cleanly with a `MAX_TURNS_REACHED` log event rather than spinning forever. The cap is intentionally generous — at five real tool calls plus a few clarification turns the budget is far below 20 — so a healthy run never hits it but a pathological prompt-injection scenario can't run the bill up.

5. **`CLAUDE_BATCH_SIZE` was removed from `src/config.py`** because the new design no longer reasons per batch of locations. The legacy `IngestionAgent.run()` default-fallback path that referenced it is unused — the orchestrator always passes `batch_size=config.RASTER_BATCH_SIZE` explicitly — but per Phase 7's "do not touch ingestion.py" guidance the docstring in `ingestion.py` was left stale and the contract is documented in this file instead.

6. **System prompt versioned at `src/agents/prompts/orchestrator_v1.txt`** rather than hardcoded in Python, so prompt revisions are reviewable in git diffs and the prompt artifact is auditable separately from the loader.

7. **Cost projection is documented in `src/config.py`** alongside the agent config block so a reviewer reading the constants sees the redesign's economic basis without having to dig through this file.

## Phase 4 follow-up — geoid_cb derivation

1. The real locations.csv (handed over after Phase 6) carries a `geoid_cb` column instead of separate `state` / `county` columns, so ingestion now derives state abbreviation and county GEOID from the first 5 digits of the 15-digit Census Block GEOID — pure data-already-present extraction, no extra column to ask the user for.
2. Explicit user-supplied `state` always wins over derived state, because future CSVs may include both columns and the user's explicit value is by definition more trustworthy than a derived one (and the test `test_explicit_state_wins_over_geoid` enforces the precedence).
3. Derivation strictly requires exactly 15 digits, with NO zero-padding leniency, because an exporter that strips the leading zero from an Alabama row (`"01001…"` becoming `"1001…"`) produces a string indistinguishable from legitimate FIPS prefixes 10–19 (DE, DC, FL, GA, HI, ID, IL, IN, IA) — and silently mis-attributing a row to the wrong state would be worse than refusing to derive and letting the row pass with `state=None`.
4. The reverse-lookup `STATE_FIPS_TO_ABBR` is built from `STATE_FIPS` at import time rather than hand-written as a second source of truth, because the two lookups must stay in sync forever and any future state-list edit (e.g. adding a territory if scope expands) should flow through one definition, not two.
5. Non-CONUS state FIPS codes (AK=02, HI=15, PR=72, etc.) are intentionally absent from `STATE_FIPS_TO_ABBR`, because the pipeline is CONUS-only by design and silently deriving an unsupported state would make the row look more complete than it is — leaving state=None at this layer keeps the data-quality breadcrumb visible.
6. The county field stores the canonical 5-digit county GEOID (state-FIPS + county-FIPS) rather than a human-readable county name, because a county lookup table would add ~3,100 hard-coded entries to config.py for marginal value at the ingestion layer — downstream code can join the 5-digit GEOID to a name table at report time if needed.
