# Decision Log

Every major design decision in this project, the alternatives considered, the reasoning, and what I would revisit. This is the source-of-truth file referenced from `README.md`.

| # | Decision | Alternatives considered | Reasoning | What I'd revisit |
|---|---|---|---|---|
| 1 | Python 3.11+ as the implementation language | Rust, Go, Scala | Standard for data engineering. `rasterio`, `geopandas`, `duckdb` all have first-class Python bindings. Aligns with the role's stack. | — |
| 2 | rasterio + GeoPandas over PostGIS | PostGIS + Docker, GDAL CLI scripts | Point-in-raster lookups are natively a raster operation. PostGIS adds a database server and Docker infrastructure for zero analytical benefit at this scale. | PostGIS would matter for polygon spatial joins at 100x scale. |
| 3 | Native Anthropic SDK over LangChain | LangChain, LangGraph, CrewAI | Fewer abstractions = clearer tool call tracing. Every step of the agent loop is visible and debuggable. Critical for a live review where I have to defend every line. | LangGraph for complex multi-agent graphs once the agent count grows beyond 5. |
| 4 | NLCD Land Cover over OSM buildings (v1.0 → v2.0 change) | OSM building footprints | (a) OSM completeness is lowest in rural areas — exactly where underserved communities live. Using it would bias the analysis against the very locations we are trying to serve. (b) NLCD is the same dataset family as TCC: same CRS (EPSG:5070), same resolution (30m), same download. Zero added infrastructure. (c) Land cover cross-validates TCC instead of adding independent noise — if TCC says 80% canopy and NLCD says forest, that's a confident signal; if they disagree, it's a quality flag. | Building height data when it becomes nationally available (USGS 3DEP LiDAR expansion). |
| 5 | DuckDB over PostgreSQL | PostgreSQL, pandas in-memory | Embeddable, no server, SQL directly on Parquet files. Handles 1M+ rows analytically without loading into memory. State-partitioned Parquet enables resumable runs for free. | Cloud data warehouse (BigQuery / Redshift / Snowflake) at 100x scale. |
| 6 | Pre-computed slope raster over per-point Horn's method | Per-point 3x3 windowed reads at query time | At 1M locations, per-point slope = 1M rasterio reads with a 3x3 window each. Pre-computing once = 1 GDAL operation + 1M cheap pixel lookups. ~100x faster, eliminates a class of edge-case bugs at raster boundaries. | — |
| 7 | Claude Sonnet for orchestration, Opus reserved for planning | Opus throughout, GPT-4 | Sonnet is sufficient for structured tool dispatch. At 20k Claude calls (1M / 50 per batch), cost management is a real engineering constraint, not a hypothetical. Opus is reserved for ambiguous reasoning (anomaly explanations, interactive-mode plain-English output). | Anthropic batch API once it supports tool_use natively for a further cost reduction. |
| 8 | Risk weights 50/30/20 (TCC / terrain / land cover) — v1.0 → v2.0 change | 60/25/15 (v1.0, with OSM buildings as the third factor) | TCC 50%: install guide explicitly names tree branches as the primary obstruction. Terrain 30%: slope is a hard physical constraint on the 25° elevation minimum — elevated mounting overcomes canopy more often than it overcomes a deep valley. Land cover 20%: cross-validates TCC but is partially redundant for forested areas, so weighted lower. All weights are sourced from `src/config.py` — change one number to retune the whole pipeline. | Calibrate weights against a held-out set of locations with known service quality once that signal becomes available from the Ready team. |

## Drift detection strategy (added in Phase 11)

When the pipeline reruns on updated data next quarter, compare the risk score distribution (mean, P25, P75, P90) against this baseline run. A shift of >5% in the High tier proportion triggers a review of NLCD TCC and Land Cover dataset versions — MRLC releases updated NLCD data on a 2-3 year cycle.

Why these specific signals:

- **Tier proportion shift** is the most reportable change — a state broadband officer reads the executive summary as "X% of homes are at high risk", so a >5% absolute shift between runs is the threshold at which the headline narrative changes and a review is warranted. <5% drift is normal sampling variance plus minor geocoding churn and is not worth flagging.
- **Risk score percentiles (mean / P25 / P75 / P90)** catch shifts in the underlying distribution that the discrete tier counts smooth over — e.g. a uniform 10% TCC bump across all rows would barely move tier counts (since the thresholds are wide) but would move P75 noticeably.
- **NLCD version is the most likely root cause** because TCC is the largest scoring weight (50%) and MRLC's release cadence (2-3 years) means a quarterly rerun is the typical first place a real-world data refresh shows up. The DEM cadence (10+ years for full national updates) and the locations CSV cadence (release-by-release) are both slower.

How to implement: the operational hook is `src.utils.metrics.compute_pipeline_metrics`, which already emits `output_quality.tier_distribution` and `output_quality.mean_risk_score`. A future watchdog can persist one row per run and compare the latest to the baseline; the threshold (>5%) is the documented review trigger.

## v1.0 → v2.0 change note

The original design used OSM building footprints as the third scoring factor with weights 60/25/15. I changed both the third factor and the weights for the reasons captured in row 4 and row 8 above. This is documented as a single coherent revision (v1.0 → v2.0) in the build plan changelog. The change is a worked example of iterative design reasoning: the original plan was technically buildable but had a sourcing flaw (OSM rural sparsity) that would have undermined the analysis in exactly the populations the challenge targets.
