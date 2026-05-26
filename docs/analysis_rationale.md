# Analysis Rationale

## Step 0: Understanding the Problem Before Writing Code

This document captures the thinking that happened before any pipeline was designed.
The challenge asked four questions about the physical problem we are solving. Those
answers shaped every dataset choice, every threshold in the scoring formula, and
every limitation we document honestly.

**Sources used in this document:**
The Starlink Business Install Guide was the primary reference. Where it pointed to
the app for precise technical measurement, those specifications were sourced from
Starlink's published hardware spec sheet and FAQ on support.starlink.com. External
references to USDA Forest Service and FCC broadband mapping guidance are cited where
they corroborate threshold choices. Each claim is attributed to its source.

---

## 1. What physically causes service interruptions?

The Starlink dish is not like an old satellite TV dish that points at a single fixed
spot in space. It communicates with dozens of small satellites moving rapidly across
the sky, handing off from one to the next continuously. To do this reliably, it
needs to see a wide arc of sky at all times — not just directly overhead.

The install guide states directly:

> "Objects that obstruct the connection between your Starlink and the satellite,
> such as a tree branch, pole, or roof, will cause service interruptions."

What makes this harder than it sounds is the word "interruptions" rather than
"failure." Because satellites travel through predictable orbital paths, a tree
branch that blocks one part of the sky will cause a brief dropout every few minutes
as each satellite passes through that blocked zone. A fixed obstruction — a tree,
a building, a hillside — creates recurring outages on every orbital pass through
that arc. The result is not a dead connection but a connection that repeatedly drops
throughout the day.

The install guide identifies three categories of fixed obstruction:

**Trees and foliage.** The guide explicitly states that even a single branch causes
outages. Tree canopy is the primary and most commonly cited obstruction source in
both the guide and Starlink's support documentation.

**Terrain.** Hills, ridges, and slopes that rise above the horizon from the dish's
perspective reduce the usable sky arc. A dish at the bottom of a valley or on a
north-facing slope has a constrained view of the southern sky where the satellite
constellation is most active.

**Structures.** Buildings, sheds, and fixed constructions. These are the hardest
to model remotely because no national dataset captures building heights at the
resolution needed — this is documented as a known limitation in Section 4.

The dish hardware has a 110 degree field of view (Starlink spec sheet). For stable
operation, Starlink's FAQ specifies that at least 100 degrees of that cone must be
completely unobstructed, with nothing rising above 25 degrees above the horizon in
any direction within that cone.

To picture 25 degrees: hold your arm straight out in front of you at zero degrees,
then raise it about a quarter of the way toward straight above your head. That angle
is roughly 25 degrees. Anything that reaches that height in the dish's field of
view — a roofline, a hillside, a row of tall trees — will cause recurring service
interruptions.

---

## 2. What does the dish need from its environment?

Beyond the basic clear-sky requirement, the install guide is specific about how the
dish behaves within its physical environment.

The dish auto-levels on startup and then auto-tilts to its optimal angle. In the
Northern Hemisphere, which includes all US locations, it tilts slightly northward
to face the arc the satellites travel across the sky. This means the 25 degree
clearance requirement is not symmetric — the northern azimuth direction matters
most for US locations.

If a ground-level location does not provide sufficient sky view, the install guide
explicitly recommends elevated mounting: a rooftop, a pole mount, or a wall bracket.
This is not a workaround — it is Starlink's own acknowledgment that many ground-level
locations will be obstructed and that mounting flexibility is part of the deployment
picture. The risk pipeline models environmental conditions; it does not model whether
an elevated mounting option is available at the property.

The mount must also be rigid and stable. Vibration from wind degrades connection
quality even when the sky view is clear, which adds a structural stability
requirement on top of the environmental obstruction question.

Summarizing the complete environmental requirements from the guide and specifications:

- A 100 to 110 degree unobstructed sky cone, measured from the dish center after
  auto-tilt
- Nothing rising above 25 degrees above the horizon in any direction within that cone
- Installation as close to vertical as possible
- Stable, rigid mounting — vibration degrades performance
- If ground level is insufficient: an elevated mounting option must exist

