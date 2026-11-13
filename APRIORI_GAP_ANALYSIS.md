# aPriori vs CAD Costing Agent — Gap Analysis

As of 2026-10-30; rows marked **Done (Iteration 5)** were closed on 2026-11-13.

The agent already covers aPriori's core costing structure for machining, stage-rated routes and
timed MIG/TIG welding. The gaps that are cheap to close reuse existing pricing paths. The expensive
ones all need new geometric measurement: wall thickness, draw or parting direction, tool access, or
sheet unfolding.

## Method and limits

- Four research agents read **public material only**. Every page on apriori.com and docs.apriori.com
  returned HTTP 403, and no agent tried to get around it.
- **No aPriori page was read in full.** Every aPriori fact below comes from a search-engine summary of a
  public page. Treat wording and numbers as secondhand.
- Most of the cost-model pages are from the 2019 R2 or 2021 R1 releases, so current behaviour may differ.
- Before you implement anything on the strength of one of these formulas, confirm it against a licensed
  aPriori instance or with aPriori directly.
- Effort: **S** reuses an existing path and takes about one iteration. **M** needs a new model or input
  but no new geometry. **L** needs new geometric measurement or a new subsystem.

## Platform features

| aPriori capability | What we have | What we'd need | Effort |
|---|---|---|---|
| Matrix costing: one scenario per region × annual volume × batch | Route comparison and bulk preview, both through `price_cad_part` | **Done (Iteration 5):** `POST /cost/matrix-preview` | — |
| Scenario comparison and what-if | `estimates.compare_estimates` with variance decomposition | A side-by-side view of two stored estimates or previews | S |
| Cost-driver breakdown (material, factory, admin) | A cost split in the stored result | A driver table read from the stored result, with no new arithmetic | S |
| Supplier quote against should-cost | `cost_actuals`: immutable, per unit, variance shown | **Done (Iteration 5):** `figure_kind: supplier_quote`, per-category gap, quotes side by side unranked | — |
| Digital factories: per-region labour, per-machine overhead (direct and indirect), material catalogue | Versioned, immutable rate sets, INR by default | Region and factory metadata on rate sets, a per-machine overhead split, and a picker. Currencies stay unconverted | M |
| Annual volume and tooling amortisation | A one-off batch charge on a stage | A lifetime or annual quantity, tooling per unit, and a quotation line | S–M |
| DFM issues highlighted on the 3D model | DFM flags with cited thresholds, and feature selection in the viewer | **Partly done (Iteration 5):** a flag marks its features with a pointer; face-level highlighting still needs per-face ids | L for faces |
| Cost hotspots on the 3D model | None. There is no per-operation cost by design | Per-operation cost, which conflicts with the plan-level cost preview | L |
| Bulk costing, BOM and reports | CSV bulk preview (never stored) and assembly roll-up | Persisted bulk runs behind a review gate, plus export | M |
| CO2e (aPriori uses IPCC GWP100 and ecoinvent) | None | A versioned emission-factor table (material kg CO2e/kg, process kWh × grid factor), labelled illustrative | M |
| Roles, approvals and audit | None | Auth, roles, an approval state on estimates, and an audit log | L |
| Comments and @mentions | None | Comment threads on estimates | M |
| PLM and CAD plug-ins, public API | REST API | A documented API, a batch trigger for PLM, and CAD plug-ins | L |

## Machining, moulding and secondary processes

| aPriori capability | What we have | What we'd need | Effort |
|---|---|---|---|
| Setup count from tool access (setup-axis GCDs, capped at 4 setups on a 3-axis mill) | One assumed setup that the engineer regroups | Per-face approach directions and an obstruction test on the B-Rep | L |
| Tolerance and roughness add finish passes or secondary operations | No tolerance input | A tolerance/Ra field per feature, a process-capability table and finish-pass logic | M |
| Threads | None | A thread recognizer and a tap/thread-mill time rule | M |
| Prismatic stock (bar or plate) with per-face allowances and utilisation | Bar-stock proposal for turned parts | Bounding-box stock, per-face allowances, utilisation and a saw operation | M |
| Feeds and speeds by material hardness, tool size picked from the feature | One fixed parameter profile | A material-keyed cutting-data table and tool diameter chosen from pocket or slot size | M |
| 2-model machining: a cast or forged blank model plus the finished model | As-formed defaults on a decided route, no blank model | A second model input and removed volume per feature | L |
| Injection-moulding cycle: cooling ∝ wall², clamp force, cavities | A flat per-kg or per-piece stage rate | Wall-thickness measurement, cycle formulas, a machine table and a runner model | L |
| Mould and tooling cost | A one-off batch charge | A tooling model (steel, size, actions, finish) | L |
| Heat treatment, surface treatment, inspection and packaging by area or batch | Flat per-kg or per-piece stage rates | Surface area and masked holes from the B-Rep, rack/hanger capacity, hourly rate × cycle | M |
| Powder metal, additive, rotational and blow moulding | None | No public algorithm was found. Out of scope | n/a |

## Sheet metal, tube, welding and assembly

