# Data Sourcing

This document maps each dataset to an install guide obstruction factor and pins versions.

## Dataset to obstruction factor mapping

<table>
  <tr>
    <th>Dataset</th>
    <th>Source</th>
    <th>Install guide obstruction factor</th>
    <th>Why this dataset</th>
  </tr>
  <tr>
    <td>NLCD 2021 Tree Canopy Cover</td>
    <td>https://www.mrlc.gov/data</td>
    <td>Tree branches named as the primary obstruction</td>
    <td>Continuous canopy density with national coverage and year pin</td>
  </tr>
  <tr>
    <td>USGS 3DEP Elevation</td>
    <td>https://www.usgs.gov/3d&#45;elevation&#45;program</td>
    <td>Terrain blocking the 25 degree minimum elevation angle and the sky cone</td>
    <td>National DEM that supports slope and aspect derivation</td>
  </tr>
  <tr>
    <td>NLCD 2021 Land Cover</td>
    <td>https://www.mrlc.gov/data</td>
    <td>Structural density context plus cross validation of canopy</td>
    <td>Same dataset family as canopy with aligned CRS and resolution</td>
  </tr>
</table>

## Version pins

<table>
  <tr>
    <th>Dataset</th>
    <th>Pin</th>
    <th>Where it lives</th>
  </tr>
  <tr>
    <td>NLCD canopy</td>
    <td>Coverage id mrlc_download__nlcd_tcc_conus_2021_v2021&#45;4</td>
    <td>src/config.py MRLC_TCC_COVERAGE_ID</td>
  </tr>
  <tr>
    <td>NLCD land cover</td>
    <td>Coverage id mrlc_download__NLCD_2021_Land_Cover_L48</td>
    <td>src/config.py MRLC_LANDCOVER_COVERAGE_ID</td>
  </tr>
  <tr>
    <td>USGS 3DEP</td>
    <td>1 arc second GeoTIFF tiles requested via TNM bbox filter</td>
    <td>Resolved URLs captured in JSONL events DEM_BBOX_QUERY_OK and HTTP_DOWNLOAD_DONE</td>
  </tr>
</table>

## MRLC bulk zips to WCS migration

Implementation: src/data/downloader.py download_tcc and download_landcover.

The bulk zip path began returning HTTP 403 AccessDenied in May 2026.
The downloader uses MRLC WCS at https://www.mrlc.gov/geoserver/mrlc_download/wcs.

<table>
  <tr>
    <th>Aspect</th>
    <th>National zip former</th>
    <th>WCS NC subset current</th>
  </tr>
  <tr>
    <td>TCC download size</td>
    <td>About 3 GB extracted</td>
    <td>About 50 to 150 MB</td>
  </tr>
  <tr>
    <td>Land cover download size</td>
    <td>About 3 GB extracted</td>
    <td>About 50 to 150 MB</td>
  </tr>
  <tr>
    <td>Bytes used by NC only pipeline</td>
    <td>About 0.4 percent</td>
    <td>About 100 percent</td>
  </tr>
  <tr>
    <td>Scope change to a different state</td>
    <td>Requires the same large downloads again</td>
    <td>Rerun downloader with new states</td>
  </tr>
</table>

## TNM polyType to bbox migration

The TNM polyType state filter stopped working in May 2026.
The downloader uses bbox and reads state bboxes from src/config.py STATE_BBOX_WGS84.

<table>
  <tr>
    <th>Detail</th>
    <th>Value</th>
  </tr>
  <tr>
    <td>NC bbox used</td>
    <td>(−84.32, 33.75, −75.46, 36.59) in lon lat order</td>
  </tr>
  <tr>
    <td>Page size</td>
    <td>max 200 via config.TNM_PAGE_SIZE</td>
  </tr>
  <tr>
    <td>Dedupe</td>
    <td>Latest vintage per 1 degree quad</td>
  </tr>
</table>

## CRS strategy

<table>
  <tr>
    <th>Layer</th>
    <th>CRS</th>
    <th>Why</th>
  </tr>
  <tr>
    <td>NLCD rasters</td>
    <td>EPSG:5070</td>
    <td>Native CRS for CONUS rasters</td>
  </tr>
  <tr>
    <td>Input locations</td>
    <td>EPSG:4326</td>
    <td>Standard lat lon input coordinates</td>
  </tr>
</table>

## Input data quality

### Ingestion level data quality flags

