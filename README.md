# CAD Costing Agent

A proof-of-concept project to estimate the zero-base manufacturing cost of mechanical parts from CAD models, eventually integrating with Creo and Windchill.

This is being built incrementally. So far it has a project foundation; deterministic costing for material, manufacturing operations, and additional components (overhead, scrap allowance, tooling) combined into a part-level estimate; CAD geometry extraction for **STL, STEP and IGES** (volume/surface area/bounding box); and a CAD-to-mass-to-cost pipeline connecting geometry to those costing calculations (`/cad/mass`, `/cost/cad-part`) alongside the manual-mass `/cost/part` endpoint. CAD ingestion goes through one **unified import layer** with an adapter per format and an explicit capability report, so **IGES** is now read alongside STEP and STL and native Creo formats are refused with an explanation rather than accepted and quietly ignored. Monetary output is denominated in a currency (**INR** by default) and presented at 2 decimal places, with a per-unit cost alongside the batch total. Costing rates are configured entirely from the browser — a **Costing Setup** screen publishes immutable, versioned costing profiles — and the estimate page renders the uploaded part in an interactive **3D viewer** while costing it against the profile version you choose. A STEP or solid-mode IGES model can also be analysed into a deterministic, explainable list of **recognized features** (holes, pockets, slots, cylinders, planes, fillets, chamfers); this is geometry only and does not affect the cost. A STEP **assembly** is decomposed into its component hierarchy — definitions, occurrences, quantities and placements — with each component measured, analysed and costed in its own right rather than the whole thing being treated as one lump of material. Recognized features can be turned into **suggested operations** with deterministic **machining-time estimates**, committed by a person into a setup-aware **manufacturing plan** that the existing costing consumes, and any two estimates can be compared with an exact **cost-driver decomposition** explaining what moved. There are still no AI agents or learned models anywhere in the calculation path, and no Creo/Windchill integration — see `PROJECT_STATUS.md` for current scope, including an explicit list of what has *not* been built, and `ARCHITECTURE.md` for the technical direction.

## Supported CAD formats

Accepting a file is not the same as supporting it, so every format is described
by what it can actually do. The table below is not written by hand: it is
`GET /cad/formats`, which is also what the upload panel renders and what the
endpoints refuse on, so there is one statement of what works and everything
reads it.

| Format | Extensions | Reader | Geometry | Viewer | Features | Costing |
|---|---|---|---|---|---|---|
| STEP | `.step`, `.stp` | `cadquery-ocp` (OpenCASCADE) | yes | yes | yes | yes |
| STL | `.stl` | `trimesh` | yes | yes | **no** | limited |
| IGES | `.iges`, `.igs` | `cadquery-ocp` (OpenCASCADE IGES reader) | yes | yes | limited | limited |
| Creo part | `.prt` | — | no | no | no | no |
| Creo assembly | `.asm` | — | no | no | no | no |
| Creo drawing | `.drw` | — | no | no | no | no |

Where a capability is anything less than full, the reason is part of the
answer:

- **STL** carries no length unit anywhere in the file, so its mass and cost are
  only right if the part was exported in millimetres, and nothing here can
  check that. Feature recognition is refused outright rather than guessed at: in
  a triangle mesh a hole is indistinguishable from a many-sided prism.
- **IGES** usually stores a part as loose trimmed surfaces rather than as a
  solid. Those surfaces are sewn and *tested for closure* before any volume is
  reported; when they close, the volume is integrated over the exact surfaces
  and is identical to STEP's. Feature recognition is the part that genuinely
  degrades — see below.
- **Creo native formats** are recognized so that they can be refused with an
  explanation, not to imply support. The formats are proprietary and
  undocumented; the OpenCASCADE build in use ships readers for STEP, IGES, STL,
  VRML and glTF and nothing else, and no open-source Python parser reads
  `.prt`/`.asm`/`.drw` reliably. Reading them needs Creo itself or a licensed
  translator. A `.prt` is not even unique to Creo — Siemens NX and Solid Edge
  use the same extension for their own unrelated formats. Export STEP or IGES
  from Creo instead.

`POST /cad/import` returns the whole picture for one uploaded file — format,
what it actually contains, where its units came from, its component structure,
its header metadata, and a capability report per capability. Pass
`?with_geometry=false` and it identifies the file without parsing it, which
takes about 1.3 seconds on a 198 MB export.

### IGES: what the two kinds of export can and cannot do

IGES is not STEP with a different extension, and the difference is worth
knowing before you export.

|  | IGES written as a **solid** (entity 186) | IGES written as **surfaces** (entity 144) |
|---|---|---|
| Volume, area, bounding box | exact, identical to STEP | exact, identical to STEP — after sewing |
| Mass and cost | identical to STEP | identical to STEP |
| Holes, fillets, chamfers | recognized, identical to STEP | **not recognized** |
| Viewer | yes | yes |

Measured on `model1_1.stp`, a real 43-face NX part, exported to IGES both ways:

```text
                        volume (mm³)   features recognized
STEP (baseline)         220621.0784    39   6 holes, 11 fillets, 22 planes
IGES solid mode         220621.0784    39   6 holes, 11 fillets, 22 planes
IGES surface mode       220621.0784    22   22 planes — no holes, no fillets
```

The volume is the same to six decimal places either way. The features are not,
and the reason is specific: a surface-mode export rewrites every cylinder as a
generic **surface of revolution**. The geometry is unchanged, but a cylinder's
*identity* is gone, and the hole and fillet recognizers match on exactly that.
Sewing the surfaces back into a solid restores the topology and is what makes
the volume trustworthy — it cannot make a spline a cylinder again.

So a surface-mode IGES file is fully costable and its holes are simply missing
from the feature list. That is reported prominently rather than left looking
like a part with no holes in it, and it is decided from the file's Directory
section before anything is parsed, so the upload panel can say which kind of
file you have straight away.

### A volume is never reported for surfaces that do not enclose one

`BRepGProp.VolumeProperties` returns a number for loose faces just as readily as
for a solid, because it integrates over whatever faces it is given. Measured on
three faces of a 40×30×20 box it returns 10666.67 mm³, which is the volume of
nothing, and nothing about the number reveals that. So closure is established
first — the file brought a solid, or the sewn shell passes
`BRep_Tool.IsClosed` — and a file that fails the test answers exactly as a
faceted STEP that does not close does:

```text
geometry_status : incomplete_requires_input
volume          : 0
surface_area    : 2000.0        (real, and still reported)
reason          : surfaces_do_not_enclose_a_solid
```

You then supply `total_volume` or `missing_volume` yourself, and it is recorded
as your assumption rather than a measurement.

### A unit is never assumed

Every format states where its unit came from — `file_declared`,
`not_carried_by_format`, or `undeclared` — and this is not cosmetic. Measured:
handed a well-formed IGES file whose unit declaration has been removed,
OpenCASCADE reads it **as inches, silently**, turning a 40 mm box into a
1016 mm one with no error and no warning. That is a 25.4× error in the mass and
therefore in the price. The unit flag is therefore read from the file's own
Global section before the reader is allowed near it, and a file that declares no
unit is refused with that explanation instead of measured.

## Requirements

- Python 3.11+

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -e ".[dev]"
```

## Run the app

```bash
uvicorn cad_costing_agent.main:app --reload
```

Then open **<http://127.0.0.1:8000/>** in a browser. That is the costing UI: drop in a CAD file,
fill in the part details, pick a rate set, and click Calculate.

The API is on the same server (`/health`, `/rates`, `/cost/cad-part`, ...), with interactive docs
at `/docs`.

## The UI

A single page served by the same FastAPI app at `/ui/` (`/` redirects there). It is plain
HTML/CSS/JavaScript with **no build step and no separate server** — nothing to install beyond the
Python dependencies.

The layout is the 3D model on the left, the costing inputs on the right, and the result across the
full width underneath.

The workflow is numbered on the page:

```text
1. CAD file
2. Part / material
3. Rate set                   money — what an hour costs
4. Time profile               duration — how long the work takes
5. Features                   (left column, beside the model)
6. Manufacturing plan         (right column: what actually gets costed)
7. Sequence
8. Additional costs
9. Calculate
```

Steps 5–7 are **optional and skippable**. With no suggestions at all the app behaves exactly as it
did before they existed: an empty plan you fill in by hand.

Steps 3 and 4 are deliberately two things. A rate set decides what an hour costs; a time profile
decides how long the work takes. Shops revise them on different schedules, and an estimate has to
be able to say which version of *each* produced it.

- **Upload** accepts `.step`, `.stp` and `.stl` by drag-and-drop onto the viewer, or a file picker.
  The model is read immediately on selection and both drawn and described (volume, surface area,
  bounding box, declared units), so an attached file is visibly distinct from a processed one.
- **3D viewer**: drag to rotate, shift-drag or right-drag to pan, scroll to zoom, pinch on touch.
  **Fit** frames the model at the current angle; **Reset view** returns to the default isometric.
  The arrow keys orbit, <kbd>Ctrl</kbd>+arrows pan, `+`/`-` zoom, and <kbd>F</kbd>/<kbd>R</kbd> are
  shortcuts for the two buttons. Every figure beside the viewer comes from the backend response —
  the browser measures nothing, and the viewer takes no part in costing.
- **Units**: STL has no unit metadata, so you choose one. STEP declares its own and the reader
  normalises to millimetres, so the control is fixed at `mm` with an explanation rather than
  letting you pick something the backend would reject.
- **Rate set**: the version selector defaults to *Latest* but any version can be pinned. The
  currency and an **illustrative-rates** badge are always visible, and the material and process
  dropdowns are populated from the selected rate set, so you cannot pick something it does not
  price.
- **Manufacturing plan** (step 6): one table grouped by setup, and what actually gets costed.
  Each setup band carries its name, its setup time, whether that time is a system estimate or your
  value, and whether the grouping is the system's or yours. Add a setup, rename it, move an
  operation between setups, reorder setups or operations, remove an empty setup. Selecting a setup
  points at the features its operations touch. Each row shows the
  sequence number, the process, the CAD feature behind it, setup and cycle time, where the time
  came from, and how confident that time is. Clicking a row shows why the operation was proposed
  *and* how its time was worked out. Rates come from the rate set — the UI never exposes
  machine/labour rates as editable fields when one is selected — and operation quantity is derived
  from the part quantity, which the API requires to match.
- **Times are proposals until you say otherwise.** An estimated time appears as a *placeholder*,
  never as the field's value, and the row reads **Needs input** until you press **Use estimate**
  or type your own — at which point it reads **System estimate** or **User value**. A row with
  neither is listed as not costed and is left out of the estimate rather than defaulted. Whatever
  you change, what the system suggested is kept beside it (`milling  was turning`), because
  discarding the original would discard the correction.
- **Time profile** (step 4): pick the published machining assumptions the estimated times are
  worked out from, and pin a version if you need an old estimate reproduced. Badges show the
  version, whether the parameters are real or illustrative, and that the profile carries no
  currency. Publish and version profiles in the **Time setup** tab.
- **Sequence** (step 7): move operations up and down; the list renumbers itself. The order is
  recorded on the estimate but **changes no cost** — there is no sequence-dependent time, no setup
  consolidation and no routing optimisation. No precedence is enforced either.
- **Additional costs**: leaving these blank **omits** them from the request so the rate set
  supplies them. The UI never sends a zero, which would silently override the rate set.
- **Results** show the total and per-unit cost prominently in ₹, then the full breakdown, the exact
  rate set and version used, and a provenance note. Illustrative rates raise an unmissable warning.
- **History** lists past estimates, opens any one of them, and compares two. Each row names
  the costing profile and version that produced it.

## Feature recognition

Upload a `.step`/`.stp` file and press **Analyse features** to get a structured list of what the
model actually contains:

```text
4 × hole (through) Ø6.5 mm depth 10 mm — confidence high
1 × hole (through) Ø14 mm depth 22 mm  — confidence high
1 × cylinder (shaft) Ø28 mm length 12 mm — confidence high
```

Every feature reports its dimensions, its axis, the CAD **face numbers** it came from, a
confidence, and the geometric conditions that identified it. Clicking a feature in the UI shows
that evidence and **marks the feature on the 3D model** — a dot at its location and a line along
its axis — so no claim has to be taken on trust and the row and the geometry are visibly the same
thing. The marker is a pointer, not a measurement: it is placed at the coordinates the backend
reported, and nothing is measured from the browser's geometry.

## Operation suggestions

Press **Suggest operations** to turn those features into a reviewable process plan:

```text
Feature              Suggested operation   Confidence   Source   User action
H-001 Ø16 through    drilling              high         B-Rep    Accept / edit / Remove
H-002 Ø30 through    drilling              high         B-Rep    Accept / edit / Remove
CYL-001 Ø60 shaft    turning               high         B-Rep    Accept / edit / Remove
PL-003 136 × 120     milling               medium       B-Rep    Accept / edit / Remove
```

The system **suggests; it does not decide**. Every row traces back to one recognized feature and
says why, so the question "why did you suggest this operation?" always has an answer. Features the
geometry cannot support an operation for are listed under **Not suggested** with the reason, rather
than being dropped or guessed at. See
[`POST /cad/operations/suggest`](#post-cadoperationssuggest) for the rule table and the response
shape.

Nothing here is a cost. A suggestion carries an operation's identity and its feature link — the
time comes next, and money only after that.

## Manufacturing family and route

The same **Suggest operations** request also returns a `process_classification`: the
manufacturing family (machining, sheet metal, casting, forging, fabrication/welding, additive,
moulding, purchased, or unknown) and the route of stages the evidence points to, shown in the
*Manufacturing process* card at the top of the Manufacturing section.

It is a **recommendation for review, never a finding.** Shape does not determine process — a
2 mm bracket may be laser-cut and bent or milled from plate — so:

- confidence is never above `medium`, and is `low` for anything but exact B-Rep evidence, for
  several solids, or when some faces were not recognized;
- every recommendation lists the signals it read (features cut into the solid, rotational form,
  constant wall thickness, one solid, every face recognized), the families it is equally
  consistent with, and what information would settle it;
- casting, forging, moulding, welding, additive and purchased are **never proposed** — nothing
  here detects evidence for them — but an engineer can choose them;
- thin evidence (only flat faces and blends, or no features) is an explicit `unknown`.

The engineer can **confirm** it, choose **another route**, or **mark it unknown**; the
recommendation is kept unchanged beside the answer and both are stored on the estimate.

Once a route is decided, the **manufacturing plan is arranged by it**. A strip above the
operations table lists the route's stages in order — each machining stage with how many
operations it holds (or "no operations yet — add one with + Add operation"), every other stage
as "priced from stage rates". Operations whose process the route does not include move to
**Not in this route**: kept exactly as they are, but not costed. Switching the route back,
withdrawing the decision, or changing a row's process to one the route includes brings them
back. Until someone decides, the plan is exactly as it was.

**Milling, drilling and turning** stages are priced by the hour from the plan's times. Every other
stage — sheet cutting, bending, casting, forging, welding, additive, moulding, finishing, purchase
— is priced from **stage rates** in the selected rate set, once the route is decided:

- A stage rate is published like any other rate (the **Rate sets** editor, *Processes* step, or
  `stage_rates` on `POST /rates`): a unit rate **per kg** or **per piece**, and/or a one-off
  **batch charge** such as a pattern or a die, with a note on where it came from. Keys are stage
  ids; a machining or unknown stage id is refused (422). No seeded rate set has any.
- A **per-kg** stage is charged on the mass entered for it, per part — on the route card, in the
  route comparison, or as `stage_inputs` on `/cost/cad-part`, e.g. `{"casting": {"mass_kg": 2.5}}`
  — or, when none is entered, on the **finished part's mass from the CAD model**, shown as "from
  CAD" and editable. For a casting or forging machined afterwards the real blank is heavier, and
  the estimate says so. An entered blank lighter than the part is refused.
- `total_cost = unit_rate × (mass_kg if per kg) × quantity + batch_charge`, per stage. Stage costs
  are direct cost, so overhead and scrap apply to them.
- A route that **starts with casting, forging, moulding, an additive build or a purchase** takes
  its material from that stage's rate: the material line is ₹0.00 and says so.
- If any stage has no rate, or a per-kg stage has no mass, `/cost/cad-part` refuses with
  `422 route_stages_not_priceable` and a reason per stage, and stores nothing — the machined
  stages are never priced as the whole.

Measured live on `model1_1.stp`, 10 off, *Casting followed by CNC machining*, an illustrative
casting rate of ₹120/kg + ₹15,000 per batch and a 2.5 kg blank: casting ₹18,000.00
(₹120 × 2.5 × 10 + ₹15,000), material ₹0.00 "included in the stage rate". A route marked unknown,
or an *unreviewed* recommendation that would need stage rates, is still refused with
`422 process_route_not_costable`. A cost request that sends no classification behaves exactly as
before.

| Endpoint | Purpose |
|---|---|
| `GET /process-routes` | the families, the 19 choosable routes, and which stages are machining (hourly) and which are priced from stage rates |
| `POST /cad/process/decide` | apply `accepted`, `edited` (with a `route_id`; `unknown` marks it unknown) or `pending` to a classification; persists nothing |
| `POST /cost/cad-part` `process_classification` | optional JSON form field; stored in the request snapshot with any plan-vs-route differences |

## Processing-time estimation

**A cast or forged blank leaves its blends unmachined.** When the route you decide on starts
by casting or forging and then machines (*Casting followed by CNC machining*, *Forging followed
by CNC machining*), the plan moves the blank's fillets and chamfers to an **"As cast — not
machined"** group: kept, but not costed. Faces, holes and pockets stay machined by default,
because the model cannot tell a machined face from an as-cast one — use *As cast* on any row to
leave it unmachined, or *Machine it* to bring a blend back.

**Turned parts are costed from the bar they are cut from.** For a part whose cylinders share
one axis and whose flat faces are all square to it, the app proposes a round bar — largest Ø +
5 mm rounded up to a 5 mm size, length + 2 mm facing at each end + 3 mm parting (illustrative)
— in a *Raw material* card. Until you confirm it, or enter the bar you actually use, material is
costed from the finished part as before; once confirmed, from the bar, with the part's own mass
still shown beside it. Turning times on such a part are worked out from the bar down to each
diameter, and its end faces and shoulders are suggested as facing on the lathe, not milling.

**Large holes are drilled smallest first.** A hole larger than 13 mm is timed as a pilot
(Ø6), then drills each at most 2.5× the previous, then its own size — Ø20 is drilled
6 → 15 → 20. Each drilling row has a dropdown offering a single drill, pilot + drill and the
step sequence, each with its own estimated time; the suggested one is marked, and whichever
you pick is what gets costed once you accept its estimate. The three numbers are illustrative
time-profile settings (*Time profiles → Drilling*), assembled from common practice — no ISO or
DIN standard prescribes a pilot sequence — so replace them with your shop's.

Each suggested operation gets a deterministic, parameter-based time estimate. A time is a much
stronger claim than an operation — "this hole is drilled" is a fact about geometry, "it takes 1.8
minutes" is a claim about a machine, a tool, a material and a shop — so every estimate is made
weak, visible and overridable:

```text
Estimated cycle: 0.23 min   (setup 5 min)

