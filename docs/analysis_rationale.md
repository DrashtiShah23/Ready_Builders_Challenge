# Analysis Rationale

## Step 0: Understanding the Problem Before Writing Code

This document records decisions made before pipeline implementation.

## Primary Evidence anchors

This section quotes the install guide language that directly anchors the scoring design.

<table>
  <tr>
    <th>Anchor point</th>
    <th>Exact language from install guide</th>
    <th>How it drives the methodology</th>
  </tr>
  <tr>
    <td>Tree obstruction</td>
    <td>Objects obstructing the connection such as a tree branch cause service interruptions</td>
    <td>Tree canopy cover is the primary factor at 50 percent weight.</td>
  </tr>
  <tr>
    <td>Sky cone requirement</td>
    <td>100 to 110 degree unobstructed field of view</td>
    <td>FOV cone informs why even partial canopy in any azimuth direction creates risk.</td>
  </tr>
  <tr>
    <td>Elevation minimum</td>
    <td>Clear sky above 25 degrees elevation in all directions</td>
    <td>Slope threshold derived by trigonometry from this exact requirement.</td>
  </tr>
  <tr>
    <td>Permanence</td>
    <td>Fixed obstructions cause recurring outages on every orbital pass</td>
    <td>Risk score reflects persistent environmental conditions not temporary ones.</td>
  </tr>
</table>

<table>
  <tr>
    <th>Topic</th>
    <th>Answer</th>
  </tr>
  <tr>
    <td>Primary source</td>
    <td>Starlink Business Install Guide.</td>
  </tr>
  <tr>
    <td>Specs source</td>
    <td>Starlink hardware spec sheet and FAQ on support.starlink.com.</td>
  </tr>
  <tr>
    <td>Corroborating sources</td>
    <td>USDA Forest Service guidance and FCC broadband mapping guidance.</td>
  </tr>
</table>

## 1. What physically causes service interruptions?

Service interruptions come from fixed obstructions blocking the satellite link.

<table>
  <tr>
    <th>Obstruction type</th>
    <th>What it does</th>
    <th>Source</th>
  </tr>
  <tr>
    <td>Trees and foliage</td>
    <td>Branches obstruct the sky cone and cause recurring dropouts.</td>
    <td>Install guide and Starlink support documentation.</td>
  </tr>
  <tr>
    <td>Terrain</td>
    <td>Hills and slopes raise the horizon and reduce usable sky arc.</td>
    <td>Install guide interpretation of minimum elevation clearance.</td>
  </tr>
  <tr>
    <td>Structures</td>
    <td>Buildings obstruct the sky, but height is hard to model remotely.</td>
    <td>Install guide and limitations in Section 4.</td>
  </tr>
  <tr>
    <td>Quoted guide text</td>
    <td>Objects obstructing the connection cause service interruptions.</td>
    <td>Install guide quote in original version of this document.</td>
  </tr>
</table>

## 2. What does the dish need from its environment?

These requirements define the physical constraints used by thresholds.

<table>
  <tr>
    <th>Requirement</th>
    <th>Value</th>
    <th>Source</th>
  </tr>
  <tr>
    <td>Dish field of view</td>
    <td>110 degree field of view.</td>
    <td>Starlink spec sheet support.starlink.com.</td>
  </tr>
  <tr>
    <td>Unobstructed cone</td>
    <td>At least 100 degrees unobstructed within the cone.</td>
    <td>Starlink FAQ.</td>
  </tr>
  <tr>
    <td>Minimum elevation clearance</td>
    <td>Nothing above 25 degrees above the horizon within the cone.</td>
    <td>Starlink FAQ support.starlink.com.</td>
  </tr>
  <tr>
    <td>Auto leveling and tilt</td>
    <td>Dish auto levels and tilts; northern azimuth matters in the US.</td>
    <td>Install guide behavior description.</td>
  </tr>
  <tr>
    <td>Mounting mitigation</td>
    <td>Elevated mounting is recommended when ground level is obstructed.</td>
    <td>Install guide mounting guidance.</td>
  </tr>
  <tr>
    <td>Mount stability</td>
    <td>Rigid stable mounting; vibration degrades quality.</td>
    <td>Install guide.</td>
  </tr>
