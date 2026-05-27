# Decision Log

Every major design decision in this project is recorded here.

<table>
  <tr>
    <th>Number</th>
    <th>Decision</th>
    <th>Alternatives considered</th>
    <th>Reasoning</th>
    <th>Implementation</th>
    <th>What I would revisit</th>
  </tr>
  <tr>
    <td>1</td>
    <td>Python 3.11 as the implementation language</td>
    <td>Rust for data pipeline, Go for data pipeline, Scala for Spark jobs</td>
    <td>Python is standard for data engineering and geospatial analysis. The required libraries have strong Python support.</td>
    <td><a href="../pipeline.py">pipeline.py</a></td>
    <td>Rust for performance critical components if scale grows.</td>
  </tr>
  <tr>
    <td>2</td>
    <td>rasterio and GeoPandas over PostGIS</td>
    <td>PostGIS with Docker, GDAL command line scripts, direct database spatial joins</td>
    <td>Point in raster lookups are native raster operations. A database server adds operational burden without improving raster sampling.</td>
    <td><a href="../src/tools/tcc.py">src/tools/tcc.py</a></td>
    <td>PostGIS for polygon spatial joins at much larger scale.</td>
  </tr>
  <tr>
    <td>3</td>
    <td>Native Anthropic SDK over LangChain style frameworks</td>
    <td>LangChain tool wrappers, LangGraph agent graphs, CrewAI agent framework</td>
    <td>Fewer abstractions keep tool call tracing visible and debuggable. This is critical for a live review and for tests.</td>
    <td><a href="../src/agents/orchestrator.py">src/agents/orchestrator.py</a></td>
    <td>LangGraph once the agent graph grows beyond five tools.</td>
  </tr>
  <tr>
    <td>4</td>
    <td>NLCD Land Cover over OSM building footprints</td>
    <td>OSM building footprints only, OSM plus a building density proxy, county building permit datasets</td>
    <td>OSM completeness is weakest in rural areas that matter most for underserved communities. NLCD is nationally consistent and aligns with canopy data.</td>
    <td><a href="../src/tools/landcover.py">src/tools/landcover.py</a></td>
    <td>Building height data when a national public dataset becomes available.</td>
  </tr>
  <tr>
    <td>5</td>
    <td>DuckDB over PostgreSQL</td>
    <td>PostgreSQL server, pandas only aggregation, cloud warehouse queries</td>
    <td>DuckDB runs SQL directly on Parquet with no server. This keeps the repo runnable without infrastructure.</td>
    <td><a href="../src/data/store.py">src/data/store.py</a></td>
    <td>Cloud warehouse at much larger national scale.</td>
  </tr>
  <tr>
    <td>6</td>
    <td>Precomputed slope raster over per point Horn method</td>
    <td>Per point Horn kernel reads, on demand elevation windows, slope computed during enrichment</td>
    <td>Per point slope would require one windowed read per location and would be slow. Precomputing once makes per location reads cheap.</td>
    <td><a href="../src/data/downloader.py">src/data/downloader.py</a></td>
    <td>Recompute slope when DEM vintage changes or resolution changes.</td>
  </tr>
  <tr>
    <td>7</td>
    <td>Pipeline level Claude orchestration over per batch reasoning</td>
    <td>Per batch Claude calls over 50 locations, per row Claude reasoning, per row tool dispatch</td>
    <td>Per batch reasoning would require about 46,700 Claude calls on 4.67M rows. Pipeline orchestration uses one orchestration session of approximately 6 turns and costs about 0.098 dollars.</td>
    <td><a href="../src/agents/orchestrator.py">src/agents/orchestrator.py</a></td>
    <td>Async dispatch when a batches API supports tool_use orchestration.</td>
  </tr>
  <tr>
    <td>8</td>
    <td>Claude Sonnet for orchestration and Opus reserved for planning</td>
    <td>Claude Opus for orchestration, GPT 4 style model for orchestration, no model reasoning</td>
    <td>Orchestration requires structured tool decisions rather than deep creative reasoning. A smaller model keeps cost low while preserving correctness.</td>
    <td><a href="../src/config.py">src/config.py</a></td>
    <td>Opus for interactive explanations once a user interface requires higher quality prose.</td>
  </tr>
  <tr>
    <td>9</td>
    <td>Risk weights 50 30 20 for canopy, terrain, and land cover</td>
    <td>Weights 60 25 15, equal weights, learned weights from calibration data</td>
    <td>Canopy is the primary obstruction named by the install guide. Terrain is a hard constraint and land cover provides supporting context.</td>
    <td><a href="../src/config.py">src/config.py</a></td>
    <td>Calibration against measured service quality once outcomes exist.</td>
  </tr>
  <tr>
    <td>10</td>
    <td>Single orchestrator with five tools over multiple distinct agent classes</td>
    <td>Separate IngestionAgent class, EnvironmentalAgent class, ScoringAgent class each with their own Claude client</td>
    <td>The pipeline is sequential with no parallel branches. Multiple agent classes would add coordination overhead without analytical benefit. The five tools provide clear scope boundaries equivalent to agent boundaries while keeping orchestration visible in one place.</td>
    <td><a href="../src/agents/orchestrator.py">src/agents/orchestrator.py</a></td>
    <td>Split into separate agent processes if parallel enrichment becomes needed at larger scale.</td>
  </tr>
</table>

## Drift detection strategy

When the pipeline reruns next quarter, compare the risk score distribution to this baseline.
If High tier share shifts by more than 5 percent, review NLCD dataset versions.

<table>
  <tr>
    <th>Signal</th>
    <th>Why it matters</th>
    <th>Implementation hook</th>
  </tr>
  <tr>
    <td>Tier proportion shift</td>
    <td>It changes the headline narrative a broadband officer reads.</td>
    <td>src/utils/metrics.py compute_pipeline_metrics output_quality tier_distribution</td>
  </tr>
  <tr>
    <td>Risk score percentiles</td>
    <td>They detect distribution drift that tier bins can hide.</td>
    <td>src/utils/metrics.py compute_pipeline_metrics output_quality mean_risk_score</td>
  </tr>
  <tr>
    <td>Likely root cause</td>
    <td>NLCD updates affect the largest weight and can shift outcomes.</td>
    <td>docs/data_sourcing.md version pins and config coverage identifiers</td>
  </tr>
</table>

## Version change note

The initial plan used OSM buildings as a third factor with weights 60 25 15.
The system moved to NLCD land cover and weights 50 30 20 for documented reasons above.