Method:      drilling_time_baseline_v1
Evidence:    Ø16 mm × 12 mm deep through hole
             spindle speed 497 rpm = 1000 × 25 m/min / (π × 16 mm)
             feed rate 74.6 mm/min = 0.15 mm/rev × 497 rpm
             drill travel 17 mm = 12 mm depth + 2 mm approach + 3 mm breakthrough
Assumptions: cutting speed 25 m/min · feed 0.15 mm/rev · approach 2 mm ·
             breakthrough 3 mm (through holes only) · spindle 50–6000 rpm ·
             setup 5 min per operation line
Confidence:  medium  (never higher — see below)
```

**The formulas.** Textbook relationships, not a machining database. Spindle speed is shared:
`rpm = clamp(1000·Vc / (π·D), min, max)`.

| Operation | Cycle time |
|---|---|
| Drilling | `travel / (f_rev · rpm)`, travel = depth + approach + breakthrough (through holes only) |
| Milling — facing | `(area/stepover + approach) / (f_tooth · teeth · rpm)`, one pass |
| Milling — pocket/slot | same, × `ceil(depth / axial_depth_of_cut)` passes |
| Milling — fillet/chamfer | `(length + approach) / feed_rate` — a blend is followed, not swept |
| Turning | `passes · (length + approach) / (f_rev · rpm)`, passes = `ceil(stock/doc)` |

**No time is ever invented.** Where a formula's inputs are missing, where the operation has no
formula, or where the file's units were never established, the result is `needs_user_input` with
the missing pieces named — and `setup_time_minutes` / `cycle_time_minutes` come back **null**, not
zero, so nothing downstream can add a missing time into a total. A faceted export reports its units
as "as written in the file", so times for it are refused rather than guessed at a possible
factor-of-1000 error.

**Confidence is capped at `medium`, always.** A generic parameter baseline describes a plausible
shop, not yours. It drops to `low` when the operation suggestion behind it was `low`, and can never
rise above the suggestion.

**Time assumptions are separate from rate sets.** Assumptions decide duration; rates decide money.
Sharing one model would let editing a feed rate silently change a price.

## Time profiles

The parameters above are not hard-coded: they live in a **time profile**, which is versioned and
immutable in exactly the way a rate set is.

```text
Time profile
Pune Shop Machining   pune-shop-time   v1.0.0

Drilling   cutting speed 25 m/min · feed 0.15 mm/rev · approach 2 mm ·
           breakthrough 3 mm · setup 5 min
Milling    cutting speed 80 m/min · feed 0.08 mm/tooth · 4 teeth ·
           Ø12 mm cutter · stepover 0.6 × Ø · axial DOC 2 mm · approach 5 mm · setup 10 min
Turning    cutting speed 120 m/min · feed 0.2 mm/rev · DOC 1.5 mm ·
           stock allowance 2 mm · approach 3 mm · setup 12 min
Spindle    50–6000 rpm
```

All 21 parameters are editable in the **Time setup** tab — every value the estimator reads, so
there are no hidden constants. A profile has **no currency**: nothing in it is money.

**A published version is never edited.** Changing an assumption means publishing a new version:

```text
v1.0.0  feed 0.15 mm/rev   →   Ø16 × 12 through hole = 0.23 min
v2.0.0  feed 0.05 mm/rev   →   the same hole         = 0.68 min
```

Estimates already made under v1.0.0 keep it, and keep the time it produced. Re-posting a published
version returns `409`.

**Provenance.** Every estimated time records the profile id, version and name that produced it,
*and* the parameter values that actually entered the calculation — so a historical operation can
answer "why 2.37 minutes?" on its own:

```jsonc
"time_profile_id": "pune-shop-time", "time_profile_version": "1.0.0",
"time_profile_name": "Pune Shop Machining",
"parameters": {
  "drilling_cutting_speed_m_per_min": 25.0, "drilling_feed_mm_per_rev": 0.15,
  "drilling_approach_mm": 2.0, "drilling_breakthrough_mm": 3.0,
  "resolved_spindle_rpm": 497.36, "resolved_feed_rate_mm_per_min": 74.6
}
```

The full profile is snapshotted once on the estimate (`time_profile_snapshot`), so publishing a
later version can never change what a stored estimate says it assumed.

**Validation.** The backend is the source of truth. Out-of-range parameters are refused with `422`
— a feed of 800 mm/rev is a slipped decimal point, not a machining choice — and a spindle range
where the minimum is not below the maximum is refused too. Values that are *legal but unusual* are
reported as advisory notes and still publish: the parameters are the user's to choose. The one
place the model still clamps rather than refuses is spindle speed, because that clamp is part of
the deterministic model — a machine really does have a top and bottom speed.

### Time-profile endpoints

```text
GET  /time-profiles                        list every profile, every version
GET  /time-profiles/{id}[?version=]        one profile; omit version for the latest
POST /time-profiles/validate               would this publish? (stores nothing)
POST /time-profiles                        publish a new profile
POST /time-profiles/{id}/versions          publish a version of an existing profile
```

`POST /operations/estimate-time` takes `?time_profile_id=…&time_profile_version=…`. Explicit
`assumptions` in the body still win — that lets a caller try parameters they have not published —
and the result then records *no* profile, because none produced it.

### `POST /operations/estimate-time`

Deliberately takes **no CAD file** — recognition has already run, so it accepts the normalized
operations `/cad/operations/suggest` returns. Re-uploading the model would mean parsing a 200 MB
export again for an answer that costs microseconds; hence the path outside `/cad`.

```bash
curl -X POST http://127.0.0.1:8000/operations/estimate-time   -H 'Content-Type: application/json' -d '{
    "units": "mm",
    "operations": [
      {"operation_id": "OP-001", "operation_type": "drilling", "feature_id": "H-001",
       "feature_type": "hole", "feature_subtype": "through",
       "dimensions": {"diameter_mm": 16.0, "depth_mm": 12.0}, "confidence": "high"}
    ]
  }'
```

Optional `assumptions` overrides any parameter. The response echoes the assumptions actually used,
counts `estimated` versus `needs_input`, and warns that nothing has been adopted. Deterministic:
the same operations under the same assumptions always give the same times. Persists nothing.

## The manufacturing plan

The plan is the editable record of what will actually be made, and the only thing that reaches
costing. It is organised into **setups**:

```text
SETUP 1 — Main fixture          setup time 10 min   (charged once)
  1  Facing     F03    0.80 min
  2  Drilling   H01    0.23 min
  3  Drilling   H02    0.23 min
  4  Drilling   H03    0.23 min

SETUP 2 — Side fixture          setup time 8 min    (charged once)
  1  Drilling   H04    0.75 min
