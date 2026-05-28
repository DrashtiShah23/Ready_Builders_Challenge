# AI Tool Disclosure

This file lists every AI tool used to build this project and the moments where I made a different call from what was suggested. The challenge asked for this disclosure. I have kept it readable for anyone reviewing the submission.

## Tools used

These are the only AI tools involved in this project.

<table>
  <tr>
    <th>Tool</th>
    <th>What I used it for</th>
  </tr>
  <tr>
    <td>Claude API claude-sonnet-4-6</td>
    <td>The coordinating agent in the pipeline. After each processing step it reads a short summary and decides whether to continue or stop. At the end it writes the plain English findings report. It never sees the raw location data, only summaries of what each step produced.</td>
  </tr>
  <tr>
    <td>Cursor</td>
    <td>The coding assistant used throughout the build. It helped write code, refactor, and draft documentation. Everything it produced was reviewed before being committed.</td>
  </tr>
  <tr>
    <td>Gemini</td>
    <td>Generated the two architecture diagram images saved in the docs folder. Used only for image generation, not for any code or pipeline logic.</td>
  </tr>
  <tr>
    <td>Claude Opus via Cursor</td>
    <td>Used during the planning phase to think through the architecture before writing any code. Not used at runtime.</td>
  </tr>
  <tr>
    <td>Claude interactive mode</td>
    <td>When a user queries a single address the pipeline scores it in Python and then asks Claude to explain in plain English why that location received its risk level. Costs about $0.01 per query.</td>
  </tr>
</table>

No other AI tools were used. Not GitHub Copilot, not ChatGPT, not any framework that hides API calls behind a Python library.

## Where I made a different call

These are the moments where I changed direction during the build. Written in the order they happened. Each one shows how it started, what I built instead, and the reasoning.

**The CSV did not match the brief**

How it started: The ingestion code was built assuming the CSV would have explicit state and county columns as the challenge brief described.

What I built: The real CSV had a 15-digit Census Block GEOID instead and I derived state and county from the first digits of that code automatically.

The reasoning: The actual file is always the source of truth, not the spec.

**Risk weights had to come from somewhere real**

How it started: Equal weights of 33% for all three scoring factors seemed reasonable since no calibration data existed.

What I built: Canopy at 50%, terrain slope at 30%, and land cover at 20%, taken directly from the Starlink install guide which names tree branches as the primary obstruction.

The reasoning: Equal weights are a guess dressed up as a decision and every weight needs to trace back to a source document.

**Downloading 3 GB to use 0.4% of it**

How it started: The design downloaded full national NLCD raster files at about 3 GB each and used a state code parameter for elevation tiles that was silently returning results from Oregon.

What I built: Switched to the MRLC WCS endpoint requesting only the NC bounding box at about 50 MB and fixed the elevation download by switching to a bounding box query.

The reasoning: The broken URLs forced the better decision but downloading 3 GB to use 0.4% of it was already wrong.

**Computing terrain slope once not four million times**

How it started: The straightforward approach reads a 3x3 window of elevation pixels around each location at query time to compute slope on the fly.

What I built: Precomputed the entire slope raster once from the DEM tiles before the pipeline runs so every location lookup reads one pixel from that file.

The reasoning: Computing slope per point at 4.67 million locations means 4.67 million windowed reads against a compressed multi-gigabyte file and precomputing once turns that into one operation.

**One orchestrator not five separate agent classes**

How it started: The natural design would have been five separate agent classes each with their own Claude client, one per pipeline step.

What I built: One orchestrator with five tools where each tool has a defined scope and data contract enforced by Pydantic schemas.

The reasoning: The pipeline is sequential with no parallel branches so multiple agent classes add coordination overhead without any analytical benefit.

**Finding nearby alternatives is math not AI**

How it started: The buffer search returning nearby lower-risk locations could have been a Claude tool call letting the agent reason about which alternatives to suggest.

What I built: A deterministic haversine distance query over the scored parquet file with no Claude call involved.

The reasoning: Finding the three nearest points with lower risk scores is a spatial math problem and calling Claude for it adds latency and cost for zero reasoning benefit.

**The map failure should not kill the report**

How it started: When the map rendering step failed the pipeline stopped entirely and no outputs were written.

What I built: Map failure is non-fatal, the pipeline logs a MAP_RENDER_FAILED event and continues, and the analysis report and parquet summaries always generate.

The reasoning: The data outputs are the primary deliverable and a visualization bug should never prevent the findings from being written.

**The county filter needed a complete rebuild**

How it started: The county filter used GeoJSON properties to show and hide markers with JavaScript and after two attempts to fix it the filter still did not work.

What I built: Scrapped the GeoJSON approach entirely and switched to a separate Folium FeatureGroup per county that swaps the entire layer on dropdown change.

The reasoning: Sometimes the first approach is architecturally wrong and needs replacing not patching.

**Seasonal risk should advise not change the score**

How it started: The natural approach would adjust the stored composite risk score by season, lowering it in winter when deciduous trees shed their leaves.

What I built: One stored score per location based on peak summer canopy with a seasonal advisory note returned in interactive mode for deciduous and mixed forest locations.

The reasoning: Two different scores for the same location would break every comparison and county ranking.

**Monitoring from your own logs not a third party**

How it started: The obvious approach for observability was LangSmith or a separate monitoring agent.

What I built: metrics.py parses the structured JSONL logs the pipeline already writes and covers every monitoring item the challenge asked for including per-step success rates, latency, token usage, estimated cost, tool call accuracy with four signals, scored location percentage, and a quarterly drift detection strategy documented in decision_log.md.

The reasoning: The pipeline already logs everything it does and routing that data through a third-party service adds a dependency for no benefit.

The full build history and every phase-by-phase decision is documented separately and available on request.
