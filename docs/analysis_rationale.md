# Analysis Rationale

This is the reasoning behind the coverage risk score. I wrote it the same way I would explain the work to a teammate. What data I trusted. What I could not measure. What a score should mean in operations.

## From install guide to methodology

Before writing a single line of code I spent time reading the Starlink Business Install Guide carefully. What struck me immediately was how specific it was about the physical problem. The guide does not say coverage may be poor in some areas. It says that a tree branch, a single one, causes service interruptions. That specificity drove the whole methodology. Each scoring factor in this pipeline traces back to something the guide names explicitly as a cause of failure.

## Step 0: Understanding the Problem Before Writing Code

I wanted the pipeline design to be stable before implementation. So I wrote down the physical constraints first. Then I picked the simplest public datasets that match those constraints. Then I picked thresholds with clear justifications.

## Primary Evidence anchors

These tables are the literal anchor points. They show the guide statements I treated as requirements. If a factor is not traceable back to this kind of statement, it does not belong in the score.

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

At the end of the day the failure mode is simple. Something blocks the dish view of the sky. When that obstruction is fixed, the dish sees it again and again. That is what creates recurring interruptions.

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

This is where the score stops being abstract. The dish has a field of view. It has a minimum elevation clearance. Those requirements are what I convert into thresholds later. I keep the wording concrete so it stays auditable.

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

## Why this approach and not something else

I considered more sophisticated approaches. LiDAR data would give precise tree heights instead of canopy coverage percentages, which would be far more accurate. A machine learning model trained on past installation outcomes would give empirically calibrated weights instead of expert judgment. The reason I chose three national raster datasets with a weighted formula is simple: the data for the better approaches does not exist at national scale. LiDAR is not available for all of North Carolina. There are no labeled datasets of Starlink installation outcomes to train on. The approach I built is the most rigorous one that is actually possible with public data today. I documented where better data would change the analysis and designed the pipeline so recalibration is a one line config change when that data eventually exists.

For a statewide workflow I needed national coverage. I also needed datasets that measure the same physical mechanisms the guide talks about. These datasets approximate sky visibility using national remote sensing.

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

## What at risk actually means

A broadband officer reading these results needs to know one thing: a High risk score is a flag for prioritization, not a verdict. It means the environmental data suggests this location is likely to have signal problems if a dish is installed at ground level without elevated mounting. It does not mean the location cannot be served. Many High risk locations will be perfectly serviceable once an installer chooses a rooftop mount or a pole extension. What the score says is: do not send an installer without a site assessment first. The cost of a failed installation including the truck roll, the technician time, and the potential refund is higher than the cost of one site visit to verify. That asymmetry is why the methodology is deliberately conservative.

Remote sensing is good at broad patterns. It is not good at the last few meters around a house. This section is the honest list of what the model cannot see. It is also the reason the workflow includes site assessment.

<table>
  <tr>
    <th>Factor</th>
    <th>Why remote sensing cannot capture it</th>
    <th>Impact on risk scores</th>
  </tr>
  <tr>
    <td>Exact tree heights</td>
    <td>TCC measures canopy area percent, not height.</td>
    <td>Scores in areas of known ornamental vegetation or low shrubland may overstate risk. Field teams should note actual vegetation height on arrival and flag if the canopy is below shoulder height.</td>
  </tr>
  <tr>
    <td>Seasonal canopy variation</td>
    <td>NLCD 2021 TCC is peak summer snapshot.</td>
    <td>Locations in counties with primarily deciduous forest that score High in summer should be reassessed using the winter advisory from interactive mode before final deployment decisions.</td>
  </tr>
  <tr>
    <td>Building heights</td>
    <td>No national public building height dataset at location resolution.</td>
    <td>In dense urban areas treat Moderate scores as potentially High until a site visit confirms roof mounting is viable.</td>
  </tr>
  <tr>
    <td>Dish mounting options</td>
    <td>Remote sensing cannot see rooftop geometry or permissions.</td>
    <td>A High score does not mean unserviceable. It means assess first. One site visit can often resolve the uncertainty.</td>
  </tr>
  <tr>
    <td>Sub 30 meter obstructions</td>
    <td>Single trees can be invisible inside a larger pixel.</td>
    <td>Edge cases near property boundaries in otherwise open areas may be underscored. Installers should walk the site perimeter.</td>
  </tr>
  <tr>
    <td>Temporal changes after 2021</td>
    <td>New construction and tree growth are not reflected.</td>
    <td>Scores should be treated as 2021 baseline estimates. The pipeline is designed to rerun on updated NLCD vintages when they become available.</td>
  </tr>
</table>

## 5. How the Thresholds Were Derived

Thresholds are where you can accidentally smuggle opinion into a model. I tried to avoid that. Each threshold is tied to a physical constraint or a conservative engineering assumption. Where I had to make a judgment call, I wrote it down.

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

The 0.60 threshold was chosen deliberately so that one elevated signal without corroboration from the other two factors lands in Moderate rather than High, preventing a single noisy reading from triggering a false alarm.

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

Seasonality matters. The score uses a single NLCD canopy snapshot. That is a stable baseline. The interactive view can add a winter advisory without rewriting the stored score.

The batch pipeline stores one peak summer NLCD TCC score per location.
Seasonal advice is returned at query time and does not change stored scores.

Deciduous leaf drop can reduce obstruction in winter.
Evergreen remains stable and mixed forest has partial seasonal change.

## 8. What Would Change These Thresholds

I do not want these thresholds to be frozen forever. I want them to be easy to update when better evidence exists. This section describes the triggers that justify a change.

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

The tier is the operational output. It is meant to change what you do next. It is not meant to be a verdict about eligibility. It is a triage tool for limited staff time.

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