```

**A setup is one fixturing of the part, and its setup time is charged once** — however many
operations it holds. Cycle time is charged for every operation. Before this existed, six drilled
holes were six operations *and* six setups, which multiplied the setup cost by six and produced a
number no shop would recognise. On `model1_1.stp` the difference is **280 minutes of setup versus
10**.

The grouping is not inferred from process type — a shared process is not evidence of shared
fixturing, and geometry cannot prove either way. So the system groups conservatively (everything in
one setup), marks it `system_assumed` at **low confidence**, states its assumptions, and leaves the
engineer with final authority:

```text
source:     system
reason:     nothing in the CAD proves a second fixturing is needed
confidence: low
assumption: the part can be held in one fixture in one orientation for these operations
```

The setup's *estimated* time is the **largest** per-process baseline among its operations, never
their sum — one fixturing takes as long as its most demanding process needs. Like every other
estimate here it is a proposal until someone accepts or replaces it; an uncommitted setup charges
nothing and says so.

There are two levels of order, both editable and both persisted: the setups run in their own
sequence, and operations run in theirs *within* a setup. Neither changes a number — but setup
**membership** does, and the plan shows the effect live:

```text
2 setups   Setup time 20 min   Cycle time 149.1 min   Operation cost ₹5,763 = ₹600 setup + ₹5,163 run
Since your last change: +1 setup · +10 min setup time · +300 INR
```

That preview comes from `POST /operations/plan-cost-preview`, which prices the plan through the
same unmodified calculators an estimate uses, so a preview can never quietly differ from the
estimate that follows it. The browser does no costing arithmetic.

Every operation carries two parallel sets of values that never mix:

```text
system_suggested_operation / _setup_time / _cycle_time   what the system proposed
system_time_method / _confidence / _status               and how it got there
system_setup_id                                          which setup it put the operation in
user_operation_type / user_setup_time / user_cycle_time  what the engineer chose
user_setup_id                                            and where they moved it
user_action, sequence_position, origin                   and what they did
```

**The system's proposal is never overwritten.** An edit writes to the `user_*` side, so the pair
*(predicted, chosen)* survives — which is the only reason a later learning step will have anything
to learn from.

**What gets costed** resolves in one direction only: the engineer's typed value, else the system's
value *if they accepted it*, else **nothing**. Generating an estimate does not commit it, and
changing an operation's process invalidates a time estimated for the old one. An operation with no
committed time is omitted from the cost — never zeroed, never averaged, never carried over — and
the plan says which ones and why.

Send the plan to `/cost/cad-part` as a JSON `manufacturing_plan` form field. It is mutually
exclusive with `operation_requirements`: accepting both would leave it ambiguous which produced the
number.

### Feedback: what was predicted versus what was chosen

Every estimate persists a `plan_snapshot` holding the plan and one `OperationFeedback` row per
operation:

```jsonc
{
  "operation_id": "OP-005", "feature_id": "CYL-001", "sequence_position": 5,
  "suggested_operation": "turning", "suggested_setup_time": 12.0, "suggested_cycle_time": 0.44,
  "final_operation": "milling",     "final_setup_time": 9.0,      "final_cycle_time": 5.5,
  "operation_changed": true, "setup_time_changed": true, "cycle_time_changed": true,
  "suggestion_confidence": "high", "time_confidence": "medium",
  // Which assumptions produced the prediction. Without this a correction is
  // uninterpretable: "the system said 20 and the engineer said 32" means
  // nothing unless you know which feeds and speeds the 20 came from.
  "time_profile_id": "pune-shop-time", "time_profile_version": "1.0.0",
  "time_profile_name": "Pune Shop Machining",
  "time_parameters": { "turning_feed_mm_per_rev": 0.2, "...": "..." },
  // And how it was grouped into a fixturing, so "the system put H1-H4 together
  // and the engineer moved H4 out" is a correction a later step can read.
  "system_setup_id": "SETUP-001", "final_setup_id": "SETUP-002", "setup_changed": true,
  "system_setup_grouping": "user_defined", "setup_operation_count": 1,
  "system_setup_time_minutes": 10.0, "final_setup_time_minutes": 8.0,
  "setup_time_corrected": true,
  "time_source": "user", "user_action": "edited", "costed": true,
  "timestamp": "2026-09-03T..."
}
```

**Nothing is trained and nothing adapts.** No model, no fitting, no weights; a recorded correction
never changes a later prediction. The estimator is a fixed set of formulas and stays that way. The
data exists so a future step can answer one question — *what did the system predict, and what did
the engineer actually choose?* — which is only answerable if the corrections were recorded while
they happened.

Removed suggestions are recorded too: "the system suggested this and the engineer took it out" is a
correction like any other, and dropping those rows would leave a dataset that only ever saw its own
successes. A hand-added operation is **not** recorded as a correction — there was no prediction to
correct.

A later edit creates a **new** estimate with its own snapshot; an existing estimate is never
rewritten.

### Geometry and mass from a faceted export

**Feature recognition and mass extraction are independent.** A faceted model may give limited
features and a completely valid mass, and that is a perfectly good outcome — costing never depends
on recognition.

| | Analytic B-Rep | Faceted |
|---|---|---|
| Volume from | `BRepGProp` mass properties over exact faces | signed volume of the polygon mesh |
| `geometry_source` | `brep` | `mesh` |
| `volume_method` | `brep_mass_properties` | `mesh_signed_volume` |
| Reported when | the file loads | **every** connected component is watertight |
| `volume_confidence` | `high` | `high`, or `repaired` if facets had to be restored |

A faceted volume is exact for the polygons it was given, but the polygons are themselves a
tessellation, so a curved part reads very slightly large — a bore approximated by flat strips
removes a little less material than the true cylinder. Measured on a real bracket: **220,606.97 mm³
faceted against 220,621.08 mm³ analytic, a difference of 0.006%**, giving the same cost.

#### `OPEN_SHELL` does not mean the geometry is open

This is the trap the real files set. Their 6,473 shells are all declared `OPEN_SHELL`, but that is
a statement about the STEP entity, not about the polygons. Welded together, those 6,473 "open
shells" turn out to be face *groups* forming only **100** connected components, 63 of them
watertight. Closure is therefore decided by examining the topology — never by reading the entity
name.

#### Repairing what is uniquely determined

One repair is performed, and only one, because it is the only case where nothing has to be chosen.
A triangulated surface missing a single facet leaves a boundary of **exactly three edges** over
three vertices that are already in the mesh, and exactly one triangle can fill it. Its orientation
is fixed by the three unmatched directed edges. **No tolerance is involved** — this is topology,
not fitting.

Three conditions must all hold, or nothing is filled:

1. the boundary chain is exactly three vertices, each joined to the other two;
2. each of the three edges is currently used by exactly one triangle, so filling cannot create
   non-manifold topology;
3. those three edges come from **three different triangles** — a lone triangle floating in space
   also has three free edges, and "filling" it would fold a copy back onto itself into a
   zero-volume shell that would then read as watertight.

Anything larger is refused. A four-edge hole already offers a choice of diagonal; a hundred-edge
hole would mean inventing a face. T-junctions are not repaired either. Two triangular holes that
touch at a corner form one five-vertex chain and are both left alone.

When a repair runs it is reported, never hidden: `repair_applied`, `repair_method`,
`repaired_facet_count`, `repair_tolerance_mm`, and `volume_confidence` becomes `repaired` rather
than `high`. The volume is exact for the reconstructed polygon mesh; the mesh itself remains an
approximation of the original CAD surfaces.

#### Supplying geometry the file does not contain

When the polygons do not enclose a solid, the answer is not a failure — it is a question. The API
returns `geometry_status: "incomplete_requires_input"` with a volume of **0** and an `incomplete`
report saying what is known, what is missing and how to resolve it:

```jsonc
{
  "geometry_status": "incomplete_requires_input",
  "volume": 0.0,
  "volume_confidence": "unavailable",
  "incomplete": {
    "component_count": 100, "closed_component_count": 68, "open_component_count": 32,
    "known_volume_mm3": 1981004.3,       // the watertight part ALONE — not the part's volume
    "known_polygon_share": 0.32,
    "restored_facet_count": 1346,
    "unresolved": [ { "index": 4, "polygon_count": 120792, "boundary_edge_count": 1486, "...": "..." } ],
    "resolution_methods": ["total_volume", "missing_volume", "upload_corrected_cad"],
    "explanation": "..."
  }
}
```

`/cad/mass` and `/cost/cad-part` answer `422` with `{"error": "needs_geometry_input", ...}` rather
than an opaque failure, so a caller knows to ask rather than to give up.

Two supported answers, both passed as form fields:

| `geometry_completion_method` | `geometry_completion_volume_mm3` | Volume used |
|---|---|---|
| `total_volume` | the whole part's volume | exactly what you supplied |
| `missing_volume` | what the unmeasured components hold | `known_volume_mm3` + what you supplied |

The third option is not an API call: **upload a corrected CAD file**. A solid B-Rep export needs no
assumption at all.

Nothing is inferred. There is no bounding-box volume, no convex hull, no guessed thickness, and the
watertight-components-only volume is never used unless you explicitly choose `missing_volume`,
which says to add to it.

**The assumption travels with the result.** `geometry_source` becomes `faceted_user_completed`,
`volume_method` records which answer was given, `volume_confidence` becomes `user_supplied`, and
the figure appears in `user_supplied_total_volume_mm3` or `user_supplied_missing_volume_mm3`. It is
persisted on the estimate, so a reader years later sees the original CAD state, the assumption and
the volume it produced. A later, different assumption creates a new estimate and never alters an
old one.

In the UI an amber **Incomplete CAD geometry** panel appears with the known volume and the three
choices; *Calculate estimate* stays disabled until an assumption is applied, and the result then
says *"Volume assumed, not measured."*

#### Volume is reported only when the topology supports it

Vertices are welded by position (a faceted exporter usually writes a separate point for every
corner of every facet, so without welding nothing touches anything). The welded triangles are split
into connected components, and a component counts as closed when every edge is shared by exactly
two triangles **and** the winding is consistent — flip one triangle and the mesh is still
watertight by edge count while the volume is silently wrong.

If any component is open, **no volume is reported at all**. Summing the closed ones and ignoring
the rest would quietly price a fraction of the model. There is no bounding-box volume, no convex
hull, no guessed thickness and no repair of open geometry; the refusal instead names the counts so
you can see exactly what failed.

#### Units

A faceted file's coordinates are taken verbatim, so its declared `LENGTH_UNIT` is read and
converted to millimetres. The real Creo exports are in **metres** — getting that wrong would scale
every mass by a thousand. A file whose unit cannot be determined is refused rather than assumed.

### What feature recognition can recover

A STEP file can describe a part in two very different ways, and which one you have decides what
can be recovered:

| | Analytic B-Rep | Faceted |
|---|---|---|
| What the file stores | exact surfaces — `CYLINDRICAL_SURFACE`, `ADVANCED_FACE` | flat polygons — `FACETED_BREP`, `POLY_LOOP` |
| A hole's diameter is | a number written in the file | measured by fitting a circle to the polygons |
| Recognized | holes, cylinders, pockets, slots, fillets, chamfers, planes | holes, cylinders (shafts), flat regions |
| `source_type` | `brep` | `faceted` |

The file is classified before anything is loaded, by counting entity keywords in the raw bytes.
That takes about two seconds on a 200 MB export, against roughly twenty for OpenCASCADE merely to
read the same file — so an unusable file is diagnosed quickly instead of slowly.

Both paths produce **the same feature objects**. A hole is a hole whichever representation it came
from; only `source_type`, the detection method and the evidence differ.

#### How a hole is recovered from polygons

Adjacent coplanar polygons are merged back into flat patches, and patches that meet at a shallow
angle are joined into a band — strips of a tessellated bore meet at a few degrees, while a bore
meets the face it breaks through at ninety. A circle is then fitted to the band's vertices.

Because a tessellator places its vertices *on* the true surface and joins them with chords, the
fitted radius is the real radius rather than the inscribed-polygon radius. How far the flat strips
stand inside that circle is what separates a tessellated circle from a designed polygon: below 6%
of the radius it is round (a circle cut into ten or more segments), above it is not (an octagon is
7.6%, a hexagon 13.4%). This is why a hexagonal hole is left unclassified instead of being
reported as a round one.

#### A known Creo export defect

Some Creo faceted exports write `FACETED_BREP` over an `OPEN_SHELL`. ISO 10303-42 requires a
`CLOSED_SHELL`, so OpenCASCADE rejects every solid and the file reads as completely empty — no
faces, no volume, no preview. This is detected and named, rather than being reported as a part
with no features. **Re-export as solid B-Rep geometry** if you want feature recognition, geometry
or costing from such a file.

### What it recognizes

| Type | What is detected | Reported |
|---|---|---|
| `hole` | complete internal cylinders, classified by their end conditions | diameter, depth, through/blind, counterbore, countersink angle |
| `cylinder` | complete cylindrical surfaces that are not holes | diameter, length, `shaft` (material inside) or `bore` |
| `pocket` | a planar floor enclosed by walls that rise from it | length, width, depth, floor area |
| `slot` | the same, when the floor is ≥ 3× longer than it is wide | length, width, depth |
| `fillet` | a partial cylindrical sweep tangent to its neighbours | radius, length, concave/convex |
| `chamfer` | a narrow planar bevel at equal oblique angles to two neighbours | width, length, angle |
| `plane` | every remaining flat face, completing the inventory | area, length, width, normal |

A face belongs to **at most one** feature — a bore is reported as a hole, not also as a cylinder —
so the counts describe the model once. Faces no rule explains are counted as
`unclassified_face_count` rather than quietly dropped.

### Confidence

Deterministic, not statistical. Nothing is learned or estimated:

- **high** — every condition the rule requires was checked and satisfied, and every reported
  dimension was measured from the geometry.
- **medium** — the feature matched, but a property was inferred rather than measured, or a
  secondary condition held only approximately. Chamfers are never reported above medium.
- **low** — a candidate exists but the classification remains ambiguous, such as a bore whose ends
  could not be read.

### Deliberate limits

- **STEP only.** An STL is a triangle mesh with no units and no structure, and is refused here
  rather than guessed at. STL remains fully supported for geometry, the 3D viewer and costing.
- **From a faceted export, only holes, shafts and flat regions are reconstructed.** Pockets,
  slots, fillets and chamfers are not: on polygons a fillet cannot be told from a chamfer or a
  coarsely tessellated curve, and a pocket floor cannot be told from any other flat region
  without the boundary structure a B-Rep has and a polygon soup does not.
- **A coarse polygon is never reported as a round hole.** An octagonal hole stays unclassified.
- **No manufacturing operations are inferred.** A hole is reported as a hole, never as a drilling
  operation. Turning features into operations is a later step.
- **No machine learning, no AI, no randomness.** The same file always produces the same list.
- **Nothing is persisted.** Features are computed on demand and never stored on an estimate.
- Creo `.prt`/`.asm` are still not readable; export STEP.

Known gaps are listed in `ARCHITECTURE.md` — most importantly, a slot cut clean **through** a part
has no floor and is not detected as a slot, and non-analytic (swept, freeform) faces are counted
as unclassified rather than guessed at.

## Costing Setup

The **Costing setup** tab is where the rates come from. Nothing about pricing needs the API or a
hand-written JSON file any more.

Profiles are listed as cards — name, currency, current version, how many materials and processes
it prices, and whether its rates are real or illustrative — with every published version listed
beneath, the newest marked **Current** and the rest **Superseded**. The seeded `poc-*` profiles are
plainly labelled *Illustrative rates*.

Creating or revising a profile runs through a five-step wizard:

1. **General** — profile name, version, currency (INR by default), description, and whether these
   are real rates or illustrative placeholders. The profile ID is derived from the name so there is
   no slug to invent.
2. **Materials** — name, density (kg/m³) and cost per kg, one row per material.
3. **Processes** — machine and labour rate per hour, one row per process. Any process name is
   accepted; the dropdown only suggests common ones.
4. **Additional costs** — overhead %, scrap % and tooling per batch, with the same meaning they
   have everywhere else in the system.
5. **Review & publish** — the whole configuration on one page, then the backend is asked whether it
   would publish, and only then is **Publish this version** enabled.

### Changing rates means publishing a new version

**A published `(profile, version)` can never be edited.** There is no edit or delete anywhere in
the UI or the API. To change a rate, use **Create new version**: it copies the previous
configuration so only what actually changed has to be touched, and the new version must have a
higher number. Every earlier version stays readable, and every estimate keeps pointing at the exact
version that produced it — so publishing new rates never moves an old quotation.

Drafts live only in the browser while the wizard is open. Nothing is stored until you publish, so
an abandoned edit leaves nothing behind.

### The profile is the source of material data

When a profile is selected, **both** the price per kg **and the density** come from it. A profile
that defines "EN8 steel" can cost a part in EN8 steel, and a profile that declares its own density
for a material is the density used to derive mass. Only an estimate that selects no profile falls
back to the small built-in material catalog.

All arithmetic happens in the backend. The page formats what the API returns and contains no
costing logic of its own.

> Money is displayed with the currency the estimate was made in — `₹` for INR, otherwise the code.
> Currencies are never converted.

## Run tests

```bash
pytest
```

## Persistence

Rate sets and completed estimates are stored in SQLite via SQLAlchemy 2.x.

> This is **POC-grade persistence**, not production-grade. There is no auth, no backup story, and no concurrency handling beyond SQLite's own. Schema changes *are* now managed — see [Migrations](#migrations).

**Configuration.** The database URL comes from `CAD_COSTING_DATABASE_URL`, defaulting to `sqlite:///./cad_costing_agent.db`:

```bash
export CAD_COSTING_DATABASE_URL="sqlite:///./my-estimates.db"
uvicorn cad_costing_agent.main:app
```

**Initialization and seeding** happen automatically on startup: the database is migrated to head, then the illustrative rate sets are inserted. Seeding is **idempotent** — it skips any `(rate_set_id, version)` already present, so restarting never duplicates or overwrites stored rates.

### Migrations

Schema changes are managed with **Alembic**, so a database can be upgraded in place rather than deleted and recreated.

Startup calls `run_migrations`, which distinguishes three cases so that adopting Alembic on an existing database cannot lose anything:

| Database state | What happens |
|---|---|
| Empty | `upgrade head` — creates the tables |
| Already under Alembic (`alembic_version` present) | `upgrade head` — applies any outstanding revisions |
| Pre-Alembic (tables exist, no `alembic_version`) | **`stamp head`** — records the version without running DDL |

That third case matters: a database from before Alembic was adopted already has exactly the schema revision `0001` describes. Running the migration against it would fail on the existing tables, and dropping-and-recreating would throw away stored rate sets and estimates. Stamping records the version and touches no data.

The CLI works too, and reads the same `CAD_COSTING_DATABASE_URL` — `alembic.ini` deliberately does **not** set `sqlalchemy.url`, so the CLI can never drift from what the app uses:

```bash
alembic current              # which revision this database is at
alembic history              # available revisions
alembic upgrade head         # apply outstanding migrations
alembic revision --autogenerate -m "add something"   # after changing db_models.py
```

Migration scripts live in `src/cad_costing_agent/migrations/` so they ship with the package and are found regardless of the working directory.

> `downgrade()` on the initial revision drops both tables and everything in them. It is never called automatically — only ever by hand, deliberately.

**Schema** — two tables:

| Table | Key | Notable columns |
|---|---|---|
| `rate_sets` | unique `(rate_set_id, version)` | `name`, `description`, `is_illustrative`, `currency`, `rates` (JSON: materials, operations, additional costs), `created_at` |
| `estimates` | autoincrement `id`; unique `estimate_id` (uuid4 hex) | `created_at`, `source`, part identity, `rate_set_id`/`rate_set_version`, `currency`, the four cost totals, and three JSON snapshots |

The rate detail is one JSON column rather than child tables: a rate set version is immutable and always read and written whole, so splitting it would add joins and partial-update risk for nothing. Identity stays in real indexed columns, since that is what gets queried.

**Audit snapshots.** Every stored estimate carries `request_snapshot` (what was asked for), `rate_set_snapshot` (the exact rates resolved), and `result_snapshot` (the full computed breakdown). That makes a historical estimate self-contained: publishing a newer rate set version never changes what an older estimate says.

**Sessions** are injected per request through FastAPI's dependency system rather than reached for globally, so tests swap the whole database out via `app.dependency_overrides`. Each test runs against its own freshly-seeded in-memory database, so estimates written by one test are invisible to the next.

## Money: currency and rounding

**Currency.** Every rate set carries a 3-letter code; the POC default is **INR**, and every seeded rate set — and every row that existed before this was added — is INR-denominated. An estimate is denominated in the currency of the rate set that produced it, and an estimate made without a rate set reports the default. **There is no currency conversion anywhere.**