</table>

## 3. What publicly available datasets can model this at scale?

These datasets approximate sky visibility using national remote sensing.

<table>
  <tr>
    <th>Dataset</th>
    <th>What it measures</th>
    <th>Obstruction factor from install guide</th>
    <th>Why this over alternatives</th>
  </tr>
  <tr>
    <td>NLCD 2021 Tree Canopy Cover</td>
    <td>Tree canopy percentage per pixel, 0 to 100.</td>
    <td>Tree branches and foliage obstruction.</td>
    <td>Continuous canopy signal with national coverage and version pin.</td>
  </tr>
  <tr>
    <td>USGS 3DEP elevation derived slope and aspect</td>
    <td>Slope degrees and aspect direction from elevation.</td>
    <td>Terrain blocking the minimum elevation angle.</td>
    <td>National DEM at 10 to 30 meter resolution.</td>
  </tr>
  <tr>
    <td>NLCD 2021 Land Cover</td>
    <td>Land use class codes and class names per pixel.</td>
    <td>Structural density context and cross validation of canopy.</td>
    <td>Same dataset family as canopy with aligned CRS and resolution.</td>
  </tr>
</table>

## 4. What cannot be modeled remotely and why?

These limitations define where human site assessment remains required.

<table>
  <tr>
    <th>Factor</th>
    <th>Why remote sensing cannot capture it</th>
    <th>Impact on risk scores</th>
  </tr>
  <tr>
    <td>Exact tree heights</td>
    <td>TCC measures canopy area percent, not height.</td>
    <td>Risk may be overstated for shrubs and understated for tall isolated trees.</td>
  </tr>
  <tr>
    <td>Seasonal canopy variation</td>
    <td>NLCD 2021 TCC is peak summer snapshot.</td>
    <td>Winter leaf drop can reduce obstruction for deciduous and mixed forest.</td>
  </tr>
  <tr>
    <td>Building heights</td>
    <td>No national public building height dataset at location resolution.</td>
    <td>Urban structural obstruction may be understated.</td>
  </tr>
  <tr>
    <td>Dish mounting options</td>
    <td>Remote sensing cannot see rooftop geometry or permissions.</td>
    <td>High risk means assess, not unserviceable.</td>
  </tr>
  <tr>
    <td>Sub 30 meter obstructions</td>
    <td>Single trees can be invisible inside a larger pixel.</td>
    <td>Risk may be understated for edge cases.</td>
  </tr>
  <tr>
    <td>Temporal changes after 2021</td>
    <td>New construction and tree growth are not reflected.</td>
    <td>Scores are point in time assessments and need periodic reruns.</td>
  </tr>
</table>

## 5. How the Thresholds Were Derived

Every threshold ties to physical requirements and conservative assumptions.

<table>
  <tr>
    <th>Factor</th>
    <th>Threshold</th>
    <th>Physical justification</th>
    <th>Calibration note</th>
  </tr>
  <tr>
    <td>Tree canopy cover</td>
    <td>High above 50 percent, Moderate 20 to 50 percent, Low below 20 percent.</td>
    <td>Majority canopy within surrounding area raises obstruction likelihood.</td>
    <td>Future calibration uses installation outcomes versus canopy values.</td>
  </tr>
  <tr>
    <td>Alternative thresholds considered</td>
    <td>40 percent and 60 percent both evaluated</td>
    <td>40 percent would flag suburban tree cover that rarely causes meaningful obstruction producing more false positives. 60 percent would miss locations that are substantially obstructed but not quite dense forest. 50 percent is the defensible midpoint without calibration data.</td>
    <td>With actual install outcome data a logistic regression on success rate versus canopy percent would replace this prior.</td>
  </tr>
  <tr>
    <td>Terrain slope</td>
    <td>High above 20 degrees, Moderate 10 to 20 degrees, Low below 10 degrees.</td>
    <td>Terrain rise = 20 times tan(20 degrees) equals approximately 7.3 meters. Apparent elevation angle = arctan(7.3 divided by 20) equals approximately 20 degrees, which approaches the 25 degree minimum clearance requirement.</td>
    <td>Pixel averages can hide steeper local extremes within each pixel.</td>
  </tr>
  <tr>
    <td>Land cover</td>
    <td>Forest codes High, Developed codes Moderate, Open and water codes Low.</td>
    <td>Forest classification signals persistent canopy obstruction context.</td>
    <td>Developed is Moderate due to roof mounting mitigation and missing height data.</td>
  </tr>