<table>
  <tr>
    <th>Reason code</th>
    <th>Meaning</th>
  </tr>
  <tr>
    <td>NULL_COORDINATE</td>
    <td>Latitude or longitude missing</td>
  </tr>
  <tr>
    <td>OUT_OF_BOUNDS</td>
    <td>Coordinate outside the CONUS bounding box in src/config.py</td>
  </tr>
  <tr>
    <td>PARSE_ERROR</td>
    <td>Non numeric coordinate or malformed row</td>
  </tr>
  <tr>
    <td>DUPLICATE_DROPPED</td>
    <td>Repeated location_id, first occurrence wins</td>
  </tr>
  <tr>
    <td>INVALID_STATE</td>
    <td>State field not in the US state list</td>
  </tr>
</table>

### geoid_cb column

Implementation: src/agents/ingestion.py _derive_state_county_from_geoid.

The CSV does not provide explicit state or county columns.
It provides geoid_cb as a 15 digit Census Block GEOID.

<table>
  <tr>
    <th>Derived field</th>
    <th>Derived from geoid_cb</th>
    <th>Meaning</th>
  </tr>
  <tr>
    <td>state</td>
    <td>Digits 1 to 2</td>
    <td>State FIPS</td>
  </tr>
  <tr>
    <td>county</td>
    <td>Digits 1 to 5</td>
    <td>County GEOID, state FIPS plus county FIPS</td>
  </tr>
</table>

Strict contract: geoid_cb must be exactly 15 numeric digits.
Shorter values are rejected to avoid ambiguous leading zero loss.
During the full pipeline run on 4.67M rows, 12 duplicate location_ids were found and dropped using first occurrence wins deduplication policy.

## NLCD TCC NoData behavior on non tree classes

Implementation: src/tools/tcc.py fetch_tcc returns tcc missing True for NoData pixels.

The first pipeline run on 10,000 NC locations surfaced 33.29 percent TCC missing.
Investigation confirmed this is NLCD TCC behavior, not a raster coverage gap.

<table>
  <tr>
    <th>NLCD code</th>
    <th>Class</th>
    <th>TCC missing rate</th>
  </tr>
  <tr><td>11</td><td>Open Water</td><td>87.5 percent</td></tr>
  <tr><td>21</td><td>Developed, Open Space</td><td>20.9 percent</td></tr>
  <tr><td>22</td><td>Developed, Low Intensity</td><td>21.3 percent</td></tr>
  <tr><td>23</td><td>Developed, Medium Intensity</td><td>50.9 percent</td></tr>
  <tr><td>24</td><td>Developed, High Intensity</td><td>86.7 percent</td></tr>
  <tr><td>31</td><td>Barren Land</td><td>86.7 percent</td></tr>
  <tr><td>41 42 43</td><td>Forest classes</td><td>0 to 20.0 percent</td></tr>
  <tr><td>52</td><td>Shrub Scrub</td><td>33.1 percent</td></tr>
  <tr><td>71</td><td>Grassland Herbaceous</td><td>78.1 percent</td></tr>
  <tr><td>81</td><td>Pasture Hay</td><td>68.4 percent</td></tr>
  <tr><td>82</td><td>Cultivated Crops</td><td>88.0 percent</td></tr>
  <tr><td>90</td><td>Woody Wetlands</td><td>19.3 percent</td></tr>
  <tr><td>95</td><td>Emergent Herbaceous Wetlands</td><td>71.4 percent</td></tr>
</table>

The scoring uses tcc_pct is None as tcc_score 0.0.
This conservatively caps the composite score below the High tier cutoff.

## What cannot be modeled with public data

<table>
  <tr>
    <th>Factor</th>
    <th>Why it is not captured</th>
  </tr>
  <tr>
    <td>Exact tree heights</td>
    <td>TCC measures canopy area percent, not height</td>
  </tr>
  <tr>
    <td>Building heights</td>
    <td>No national public dataset exists at required resolution</td>
  </tr>
  <tr>
    <td>Seasonal canopy variation</td>
    <td>NLCD TCC is a 2021 peak summer snapshot</td>
  </tr>
  <tr>
    <td>Sub 30 meter obstructions</td>
    <td>Single trees can be invisible within a larger pixel</td>
  </tr>
  <tr>
    <td>Microsite conditions</td>
    <td>Roof access and permissions require site assessment</td>
  </tr>
  <tr>
    <td>Temporary obstructions</td>
    <td>Cranes and seasonal objects change rapidly</td>
  </tr>
</table>
