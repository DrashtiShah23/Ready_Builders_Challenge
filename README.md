# LEO Satellite Coverage Risk Analysis

An agent-driven data pipeline that scores every BEAD-committed broadband location in North Carolina (**4,674,905 GPS coordinates**) for LEO satellite (Starlink) signal-obstruction risk using national tree-canopy, terrain, and land-cover datasets. Built for the [Ready Builders Challenge #50](https://github.com/ready/builders-challenge/issues/50) — the output supports state broadband officers in understanding where committed LEO coverage may be degraded by environmental conditions.

## Key Findings (full 4.67M-row NC run)

Verified on the full dataset, single 20-minute pipeline run:

| Risk tier | Locations | Share | Plain-English read |
|---|---:|---:|---|
| **High** | ~919,000 | **19.7%** | ~1 in 5 committed broadband locations face material tree, terrain, or land-cover obstruction risk |
| Moderate | ~1,450,000 | 31.1% | Mounting / aiming guidance recommended |
| Low | ~2,300,000 | 49.3% | Best available remote assessment supports clear sky access |
| UNSCORED | minimal | <0.1% | Environmental data unavailable for the pixel |

- **Total API cost: $0.098** for the full 4.67M-row run (one orchestration session of approximately 6 turns, five pipeline-level tools).
- **Wall clock: ~20 minutes** on a single MacBook (no distributed infra).
- **Primary risk driver: tree canopy cover** (50% of composite weight; install guide names tree branches as the primary obstruction).
- **TCC missing rate: 31.65%** — confirmed as NLCD's documented NoData behavior on non-tree land cover (water, developed, cropland), not a coverage gap. Full breakdown in [`docs/data_sourcing.md`](docs/data_sourcing.md) § "NLCD TCC NoData on non-tree land cover classes".

### Top 10 at-risk counties (by High-risk count)

| Rank | County (GEOID) | High count | High % | Driver |
|---:|---|---:|---:|---|
| 1 | Wake (37183) | 102,592 | 23.6% | Raleigh metro — City of Oaks tree canopy |
| 2 | Mecklenburg (37119) | 81,832 | 21.3% | Charlotte metro |
| 3 | Durham (37063) | 38,486 | 33.2% | Dense urban canopy incl. Duke Forest |
| 4 | Buncombe (37021) | 38,007 | 30.8% | Asheville — Appalachian terrain + mountain forest |
| 5 | Guilford (37081) | 34,594 | 17.4% | Greensboro |
| 6 | Forsyth (37067) | 30,104 | 19.1% | Winston-Salem |
| 7 | **Orange (37135)** | 26,721 | **43.6%** | **Highest proportion** — Chapel Hill / Carrboro dense-canopy preservation policies |
| 8 | Gaston (37071) | 21,260 | 20.4% | — |
| 9 | Henderson (37089) | 19,429 | 33.6% | Hendersonville — Blue Ridge mountain slope |
| 10 | Randolph (37151) | 18,580 | 28.2% | — |

**Analytical insight.** Orange County tops the per-capita ranking at 43.6% despite not being the largest county — driven by dense forest canopy in Chapel Hill and Carrboro. Henderson County's high rate is terrain-driven from Blue Ridge slope. The contrast between canopy-driven (Orange) and terrain-driven (Henderson) risk demonstrates that the composite scoring model captures two distinct physical obstruction mechanisms rather than collapsing them into a single signal.

Full per-state and per-county summaries live at `outputs/scored/risk_summary_by_state.parquet` and `risk_summary_by_county.parquet`. Interactive map at `outputs/risk_map.html`.

## Quick Start

Clone to running in 5 commands:

```bash
git clone <repo-url>
cd leo-satellite-coverage-risk
pip install -r requirements.txt
cp .env.example .env   # then add your ANTHROPIC_API_KEY
python pipeline.py --csv data/locations.csv
```

Verify the pipeline structure end-to-end with no Claude API spend:

```bash
python pipeline.py --csv data/locations.csv --mode dry-run
```

## Architecture

The pipeline is one orchestration session of approximately 6 turns orchestrating five deterministic pipeline-level tools. Each tool internally runs the full dataset through the Phase 1–6 agents (ingestion, environmental enrichment, scoring) and persists its output to parquet; Claude reasons over each tool's *summary* — not per-row data — and decides whether to proceed. **At 4.67M rows the total Claude spend is < $0.10** (vs. ~$10,600 for a per-row design — see Decision 6 below).

```mermaid
flowchart TD
    CSV[Locations CSV<br>4.67M NC rows] --> ORCH

    subgraph CLAUDE[" Orchestration session (~6 turns) · 5 pipeline-level tools "]
        direction TB
        ORCH[Claude Orchestrator<br>claude-sonnet-4-6 · tool_use loop] --> T1
        ORCH --> T2
        ORCH --> T3
        ORCH --> T4
        ORCH --> T5
        T1[ingest_locations<br>validate · dedup · batch]
        T2[sample_environment<br>TCC · slope · land_cover]
        T3[score_risk<br>50/30/20 composite]
        T4[validate_results<br>4 sanity checks]
        T5[generate_report<br>md · parquet · map]
    end

    T1 --> P1[(validated_locations.parquet)]
    P1 --> T2
    T2 --> P2[(enriched_locations.parquet)]
    P2 --> T3
    T3 --> P3[(scored_locations.parquet)]
    T3 --> STORE[(Hive-partitioned store<br>outputs/scored/state=NC/)]
    P3 --> T4
    P3 --> T5
    STORE --> T5
    T5 --> REPORT[analysis_report.md]
    T5 --> MAP[risk_map.html · Folium]
    T5 --> SUMM[risk_summary_by_state.parquet<br>risk_summary_by_county.parquet]

    STORE -.->|DuckDB SQL| ANALYST[Analyst / dashboard]

    classDef tool fill:#fef3c7,stroke:#92400e,color:#000
    classDef store fill:#dbeafe,stroke:#1e3a8a,color:#000
    classDef out fill:#dcfce7,stroke:#166534,color:#000
    class T1,T2,T3,T4,T5 tool
    class P1,P2,P3,STORE store
    class REPORT,MAP,SUMM out
```

The Phase 3 raster tools (`fetch_tcc`, `fetch_elevation`, `fetch_land_cover`) are **internal Python functions** called by `sample_environment` — not exposed to Claude directly. Per-location Claude reasoning is preserved only in `interactive` mode (single-coordinate query, ~$0.01 per call) for the "user gives coordinates, agent explains sky visibility" scenario in the challenge brief.

Full diagrams and design rationale live in [`docs/architecture.md`](docs/architecture.md).

## Decision Log

| # | Decision | Alternatives considered | Reasoning | What I'd revisit |
|---|---|---|---|---|
| 1 | **Python 3.11+** | Rust, Go, Scala | Standard for data engineering. `rasterio`, `geopandas`, `duckdb` all have first-class Python bindings. Aligns with the team's stack. | — |
| 2 | **rasterio + GeoPandas over PostGIS** | PostGIS + Docker | Point-in-raster lookups are natively a raster operation. PostGIS adds a database server with zero analytical benefit at this scale. | PostGIS for polygon spatial joins at 100× scale. |
| 3 | **Native Anthropic SDK over LangChain** | LangChain, LangGraph, CrewAI | Fewer abstractions = clearer tool-call tracing. Every step of the agent loop is visible and debuggable. Critical for a live review. | LangGraph for multi-agent graphs once the agent count grows past 5. |
| 4 | **NLCD Land Cover over OSM buildings** (v1→v2 change) | OSM building footprints | OSM completeness is lowest in rural areas — exactly where underserved communities live. NLCD is the same dataset family as TCC (same CRS / 30m / same download). Cross-validates TCC instead of adding independent noise. | Building height data when nationally available (USGS 3DEP LiDAR expansion). |
| 5 | **DuckDB over PostgreSQL** | PostgreSQL, in-memory pandas | Embeddable, no server, SQL directly on Parquet files. State-partitioned Parquet enables resumable runs for free. Handles 1M+ rows analytically without loading into memory. | Cloud warehouse (BigQuery / Snowflake) at 100× scale. |
| 6 | **Pipeline-level Claude orchestration over per-batch reasoning** (Phase 7 redesign) | Per-batch Claude calls (~50 locations/call), per-location tool dispatch | Per-batch design = ~46,700 Claude calls = ~$10,600 for 4.67M rows. Pipeline-level = **one orchestration session of approximately 6 turns, 5 tools, < $0.10** total. Per-location reasoning preserved in interactive mode where the cost shape fits the use case. | Async batch dispatch once Anthropic's Batches API supports `tool_use` natively. |
| 7 | **Pre-computed slope raster over per-point Horn's method** | Per-point 3×3 windowed reads at query time | 1M locations × 3×3 windowed read = 1M rasterio reads. Pre-computing once = 1 GDAL op + 1M cheap pixel lookups. ~100× faster, eliminates raster-edge bugs. | — |

See [`docs/decision_log.md`](docs/decision_log.md) for the full table (including risk weights and the v1.0 → v2.0 OSM-buildings rationale) and the drift-detection strategy.

## Data Sources

| Dataset | Source | Install-guide obstruction factor | Why chosen |
|---|---|---|---|
| NLCD 2021 Tree Canopy Cover | USGS / MRLC (WCS) | **Tree branches** — primary obstruction named in the install guide | Continuous 0–100% canopy density at 30m, nationally consistent, version-pinned |
| USGS 3DEP Elevation | USGS National Map | **Terrain** blocking 25° minimum elevation / 100° FOV cone | Only national DEM at 10–30m; slope via Horn's method directly models sky loss |
| NLCD 2021 Land Cover | USGS / MRLC (WCS) | **Structural-density context** (developed codes) + TCC cross-validation | Same dataset family as TCC; cross-validates canopy at zero extra infrastructure |

See [`docs/data_sourcing.md`](docs/data_sourcing.md) for source URLs, version pins, the 2026 MRLC bulk-zip → WCS migration, and quality notes.

## Full-scale cost projection

API cost is essentially **constant across dataset size** in this design — that is the key economic property of the Phase 7 redesign. The pipeline always runs one orchestration session of approximately 6 turns with the same 5 tools regardless of whether the locations parquet has 10k rows or 4.67M.

| Scale | Claude API cost | Wall clock | Disk (raw rasters) | Notes |
|---|---:|---:|---:|---|
| 10k rows (NC sample) | $0.091 | ~1 min | ~250 MB | Smoke run, used for STOP-gate verification |
| 100k rows | ~$0.10 | ~3 min | ~250 MB | API cost identical — rasters reused |
| **4.67M rows (full NC)** | **$0.098** | **~20 min** | **~250 MB** | Verified on the live 2026-05-26 run |
| 50M rows (CONUS projection) | ~$0.10–0.15 | ~3–4 hr | ~5–10 GB | Estimate: Claude bill flat; raster download per added state ≈ 250 MB |

Wall-clock scales linearly with the raster-sampling step (CPU-bound, single-threaded `rasterio.sample` at ~3.5k locations/sec on a MacBook). Memory stays under 2 GB at every scale because environmental enrichment is chunked at `RASTER_BATCH_SIZE = 50,000`.

## Running the pipeline

### Setup (run once)

Before running the pipeline, download the required rasters:

```bash
python -m src.data.downloader --states NC
```

Batch (full dataset):
```bash
python pipeline.py --csv data/locations.csv
```

Subset by state:
```bash
python pipeline.py --csv data/locations.csv --states NC CA
```

Sample for testing:
```bash
python pipeline.py --csv data/locations.csv --sample 10000
```

Resume from the last completed step:
```bash
python pipeline.py --csv data/locations.csv --resume
```

Interactive (one coordinate; includes nearby lower-risk alternatives within 5 km by default):
```bash
python pipeline.py --mode interactive --lat 35.06 --lon -80.66
python pipeline.py --mode interactive --lat 35.06 --lon -80.66 --buffer 5000
```

Dry-run (no Claude spend):
```bash
python pipeline.py --csv data/locations.csv --mode dry-run
```

Post-run observability:
```bash
python -m src.utils.metrics logs/pipeline_run_<run_id>.jsonl outputs/scored
```

## Known limitations

These factors materially affect real-world Starlink performance but cannot be captured by any nationally available public dataset. Documented up front so reviewers know what the pipeline is **not** claiming to model. Full discussion in [`docs/analysis_rationale.md`](docs/analysis_rationale.md) § 4 and [`docs/data_sourcing.md`](docs/data_sourcing.md) § "What cannot be modeled with public data".

- **Exact tree heights.** TCC measures canopy area %, not height — a 90% TCC pixel could be shrubs or 100 ft pines. NLCD does not publish a national canopy-height layer.
- **Building heights.** No national public dataset exists. OSM has footprints, not heights.
- **Seasonal variation.** NLCD TCC is a 2021 peak-summer snapshot; deciduous canopy varies seasonally.
- **Sub-30m obstructions.** A single tall tree on a property edge may not register in a 30m pixel.
- **Microsite conditions.** Rooftop access, mounting options, HOA restrictions, landlord permission — require physical assessment.
- **NLCD TCC NoData on non-tree pixels.** 31.65% of the full-dataset rows have null TCC (water, developed, crops). Scoring treats null TCC as 0.0 (most-favorable bucket), which conservatively understates the headline High share — restricted to rows with TCC data, High share is ~8.75%. Documented in [`docs/data_sourcing.md`](docs/data_sourcing.md) and surfaced in every run's `analysis_report.md` Data Quality section.

**Operational framing.** High → priority site assessment, *not* unserviceable. Low → best available remote assessment, *not* a guarantee. Every High classification is a recommendation to investigate before commitment, not a denial of service.

## Production considerations

These are not built in this implementation but are the right next steps for productionization:

- **Orchestration.** Replace the sequential pipeline runner with Apache Airflow DAGs for scheduling, retries, and monitoring. The five tool boundaries already produce stable parquet artifacts (`validated_locations.parquet`, `enriched_locations.parquet`, `scored_locations.parquet`) — they map cleanly onto five Airflow tasks.
- **Data access.** Use Cloud-Optimized GeoTIFFs (COGs) on S3 with HTTP range requests instead of full file downloads — enables serverless access and eliminates the ~250 MB per-state local download. MRLC's WCS already does subset queries; COGs would compose well on top.
- **Scale.** At 100× rows (CONUS-wide) the Claude API surface is **unchanged** — one orchestration session of approximately 6 turns, five tools, ~$0.10. The bottleneck moves to raster sampling; horizontally parallelize `sample_environment` by state partition.
- **Drift detection.** Rerun quarterly. Compare risk-score distribution (mean, P25 / P75 / P90) against this baseline. A >5% absolute shift in the High tier triggers a NLCD version review — MRLC releases updated NLCD on a 2–3 year cycle. The operational hook is `src.utils.metrics.compute_pipeline_metrics`; see [`docs/decision_log.md`](docs/decision_log.md) § "Drift detection strategy".
- **Observability.** Every run already writes a structured JSONL log (`logs/pipeline_run_<run_id>.jsonl`). Pipe to Datadog / Honeycomb / Grafana Loki for centralized dashboards; the `compute_pipeline_metrics` shape is dashboard-ready.
- **Building height.** When national LiDAR becomes available (USGS 3DEP LiDAR program is expanding), add canopy height and building height as a fourth factor. The `score_components` API already accepts arbitrary signals — adding a fourth weight is a `config.py` change plus one new bucket function.

## Project structure

```
leo-satellite-coverage-risk/
├── docs/        # Architecture, decision log, data sourcing, analysis rationale
├── src/
│   ├── agents/  # ingestion, environmental, orchestrator, scoring (+ prompts/)
│   ├── tools/   # fetch_tcc, fetch_elevation, fetch_land_cover (internal raster lookups)
│   ├── data/    # Downloader (MRLC WCS + USGS TNM) + Parquet/DuckDB store
│   ├── schemas/ # Pydantic data contracts for every pipeline stage
│   └── utils/   # Logger, metrics, geo helpers
├── tests/       # pytest unit tests per module (380+ tests)
├── pipeline.py  # CLI entrypoint (batch / interactive / dry-run modes)
├── pyproject.toml
└── requirements.txt
```

## AI tools used

Disclosed per the challenge requirements: this project uses **Claude (Anthropic API)** as the runtime orchestrator agent and **Cursor** as the development IDE. Three cases where Cursor-generated code diverged from the literal build-plan text are logged in [`AI_TOOLS.md`](AI_TOOLS.md) with the rationale for each kept deviation.

## License

MIT