</table>

## 6. Composite Score and Tier Thresholds

The composite score is computed using weighted component scores.

<table>
  <tr>
    <th>Tier</th>
    <th>Score range</th>
    <th>Worked example</th>
    <th>What a broadband officer should do</th>
  </tr>
  <tr>
    <td>High</td>
    <td>0.60 and above</td>
    <td>(1.0 times 0.50) plus (0.5 times 0.30) plus (0.0 times 0.20) equals 0.65.</td>
    <td>Prioritize site assessment before scheduling installation.</td>
  </tr>
  <tr>
    <td>Moderate</td>
    <td>0.30 to 0.59</td>
    <td>(1.0 times 0.50) plus (0.0 times 0.30) plus (0.0 times 0.20) equals 0.50.</td>
    <td>Proceed with standard workflow and flag potential obstructions.</td>
  </tr>
  <tr>
    <td>Low</td>
    <td>Below 0.30</td>
    <td>(0.5 times 0.50) plus (0.0 times 0.30) plus (0.0 times 0.20) equals 0.25.</td>
    <td>Proceed with confidence, while noting known limitations.</td>
  </tr>
  <tr>
    <td>UNSCORED</td>
    <td>All inputs null</td>
    <td>All environmental signals are missing for the coordinate.</td>
    <td>Require manual assessment before any deployment decision.</td>
  </tr>
</table>

## 7. Seasonal Variation Handling

The batch pipeline stores one peak summer NLCD TCC score per location.
Seasonal advice is returned at query time and does not change stored scores.

Deciduous leaf drop can reduce obstruction in winter.
Evergreen remains stable and mixed forest has partial seasonal change.

## 8. What Would Change These Thresholds

Thresholds can be updated when new evidence becomes available.

<table>
  <tr>
    <th>Condition</th>
    <th>What changes</th>
    <th>How to recalibrate</th>
  </tr>
  <tr>
    <td>Ground truth calibration data appears</td>
    <td>Thresholds move from priors to fitted values.</td>
    <td>Fit thresholds using installation outcomes versus canopy and slope values.</td>
  </tr>
  <tr>
    <td>Operator feedback indicates systematic errors</td>
    <td>Weights and thresholds shift toward observed failure modes.</td>
    <td>Adjust based on support tickets and measured service quality outcomes.</td>
  </tr>
  <tr>
    <td>Dataset vintages update</td>
    <td>New NLCD layers can shift distributions.</td>
    <td>Rerun, compare tier proportions, and review thresholds if drift is large.</td>
  </tr>
</table>

## 9. Operational Meaning of Each Risk Tier

These tiers support BEAD grant operations and installation planning.

<table>
  <tr>
    <th>Tier</th>
    <th>Score range</th>
    <th>Recommended action</th>
  </tr>
  <tr>
    <td>High</td>
    <td>0.60 and above</td>
    <td>Prioritize physical site assessment before committing installation resources.</td>
  </tr>
  <tr>
    <td>Moderate</td>
    <td>0.30 to 0.59</td>
    <td>Proceed with notes and consider roof mounting as a precaution.</td>
  </tr>
  <tr>
    <td>Low</td>
    <td>Below 0.30</td>
    <td>Proceed with confidence while acknowledging resolution and vintage limits.</td>
  </tr>
  <tr>
    <td>UNSCORED</td>
    <td>All inputs null</td>
    <td>Manual assessment required before any deployment decision.</td>
  </tr>
</table>

The methodology is deliberately conservative because the cost of a failed installation attempt including a technician truck roll and potential refund is higher than the cost of an extra site assessment for a location that turns out to be serviceable.