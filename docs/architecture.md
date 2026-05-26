# System Architecture

## Pipeline flow

```mermaid
flowchart TD
    A[Locations CSV<br>~1M rows] --> B[Ingestion Agent<br>validate · deduplicate · batch]
    B --> C[Environmental Data Agent<br>fetch TCC · elevation · land cover]
    C --> D[Claude Orchestrator Agent<br>tool_use API · batch reasoning]
    D -->|fetch_tcc| E[(NLCD TCC<br>GeoTIFF raster)]
    D -->|fetch_elevation| F[(USGS 3DEP<br>pre-computed slope raster)]
    D -->|compute_risk_score| G[Risk Scoring Engine<br>TCC 50% + terrain 30% + land cover 20%]
    C -->|fetch_land_cover| LC[(NLCD Land Cover<br>GeoTIFF raster)]
    G --> H[(Parquet State Store<br>partitioned by state)]
    H --> I[DuckDB Analytics<br>aggregations · insights]
    I --> J[Analysis Report<br>Markdown]
    I --> K[Interactive Map<br>Folium HTML]
    D -->|error| L[Failure Log<br>JSONL · retry queue]
    M[Human Review Gate] -.->|override anomalies| H
```

## Agent sequence

```mermaid
sequenceDiagram
    participant U as User/Runner
    participant A as Claude Orchestrator
    participant T1 as fetch_tcc
    participant T2 as fetch_elevation
    participant T3 as compute_risk_score

    U->>A: Process batch of 50 enriched locations
    A->>A: Reason: call fetch_tcc for each location
    A->>T1: fetch_tcc(lat, lon)
    T1-->>A: {tcc_pct: 73, tcc_missing: false}
    A->>T2: fetch_elevation(lat, lon)
    T2-->>A: {slope_deg: 18.2, aspect_deg: 340, elevation_missing: false}
    A->>A: Reason: north-facing slope + high canopy = likely high risk
    A->>T3: compute_risk_score(tcc_pct=73, slope_deg=18.2, land_cover_code=42, latitude=37.4)
    T3-->>A: {risk_score: 0.72, risk_tier: "High", component_scores: {...}, flags: []}
    A-->>U: Scored batch JSON
```

## Why this design

- **Agents have defined contracts.** Every stage boundary is a Pydantic model (`RawLocation` → `ValidatedLocation` → `EnrichedLocation` → `ScoredLocation`). Bad data fails loudly at the boundary instead of silently propagating downstream.
- **Claude orchestrates, it does not score.** Risk scoring is deterministic (see `src/agents/scoring.py`). Claude calls the tools, validates intermediate values, flags anomalies, and writes plain-English explanations. The formula itself is auditable and reproducible.
- **Land cover is not an LLM tool.** `fetch_land_cover` runs deterministically inside the Environmental Data Agent alongside TCC sampling — same NLCD dataset family, same CRS, same resolution. Exposing it to Claude would add cost without adding reasoning value. The land-cover code is passed into `compute_risk_score` as a plain integer input.
- **Slope is pre-computed.** Computing slope per-point via Horn's method at 1M locations = 1M 3x3 windowed rasterio reads. Pre-computing the full slope raster once = 1 GDAL operation + 1M cheap pixel lookups. ~100x speedup. See `src/data/downloader.py:precompute_slope_raster`.
- **Parquet + DuckDB instead of PostgreSQL.** Columnar Parquet partitioned by state is resumable (skip already-scored partitions on restart) and DuckDB queries it directly with SQL — no database server, no Docker, no ETL.