| aPriori capability | What we have | What we'd need | Effort |
|---|---|---|---|
| Laser/plasma cut time from cut perimeter, pierces and thickness | A flat per-piece cutting rate | Flattening, blank and hole perimeters, and a cut-rate table by thickness | L |
| Bend-brake cycle: bends × (bend time + handling by mass) | A flat per-piece bending rate | Bend detection and unfolding | L |
| Turret hits, nibbling and tooling | None | Hole and edge geometry and a tool library | L |
| Nesting and utilisation | Sheet candidate detection only | A rectangular nesting estimate with an override | M |
| Weld time from length × passes / travel speed × arc factor | **Same formula**, with welds the engineer types in | Passes already come from size / size-per-pass. Travel speed by throat size and filler is still open | S |
| Weld repositioning, clamping and robotic programming time | None | **Done (Iteration 5)** for reposition and clamp/tack per weld; no robotic programming (no robot input) | — |
| Weld prep, grind and clean-up from weld length | Per-piece stage rates | Derive them from the typed weld length (cut speed, cuts per groove type) | S–M |
| Weld auto-detection from contact regions | Manual entry | Contact analysis between assembly components | L |
| Mechanical assembly: screw, rivet, press-fit standard times | None. An assembly total carries no labour | A fastener-operation catalogue the engineer assigns (aPriori also assigns these by hand) | M |
| Tube and bar fabrication: saw, tube laser, bending | Bar-stock proposal only | Profile detection, plus cut and bend counts | M–L |
| Progressive and transfer die tooling | None | A station estimate from bend and form counts | L |

## Casting and forging

| aPriori capability | What we have | What we'd need | Effort |
|---|---|---|---|
| Poured mass = machined mass + waste + bad parts + loss, all overridable | Per-kg rate × the finished CAD mass, or a mass the engineer types in | **Done (Iteration 5):** `yield_percent` on the material-supplying per-kg stage, engineer-entered, no default | — |
| Cavities and setup amortised over the batch | A one-off batch charge | A cavity-count input and amortisation over batch size | S–M |
| Melt, pour and cool steps priced by mass, energy and time | One per-kg rate | Per-step rates. Cooling time needs wall thickness | M |
| Forging: flash, press force, tooling per die station, billet | A per-kg stage plus a batch charge | Projected area along a parting direction and a flash allowance | M |
| Cooling time from maximum wall thickness | None | A wall-thickness measurement | L |
| Core making for features hidden from the draw direction | None | Draw-direction computation | L |
| Draft, undercut and minimum-wall DFM | Hole and fillet DFM rules only | Draw direction and face-normal draft | M–L |
| Machining after casting or forging | Done: as-formed defaults with an engineer override | — | — |

## Policy conflicts

Two aPriori behaviours would break rules this project keeps on purpose. Adopting either is a product
decision, not a gap to close by default:

1. **aPriori picks the lowest-cost routing automatically.** This project recommends a route and never
   concludes one, and route comparison never ranks.
2. **aPriori grades DFM issues Critical, Major or Minor, and administrators tune the thresholds.** This
   project's DFM flags carry no severity and no confidence. Each flag states the rule, the measured value,
   the threshold and its source.

## Recommended next iterations, smallest first

All four items below were built in Iteration 5 (see `PROJECT_STATUS.md`). The next smallest open
rows are tooling amortisation over a lifetime quantity (S–M), a cost-driver table read from the
stored result (S), cavity count on a stage (S–M), and weld prep/grind from weld length (S–M).

1. **Casting/forging yield on per-kg stages (S).** One editable, labelled field. It closes a real
   under-costing gap: casting is charged on finished mass with no gating or loss. Planning for this is
   under way.
2. **Matrix preview (S).** Rate sets × quantities through `price_cad_part`, storing nothing.
3. **Supplier-quote variance (S).** A source flag on `cost_actuals`.
4. **Weld handling parameters (S).** Repositioning, clamping and programming time in the time profile.

## Sources

The research agents saw only search summaries of these pages, not the pages themselves.

- Regional Data Libraries datasheet (Apr 2026): https://www.apriori.com/wp-content/uploads/2026/04/aPriori_Regional_Data_Libraries_041326_v10.pdf
- Matrix costing (2021 R1): https://docs.apriori.com/Collateral/Documents/English-US/online_help/2021_R1/APRIORI_USER_GUIDE/AP_AUG127Using_Matrix_Costing.html
- Design guidance severity: https://docs.apriori.com/en/Design/apd/ug/review-design-guidance/
- Material cost and utilisation (2019 R2): https://www.apriori.com/Collateral/Documents/English-US/online_help/2019_R2/COST_MODEL_GUIDE/AP_CMG0129Material_Cost_and_Utilization.html
- MIG welding options (2019 R2): https://docs.apriori.com/Collateral/Documents/English-US/online_help/2019_R2/COST_MODEL_GUIDE/AP_CMG0072MIG_Welding_GCD-level_Options.html
- Assembly enhancements (22.1): https://docs.apriori.com/en/Pro/current/app/rn/22-1-x-x/22-1-0-0/assembly-cm-enhancements.htm
- Setup-count limit (24.1.3): https://docs.apriori.com/en/Pro/current/app/rn/24-1-x-x/24-1-3-0/pg-machining.htm
- Injection-moulding times (2021 R1): https://docs.apriori.com/Collateral/Documents/English-US/online_help/2021_R1/COST_MODEL_GUIDE/AP_CMG1050Times.html
- Sand-casting melting formulas (2019 R2): https://www.apriori.com/Collateral/Documents/English-US/online_help/2019_R2/COST_MODEL_GUIDE/AP_CMG0226Melting_Formulas_for_Sand_Casting.html
- Forging (25.1.1): https://docs.apriori.com/en/Pro/current/app/rn/25-1-x-x/25-1-1-0/pg-forging.htm
- Sustainability Insights FAQ: https://www.apriori.com/wp-content/uploads/2026/01/aPriori-FAQs-Sustainability-Insights.pdf