---

## 3. What publicly available datasets can model this at scale?

We have approximately one million locations to assess. Physical site visits are not
feasible at that scale. The question becomes: what does government geospatial data
already tell us about the sky view at each address?

Three nationally available, publicly funded, free datasets together cover the
physical conditions the install guide describes. All three are CONUS-wide, from the
same 2021 vintage, and at 30 meter or comparable resolution.

### Dataset 1 — Tree Canopy Cover (NLCD 2021 TCC, USGS/MRLC)

Every 30 by 30 meter square in the continental US is assigned a number from 0 to
100 representing the percentage of that area covered by tree canopy. A location
under dense forest scores near 100. An open field scores near 0.

The install guide names tree branches as the primary obstruction source. TCC maps
that obstruction directly and continuously. Unlike a simple forest/non-forest
classification, TCC gives a gradient signal — a location at 35 percent canopy is
meaningfully different from one at 70 percent, and TCC captures that distinction.

TCC carries the highest weight in the scoring formula at 50 percent. The install
guide makes clear that vegetation is the dominant obstruction factor, and TCC
provides the most direct, granular, nationally consistent measurement of it.

### Dataset 2 — Terrain Slope and Aspect (USGS 3DEP)

The 3D Elevation Program provides elevation data across the US at 10 to 30 meter
resolution. From raw elevation, we derive two values for each location: slope in
degrees (how steeply the land tilts) and aspect (which direction that slope faces).

These values map directly to the 25 degree elevation angle requirement. A location
at the bottom of a steep south-facing valley has a terrain horizon that rises well
above 25 degrees in the direction the dish most needs to see. Slope is a hard
physical constraint in a way that canopy is not — you can mount a dish above trees,
but you cannot mount a dish above a hillside. This is why terrain carries 30 percent
of the composite score.

Slope also has a partial mitigation: a skilled installer using a tall pole mount
or rooftop installation raises the observation point, reducing the apparent terrain
elevation angle. This addressability is why terrain is weighted below canopy rather
than above it.

### Dataset 3 — Land Cover Classification (NLCD 2021 Land Cover, USGS/MRLC)

This dataset classifies every 30 by 30 meter square into a land use category:
deciduous forest (code 41), evergreen forest (42), mixed forest (43), developed
land at four intensity levels (21 through 24), grassland, cropland, barren, and
others.

Land cover serves two roles. First, it cross-validates the TCC reading. If TCC
reports 75 percent canopy and land cover confirms evergreen forest, that is a
high-confidence signal. If TCC is high but land cover says developed high-intensity,
that discrepancy is a data quality flag worth investigating. Second, developed-area
codes add structural density context that TCC alone does not capture — acknowledging
that buildings in urban areas obstruct the dish even when tree canopy is low.

Land cover comes from the same dataset family as TCC — same coordinate system, same
resolution, same download pipeline. Adding it costs nothing in complexity while
adding meaningful cross-validation to the primary signal. It carries 20 percent of
the composite score in a supporting role.

---

## 4. What cannot be modeled remotely — and why?

Honest analysis requires knowing where the data ends. The following limitations are
a guide to where risk scores should be treated with extra caution and where physical
site assessment is genuinely necessary before making deployment decisions.

**Exact tree heights.** TCC measures the percentage of ground covered by canopy,
not how tall the trees are. A location with 80 percent canopy could be sitting under
10-foot ornamental shrubs or 100-foot Douglas firs. Both score identically in TCC,
but only the latter meaningfully threatens the 25 degree elevation requirement. This
means TCC-based scores may overstate risk for short-canopy locations and may
understate risk for locations with a small number of very tall isolated trees. LiDAR
canopy height models would resolve this but are not available nationally.

**Seasonal canopy variation.** NLCD TCC is a 2021 peak-summer snapshot. Deciduous
trees (NLCD code 41) lose their leaves entirely in winter, dropping from near-full
canopy obstruction to essentially none. Evergreen trees (code 42) stay the same
year-round. A location assessed as high-risk based on summer data in the
northeastern or midwestern US may be fully serviceable from October through April.
The pipeline stores peak-summer scores. The agent's analyze_location tool adds a
seasonal advisory for deciduous and mixed-forest locations without changing the
stored score — a query-time note rather than an override, to avoid confusion in
reports where the same location would otherwise show different scores depending on
when it was queried. This is flagged as open item OI-03 pending team guidance.