**Rounding.** Computed cost amounts are presented and stored at **2 decimal places, half-up** (the money convention; Python's built-in `round` is half-to-even and would turn `0.125` into `0.12`).

Crucially, rounding is *presentation only*. A monetary field holds the exact value in memory and is rounded when serialized, so the costing formulas are untouched and no intermediate is ever rounded and then reused. `1367.0434666666667` is now reported as `1367.04`.

Two consequences worth stating plainly:

- Because each amount is rounded from its own exact value, **displayed components can differ from a displayed total by a penny**. Rounding components first and summing them would hide that, but the total would no longer be the rounded true total — the worse trade.
- **Rates are not rounded.** The policy covers computed cost *amounts*; input rates echoed back for explainability (cost per kg, machine and labor rates per hour) are reported as configured, since a legitimate rate like `0.125/kg` would be distorted by 2-decimal rounding.

**Per-unit cost.** Every part estimate reports `per_unit_cost` alongside the batch `total_part_cost`:

```
per_unit_cost = total_part_cost / part.quantity
```

The batch total already accounts for quantity, so dividing it back out is the only place quantity is undone — it is never applied a second time. Because setup and tooling are one-time batch costs, per-unit cost falls as quantity rises, which is the point of reporting it.

## Rate sets

Every rate used to be an independent per-request value, so two estimates could differ purely because of rates with nothing in either result recording which rates were used. A **rate set** gives that collection of inputs a stable identity and an explicit version, so an estimate can state exactly what produced it.

A rate set is identified by (`rate_set_id`, `version`) and covers material prices and densities, machine and labor rates per process type, and the overhead/scrap/tooling inputs. Resolving an id without a version selects the highest version; the exact version resolved is always echoed back in the estimate. Pin `rate_set_version` for full reproducibility.

> The catalog is **ILLUSTRATIVE POC VALUES ONLY** — placeholders chosen to exercise the calculations and keep the worked example stable. It is not real market or customer pricing.

Rate sets are stored in SQLite (see [Persistence](#persistence)) and seeded on startup, and **can be authored through the API** — you do not need to edit Python to enter real costing rates. An existing `(rate_set_id, version)` is immutable: there is no update path anywhere, so changed rates mean publishing a new version.

### `POST /rates` — author a rate set

Creates one new rate set version and returns it as stored. Responds `201` on success, `422` for validation failures, and **`409` if that `(rate_set_id, version)` already exists** — existing versions are immutable.

```bash
curl -X POST http://127.0.0.1:8000/rates -H "Content-Type: application/json" -d '{
  "rate_set_id": "acme-machine-shop",
  "name": "ACME Machine Shop",
  "version": "1.0.0",
  "is_illustrative": false,
  "materials": {"steel": {"name": "Steel", "cost_per_kg": 72.5, "density_kg_m3": 7850}},
  "operations": {"milling": {"machine_rate_per_hour": 1850.0, "labor_rate_per_hour": 420.0}},
  "additional_costs": {"overhead_rate_percent": 18.0, "scrap_rate_percent": 3.0, "tooling_cost_per_batch": 4500.0}
}'
```

Two defaults worth knowing:

- `currency` defaults to **INR**.
- `is_illustrative` defaults to **true**. Set it to `false` when the rates are real — the flag travels into every estimate made with the rate set, so a reader can tell a placeholder figure from a real one.

### `POST /rates/validate` — check before publishing

Validates a candidate rate set and reports whether it would publish, without storing anything and
without reserving an id. The setup screen's review step uses it so the "ready to publish" verdict
comes from the backend rather than the browser.

Malformed values never reach it — `RateSet` rejects those as a 422 first. What it adds is the part
that needs the stored catalog:

```jsonc
{
  "valid": true,
  "rate_set_id": "pune-machine-shop",
  "name": "Pune Machine Shop",
  "version": "2.0.0",
  "currency": "INR",
  "is_illustrative": false,
  "material_count": 3,
  "process_count": 3,
  "version_already_exists": false,
  "is_new_profile": false,
  "messages": []
}
```

`valid` is false when that version has already been published, because a published version is
immutable. `messages` also carries non-blocking warnings — an illustrative profile, or one that
prices no materials or no processes.

### `POST /rates/{rate_set_id}/versions` — add a version to an existing family

The same thing with the id taken from the path, so the body is the rate set minus `rate_set_id`. It shares one creation path with `POST /rates`, so validation and the immutability rule are identical.

```bash
curl -X POST http://127.0.0.1:8000/rates/acme-machine-shop/versions \
  -H "Content-Type: application/json" \
  -d '{"name": "ACME Machine Shop", "version": "2.0.0", "is_illustrative": false,
       "materials": {"steel": {"name": "Steel", "cost_per_kg": 81.0, "density_kg_m3": 7850}},
       "operations": {"milling": {"machine_rate_per_hour": 2100.0, "labor_rate_per_hour": 480.0}}}'
```

Publishing a new version never disturbs existing estimates: each estimate keeps the version it was made with and the rate snapshot taken at the time. There is no `PUT`, `PATCH`, or `DELETE` for rate sets.

> The rate sets seeded at startup (`poc-default`, `poc-machine-shop`) remain **ILLUSTRATIVE POC VALUES ONLY** — placeholders, not real market or customer pricing. Rate sets you author are yours; mark them `is_illustrative: false` when they hold real numbers.

### Rate precedence

**Explicit per-request values always win; the rate set only supplies what the request omits.**

| Input | With a rate set | Without a rate set |
|---|---|---|
| Material rate | From the rate set (400 if it doesn't price that material) | From the built-in material catalog, as before |
| `operations[]` | Carry their own rates — **never** overridden by the rate set | Unchanged |
| `operation_requirements[]` | Rates resolved from the rate set by `process_type` | Rejected — they need a rate set |
| `additional_costs` | Supplied → replaces the rate set's **wholesale**; omitted → the rate set's values | Supplied → used; omitted → zeros, as before |

Precedence keys off *structural presence*, never off a value, so a defaulted `0.0` can never silently override a rate set. Both operation lists may be used together: explicitly-rated operations are costed first, then rate-set-resolved ones.

### `GET /rates`

Lists every rate set in the catalog, every version included.

```json
[
  {"rate_set_id": "poc-default", "name": "POC Default", "version": "1.0.0", "description": "...", "is_illustrative": true},
  {"rate_set_id": "poc-machine-shop", "name": "POC Machine Shop", "version": "1.0.0", "description": "...", "is_illustrative": true},
  {"rate_set_id": "poc-machine-shop", "name": "POC Machine Shop", "version": "2.0.0", "description": "...", "is_illustrative": true}
]
```

### `GET /rates/{rate_set_id}`

Returns one full rate set. Omit `?version=` for the latest version of that id (compared numerically, so 10.0.0 outranks 2.0.0). An unknown id or version returns `404`.

```bash
curl "http://127.0.0.1:8000/rates/poc-machine-shop?version=1.0.0"
```

### `GET /estimates`

Browses estimate history, newest first. Returns concise rows **without** the audit snapshots — fetch a single estimate to see those.

| Query param | Meaning |
|---|---|
| `limit` | Page size, 1–100, default 20 |
| `offset` | Rows to skip, default 0 |
| `part_id` | Only estimates for this part |
| `rate_set_id` | Only estimates produced with this rate set |
| `created_after` / `created_before` | Inclusive UTC bounds |

Filters combine. `total` counts everything matching the filters, not just the page, so a client can page through the rest.

```bash
curl "http://127.0.0.1:8000/estimates?part_id=P100&limit=10&offset=0"
```

```json
{
  "total": 2, "limit": 10, "offset": 0,
  "items": [
    {
      "estimate_id": "121942aa87e342dba28a8a431f8cb938",
      "created_at": "2026-08-31T09:41:02.118431",
      "source": "cost/part",
      "part_id": "P200", "part_name": "Flange",
      "rate_set_id": "poc-machine-shop", "rate_set_version": "1.0.0",
      "total_material_cost": 16.0, "total_operation_cost": 1320.0,
      "total_additional_cost": 450.4, "total_part_cost": 1786.4
    }
  ]
}
```

History is ordered by a monotonic insertion key rather than by `created_at`. The clock's granularity is coarse enough (~15 ms on Windows) that estimates created in quick succession can share a timestamp, which would otherwise leave "newest first" — and therefore paging — ambiguous.

An out-of-range `limit`/`offset` returns `422`; an offset past the end returns an empty page with the real `total`.

### `GET /estimates/compare`

Compares two persisted estimates side by side. Deltas are `comparison - baseline`.

```bash
curl "http://127.0.0.1:8000/estimates/compare?baseline=<id-a>&comparison=<id-b>"
```

```json
{
  "baseline": {"estimate_id": "bb46cfc9...", "total_part_cost": 1363.2, "...": "summary row"},
  "comparison": {"estimate_id": "121942aa...", "total_part_cost": 1786.4, "...": "summary row"},
  "older_estimate_id": "bb46cfc9...",
  "newer_estimate_id": "121942aa...",
  "comparison_is_newer": true,
  "material_cost":   {"baseline": 8.0,    "comparison": 16.0,   "delta": 8.0,   "percent_change": 100.0},
  "operation_cost":  {"baseline": 960.0,  "comparison": 1320.0, "delta": 360.0, "percent_change": 37.5},
  "additional_cost": {"baseline": 395.2,  "comparison": 450.4,  "delta": 55.2,  "percent_change": 13.97},
  "total_part_cost": {"baseline": 1363.2, "comparison": 1786.4, "delta": 423.2, "percent_change": 31.04},
  "basis": "Both sides are read from the totals persisted with each estimate. ..."
}
```

Which side is newer is decided by timestamp, **independently of the argument order** — swapping `baseline` and `comparison` flips the deltas but reports the same `older_estimate_id`/`newer_estimate_id`. `percent_change` is `null` against a zero baseline rather than infinite. An unknown id on either side returns `404`.

> **Historical integrity.** Comparison reads the totals persisted with each estimate and never recalculates. Publishing a newer rate set version, changing the catalog, or creating new estimates leaves existing estimates and comparisons byte-identical — the stored snapshot is the audit source of truth.

### `GET /estimates/{estimate_id}`

Retrieves a persisted estimate by the `estimate_id` returned from `/cost/part` or `/cost/cad-part`, including its three audit snapshots. An unknown id returns `404`.

```json
{
  "estimate_id": "fbeb186475ee4908af5f5ee20dcb7333",
  "created_at": "2026-08-31T09:18:28.449883",
  "source": "cost/part",
  "part_id": "P100", "part_name": "Flange", "material": "Steel", "quantity": 4,
  "rate_set_id": "poc-machine-shop", "rate_set_version": "1.0.0",
  "total_material_cost": 8.0, "total_operation_cost": 960.0,
  "total_additional_cost": 395.2, "total_part_cost": 1363.2,
  "request_snapshot": {"...": "what was asked for"},
  "rate_set_snapshot": {"...": "the exact rates resolved"},
  "result_snapshot": {"...": "the full computed breakdown"}
}
```

### `POST /estimates/{estimate_id}/actuals` — record a client's actual cost or a supplier's quote

Records a cost stated outside this application (an ERP actual, or a supplier's quotation — `figure_kind` says which) against an existing estimate, and returns it with its per-unit variance. Nothing in the estimate changes, and no costing reads an actual. In the UI this is in the **History** detail, under *Actual cost (client data)*.

```json
{
  "figure_kind": "actual",
  "source": "Client ERP actual FY2025",
  "cost_basis": "per_batch",
  "batch_quantity": 10,
  "currency": "INR",
  "total_cost": 3400.0,
  "material_cost": 600.0,
  "machining_cost": 2500.0,
  "client_part_number": "BRK-100",
  "drawing_revision": "B",
  "cost_date": "2025-03-31"
}
```

- `figure_kind`, `source`, `cost_basis`, `currency` and `total_cost` are required. Neither `figure_kind` (`actual` or `supplier_quote`) nor `cost_basis` has a default; a payload without `figure_kind` — the shape accepted before Iteration 5 — is refused (422).
- `supplier_name` and `quote_reference` are optional free text for a `supplier_quote` and refused on an `actual`. They are stored as entered and matched to nothing.
- `batch_quantity` is required for `per_batch`. A per-batch cost without it is refused (422), never treated as one unit.
- `material_cost`, `machining_cost` and `additional_cost` are optional. One left out comes back `not_supplied`, never zero. A breakdown that adds up to more than `total_cost` is refused.
- A currency different from the estimate's is refused (422). Nothing is converted.
- Records are never edited. To correct one, send a new record with `supersedes_actual_id`; the original stays listed, marked `is_current: false`. A record can be corrected once (a second correction of the same record is `409`).

The variance is per unit: `estimate - actual`, percentage relative to the actual (omitted when the actual is zero). Measured on `model1_1.stp`, 10 off, `poc-default` (estimate ₹316.39 per unit), with the request above:

```
total      est 316.39  actual 340.00  variance -23.61  -6.95%
material   est   1.39  actual  60.00  variance -58.61  -97.69%
machining  est 315.00  actual 250.00  variance +65.00  +26.00%
additional est   0.00  not supplied
unattributed actual: 30.00 per unit
```

An assembly estimate is compared on its total only, because its material/operation/additional figures cover manufactured components alone. An incomplete or mixed-currency assembly has no total to compare, and its variance is `unavailable` with the reason. A batch size different from the estimate's, or one not stated, is reported as a warning and not adjusted for.

A `supplier_quote` is reported in separate fields, never as a variance: `quote_per_unit`, `gap` (`quote - should-cost`) and `gap_percent` (relative to the should-cost), with a sentence giving direction and size and no verdict. Measured against a ₹242.00 per-unit should-cost: quotes of 100, 400 and 850 per unit gave gaps of −142.00 (−58.68%), +158.00 (+65.29%) and +608.00 (+251.24%).

### Previews and advice (nothing here is stored)

| Endpoint | What it does |
|---|---|
| `POST /cost/route-comparison` | The recommended route and the candidate families' routes, each priced as if decided, through the same path as `/cost/cad-part`. Unpriceable routes are listed with their reason. Never ranked or chosen; "Use this route" in the route card records the engineer's decision. |
| `POST /cost/matrix-preview` | `/cost/cad-part`'s inputs with `rate_sets` (JSON list of `{rate_set_id, version}`) and `quantities` (JSON list) in place of one of each. One part priced per rate-set version x quantity through the same path, plan as sent. Caller's order, never ranked; an unpriceable cell carries the reason and no figure; nothing stored; at most `CAD_COSTING_MAX_MATRIX_CELLS` (24) cells. In the UI: **Matrix preview** in the Cost section. |
| `GET /estimates/{estimate_id}/cost-drivers` | One stored estimate's total split into the stored figures that built it (material, each route stage, operations by process type, overhead, scrap, tooling; assembly roll-ups by category and component), each with its share of the stored total. Build-up order, never ranked; any remainder is a `reconciliation` line. Read-only. In the UI: **Cost drivers** in the Cost section and History. |
| `POST /cad/wall-thickness` | Measured wall thickness of an exact B-Rep part: the thinnest wall found (value, faces, location), per-face minima and a fixed-bin distribution, with the method and what it can miss. Faceted STEP, STL and IGES surface models are refused. Read-only. |
| `POST /cad/setups/propose`, `POST /cad/setups/apply-proposal` | A setup grouping proposed from the direction a tool must approach each feature, checked with straight rays; unreachable operations are listed. Propose changes nothing; apply recomputes it and regroups the plan through the engineer's fields. In the UI: **Proposed setups by approach direction** under the operations table. |
| `POST /cad/sheet-metal`, `POST /operations/sheet-metal-lines` | Whether a part is a constant-thickness sheet, its bends, and — for a K-factor you enter (no default) — its flat pattern, cut perimeter and pierces; then unaccepted cut and bend plan lines from that measurement, timeable at hourly rates. In the UI: the sheet-metal card in Manufacturing. |
| `POST /cad/draw-direction` | Candidate pull axes for a cast, moulded or forged part, with draft per face and undercut faces; a recommended axis (fewest undercuts, then least zero-draft area — a ranking, not a decision) or the axis you enter. No parting line. Read-only. Draft and undercut DFM rules run in `/cad/dfm` only when a forming process is stated. |
| `POST /cad/governing-wall`, `POST /operations/moulding-line` | The thickest wall found (largest inscribed ball) and, for a cavity count you enter, the one injection-moulding line timed from it: cooling from wall^2, plus fill, pack and open/eject, per part. Priceable by time at an hourly `injection_moulding` rate. |
| `POST /cad/sheet-stock/propose`, `POST /cad/sheet-stock/decide` | A rectangular grid nest of the measured flat blank on the sheet, spacing and margin you enter (parts per sheet, utilisation, both orientations), proposed as the material basis and used only once confirmed. |
| `POST /cad/bulk/preview` | A CSV (`file, part_id, part_name, material, quantity`, optional `rate_set_id, rate_set_version, route_id, source_unit`) plus the CAD files. Each part priced with the system's estimated times, every figure labelled **"Unreviewed system estimates — not an estimate, not quotable"**. `?format=csv` returns the export. Up to 100 rows / 50 files / 300 MB. In the UI: the **Bulk preview** tab. |
| `POST /cad/dfm` | Advisory design-for-manufacturing flags from the recognized features (deep holes, deep narrow pockets or slots, small internal fillet radii, many distinct hole sizes), each with the measured value, the threshold, the profile version and its source. Also returns `flagged_features`, the recognized features the flags name, so a flag row can mark them on the 3D model (a pointer at the recognized location, not a face highlight). Never changes a cost. In the UI: the DFM panel in the CAD section. |
| `GET /dfm-profiles`, `GET /dfm-profiles/{id}`, `POST /dfm-profiles/{id}/versions` | The DFM threshold profiles: versioned and immutable like time profiles. The seeded `dfm-general-guidelines` 1.0.0 is illustrative and cites a vendor CNC design guide; one threshold has no published source and says so. |

### `GET /estimates/{estimate_id}/actuals`

Every figure recorded against the estimate, oldest first, each with its `figure_kind`, `is_current`, `superseded_by` and its variance or gap. `supplier_quotes` lists the current quotes side by side in the order entered — `ranked: false`, `preferred: null`; nothing is sorted by price.

## API

### `POST /cost/material`

Calculates the material cost of a part: `mass_kg * material_cost_per_kg * quantity`.

> **Material cost rates are ILLUSTRATIVE POC VALUES** (see `src/cad_costing_agent/materials.py`), not real market or customer pricing.

Known materials: `Steel`, `Aluminium`.

Request:

```json
{
  "part_id": "P1",
  "part_name": "Bracket",
  "material": "Steel",
  "mass_kg": 2.0,
  "quantity": 3
}
```

Response:

```json
{
  "part_id": "P1",
  "part_name": "Bracket",
  "material": {"name": "Steel", "density_kg_m3": 7850.0, "cost_per_kg": 0.8, "illustrative": true},
  "mass_kg": 2.0,
  "quantity": 3,
  "material_cost_per_kg": 0.8,
  "material_cost": 1.6,
  "total_material_cost": 4.8,
  "formula": "total_material_cost = mass_kg * material_cost_per_kg * quantity",
  "is_illustrative_rate": true
}
```

An unknown material returns `400`; invalid mass/quantity (e.g. negative or zero) returns `422`.

### `POST /cost/operation`

Calculates the cost of a single manufacturing operation:

```
machine_cost = machine_rate_per_hour * (cycle_time_minutes * quantity / 60)
setup_cost   = machine_rate_per_hour * (setup_time_minutes / 60)
labor_cost   = labor_rate_per_hour * (cycle_time_minutes * quantity / 60)
total_operation_cost = machine_cost + setup_cost + labor_cost
```

> **Rates in the example below are ILLUSTRATIVE POC VALUES ONLY**, not real market/customer pricing. Rates are supplied per-request (there is no built-in rate lookup), and each request carries its own `is_illustrative_rate` flag.

Request (illustrative CNC Drilling example):

```json
{
  "operation_id": "OP1",
  "operation_name": "CNC Drilling",
  "process_type": "Drilling",
  "machine_rate_per_hour": 1800,
  "labor_rate_per_hour": 500,
  "setup_time_minutes": 20,
  "cycle_time_minutes": 2,
  "quantity": 10
}
```

Response:

```json
{
  "operation_id": "OP1",
  "operation_name": "CNC Drilling",
  "process_type": "Drilling",
  "quantity": 10,
  "machine_rate_per_hour": 1800.0,
  "labor_rate_per_hour": 500.0,
  "setup_time_minutes": 20.0,
  "cycle_time_minutes": 2.0,
  "machine_cost": 600.0,
  "setup_cost": 600.0,
  "labor_cost": 166.67,
  "total_operation_cost": 1366.67,
  "formulas": [
    "machine_cost = machine_rate_per_hour * (cycle_time_minutes * quantity / 60)",
    "setup_cost = machine_rate_per_hour * (setup_time_minutes / 60)",
    "labor_cost = labor_rate_per_hour * (cycle_time_minutes * quantity / 60)",
    "total_operation_cost = machine_cost + setup_cost + labor_cost"
  ],
  "is_illustrative_rate": true
}
```

Invalid values (e.g. negative/zero rates, cycle time, or quantity; negative setup time) return `422`. Tooling, inspection, scrap, and overhead costs are not part of this calculation yet.

### `POST /cost/part`

Combines material cost, a list of manufacturing operation costs, and the additional cost components (overhead, scrap allowance, tooling) into a single part-level estimate:

```
direct_cost_base      = total_material_cost + total_operation_cost
overhead_cost         = overhead_rate_percent / 100 * direct_cost_base
scrap_cost            = scrap_rate_percent / 100 * direct_cost_base
tooling_cost          = tooling_cost_per_batch
total_additional_cost = overhead_cost + scrap_cost + tooling_cost
total_part_cost       = direct_cost_base + total_additional_cost
```

`operations` may be an empty list (material-cost-only estimate).

> **Additional cost components.** All three are supplied per request under `additional_costs` and **default to zero**, so omitting the field reproduces exactly the material + operations total. Every component stays individually visible in the response alongside its rate, formulas, and assumptions.
>
> Both percentages apply to the *same* `direct_cost_base`, which excludes overhead, scrap, and tooling — so the components never compound: overhead is never charged on scrap, scrap never on overhead, and neither on tooling.
>
> Bounds: `overhead_rate_percent` ∈ [0, 1000] (the upper cap catches a fraction like `0.15` being passed where a percent like `15` was meant), `scrap_rate_percent` ∈ [0, 100) (at 100% no good parts are produced, so a part cost would be meaningless), `tooling_cost_per_batch` ≥ 0. All are **illustrative POC inputs**, not real pricing.

> **Quantity handling**: each operation's `quantity` must equal the part's `quantity` (400 error otherwise). Material cost and each operation's cost are already scaled for the full batch quantity by their own calculations, so the part-level total sums them as-is without multiplying by quantity again — avoiding double-counting. The additional components are batch-level for the same reason: the percentages inherit batch scaling from `direct_cost_base`, and `tooling_cost_per_batch` is a one-time batch cost like operation setup cost. Neither is re-scaled by quantity.

#### Worked example

Every figure below is exact and can be checked by hand:

```
Part      Steel @ 0.80/kg, mass 2.5 kg, quantity 4
          material_cost       = 2.5 * 0.80          =    2.00 per part
          total_material_cost = 2.00 * 4            =    8.00

Operation machine 1200/hr, labor 600/hr, setup 30 min, cycle 3 min, qty 4
          run hours           = 3 * 4 / 60          =    0.2 h
          machine_cost        = 1200 * 0.2          =  240.00
          setup_cost          = 1200 * (30 / 60)    =  600.00
          labor_cost          = 600 * 0.2           =  120.00
          total_operation_cost                      =  960.00

          direct_cost_base    = 8.00 + 960.00       =  968.00
          overhead  @ 10%     = 968.00 * 0.10       =   96.80
          scrap     @  5%     = 968.00 * 0.05       =   48.40
          tooling             = flat                =  250.00
          total_additional_cost                     =  395.20

          TOTAL PART COST     = 968.00 + 395.20     = 1363.20  INR
          per unit            = 1363.20 / 4         =  340.80  INR
```

Request:

```json
{
  "part": {"part_id": "P100", "part_name": "Flange", "material": "Steel", "mass_kg": 2.5, "quantity": 4},
  "operations": [
    {"operation_id": "OP1", "operation_name": "Milling", "process_type": "Milling", "machine_rate_per_hour": 1200, "labor_rate_per_hour": 600, "setup_time_minutes": 30, "cycle_time_minutes": 3, "quantity": 4}
  ],
  "additional_costs": {"overhead_rate_percent": 10.0, "scrap_rate_percent": 5.0, "tooling_cost_per_batch": 250.0}
}
```

Response (abbreviated — each entry in `operations` is a full `/cost/operation`-style breakdown, and `material` is a full `/cost/material`-style breakdown):

```json
{
  "part_id": "P100",
  "part_name": "Flange",
  "material": { "...": "see /cost/material response shape", "total_material_cost": 8.0 },
  "operations": [ { "...": "see /cost/operation response shape", "total_operation_cost": 960.0 } ],
  "total_material_cost": 8.0,
  "total_operation_cost": 960.0,
  "direct_cost_base": 968.0,
  "additional_costs": {
    "direct_cost_base": 968.0,
    "overhead_rate_percent": 10.0, "overhead_cost": 96.8,
    "scrap_rate_percent": 5.0, "scrap_cost": 48.4,
    "tooling_cost_per_batch": 250.0, "tooling_cost": 250.0,
    "total_additional_cost": 395.2,
    "formulas": ["direct_cost_base = ...", "overhead_cost = ...", "scrap_cost = ...", "tooling_cost = ...", "total_additional_cost = ..."],
    "assumptions": ["Overhead and scrap are both percentages of direct_cost_base ...", "..."]
  },
  "total_additional_cost": 395.2,
  "total_part_cost": 1363.2,
  "per_unit_cost": 340.8,
  "currency": "INR",
  "formula": "total_part_cost = total_material_cost + total_operation_cost + total_additional_cost",
  "quantity_assumption": "Each operation's quantity must equal the part's quantity: ..."
}
```

An unknown material, or an operation quantity that doesn't match the part quantity, returns `400`. Invalid part, operation, or additional-cost fields (negative values, a scrap rate ≥ 100%, an overhead rate > 1000%) return `422`. Engineering cost, inspection cost, and route optimization are still not part of this estimate.

#### The same example through a rate set

The identical result can be produced by selecting a rate set instead of restating every rate, which additionally records *which* rates were used. `poc-machine-shop` v1.0.0 prices Steel at 0.80/kg, Milling at 1200/600 per hour, and carries overhead 10% / scrap 5% / tooling 250 — so this returns the same **1363.20**:

```json
{
  "part": {"part_id": "P100", "part_name": "Flange", "material": "Steel", "mass_kg": 2.5, "quantity": 4},
  "operation_requirements": [
    {"operation_id": "OP1", "operation_name": "Milling", "process_type": "Milling", "setup_time_minutes": 30, "cycle_time_minutes": 3, "quantity": 4}
  ],
  "rate_set_id": "poc-machine-shop",
  "rate_set_version": "1.0.0"
}
```

Note that `operation_requirements` carry no rates — times and quantity describe the part's process plan, while the rates describe the shop and come from the rate set.

The response adds a provenance stamp and the id of the persisted estimate, and `rate_precedence` restates the rule above:

```json
{
  "estimate_id": "fbeb186475ee4908af5f5ee20dcb7333",
  "rate_set": {"rate_set_id": "poc-machine-shop", "name": "POC Machine Shop", "version": "1.0.0", "is_illustrative": true},
  "total_part_cost": 1363.2,
  "rate_precedence": "Explicit per-request values always win; the rate set only supplies what the request omits. ..."
}
```

Every successful `/cost/part` and `/cost/cad-part` call is persisted and can be re-read later via `GET /estimates/{estimate_id}`.

The **rates actually used** need no extra fields — they are already visible in the existing breakdown: `material.material_cost_per_kg`, each operation's `machine_rate_per_hour` / `labor_rate_per_hour`, and `additional_costs.overhead_rate_percent` / `scrap_rate_percent` / `tooling_cost_per_batch`.

### `POST /cad/geometry`

Accepts an uploaded CAD file (**STL or STEP**) and returns extracted geometry properties: volume, surface area, and bounding-box dimensions. STL is read with [`trimesh`](https://trimesh.org/); STEP is read with `cadquery-ocp` (OpenCASCADE), whose `BRepGProp` integrates over the exact B-Rep faces rather than a mesh approximation. Both are deterministic for a given file.

The response also carries `detected_unit`: `null` for STL (no unit metadata exists), `"mm"` for STEP (the reader normalized it).

> **This endpoint is standalone geometry extraction only — it is not connected to costing.** It does not calculate mass, material cost, or any part/operation cost; it knows nothing about material density, cost rates, or costing formulas.

Request: `multipart/form-data` with a single `file` field containing an STL file.

```bash
curl -F "file=@cube.stl" http://127.0.0.1:8000/cad/geometry
```

Response (for a 10×20×30 box):

```json
{
  "source_format": "stl",
  "volume": 6000.0,
  "surface_area": 2200.0,
  "bounding_box": {"x": 10.0, "y": 20.0, "z": 30.0},
  "detected_unit": null,
  "units_note": "Units match whatever length unit the source CAD file used; STL does not embed unit information, so no unit conversion is applied here."
}
```

The same box as STEP (`curl -F "file=@box.step" ...`) returns identical geometry with `"source_format": "step"` and `"detected_unit": "mm"`.

An unsupported file format (anything other than `.stl`, `.step`, `.stp`) or content that isn't valid geometry in the declared format (empty file, corrupt content, a structurally valid STEP carrying no geometry) returns `400`.

### `POST /cad/mesh`

Everything the 3D viewer needs from one upload: the same geometry `/cad/geometry` reports, plus a
browser-renderable triangle mesh. Visualization only — no mass, no cost, and nothing is persisted.

```bash
curl -X POST http://127.0.0.1:8000/cad/mesh -F "file=@bracket.step"
```

```jsonc
{
  "filename": "bracket.step",
  "geometry": { "source_format": "step", "volume": 42675.066, "...": "..." },
  "mesh": {
    "source_format": "step",
    "positions": [0.0, 0.0, 0.0, 80.0, 0.0, 0.0, "..."],  // flat x,y,z triples
    "indices":   [0, 1, 2, "..."],                        // flat triangle triples
    "vertex_count": 788,
    "triangle_count": 784,
    "bounds": { "min": [0.0, 0.0, 0.0], "max": [80.0, 50.0, 22.0] },
    "tessellation": {
      "method": "opencascade-brepmesh",
      "linear_deflection": 0.09687,
      "angular_deflection": 0.5,
      "relative_linear_deflection": 0.001,
      "coarsened": false,
      "note": "Tessellated with OpenCASCADE at a linear deflection of 0.1% ..."
    },
    "display_mesh_simplified": false,      // true if the preview was decimated
    "original_triangle_count": 784,        // what the CAD data actually holds
    "display_triangle_count": 784,         // what is carried in `indices`
    "simplification_method": null,         // set when the preview was reduced
    "simplification_ratio": null           // display / original, when reduced
  },
  "mesh_error": null
}
```

STEP is tessellated server-side with OpenCASCADE; STL is passed through as-is. Both come back in
the same shape, so the viewer does not care which format the file was.

**A model above the browser's triangle budget is reduced, not refused.** The full-resolution mesh
is built as always and every engineering number is measured from it; only then is a separate
*display* mesh decimated server-side down to `CAD_COSTING_MAX_VIEWER_TRIANGLES` and sent in its
place. The five fields above then say exactly what happened, so a reduced count is never mistaken
for the model's own — for the real 198 MB `Figure 3.stp`:

```jsonc
"triangle_count": 79273,                                // what you are looking at
"display_mesh_simplified": true,
"original_triangle_count": 272113,                      // what is measured and costed
"display_triangle_count": 79273,
"simplification_method": "deterministic_vertex_clustering",
"simplification_ratio": 0.291324
```

The reduction is display-only and is never used for volume, surface area, mass, feature
recognition or cost — see [The 3D viewer](ARCHITECTURE.md#the-3d-viewer). It is deterministic, so
the same bytes always give the same picture.

Only when even the decimation cannot produce a usable mesh — nothing survives it, or the model is
past `CAD_COSTING_MAX_VIEWER_INPUT_TRIANGLES` — does the response return `200` with `mesh: null`
and a `mesh_error` explaining why. Geometry and costing do not depend on the mesh, so a part that
is too heavy to *draw* is still a part you can *cost*. Genuinely unreadable input returns `400`.

### `POST /cad/features`

Recognize features in an uploaded STEP file. Visualization- and analysis-only: it calculates no
mass, no cost, and persists nothing.

```bash
curl -X POST http://127.0.0.1:8000/cad/features -F "file=@bracket.step"
```

```jsonc
{
  "filename": "bracket.step",
  "source_format": "step",
  "units": "mm",
  "solid_count": 1, "face_count": 14, "edge_count": 31,
  "feature_counts": { "hole": 5, "cylinder": 1, "plane": 8 },
  "unclassified_face_count": 0,
  "summary": ["4 × hole (through) Ø6.5 mm depth 10 mm — confidence high", "..."],
  "features": [
    {
      "feature_id": "H-001",
      "feature_type": "hole",
      "subtype": "through",
      "solid_index": 0,
      "dimensions": { "diameter_mm": 6.5, "depth_mm": 10.0 },
      "location": { "x": 10.0, "y": 10.0, "z": 0.0 },
      "axis": { "x": 0.0, "y": 0.0, "z": 1.0 },
      "source_faces": [7],
      "source_edges": [10, 18, 19],
      "confidence": "high",
      "detection_method": "complete_internal_cylinder_classified_by_end_conditions",
      "evidence": [
        "face(s) [7] sweep 360.0° about a common axis at radius 3.250 mm, so they form one complete bore",
        "the surface normal points toward the axis, so the material is outside it",
        "edge 18 is an inner boundary of face 5, so the bore breaks through that face",
        "edge 10 is an inner boundary of face 3, so the bore breaks through that face"
      ]
    }
  ],
  "solids": [ { "solid_index": 0, "face_count": 14, "volume_mm3": 42675.066, "features": ["..."] } ],
  "warnings": [],
  "elapsed_ms": 86.4
}
```

`source_faces` and `source_edges` index the STEP shape's own face and edge maps and are stable for
a given file, which is what lets the viewer mark a selected feature on the model.

`elapsed_ms` is the only field that varies between runs on the same input. `representation` says
which kind of file it was, `facet_count` is populated for faceted models, and each feature carries
`source_type` plus either `source_faces` (B-Rep) or `source_facets` (faceted).

### `POST /cad/operations/suggest`

Suggest manufacturing operations for the features recognized in a STEP file. Runs the same
recognition `/cad/features` does — one upload, one parse — then applies deterministic
feature-to-operation rules.

**Suggestions only.** Nothing is costed, no estimate is touched, no rate is read or invented, and
nothing is persisted.

```bash
curl -X POST http://127.0.0.1:8000/cad/operations/suggest \
  -F "file=@bracket.step" -F "rate_set_id=poc-default"
```

```jsonc
{
  "filename": "bracket.step",
  "representation": "brep",
  "feature_count": 27,
  "features": ["..."],                    // exactly what /cad/features returns
  "suggestion_counts": { "drilling": 4, "turning": 2, "milling": 16 },
  "summary": ["1 × drilling — from hole (through) Ø16 mm depth 12 mm — confidence high", "..."],
  "suggestions": [
    {
      "suggestion_id": "SUG-001",
      "feature_id": "H-001",                     // the feature that caused this
      "feature_type": "hole",
      "feature_subtype": "through",
      "feature_label": "hole (through) Ø16 mm depth 12 mm",
      "feature_confidence": "high",
      "feature_detection_method": "complete_internal_cylinder_classified_by_end_conditions",
      "operation_type": "drilling",              // the system's proposal
      "operation_name": "Drill hole (through) Ø16 mm depth 12 mm",
      "confidence": "high",
      "source_type": "brep",
      "rule_id": "through_hole_to_drilling",
      "reason": "cylindrical through-hole detected, so it can be drilled from one side",
      "evidence": [
        "Ø16 mm, depth 12 mm",
        "CAD face(s) 5",
        "recognized by rule '...' at confidence high",
        "this feature was read from the file's exact B-Rep surfaces"
      ],
      "alternatives": [],
      "source_faces": [5],                       // what the viewer marks
      "editable": true,
      "origin": "system",
      "user_action": "pending",
      "user_operation_type": null,               // an edit lands here, never overwriting above
      "costable": false,
      "missing_for_costing": ["setup_time_minutes", "cycle_time_minutes"],
      "not_costable_reason": "Suggested operation — not yet costed because processing time has not been estimated.",
      "priced_by_rate_set": true
    }
  ],
  "declined": [
    {
      "feature_id": "PL-014",
      "feature_label": "plane 8 × 8 mm area 64 mm²",
      "rule_id": "minor_plane_declined",
      "reason": "this planar face is 0.7% of the largest planar face on its solid, below the 10% at which a flat is treated as a significant machined surface. ..."
    }
  ],
  "warnings": ["These are suggestions, not decisions. ...", "No setup or cycle time is suggested. ..."],
  "rules": ["through_hole_to_drilling", "external_cylinder_to_turning", "..."],
  "priced_processes": ["drilling", "milling", "turning"],
  "costable": false,
  "elapsed_ms": 1.2
}
```

**The rules.** Each one is keyed to evidence a recognizer actually produces:

| Recognized feature | Suggested | Confidence ceiling | Alternatives offered |
|---|---|---|---|
| hole — `through`, `blind` (incl. countersunk) | drilling | high | — |
| hole — `counterbore` | drilling | high | milling |
| cylinder — `shaft` (external) | turning | high | milling |
| cylinder — `bore` (internal, ends unclassified) | drilling | **medium** | milling, turning |
| pocket / slot | milling | high | — |
| plane — at least 10% of the solid's largest planar face | milling (facing) | **medium** | — |
| fillet / chamfer — recognized at `high` only | milling (finishing) | **medium** | — |
| hole — `ambiguous`, `enclosed`, unknown subtype | **nothing** | — | declined, with the reason |
| plane — below the significance threshold, or no area | **nothing** | — | declined, with the reason |
| fillet / chamfer recognized below `high` | **nothing** | — | declined, with the reason |

A suggestion's confidence is the **weakest** of the feature's own confidence, the rule's ceiling,
and a per-source ceiling — `faceted` caps everything at `medium`, because a feature fitted to
polygons is weaker evidence than one read from exact surfaces. It can therefore only ever be lower
than the feature's confidence, never higher.

**Nothing becomes a cost by itself.** A suggestion carries an operation identity and its link to a
CAD feature — no setup time, no cycle time, no rate. Times come from you; rates come from your rate
set, resolved by `process_type` exactly as for a hand-entered operation. `rate_set_id` is optional
and only makes the response *report* whether your rate set prices the suggested process
(`priced_by_rate_set`); it never changes what is suggested.

An unknown `rate_set_id` returns `404`. An STL returns `400`: suggestions come from features and
recognition needs a B-Rep. A faceted model above `CAD_COSTING_MAX_FACETS` returns `400` from
recognition, unchanged from `/cad/features` — geometry, the preview and costing are unaffected.

## Assemblies are assemblies, not one lump of geometry

A STEP assembly stores each part once and then places it as many times as it is
used. Uploaded as it stands, all of that arrives as a single shape: one volume,
one mass, one cost, with 43 components and 102 placements collapsed into it.
`POST /cad/assembly` decomposes it instead.

```text
FIGURE_2                              43 definitions, 102 occurrences, 4 levels
  ILLUSTRATION_ASM
    31360001DG07220_ASM               at (0.009, -137.998, 76.000)
      31360099DG13437_PRT             at (0.000, -18.000, 45.000)
      31161800SD00018_PRT   x12
      31161500SD00049_PRT   x8
      ...
```

**A definition is not an occurrence.** Twelve bolts are one part definition and
twelve placements, each with its own transform. Geometry hangs off the
definition and is measured once; an occurrence is an id, a parent, a name and a
placement. Conflating the two would either lose eleven bolts from the bill of
materials or store the same geometry twelve times.

**A quantity is an occurrence count, not a production quantity.** How many times
this assembly uses a component is in the file. How many assemblies you intend to
build is not, and the two are never conflated.

**Placements are kept, relative to the parent and to the assembly.** Both are
carried as twelve plain numbers, so orientation and position survive
decomposition — which is what a future exploded view would displace from.

**A part is still a part.** A file with several solid bodies and no assembly
entities is reported as a single part, because multi-body is a property of a
part and calling its bodies components would invent a structure the file does
not contain. `model2.stp` is exactly this case: two solids, one product, no
assembly.

### Taking it apart on screen

An assembly can be pulled apart with a slider to see what is inside it.

```text
Explode  [────────────●──────]  70%   [Reset explosion]

A visual exploded arrangement, derived from the assembly's own placements and
hierarchy. It is not an assembly or disassembly sequence, and the connector
lines are a reading aid, not a path a part travels.
```

**It is not a disassembly sequence, and is never described as one.** Nothing
here knows which part goes in first, what has to come off before what, or
whether a component can actually be withdrawn along the direction it is drawn
moving. That is a manufacturing decision made by people, and no amount of
geometry implies it.

**Components move in rigid groups, so a sub-assembly stays readable.** The tree
is walked to find the shallowest depth at which it actually branches — a chain
of single children is skipped, because separating an only child from its parent
moves everything and shows nothing — and each group at that depth moves as one
unit away from the assembly's centre:

```text
direction = normalise(group centre - assembly centre)
drawn     = CAD placement + direction x distance x factor
```

Everything below a group inherits its offset **exactly**, so the parts inside a
sub-assembly keep their positions relative to each other while the sub-assembly
separates from its neighbours. On the real 198 MB export the top two levels are
single children, so the explosion happens at depth 3 among 83 sibling groups.
A group sitting on the assembly centre has no outward direction; rather than
leaving it buried it is given one from a deterministic spiral over the sphere,
so the same file always produces the same arrangement.

**The CAD placement is never overwritten.** Each component carries the placement
the file states plus a display *offset*; the drawn position is rebuilt from
both every time the slider moves. Returning to 0% therefore restores the file's
own arrangement exactly rather than approximately, and selecting a component
while the view is exploded says both:

```text
Showing PIN only · used x2 · drawn at (-25.8, 29.9, 47.2),
CAD places it at (10, 20, 25).
```

**It cannot touch an engineering number.** `exploded_view.py` imports nothing
from the rest of the application, and nothing but the viewer reads what it
produces — both asserted by tests that parse imports and scan the package. An
exploded position is never persisted either: a slider position is a viewing
preference, not CAD truth, so a historical estimate keeps the placements the
file states. Verified on the pump assembly: volume, surface area and bounding
box read identically at 0% and 100%.

**The slider moves transforms and nothing else.** Each definition is meshed once
and drawn at each of its occurrences — twelve identical bolts are one geometry
and twelve placements — so exploding is a multiply-add per component. Measured
in the browser: **0.1–2 ms** per slider move, with no request, no reparse, no
re-tessellation and no recalculation.

### Not every component is machined here

An assembly is not a bag of identical machined parts. A pump has a housing that
is made, a bearing that is bought, a bolt that is a catalogue item, and a
shipping washer that should not be in the manufacturing cost at all. Costing all
four the same way gives a number that is confidently wrong — a bearing costed by
its volume of steel is out by an order of magnitude in whichever direction the
market happens to sit.

So every component is one of five things, and it decides entirely how the
component gets a cost:

```text
manufactured   made here: geometry -> mass -> material -> features ->
               operations -> setups -> the unchanged costing engine
purchased      bought: the unit cost you state, with a supplier
standard       a catalogue item: the unit cost you configure
excluded       deliberately not in the manufacturing cost
unresolved     not enough information to cost it, and it says so
```

**No CAD format states make/buy, and nothing here pretends otherwise.** A
purchased bearing is modelled with exactly the same kind of solid as a machined
housing; a bolt looks like a turned shaft; a filename is a filename. So a
classification is never inferred from a name, a part number, a shape, an
appearance or a material — a rule a test enforces by reading the source of the
function that makes the suggestion and asserting it cannot see any of them.

What the system will say is narrower, and it says only that:

```text
HOUSING   Manufactured   system suggested · low confidence

The file carries measurable solid geometry for this component, which is what
this application knows how to cost. That is evidence about what can be costed
here, not evidence that the part is made rather than bought: a purchased
bearing is modelled in CAD exactly like a machined housing. No CAD format
states make/buy. Confirm it or change it.
```

The suggestion is used until someone answers — an assembly would otherwise cost
nothing the moment this shipped — but never invisibly: every line reports
whether its classification came from the system or from a person, in the UI, in
the API and in the stored estimate. **Your answer never overwrites it.**
`system_classification` and `user_classification` sit side by side for the life
of the estimate, and the pair is recorded as feedback. Nothing is trained on it.

### What the assembly comes to

```text
component        classification   qty     per unit       extended
HOUSING          MANUFACTURED      x2       228.14         456.28
COVER            PURCHASED         x1       250.00         250.00
PIN              STANDARD          x2        12.50          25.00
                                                         --------
Manufactured                                               456.28
Purchased                                                  250.00
Standard                                                    25.00
Excluded                                                     0.00
                                                         --------
Assembly total                                             731.28 INR
```

Every contribution is shown separately, the excluded zero included — shown
rather than omitted, so a deliberate exclusion reads as a decision rather than
an absence.

There is still **no assembly labour and no fastening or joining time**, neither
of which this application calculates, and every summary says so. And nothing
here looks a price up: purchased and standard costs are what a person entered,
there is no standards database, no supplier is contacted, and **a purchased
part's cost is never derived from its weight**.

**A component that cannot be costed is never counted as zero.** It is listed
with the reason — no geometry in the file, classified as bought with no price
entered, a price with no currency, nobody has classified it, a different
currency — and the total stays marked incomplete:

```text
Assembly total incomplete — 1 component(s) need costing input.
```

The one legitimate zero is an **excluded** component, and it is legitimate
because a person authorised it. It contributes 0, states its reason, keeps its
place in the tree and the picture, and — unlike an unresolved one — does *not*
make the total incomplete. A deliberate omission is not a gap.

### Turning an estimate into a document somebody can send

A verified estimate becomes a printable quotation from its row in the history
table — **Generate quotation** — and the report opens as a standalone page with
no navigation, no viewer and no interactive panels on it. **Print / Save as PDF**
uses the browser's own print dialog; there is no PDF library here and no second
costing path.

```text
Quotation / Costing Report                              Q-0001
Prepared for the customer named below       Issued 4 Sept 2026
                       Estimate 81269422ce0741a7bbfd4e5fede2e5eb
──────────────────────────────────────────────────────────────
Customer     Acme Pumps Pvt Ltd     Part       Bushing, bronze-lined
Project      Line 4 retrofit        Model ref. hole.step
Prepared by  A. Engineer            Quantity   25 off
Validity     30 days                Currency   INR

PLEASE NOTE
  Illustrative rates — not a commercial price
  The rate set behind these figures is marked illustrative: its material
  prices and machine rates are placeholders for demonstration…

COST BREAKDOWN
  Material                                            ₹1,594.55
  Manufacturing — setup                                 ₹466.67
  Manufacturing — run                                   ₹949.17
  Overhead                                              ₹376.30
  Scrap allowance                                       ₹180.62
  Tooling                                               ₹300.00
  ────────────────────────────────────────────────────────────
  Total                                               ₹3,867.31
  Per unit                                              ₹154.69
```

**Every figure is read from the estimate, and nothing is recalculated.** The
quotation layer imports the assumptions reader and the money policy, and
nothing else — no calculator, no rate set, no repository, no CAD module. There
is no second costing path because there is nowhere for one to come from, and a
test parses the imports to keep it that way.

**The cost lines partition the total.** No line contains part of another, so
they sum — and because regrouping is the one place a document could quietly
disagree with the estimate it presents, the sum is checked against the figure
the estimate stored. A remainder is *stated*, never absorbed:

```text
The lines above sum to 2,272.76 but the estimate recorded 3,867.31, leaving
1,594.55 unaccounted for. The recorded total is the authority; this difference
is reported rather than absorbed.
```

**Two views.** The customer document carries the subject, quantity, material,
scope, cost categories, total, per-unit and every warning. The engineering view
adds setups, operations with their setup/run split, feature references, profile
versions, component decisions and the system/engineer differences. The internal
correction records the learning architecture keeps are **absent** from the
customer view rather than merely unrendered — a field a template does not print
is still a field in the JSON somebody forwards, so a test serializes a customer
quotation and greps it for those keys.

### An assumption is never printed as a measurement

Where a volume was supplied by a person because the file could not be measured,
the document says so in those words and the phrase "CAD volume" never appears:

```text
┌────────────────────────────────────────────────────────────┐
│ Volume assumed, not measured from CAD.                     │
│ User-supplied assumption: 39,81,004.3 mm³                  │
│ Volume used: 39,81,004.3 mm³                               │
└────────────────────────────────────────────────────────────┘
```

That is the single most consequential sentence on the page. A mass is a volume
times a density and a price is a mass times a rate, so an assumed volume
relabelled as measured would make the whole quotation a fabrication. Both the
sentence and the absence of the phrase are asserted by tests.

Every other warning is carried onto the document too — illustrative rates,
illustrative time assumptions, repaired geometry, a volume derived from polygons
rather than exact surfaces, operations left out for want of a committed time,
components deliberately excluded. There is **no parameter that hides them**, in
the API or on the page.

### A quotation is refused rather than made misleading

Some estimates must not become customer-facing documents at all:

```text
                        Quotation unavailable

  1 component(s) still require costing input. A partial sum must not be
  presented as a price, so no customer quotation is produced until every
  component is costed or explicitly excluded.

  This assembly's components are priced in more than one currency (EUR, INR).
  Nothing here converts between currencies, so there is no aggregated total
  to quote. Price every component in one currency first.
```

Issuing such a quotation is refused with a 409. The **engineering view still
renders**, because the person fixing the problem has to be able to see it — only
the customer document is withheld. Geometry that was never resolved blocks it
the same way, and nothing is ever filled in with a zero to make a total appear.

### A quotation stays what it was

Issuing one records the metadata and the totals as issued; the figures are not
copied, because they are re-read from the estimate's own immutable snapshots
every time the document is rendered. So there is one source for them rather than
two that could drift, and `total_at_issue` exists to be *checked* against a
re-render rather than trusted.

Verified live: after publishing a rate set version with steel at ₹9,999/kg and
every machine rate at ₹9,999/hour, quotation `Q-0001` still reads ₹3,867.31,
still names **POC Machine Shop v2.2.0**, and every cost line is byte-identical.
A later estimate of the same part, a later component classification and a later
time profile change it just as little.

An issued quotation is immutable for the same reason a rate set version is: it
is a document somebody already has. A correction is a new quotation with a new
number — sequential (`Q-0001`) unless you supply your own reference, and never
reused.

### Where the system is wrong, read back from what engineers chose

Four steps have been recording corrections — the operation and time pairs, the
setup regroupings, the make/buy pairs, and since Step 28 every one of those
again with the component that produced it. **Feedback** reads them back:

```text
Feedback & learning — where the system is wrong

Reviewed operations   713      Accepted unchanged   91%      Never finished   302

Accepted without change      91%    376 / 411    411 observation(s)
                                    302 excluded
Process changed               1%      4 / 712
Cycle time changed            5%     19 / 405
Moved between setups          1%     10 / 713
Classification changed       19%     42 / 222
```

Those are this project's own figures, measured over its 127 stored estimates.

**Nothing here learns.** No model is fitted, no rule is changed, no confidence
is recalibrated, no time profile is adjusted and no future estimate is affected.
The analytics module imports **nothing** from the rest of the application — it
is handed plain snapshots and hands back plain models — so it cannot reach a
rule or a profile to change one, and tests assert that in both directions. Every
response carries the sentence saying so.

### An acceptance rate needs the right denominator

An operation nobody ever committed a time for was not accepted and was not
corrected — it was never finished, usually because the rate set prices no rate
for the process suggested. Counting those as rejections made a rule with **one**
correction read as 46% acceptance:

```text
Rule                            Suggested  Accepted  Changed  Unfinished  Acceptance
significant_plane_to_milling          354       164        1         182         95%
through_hole_to_drilling              192       152        0          17         87%
external_cylinder_to_turning          100         8        1          91         89%
fillet_to_finishing_milling            62        52        2           8         96%
blind_hole_to_drilling                  4         0        0           4          — [thin]
```

`external_cylinder_to_turning` read as **8%** before that was fixed. Every rate
now states its numerator, its denominator, what it excluded and why.

### The confusion matrix, and the records behind it

```text
System suggested   Engineer chose   Count
milling            milling            413
drilling           drilling           196
turning            turning             99
milling            turning              2   see the records
milling            drilling             1   see the records
turning            milling              1   see the records
```

Clicking through gives the individual corrections, so a percentage is checkable:

```text
MILLING → TURNING · 2 CORRECTION(S)
Estimate   Component  Feature   Operation  System     Engineer  Rule                          Evidence
b33a7b71   —          FIL-001   OP-007     0.03 min   4 min     fillet_to_finishing_milling   brep (derived)
```

### Honesty about small samples

A correction rate over four observations is noise wearing a percentage sign, so
every metric carries `sample_count`, `minimum_required` and `is_actionable`, and
below the threshold the finding is a sentence rather than a number:

```text
4 observation(s) — not enough evidence for a reliable pattern.
```

The threshold is configurable (`CAD_COSTING_MIN_FEEDBACK_SAMPLE`, default 8) and
every response states which one it was judged against. It is a floor, **not a
significance test** — this project makes no distributional claims.

**Medians carry the headline.** One engineer typing 90 minutes where the system
said 0.6 would otherwise define a shop's entire bias. Outliers are listed
separately and still counted, because an order-of-magnitude miss is the most
informative record in the set and deleting it would only make the average look
calm. **Relative error refuses to divide by a near-zero prediction** — a
prediction of 0.0008 min corrected to 0.5 is a 62,400% error and says nothing —
so those records are excluded from that figure only, counted, and kept
everywhere else.

### Confidence is measured, not assumed to be calibrated

```text
Band      Reviewed  Accepted  Unfinished  Acceptance
high           296       160         112         87%
medium         416       216         190         96%
```

An **empirical** acceptance rate. Confidence in this application is a three-way
classification of which rule branches fired, never a probability, so a
difference between bands is a measured association and nothing more. B-Rep and
faceted evidence are never combined silently.

### The estimate is a workflow, not a dashboard

One upload fans out into a lot of information, and for most of this project's life all of it
shared one page: eighteen cards, ten of them visible before anything was uploaded, and about
twelve screens of scrolling once a 39-feature part had been analysed. The capabilities were
right and the presentation was wrong.

The estimate is now six sections, in the order the work happens:

```text
Overview      the model, its dimensions and mass, the cost, warnings, what to do next
CAD           geometry, and the recognized features as counts and summaries
Manufacturing the setup-grouped routing, the suggestions behind it, the time profile
Cost          the total, the breakdown, the assumptions, "why did this change?"
Assembly      structure, classification, per-component plans, the roll-up
Quote         the printable customer document, and the engineering view
```

Measured on the same real part, before and after: the page went from 6,621 px in one section to
1,415 px (Overview), 1,462 px (CAD), 1,860 px (Cost), 777 px (Assembly) and 588 px (Quote).
Horizontal overflow went from 91 px to none, which is what made the operations table readable —
process names were being clipped to "drilli".

Manufacturing was the one section still long, at 4,217 px for a 31-operation routing, and a
second pass took it to **2,071 px**. Almost all of it was layout rather than content: each row
stacked the process select over the name input and the move buttons under the sequence number,
which cost 67 px a row and made the `#` column 126 px wide — wider than Feature and Confidence
together. The same controls on one line come to **35 px a row**, uniform. The charging-rules
paragraph, the numbered sequence list and the per-feature suggestion breakdown moved into
disclosures, so the section now opens on the figures: one setup, 93.9 min of cycle time, and
₹3,047.00 of operation cost split into setup and run. Each operation's explanation, rule id and
parameters are behind a `Details` button — the row click had always revealed them and nothing
said so.

Nothing was removed. The per-feature table with its identifiers, axes and CAD face counts is
one click behind *View detailed feature analysis*, which takes the features card from 725 px to
2,909 px when opened. Feedback and correction analytics, evidence diagnostics, rate-set
authoring and time-profile authoring moved out of the estimate flow into **Engineering**, **Rate
sets** and **Time profiles** beside **History**. The costed figures are identical either way:
the same part comes to ₹3,060.86 for ten, ₹306.09 each, before and after.

### Absent is not the same as missing

Step 29's readiness report counted every empty field as a gap. Measuring the
real database showed two of its three headline gaps were not gaps at all, so
each evidence field is now **classified** rather than counted:

```text
Evidence                              Known    Of   Known %   Not applicable  Recoverable  Permanent
Feature-recognition confidence            7   719        1%                1            0        712
Component identity                      264   264      100%              456            0          0
Paired cycle time                       412   412      100%              308            0          0
Suggestion rule                         719   719      100%                1            0          0
Evidence type (B-Rep or faceted)        719   719      100%                1            0          0
Time-profile version                    514   514      100%              206            0          0
```

**Not applicable** means the value cannot exist for that record. Every one of
the 449 records Step 29 reported as missing a component identity came from a
single-part estimate, and a standalone part has no component. Every one of the
302 "missing" paired cycle times was an operation nobody ever committed a time
for — which Step 29 itself requires to stay out of every time statistic. And 206
records with no time-profile version had no profile *id* either: the plan ran
under the built-in baseline, which has no published version.

**Permanent** means it was never captured and nothing stored can recover it. The
CAD file is not kept, so a feature confidence that was not written down is gone
for good. That is reported as a limitation, never as a backlog, and it is never
filled with a guess.

`percent_known` divides by the records where the value *could* exist — the same
lesson as the acceptance denominator.

### The one gap that was real

The recognizer has exposed a feature confidence since Step 19, and Step 29
recorded it on the plan and the feedback record. Yet all 713 stored records
lacked it, because the **operations table builds its plan payload in the
browser** rather than calling `build_plan` — and any field it does not copy
never reaches the server. `source_type` the analytics can recover afterwards
from the viewer linkage; feature confidence it cannot recover from anything.

That is fixed. A new estimate now persists the recognizer's answer on every
operation, verified end to end. The 712 historical records stay honest.

### What a later learning step would have to work with

```text
Learning dataset readiness

total feedback records                      713
with a system prediction and a user outcome 712
with a usable time correction               405
with component identity                     264
with feature identity                       712
with a rule id                              712
with a known source type                    712
POTENTIAL TRAINING ROWS                     712
insufficient rows                             1

biggest gaps: feature_confidence 713 · component_identity 449 ·
              paired_cycle_time 308 · time_profile_version 199
```

Reporting only. `rule_id` and `source_type` are recovered for every historical
record by joining each feedback row to the plan stored beside it — a
reconstructed fact is flagged as one, so a reader can tell it from a recorded
value.

### Historical corrections stay what they were

Verified live: publishing a rate set version with steel at ₹9,999/kg and a new
time-profile version left the summary, the records and the time report
**byte-identical**. Every figure comes from the estimates' own snapshots, the
same property the cost comparison and the quotation rest on.

### Every manufactured component gets its own manufacturing plan

A manufacturing plan is a routing for **one thing clamped in a fixture**. An
assembly is not that, so it never gets one flat plan. Each manufactured
component owns a complete plan of its own — its own features, its own suggested
operations, its own estimated times, its own setups, its own sequence:

```text
Component manufacturing plans           3 component plan(s) built in 129 ms

Component     Classification   Plan
HOUSING       MANUFACTURED     GENERATED
  ×2 in this assembly
  6 feature(s) · 6 operation(s) · 1 setup(s) · setup 10 min · cycle 1.84 min
COVER         MANUFACTURED     NEEDS USER INPUT
  4 feature(s) · 4 operation(s) · 1 setup(s) · setup 10 min est. · cycle 1.14 min est.
  This plan is not finished: the inputs below are missing. The operations they
  belong to are left out of the cost rather than defaulted to zero.
PIN           MANUFACTURED     NEEDS USER INPUT
BEARING       PURCHASED        NO MANUFACTURING PLAN
BOLT          STANDARD         NO MANUFACTURING PLAN
SEAL          EXCLUDED         NO MANUFACTURING PLAN
```

**Two components never share a setup.** A housing and a shaft that happen to use
the same machine are still two clampings, and a shared process type has never
been evidence of shared fixturing in this codebase. Setup time is charged once
per setup, so merging them would under-cost the assembly by exactly the setups
nobody charged for. There is also **no assembly-level routing**: nothing here
knows what order components are made or joined in, and inventing one would be a
manufacturing claim this application cannot support.

**"No manufacturing plan" and "not generated" are different answers.** The first
is a component that is bought or excluded — the absence of a plan is correct.
The second is work nobody has planned yet. They must never look alike, because
the second must never be costed as though it were the first. An unresolved or
geometry-less component is a third answer again: *cannot be planned yet*, with
the reason.

### Traceable from the assembly down to the setup

```text
assembly → component → feature → operation → setup

HOUSING/OP-001   milling   features=['HOUSING/PL-001']   setup=HOUSING/SETUP-001
HOUSING/OP-002   milling   features=['HOUSING/PL-002']   setup=HOUSING/SETUP-001
COVER/OP-001     milling   features=['COVER/PL-001']     setup=COVER/SETUP-001
PIN/OP-001       turning   features=['PIN/CYL-001']      setup=PIN/SETUP-001
```

Every id is namespaced by its component, so two components cannot both call
their first operation `OP-001` and an operation always leads back to the
geometry that caused it. Recognition runs **per component** on that component's
own shape — never on the assembly's aggregate — so a housing's holes are never
attributed to a shaft.

### Editing a component's plan

Selecting a manufactured component opens it:

```text
HOUSING                            MANUFACTURED  GENERATED

Quantity                  ×2 in this assembly · one plan, reused
Features                  6
Operations                6
Setups                    1
Setup time                10 min, charged once for the setup
Cycle time                1.84 min per part
Component cost            ₹20,537.65 each · ₹41,075.31 for 2

Manufacturing plan
Setup 1 — Setup 1
  Setup time: 10 min (system estimate, accepted) · charged once for this setup
  • Mill plane 40 × 20 mm area 800 mm² — 0.17 min (system estimate, accepted) · HOUSING/PL-001
  • Mill plane 60 × 40 mm area 2400 mm² — 0.5 min (system estimate, accepted) · HOUSING/PL-005

[ Edit this component's plan ]
```

**Edit this component's plan** opens it in the *existing* operations table with a
banner naming the component, so all twelve editing operations — accept, reject,
change process, change times, add, remove, create and remove a setup, move an
operation between setups, change a setup's time, reorder both — work exactly as
they do for a single part. They are not reimplemented: `plan.js` owns the one
implementation of "which time counts", and a second copy would drift from it.

Setup and cycle time are always reported **apart**. One is charged per setup and
the other per part, so a combined figure cannot be multiplied by anything
correctly. And a plan with nothing committed shows its estimate *labelled as an
estimate* — its committed time is a genuine `0.00`, which would read as "this
component takes no time to make".

### One plan per definition, not per occurrence

A housing placed twice is planned **once**. Its per-part cost is multiplied by
the occurrence count in the roll-up, and by the production quantity exactly once
after that:

```text
HOUSING plan: 6 operations in 1 setup of 10 min
reported setup time: 10 min          (not 60 — setup is charged once per setup)

 1 assembly:  ×2 = 2 housings    ₹64,585.63
10 assemblies: 2 × 10 = 20       ₹6,45,856.30
                                 setup time still 10 min
```

### An occurrence count is not a production quantity

A component's quantity is how many times *this assembly* uses it. How many
assemblies you are building is a different number that no CAD file contains, and
the two are multiplied exactly once:

```text
Bearing, used x4 in the assembly · building 10 assemblies · = 40 bearings

per assembly     250.00 x 4  = 1,000.00
10 assemblies    250.00 x 40 = 10,000.00
```

The definition stays one reusable geometry however many are built.

### Currencies are reported, never converted

A purchased or standard cost must state its currency. If an assembly ends up
with components in more than one, the total is reported as **not directly
aggregatable**, the figures cover the base currency only, and the rest are
listed with the reason. Nothing is converted.

### What this looks like on a real assembly

`Figure 3.stp` — 198 MB, 43 definitions, 102 occurrences — carries no
transferable geometry for any of its components. So all 36 part definitions come
back as `unresolved` with the reason, and:

```text
total: none      complete: false
None of this assembly's 36 component definitions could be costed, so there is
no total to show. Nothing is reported as zero.
```

The structure, the names, the quantities and the placements are all recovered
and not one component can be measured. Both halves are reported. Classification
is what makes that file costable at all: mark a component purchased, state its
unit cost, and it contributes — a component nothing can measure can still be
priced, and the roll-up then covers exactly the components someone has priced
and says so.

### In the browser

The assembly panel shows a **tree** of occurrences — each row saying whether it
is a part or a sub-assembly, which definition it is, how many times that
definition is used, and which of those placements this row is — above a bill of
materials with name, part number, type, quantity, geometry status and volume.
Clicking either selects and isolates that component in the 3D viewer; **Show
all components** puts them back.

Beneath it, the **component classification** panel gives each part a
classification, and asks only for what that classification needs: a material for
a manufactured part, a unit cost and supplier for a purchased one, a unit cost
for a standard item, a reason for an excluded one. A row says whether its
classification came from the system or from you, and names what you changed it
from. Selecting a component opens a detail panel whose fields follow its kind —
a purchased part has a supplier and no operations; a manufactured one has
geometry and features and no supplier.

The assembly is drawn as one geometry per definition and one mesh per
occurrence, so a repeated part is uploaded to the GPU once, and both selection
and explosion are changes of transform. A single part shows none of this: no
tree, no bill of materials and no explode slider.

### Assembly endpoints

```text
POST /cad/assembly            decompose: definitions, occurrences, hierarchy,
                              quantities, placements, per-component geometry,
                              a display-only exploded arrangement, and a
                              suggested costing classification per component
                              (?measure=false for structure alone,
                               ?with_mesh=true for a selectable viewer mesh)
POST /cad/assembly/features   recognize features on one component
GET  /feedback/summary        where engineers disagree with the system, in
                              aggregate; every rate carries its counts and the
                              sample it rests on
GET  /feedback/operations     the system -> engineer confusion matrix and each
                              suggestion rule's hit rate
GET  /feedback/times          calibration per process and per profile version,
                              with outliers listed apart
GET  /feedback/setups         how often the fixturing assumption was changed
GET  /feedback/classifications  which make/buy suggestions were changed, and to what
GET  /feedback/confidence     empirical acceptance by confidence band, and
                              B-Rep against faceted
GET  /feedback/evidence       how complete the recorded evidence is per field,
                              and what can never be completed
GET  /feedback/records        the individual records behind a statistic,
                              filtered and paginated
POST /cad/assembly/plans      a manufacturing plan per manufactured component:
                              its own features, suggestions, times and setups
                              (components=[...] carries the classifications)
POST /cost/assembly           roll each component's contribution up by
                              classification, with the uncosted listed
                              (components=[...] carries the classifications and
                               unit costs; component_plans={...} each component's
                               own plan; assembly_quantity=N the production run)
GET  /estimates/{id}/quotation preview a quotation from that estimate's stored
                              snapshots (?view=customer|engineering)
POST /estimates/{id}/quotation issue one, recording its metadata and totals
GET  /quotations/{number}     an issued quotation, re-rendered from the
                              estimate it presents
```

Decomposition is a separate endpoint because it costs a different order of
magnitude: identifying the 198 MB export takes about 1.4 seconds and reading its
structure about 20, so the expensive answer is given when it is asked for and
never as a side effect of an upload.

## Why did this change?

The same CAD file can produce very different costs, because a lot of inputs feed it: a rate set
version, a time profile version, the material, the quantity, the operations and their times, the
setup grouping, the additional costs. An estimate that cannot say which of those moved is not
trustworthy, however correct its arithmetic.

Every estimate therefore carries two things beside the number.

**The assumptions it rested on**, read back from its own snapshots rather than recomputed:

```text
CAD                model1_1.stp · brep · 220,621.078 mm³ · 1.7319 kg per part
Material           Steel · 7850 kg/m³ · ₹0.80/kg          [Illustrative]
Costing profile    POC Default v1.0.0 · INR               [Illustrative]
Time profile       Live Shop Machining v2.0.0             [Real]
Plan               qty 10 · 1 setup · 6 costed operations · 10 min setup · 8.28 min cycle/part
Additional         overhead 0 % · scrap 0 % · tooling ₹0
```

Profile versions are links: open the exact rates or feeds that produced the number, even after a
newer version has superseded them.

**An account of why it differs from the previous estimate:**

```text
₹9,767.11 → ₹24,633.34   +₹14,866.23 (+152.2%)

More than one input changed, so no single one explains the difference. …
Changed: the operations, the quantity, the setup grouping.

Cost drivers
+₹24.68        Material mass × quantity   mass × quantity (kg) 17.3188 → 43.2969
+₹12,020.67    Operation time             run minutes 168.6 → 446
+₹500.00       Setup time                 setup minutes 20 → 35
+₹1,568.17     Overhead                   overhead unchanged at 12.5; it applies to a different base
+₹752.72       Scrap allowance            …
```

### How the decomposition works, and why it can be trusted

Almost every component of the cost is `rate × driver` — a price per kg times a mass, a rate per
hour times a number of hours. When both sides move, the change splits **exactly** three ways:

```text
delta         = r_a·d_a − r_b·d_b
rate effect   = (r_a − r_b)·d_b
driver effect = r_b·(d_a − d_b)
interaction   = (r_a − r_b)·(d_a − d_b)
```

Those three sum to the delta with no remainder. That is an algebraic identity, not a model:
nothing is apportioned by judgement, and when only one factor moved the interaction is zero and
the attribution is exact.

Three consequences worth stating:

- **The interaction is its own line.** The part of a change that required *both* factors to move
  appears as `Combined effect` rather than being credited to either. Folding it into one side would
  silently overstate that side's blame.
- **The parts always sum to the whole.** If they ever fail to, the remainder is reported — as
  `Rounding` when it is pennies (every stored amount is rounded to two decimals) and as
  `Unexplained` when it is more. A half-explanation that looks complete is worse than a stated gap.
- **Attribution counts inputs, not fields.** Adding a setup moves both the setup count and the
  total setup minutes; calling that "multiple inputs changed" would be technically true and
  practically misleading. Changing the *costing profile* moves the material price, the machine
  rates and the overhead percentages at once, because it supplies all of them — that is still one
  input. When genuinely separate inputs move together the answer says
  `multiple_inputs`, names them, and shows every before/after **without inventing causal
  percentages**.

**Cost-driver categories**: `material_rate`, `material_quantity`, `operation_rate`,
`operation_time`, `operation_added`, `operation_removed`, `setup_rate`, `setup_time`, `overhead`,
`scrap`, `tooling`, `combined`, `rounding`, `unexplained`.

### Sequence, stated rather than hidden

Reordering operations or setups is reported as a change with a cost impact of exactly zero:

```text
Sequence changed.
Cost impact: ₹0
Reason: the current costing model has no sequence-dependent cost.
```

Setup *membership* does change cost, and is reported separately. Saying so plainly is the point —
the limitation is easier to work with than a silence.

### Currencies are never subtracted

Two estimates in different currencies are never converted, so their money cannot be subtracted.
The comparison says so and omits the cost impacts; the input differences are still shown, because
those are still meaningful.

### Comparison endpoints

```text
GET /estimates/{id}/assumptions                  what one estimate rested on
GET /estimates/compare?baseline=A&comparison=B   totals, assumptions, changes,
                                                 cost drivers, operations, sequence
                                                 (add &detail=false for totals only)
```

Both read entirely from what each estimate stored. **Nothing is recalculated**, so publishing a
newer rate set or time profile can never change a past comparison — and the browser computes no
deltas of its own, because a plausible-looking delta invented in JavaScript would be a very
convincing wrong answer.

## Limits and configuration

Six separate budgets, because they answer different questions. In particular **a model too
complex to draw is still costable**: the viewer's limit never decides whether a part can be
priced.

| Setting | Default | What it bounds |
|---|---|---|
| `CAD_COSTING_MAX_UPLOAD_MB` | 500 | how large a file the server accepts |
| `CAD_COSTING_MAX_BREP_FACES` | 20,000 | B-Rep complexity feature recognition will analyse |
| `CAD_COSTING_MAX_FACETS` | 200,000 | polygons *feature reconstruction* will analyse |
| `CAD_COSTING_MAX_GEOMETRY_FACETS` | 1,000,000 | polygons *volume extraction* will analyse |
| `CAD_COSTING_MAX_VIEWER_TRIANGLES` | 80,000 | mesh size the browser is asked to **render** |
| `CAD_COSTING_MAX_VIEWER_INPUT_TRIANGLES` | 2,000,000 | mesh size the viewer will **read** before giving up |

Exceeding the viewer limit degrades the preview only; exceeding a recognition limit degrades
feature analysis only. Neither blocks geometry, mass or cost. The geometry budget is far higher
than the feature budget on purpose: deriving a volume is one weld and one closure scan, while
reconstructing features fits geometry to every body, so a model can be well past the feature limit
and still perfectly costable.

The last two are the two ends of one question, and the gap between them is the decimation's
working room. Being over `MAX_VIEWER_TRIANGLES` is not a refusal — the preview is reduced to fit
it, while the full-resolution mesh goes on being measured and costed. `MAX_VIEWER_INPUT_TRIANGLES`
is what bounds the memory that reduction may use, and only passing *that* costs you the preview.

### The real Creo exports

`Figure 3.stp` and `Figure 4.stp` are the same 198 MB export made twice: identical polygon counts,
identical welded topology, identical everything that matters. Both are faceted, in metres, with
`FACETED_BREP` over `OPEN_SHELL`.

Welded and repaired, they give 100 connected components of which **68 are watertight** (32% of the
polygons) and **32 are not**.

**These openings are not cracks.** That was worth checking rather than assuming, and the check
disproved it:

- only **120 of 9,039** boundary edges (1.3%) have another boundary vertex lying on them, so
  T-junctions are not the cause;
- the nearest unrelated boundary vertex is typically 0.25 mm away, so there is no "other side of a
  crack" to weld to — and widening the weld tolerance across four orders of magnitude changes
  nothing except, eventually, merging geometry that is genuinely separate;
- of 1,687 boundary chains, **1,359 are three-vertex closed loops** — the exact footprint of one
  absent triangle.

So the export is simply **incomplete**. Some polygons were never written. The 1,346 that are
uniquely recoverable are restored (see above), which closes five more components. What remains are
openings far too large to be one missing facet — some are perfectly planar 44 mm loops, i.e. whole
absent faces, and one is a 356-vertex *non-planar* boundary — plus 66 edges where three or four
polygons meet.

The part's volume is therefore **not in these files**, and no amount of repair can put it there.
Rather than stopping, the system says so and asks you for the missing figure — see *Supplying
geometry the file does not contain* below. A faceted export that *is* watertight costs normally
with no input at all.

An STL upload returns `400` with an explanation. Unreadable or empty content returns `400`; a
model beyond the analysis budget returns `400` rather than hanging.

### `POST /cad/mass`

Extracts CAD geometry (STL or STEP, via `/cad/geometry`'s logic) and converts its volume into a mass using a material's density from the existing material dataset:

```
mass_kg = (source_volume * (meters_per_unit ** 3)) * material.density_kg_m3
```

> **Unit handling depends on the format.** STL has no embedded unit metadata, so you must state the length unit the file was modeled in via `source_unit`: `mm`, `cm`, `m`, or `inch`. STEP *does* embed unit metadata, which the reader already honoured by normalizing the geometry to millimetres — so a STEP upload must pass `source_unit=mm`, and a conflicting unit is **rejected with a 400** rather than silently mis-scaling the mass by the cube of the discrepancy.
>
> Volume conversion is **cubic**, not linear — e.g. mm³→m³ uses `(0.001) ** 3`, not `0.001`. This mass is **per part** — it is not multiplied by any production quantity.
>
> This endpoint calculates mass only. It does **not** calculate cost.

Request: `multipart/form-data` with `file` (STL), `material`, and `source_unit`.

```bash
curl -F "file=@cube.stl" -F "material=Steel" -F "source_unit=mm" http://127.0.0.1:8000/cad/mass
```

Response (for a 10×20×30 mm Steel cube):

```json
{
  "source_volume": 6000.0,
  "source_unit": "mm",
  "detected_unit": null,
  "cubic_conversion_factor_to_m3": 1e-9,
  "volume_m3": 6e-6,
  "material": {"name": "Steel", "density_kg_m3": 7850.0, "cost_per_kg": 0.8, "illustrative": true},
  "mass_kg": 0.0471,
  "formula": "mass_kg = (source_volume * (meters_per_unit ** 3)) * material.density_kg_m3",
  "is_illustrative_material": true
}
```

An unknown material, an unsupported `source_unit` (only `mm`/`cm`/`m`/`inch` are supported), a `source_unit` conflicting with a STEP file's detected unit, an empty file, or invalid CAD content returns `400`.

### `POST /cost/cad-part`

The CAD-driven counterpart to `/cost/part`. Instead of a manually supplied `mass_kg`, it accepts an **STL or STEP** upload and derives mass from geometry + material density (via the same calculation as `/cad/mass`), then reuses the existing material, operation, and part-level costing calculations unchanged.

```
CAD file (STL|STEP) → geometry (volume) → mass (volume × density) → existing material costing → existing part-level costing
```

The costing stages are entirely format-agnostic: the same box uploaded as STL and as STEP yields an identical `total_part_cost`.

> **Quantity semantics**: the CAD-derived mass is the mass of a *single* part. It is passed into `Part.mass_kg` as-is; `Part.quantity` is applied exactly once, inside the existing material costing calculation (`total_material_cost = mass_kg * material_cost_per_kg * quantity`) — the same way it already works for a manually supplied mass. This endpoint never multiplies mass by quantity itself, to avoid double-counting.

Request: `multipart/form-data` with `file` (STL or STEP), `material`, `source_unit`, `part_id`, `part_name`, `quantity`, an optional `operations` field (a JSON-encoded list of the same operation objects `/cost/operation` accepts; defaults to `[]`), and — mirroring `/cost/part` — the optional `operation_requirements` (JSON-encoded), `rate_set_id`, `rate_set_version`, `overhead_rate_percent`, `scrap_rate_percent`, and `tooling_cost_per_batch` fields. The three additional-cost fields are unset by default rather than `0.0`, so they cannot silently override a selected rate set; supplying **any** of them replaces the rate set's additional costs wholesale.

```bash
curl -F "file=@cube.stl" \
     -F "material=Steel" -F "source_unit=mm" \
     -F "part_id=P001" -F "part_name=Disc" -F "quantity=10" \
     -F 'operations=[{"operation_id":"OP1","operation_name":"Cutting","process_type":"Cutting","machine_rate_per_hour":1800,"labor_rate_per_hour":500,"setup_time_minutes":20,"cycle_time_minutes":2,"quantity":10}]' \
     http://127.0.0.1:8000/cost/cad-part
```

Response (abbreviated — `geometry` is a full `/cad/geometry`-style breakdown, `mass` a full `/cad/mass`-style breakdown, and `part_cost` a full `/cost/part`-style breakdown):

```json
{
  "geometry": { "volume": 6000.0, "...": "see /cad/geometry response shape" },
  "mass": { "mass_kg": 0.0471, "...": "see /cad/mass response shape" },
  "part_cost": {
    "material": { "mass_kg": 0.0471, "total_material_cost": 0.3768, "...": "see /cost/part response shape" },
    "operations": [ { "total_operation_cost": 1366.67, "...": "..." } ],
    "total_material_cost": 0.3768,
    "total_operation_cost": 1366.67,
    "total_part_cost": 1367.04
  }
}
```

`/cost/part` is unchanged and continues to accept a manually supplied `mass_kg` for callers without a CAD file. Because this endpoint takes multipart form data rather than a JSON body, all invalid input (unknown material, unsupported unit, empty/invalid STL, malformed `operations` JSON, operation/part quantity mismatch, or invalid part/operation field values) returns `400` rather than the `422` that `/cost/part` returns for equivalent JSON-body validation errors.

No feature recognition, machining-time inference, or automatic process/operation selection exists — `operations` for `/cost/cad-part` must still be supplied explicitly by the caller, exactly as for `/cost/part`.