**Building heights.** No national public dataset captures building heights at
location resolution. Land cover tells us a location is in a developed area but not
whether the surrounding buildings are two stories or twenty. In dense urban areas
the structural obstruction component of the score may understate actual risk. MODERATE
rather than HIGH is assigned to developed codes because urban installations typically
use roof mounting which mitigates low-angle obstruction, and because without height
data the honest middle ground is more defensible than a confident HIGH.

**Dish mounting options at the property.** Two houses with identical environmental
scores may have completely different real-world outcomes depending on whether a
viable rooftop mounting point exists. Remote sensing cannot see rooftop geometry,
structural suitability, HOA restrictions, or landlord permissions. A High risk score
means a site assessment is warranted, not that service is impossible.

**Obstructions below 30 meter resolution.** A single large tree at the property
boundary may not register in a 30 meter TCC pixel if surrounded by open land, but
could still break the 25 degree elevation angle in one direction. These are edge
cases but they are real and require physical assessment to catch.

**Temporal changes after 2021.** New construction, tree growth, and logging after
the 2021 dataset vintage are not reflected. Risk scores are point-in-time assessments
and should be revalidated against updated NLCD datasets periodically.

---

## 5. How the Thresholds Were Derived

Every number in src/config.py traces back to the physical requirements in the install
guide. This section documents the derivation so each threshold can be explained,
defended, and eventually recalibrated with empirical data.

### Tree Canopy Cover thresholds

| Threshold | Risk level | Score |
|---|---|---|
| Canopy above 50 percent | High | 1.0 |
| Canopy 20 to 50 percent | Moderate | 0.5 |
| Canopy below 20 percent | Low | 0.0 |

The 50 percent HIGH threshold represents the majority-canopy breakpoint. A TCC value
of 50 percent means more than half of the 900 square meter area around the address
is covered by tree canopy. The dish's 100 to 110 degree FOV cone scans at all azimuth
angles from 25 degrees elevation to zenith — trees to the side of the dish are as
obstructive as trees directly overhead. At 50 percent canopy it is statistically
likely that at least one azimuth segment of the FOV cone intersects a canopy-covered
area.

Why not 40 percent? That would flag suburban tree cover that rarely causes meaningful
obstruction, producing more false positives and more unnecessary site assessments.
Why not 60 percent? That misses locations that are substantially obstructed but not
quite dense forest. 50 percent is the defensible midpoint without calibration data.

This threshold is also consistent with USDA Forest Service broadband planning
guidance, which uses similar canopy density thresholds for fixed wireless
serviceability assessment, and with FCC broadband mapping guidance (2022) which
identifies heavily forested terrain as a known serviceability limiter for satellite
services.

The correct long-term approach is empirical calibration: a dataset of actual
Starlink installation outcomes matched to NLCD canopy values would allow the
threshold to be fitted via logistic regression on install success rate versus canopy
percentage. In the absence of that data, 50 percent is the most defensible prior.

### Terrain slope thresholds

| Threshold | Risk level | Score |
|---|---|---|
| Slope above 20 degrees | High | 1.0 |
| Slope 10 to 20 degrees | Moderate | 0.5 |
| Slope below 10 degrees | Low | 0.0 |

The 20 degree HIGH threshold is derived from the 25 degree elevation clearance
requirement using basic trigonometry. Consider a dish installed at a point where
the terrain slopes at 20 degrees. At 20 meters horizontal distance uphill:

```
Terrain rise  = 20 × tan(20°) ≈ 7.3 meters
Apparent elevation angle of terrain ridge = arctan(7.3 / 20) ≈ 20 degrees
```

This is already approaching the 25 degree minimum. Adding natural terrain variability
within a 30 meter pixel — ridgelines, rock outcrops, uneven ground — and a 20 degree
mean slope reliably produces horizon angles in the range that conflicts with
Starlink's minimum elevation requirement.

Checking the MODERATE threshold at 15 degrees:

```
Terrain rise  = 20 × tan(15°) ≈ 5.4 meters
Apparent elevation angle = arctan(5.4 / 20) ≈ 15 degrees
```

At 15 degrees the horizon sits at approximately 15 degrees elevation — below the 25
degree minimum but meaningfully reduced sky access, justifying MODERATE rather than
LOW. The 20 degree threshold also incorporates a conservative margin because slope
raster pixels represent the average slope across a 10 to 30 meter area. Actual
worst-case spots within the pixel — valley floors and ridge crests — may be
significantly steeper than the pixel average.

### Land cover thresholds

| NLCD codes | Class | Risk level | Score |
|---|---|---|---|
| 41, 42, 43 | Deciduous, Evergreen, Mixed Forest | High | 1.0 |
| 21, 22, 23, 24 | Developed, Open Space to High Intensity | Moderate | 0.5 |
| 31, 52, 71, 81, 82, 11, 12 | Barren, Shrub, Grassland, Crops, Water | Low | 0.0 |

Forest codes are HIGH because a forest land cover classification means the entire
30 meter pixel exists within a forest ecosystem — this is not incidental tree cover,
it is a location that sits within a forested environment. The install guide is
unambiguous about forests causing signal interruption.

Land cover's critical role here is cross-validation. If canopy cover is 48 percent
(just below the 50 percent HIGH threshold) and land cover is Forest code 41, the
composite score will still trend HIGH because both independent signals indicate the
same physical reality. This cross-validation prevents edge cases near the 50 percent
canopy boundary from being systematically underscored.

Developed codes are MODERATE rather than HIGH because urban and suburban
installations typically use roof mounting which mitigates low-angle building
obstruction, and because without building height data a confident HIGH score would
be dishonest. MODERATE is the correct position when we know risk exists but cannot
quantify it.

---

## 6. Composite Score and Tier Thresholds

The composite score is computed as:

```
composite_score = (canopy_score × 0.50) + (terrain_score × 0.30) + (landcover_score × 0.20)
```

All individual component scores are in the range 0.0 to 1.0. The composite score
is therefore also in the range 0.0 to 1.0.

| Tier | Score range | Operational meaning |
|---|---|---|
| High | 0.60 and above | Multiple factors indicate significant obstruction risk. Priority site assessment before scheduling installation. |
| Moderate | 0.30 to 0.59 | Some factors elevated. Standard installation workflow with noted obstructions and roof-mount recommendation. |
| Low | Below 0.30 | Environmental conditions favor successful installation. Proceed to standard workflow with confidence. |
| UNSCORED | All inputs null | Environmental data unavailable for this location. Manual assessment required. |

### Why 0.60 is the HIGH tier cutoff

The 0.60 threshold is chosen so that a single HIGH factor with no corroboration from
the other two factors produces MODERATE rather than HIGH. This prevents one noisy
measurement from triggering a false HIGH for a location that is otherwise clear.

Working through the key boundary cases:

Canopy HIGH plus Slope MODERATE plus Landcover LOW:
(1.0 × 0.50) + (0.5 × 0.30) + (0.0 × 0.20) = 0.65 → HIGH. Correct. Heavy canopy
with rolling terrain is genuinely at-risk.

Canopy MODERATE plus Slope HIGH plus Landcover HIGH:
(0.5 × 0.50) + (1.0 × 0.30) + (1.0 × 0.20) = 0.75 → HIGH. Correct. Steep forested
slope is a compound risk.

Canopy HIGH plus Slope LOW plus Landcover LOW:
(1.0 × 0.50) + (0.0 × 0.30) + (0.0 × 0.20) = 0.50 → MODERATE. Correct. Heavy
canopy alone, with no terrain or land cover corroboration, is real risk but not
confirmed HIGH. A site assessment may find the canopy is short shrubs that do not
reach the elevation threshold.

Canopy MODERATE plus Slope LOW plus Landcover LOW:
(0.5 × 0.50) + (0.0 × 0.30) + (0.0 × 0.20) = 0.25 → LOW. Correct. Some trees but
otherwise clear environment.

The 0.60 threshold requires that the weighted obstruction signal rises above a single
elevated factor before committing to HIGH. This is a deliberate design decision to
reduce false positives in the output.

---

## 7. Seasonal Variation Handling

The batch pipeline assigns one risk score per location using peak-summer NLCD TCC
values. This is a known limitation. Two things address it without compromising the
consistency of stored scores.

The agent's analyze_location tool checks the NLCD land cover code at query time
and appends a seasonal advisory to the response for the technician:

For Deciduous Forest (code 41): leaf drop from November through March reduces
effective canopy obstruction by an estimated 30 to 60 percent. A location scored
HIGH in summer may function as MODERATE during a winter installation window. The
agent flags this and recommends scheduling the Starlink in-app obstruction check
after leaf drop if installation is planned in winter.

For Mixed Forest (code 43): partial seasonal benefit. The deciduous component sheds,
the evergreen component remains. The agent flags moderate winter improvement and
notes that the evergreen fraction maintains year-round obstruction.

For Evergreen Forest (code 42): no seasonal benefit. The agent explicitly notes this
so the technician does not assume a winter installation will perform better than the
summer score suggests.

The stored composite score does not change based on the season of the query. The
seasonal note is advisory guidance only, returned as part of the analyze_location
response. Changing the stored score based on query time would cause the same location
to show different risk levels in different reports, which would undermine trust in
the analysis. The score is a fixed assessment of peak-summer environmental conditions.
The seasonal advisory is a human-readable interpretation layered on top of it.

---

## 8. What Would Change These Thresholds

These thresholds are modeled priors derived from physical requirements. They are
the most defensible starting point in the absence of empirical data. They would
be updated under either of two conditions.

Ground truth calibration: a dataset of actual Starlink installation outcomes
(succeeded at ground level, required elevated mounting, failed entirely) matched to
the NLCD canopy percentages and slope values for those locations would allow
empirical threshold fitting. Logistic regression on install success rate versus each
environmental factor would produce data-derived thresholds that replace the current
judgment-based values.

Operator feedback: if Ready or their provider partners have internal data on which
location types generate the highest support ticket rates for connectivity problems,
those distributions would allow threshold recalibration toward the actual failure
modes observed in the field.

In the absence of either, the thresholds derive from the physical constraints in
the Starlink installation guide — the only authoritative primary source available.
All thresholds are stored in src/config.py as named constants, making future
recalibration a single-file change.

---

## 9. Operational Meaning of Each Risk Tier

This analysis was built to support the BEAD (Broadband Equity, Access, and
Deployment) grant program context, where states have committed specific locations
to be served by LEO satellite providers. The risk scores serve one operational
purpose: helping a state broadband officer prioritize a million locations into
actionable tiers, directing limited site assessment resources toward the locations
most likely to need them.

A High score means: based on remotely sensed 2021 environmental data, this location
has significant likely obstruction. Prioritize it for physical site assessment before
committing installation resources. Do not assume it is unserviceable — assume it
needs a closer look. A skilled installer with a roof mount may find it entirely
workable.

A Moderate score means: some elevated environmental factors detected. Proceed to
standard installation workflow with notes. Recommend roof mounting as a precaution.
Flag for follow-up if the installer encounters obstruction on arrival.

A Low score means: no significant environmental obstruction detected in 2021 data
at 30 meter resolution. Proceed to standard installation with confidence. This does
not rule out the sub-30m, temporal, and building-height limitations documented in
Section 4, but it represents the best available remote assessment.

An UNSCORED location means: environmental data was unavailable for this coordinate.
This may indicate an edge of coverage, a data gap, or a coordinate that falls
outside the NLCD extent. Manual assessment is required before any deployment
decision.

The methodology is deliberately conservative — it flags more locations as at-risk
rather than fewer. The cost of a failed installation attempt (failed install,
technician truck roll, potential refund) is higher than the cost of an extra site
assessment for a location that turns out to be fine.