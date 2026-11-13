# Project Status

Last updated: 2026-11-13 (Iteration 7 — wall thickness, setups from tool access, sheet-metal unfolding)

`CHECKPOINT.md` is a point-in-time handoff written at the Step 30 pause and is not
updated in place; this file is the step-by-step record.

## Current stage

**Iteration 7: the first three *large* gaps from `APRIORI_GAP_ANALYSIS.md` — each needed new
geometric measurement on the exact B-Rep** — measured wall thickness (and a minimum-wall DFM rule),
setups proposed from tool approach directions with a ray access check, and sheet-metal unfolding
with cut and bend times priceable by time. Built by three agents, reviewed and merged. Details in
*Iteration 7* below.

**Iteration 6: three more gaps from `APRIORI_GAP_ANALYSIS.md`, built by three agents, reviewed
and merged** — a stage's pattern/die charge amortised over a tool life the engineer states, a
cost-driver breakdown of any stored estimate, and weld preparation and finishing timed from the
typed weld. The review also found and fixed a double charge merged in Iteration 5 (tacking).
Details in *Iteration 6* below.

**Iteration 5: five small gaps from `APRIORI_GAP_ANALYSIS.md`, built in parallel by separate
agents, reviewed and merged** — a yield on the casting/forging stage, a matrix costing preview
(rate sets x quantities), supplier quotes compared with the should-cost, per-weld handling
time, and DFM flags pointed at on the 3D model. None adds a second pricing path, a default
figure, a ranking or a severity. On branch `timed-welding`. Details in *Iteration 5* below.

**Iteration 4: three features built in parallel by separate agents and merged** — route
comparison, advisory DFM flags, and a bulk costing preview. Route comparison and bulk
preview store nothing and price only through the one pricing path, `price_cad_part`, so a
preview cannot disagree with the estimate it previews; DFM flags never touch a cost. On
branch `iterations-1-3`; `main` is unchanged. Details in *Iteration 4* below.

**Iteration 3: a decided route's casting, forging, sheet, finishing or purchase stages are
priced from rates a person publishes in the rate set**, instead of the whole route being
refused. A stage rate is per kg or per piece, plus an optional one-off batch charge; a per-kg
stage is charged on a mass someone enters, never on the CAD mass. Every stage is priced or
none is. A route that starts by casting, forging, moulding, building or buying the part does
not charge the material again. No seeded rate set has stage rates, so nothing is priced
until real ones are published. Details in *Iteration 3* below. A pre-existing defect found on
the way — the rate-set editor broke on opening, since Step 29 — is fixed.

**Iteration 2: a part's manufacturing family and process route are recommended from
its recognized features, and an engineer confirms, overrides or marks them unknown.**
The recommendation is never more than `medium` confidence, names the features and
signals it stood on, lists the families it is equally consistent with, and says what
information would settle it. Casting, forging, moulding, welding, additive and
purchased are never proposed from geometry, because nothing here can detect evidence
for them; they can be chosen. A route containing a stage the existing costing cannot
price (anything but milling, drilling and turning) is refused by `/cost/cad-part` with
a 422 rather than priced as machining, and so is a route marked unknown. Sending no
classification changes nothing. Details in *Iteration 2* below.

**Iteration 1: a client's actual cost can be recorded against an estimate and the
variance reported.** This is the first place the system's figures meet a number it
did not produce. Only the total is required; a breakdown category left out is shown
as *not supplied*, never as zero. A per-batch figure without its batch quantity is
refused rather than assumed to be one unit, a different currency is refused rather
than converted, and an actual is never edited — a correction is a new record that
names the one it supersedes. An assembly is compared on its total only, and an
incomplete assembly reports no variance at all. Nothing in costing reads an actual,
and recording one changes no estimate. Details in *Iteration 1* below.

Iteration 0 (2026-10-19) was an audit with no code change: baseline 1760 passed,
1 skipped; the real database at Alembic `0005` with 137 estimates.

UI restructuring, ahead of a live demo (not a roadmap step): the estimate page is six
sections in the order the work happens (Overview, CAD, Manufacturing, Cost, Assembly,
Quote). Details in *Presentation* below.

Step 30: the evidence behind the feedback dataset is now measured honestly, and
the one place it was genuinely being lost has been fixed. Step 29's readiness
report named three gaps; measuring the real 127-estimate database showed **two of
them were not gaps**. All 449 records "missing" a component identity came from
single-part estimates, where a component identity does not exist, and 302 of the
308 "missing" paired cycle times were operations nobody ever finished — which
Step 29 itself requires to stay unpaired. So each evidence field is now
classified as `known`, `not_applicable`, `unrecorded` or `unrecoverable` rather
than merely counted.

The one real gap: the operations table builds its plan payload in the browser, so
the recognizer's **feature confidence** never reached the server even though the
backend was recording it correctly. Fixed going forward; the 712 historical
records are reported as permanently unrecoverable, because the CAD file is not
stored and nothing can re-derive them. **No value was invented and no unknown was
converted into a guess.** Still no learning of any kind.

Step 29: the corrections four earlier steps recorded are finally read back. A
**Feedback** view reports where engineers disagree with the system — which
processes they change, which times they correct and in which direction, how often
they regroup a setup, which make/buy suggestions they overrule, and how each
suggestion rule fares. Measured over this project's own 127 stored estimates:
713 reviewed operations, **91% accepted unchanged** over the 411 someone actually
decided about, with the 302 never finished reported separately rather than
counted as rejections.

**Nothing learns.** No model is fitted, no rule changed, no confidence
recalibrated, no profile adjusted. `feedback_analytics.py` imports nothing from
the rest of the application at all, so it cannot reach a rule or a profile to
change one — asserted structurally and again by running every report and checking
the whole database is byte-identical afterwards.

Step 28: every **manufactured** assembly component now owns a complete,
editable, traceable manufacturing plan — its own features, its own suggested
operations, its own estimated times, its own setups, its own sequence — and that
plan feeds the unchanged costing pipeline. There is deliberately **no
assembly-level plan**: a setup is one physical fixturing of one part, so two
components never share one however alike their processes look, and nothing here
knows what order components are made in. A bought or excluded component has no
machining plan, which is a different answer from "nobody has planned it yet",
which is different again from "cannot be planned yet"; collapsing the first two
would let unplanned work be costed as no work.

Step 27: a verified estimate becomes a **printable, shareable quotation** — a
standalone report page that prints to PDF through the browser's own dialog, with
no PDF library and, more importantly, **no second costing path**. The quotation
layer imports the assumptions reader and the money policy and nothing else; it
regroups figures the estimate already stored, checks that its cost lines account
for the stored total, and reports any remainder rather than absorbing it. A
supplied volume is labelled an assumption in those words and the phrase "CAD
volume" never appears near it. An estimate that must not become a customer-facing
document — an incomplete assembly, mixed currencies, unresolved geometry —
produces a stated refusal instead of a misleading partial total.

Step 26: an assembly is no longer a bag of identical machined parts. Every
component is **manufactured, purchased, standard, excluded or unresolved**, and
that decides entirely where its cost comes from — the unchanged pipeline, a unit
cost a person stated, or nowhere. No CAD format states make/buy, and nothing here
pretends otherwise: the system's only suggestion is that the file carries
measurable geometry, offered at low confidence with its own caveat, and the
engineer's answer replaces it for costing while the suggestion stays on the
record. A component nobody could price is `needs_costing_input` with the reason,
never a zero; the one legitimate zero is a component a person deliberately
excluded.

Step 25: an assembly can be pulled apart on screen. A slider separates its components into a
deterministic, hierarchy-aware **visual exploded arrangement** — sub-assemblies moving as rigid
units so the parts inside them keep their positions relative to each other — with component
selection, isolation and a full component tree beside it. It is display-only by construction: the
CAD placement is never overwritten, an exploded position is never persisted, the module that
computes it imports nothing from the rest of the application, and nothing but the viewer reads what
it produces. It is **not** an assembly or disassembly sequence and is never described as one.

Step 24: an assembly is now represented **as an assembly**. A STEP file's product structure is read
into definitions, occurrences, a hierarchy, quantities and placements; each component is measured,
analysed and costed in its own right; and the assembly total is the sum of component cost x
quantity, with every component that could not be costed listed with its reason rather than counted
as zero. On the real 198 MB Creo export that means 43 definitions, 102 occurrences and four levels
recovered in full — and **no total at all**, because not one of its components carries geometry the
reader can transfer.

Step 23: CAD ingestion goes through **one import layer** with an adapter per format, and every
format now reports **what it can honestly do** rather than merely being accepted. **IGES is
genuinely supported** — geometry, mass, viewer and costing identical to STEP, with feature
recognition working from a solid-mode export and explicitly degraded from a surface-mode one.
Native Creo `.prt`/`.asm`/`.drw` are recognized and **refused with an explanation**; no parser was
fabricated, and the architecture is in place for a future licensed one.

Step 22: an estimate now explains itself. Every result carries the **assumptions it rested on** — CAD, material, the costing profile version, the time profile version, the plan, the additional costs — read back from its own snapshots rather than recomputed. And when a number changes, a **cost-driver decomposition** says which factor moved it and by how much, with the parts guaranteed to sum to the whole. Where several inputs moved together it says so and shows every before/after instead of inventing causal percentages.

Step 21b: the manufacturing plan is now organised into **setups**, and setup time is charged **once per setup** instead of once per operation. That was a real costing error, not a refinement: on `model1_1.stp` the old behaviour would have charged 280 minutes of setup across 31 operations where the part needs 10. A setup is a physical fixturing context with explicit membership, its own time and its own place in the routing; the system groups conservatively and marks the grouping assumed at low confidence, and the engineer regroups at will with the cost impact shown live.

> **Numbering note.** Two different steps were both requested as "Step 21". The user-configurable time profiles are recorded above as Step 21; this setup-aware plan is recorded as Step 21b. Nothing from either was undone.

Step 21: the machining assumptions behind every estimated time are now **user-configurable, versioned and immutable** — a *time profile*, the exact counterpart of a rate set and deliberately separate from it, because one decides money and the other decides duration. All 21 parameters the estimator reads are editable in a **Time setup** screen, so there are no hidden constants. Every predicted time records which profile and version produced it and the parameter values that entered the calculation, and every estimate snapshots the profile whole — so publishing a revised version changes future estimates and leaves historical ones exactly as they were.

Step 20: the app can now **estimate how long an operation takes**, from a deterministic parameter model rather than a guess — and the estimate is deliberately hard to mistake for a commitment. Every time reports the formula, the geometry it consumed and every parameter that affected it; it is capped at medium confidence; and it does not reach the cost until a person accepts it or types their own. Operations, times and order are edited in one **manufacturing plan**, and every estimate persists what the system predicted beside what the engineer chose.

Step 19: recognized features are now turned into **suggested manufacturing operations** — a through hole suggests drilling, an external cylinder suggests turning, a pocket suggests milling — and each suggestion names the CAD feature that caused it, the rule that fired and the evidence it stood on. It suggests; it does not decide and it does not price. No suggestion carries a time or a rate, and an accepted one reaches the estimate only once a human types a cycle time. Features the geometry cannot support an operation for are listed with the reason rather than guessed at.

Step 18.9: a model too dense for a browser is **reduced for display instead of refused**. The full-resolution mesh is still what gets measured and costed; a separate display-only copy is decimated server-side by deterministic vertex clustering and sent to the viewer, which shows the original count, the display count and the fact that it was simplified. The real 198 MB `Figure 3.stp` now draws at **272,113 → 79,273 triangles** with no error, and its geometry and cost are identical to what they were before any simplification existed.

Step 18.8: when a CAD file does not contain enough geometry to measure a volume, the system now **asks the user for the missing figure** instead of refusing. The real Creo exports are costable this way: the assumption is applied verbatim, labelled as an assumption rather than a measurement, and persisted on the estimate. Nothing is inferred — no bounding box, no hull, no guessed thickness.

Step 18.7: faceted geometry is **repaired where the repair is uniquely determined** — a surface missing one facet leaves a three-edge hole that exactly one triangle can fill, so it is restored and reported. Investigation disproved the earlier assumption that the real Creo files' openings were tessellation cracks: they are simply missing polygons, and the larger ones cannot be recovered without inventing surface. Those two files remain uncostable, by design rather than by omission.

Step 18.6: a faceted STEP export is **measured and costed from its polygons** when they form a closed solid — welded, split into connected components, and checked for watertightness and consistent winding before any volume is reported. Analytic files still use exact B-Rep mass properties. Geometry and mass no longer depend on feature recognition, and a volume is never invented: if any component is open, nothing is reported and the counts explain why.

Step 18.5: feature recognition handles **both kinds of STEP export**. An analytic B-Rep is read as before; a **faceted** export — flat polygons with no exact surfaces — is detected and its holes, shafts and flat regions are reconstructed by fitting, producing the same feature objects marked `source_type: faceted`. What cannot be recovered reliably from polygons is stated rather than guessed. Upload, analysis and viewer budgets are now separate and configurable, so a model too complex to draw is still costable.

Step 18: a STEP model can be **analysed into structured features** — holes with diameters and through/blind classification, counterbores, countersinks, pockets, slots, cylinders, fillets, chamfers and planes — each with its dimensions, its confidence, the geometric evidence that identified it, and the CAD face numbers it came from. It is deterministic and explainable: no ML, no AI, no randomness. It branches off geometry and **changes nothing about costing**, and it deliberately does not turn features into manufacturing operations yet.

Step 17: costing rates are configured **entirely from the browser**. A **Costing setup** tab publishes immutable, versioned costing profiles — materials with their own densities and prices, machine and labour rates for any process a shop runs, and overhead/scrap/tooling — and the estimate page costs a part against the profile version you pick, recording that version permanently on the estimate. Changing rates means publishing a new version, which copies the last one; nothing already published can be edited. Entering real rates no longer requires the API or hand-written JSON.

Step 16: the browser UI has an **interactive 3D viewer**. Open <http://127.0.0.1:8000/>, drop in a STEP/STL file, and the part is rendered next to the costing inputs — rotate, pan, zoom, fit and reset it — with its geometry described alongside; fill in the details, click Calculate and get an INR estimate, with a history view that opens and compares past estimates. STEP is tessellated server-side by OpenCASCADE, so the browser never parses CAD, and the viewer is visualization only: it does not affect costing. Still plain static files with no build step. Still not implemented: `.f3d`, Creo native files, feature recognition, machining-time inference, AI, authentication, and any deployment story.

## What has been done

### Step 1 — foundation
- Chose the technology stack (Python 3.11+, FastAPI, Uvicorn, pytest) — see `ARCHITECTURE.md`.
- Created the basic project structure and `pyproject.toml`.
- Added a minimal `GET /health` endpoint.

### Step 2 — Part → Material → Material Cost
- `Part` and `Material` domain models; illustrative material dataset (`Steel`, `Aluminium`) with `density_kg_m3` and `cost_per_kg`.
- Deterministic `total_material_cost = mass_kg * material_cost_per_kg * quantity` (`costing.py`). `POST /cost/material` with tests.

### Step 3 — Part → Manufacturing Operation → Operation Cost
- `ManufacturingOperation` domain model. Deterministic machine/setup/labor cost and their sum (`operation_costing.py`). `POST /cost/operation` with tests.

### Step 4 — Part-Level Cost Estimate
- `PartCostEstimateRequest`/`Result` (`part_cost.py`) and `calculate_part_cost()`: `total_part_cost = total_material_cost + total_operation_cost`, reusing the existing material and operation calculations, with quantity-mismatch validation to avoid double-counting. `POST /cost/part` with tests.

### Step 5 — CAD geometry extraction (STL only, standalone)
- `cad_geometry.py`: `GeometryProperties`/`BoundingBoxDimensions` and `extract_geometry_from_bytes`/`extract_geometry_from_file`, using `trimesh` to read volume, surface area, and bounding box from an STL mesh, deterministically. `POST /cad/geometry` with tests. Explicitly not connected to costing at that point.

### Step 6 — CAD → mass → existing costing pipeline
- Added `cad_mass.py`: `LengthUnit` (`mm`, `cm`, `m`, `inch`), `GeometryMassResult`, and `calculate_mass_from_geometry(geometry, material_name, source_unit)`. This is the one module where geometry and material data meet.
  - **Formula**: `volume_m3 = source_volume * (meters_per_unit ** 3)`; `mass_kg = volume_m3 * material.density_kg_m3`.
  - **Unit handling**: STL has no embedded unit metadata, so `source_unit` must always be supplied explicitly by the caller (`mm`/`cm`/`m`/`inch`) — it is never assumed to be millimeters. Conversion is applied as a **cubic** factor (`meters_per_unit ** 3`), not a linear one, since volume scales with the cube of length.
  - **Quantity semantics**: the returned mass is for a single instance of the geometry (per part). It is not multiplied by any quantity here — quantity is applied exactly once, downstream, inside the existing, unmodified `calculate_material_cost`.
  - Looks up the material via the existing `materials.get_material` (unknown material still raises the same error as before).
- Added `cad_part_cost.py`: `CadPartCostEstimateResult` (`geometry`, `mass`, `part_cost`) and `calculate_cad_part_cost(...)`, which composes `calculate_mass_from_geometry` with the existing, unmodified `calculate_part_cost` — builds a `Part` from the CAD-derived mass and passes it straight into the existing part-costing pipeline. No costing formula was duplicated or changed.
- Added `POST /cad/mass` (STL upload + `material` + `source_unit` → `GeometryMassResult`; mass only, no cost) and `POST /cost/cad-part` (STL upload + `material` + `source_unit` + `part_id`/`part_name`/`quantity`/optional JSON `operations` → full geometry + mass + part-cost breakdown).
- `costing.py`, `operation_costing.py`, and `part_cost.py` were **not modified**. `Part` and its existing `/cost/part` and `/cost/material` JSON APIs are **unchanged** and continue to accept a manually supplied `mass_kg`.
- Added tests: cubic conversion factors for all four units, unsupported-unit rejection, zero/negative-volume rejection, unknown-material rejection, mass for the spec's 10×20×30 mm Steel cube example (≈0.0471 kg, computed from the actual `MATERIALS["steel"].density_kg_m3`, not a duplicated constant), and mass consistency across unit choices for the same physical size — plus API tests for `/cad/mass` (each unit, unknown material, unsupported unit, empty file, invalid STL) and `/cost/cad-part` (material only, with operations, and quantity scaling material cost while mass-per-part stays constant).
- Verified locally: `pytest` passes (74/74), and live sanity checks against a running server confirmed `/cad/mass` and `/cost/cad-part` produce the expected values (0.0471 kg mass; total material cost scaling linearly with quantity; `/cost/part` still working unchanged for manually supplied mass).
- Updated `README.md` and `ARCHITECTURE.md` to document the new modules/endpoints, the explicit unit-conversion design, and the per-part mass / quantity semantics.

### Step 7 — STEP support
- Evaluated the Python STEP/B-Rep options empirically in this venv before writing code: `pythonocc-core` is **not on PyPI** (conda-only, verified) so it cannot be used here; `gmsh` is pip-installable and lighter but **GPL**, an unacceptable licensing risk for a POC aimed at commercial Creo/Windchill integration; `cadquery`/`build123d` are modeling DSLs layered on OCP, carrying its weight plus an API this project does not need.
- **Selected `cadquery-ocp` (OpenCASCADE bindings)**: native `cp312`/`win_amd64` wheel, OCCT's mature STEP reader (AP203/AP214/AP242), analytic `BRepGProp` mass properties over the exact B-Rep (not a mesh approximation), and an OCCT licence (LGPL-2.1-style with linking exception; bindings Apache-2.0) that is commercially far safer than GPL.
- Refactored `cad_geometry.py` to dispatch on the declared file extension to a per-format reader (`trimesh` for `.stl`, OCCT for `.step`/`.stp`, canonicalized to `source_format: "step"`), returning the same `GeometryProperties` shape either way. Volume and surface area come from `BRepGProp`, the bounding box from `BRepBndLib.AddOptimal` (tight, not triangulation-padded).
- OCP is imported **lazily inside the STEP path** — it costs ~2.4 s and loads large native libraries, which STL-only callers should not pay at startup; a missing install surfaces as a clear error on STEP uploads rather than breaking the app.
- **Unit semantics resolved explicitly, not assumed.** STEP embeds unit metadata and OCCT honours it, converting geometry to millimetres on load — verified empirically by writing an INCH-unit STEP holding a 1-inch cube and reading back 25.4 mm / 16 387.064 mm³. `GeometryProperties.detected_unit` records this (`None` for STL, `"mm"` for STEP), and `calculate_mass_from_geometry` now **rejects** a conflicting `source_unit` instead of silently mis-scaling mass by 25.4³.
- `/cad/geometry`, `/cad/mass`, and `/cost/cad-part` accept STEP with **no endpoint logic changes** — they already consumed the geometry abstraction, so only descriptions were updated. No STEP-specific costing endpoint was added.
- `costing.py`, `operation_costing.py`, `part_cost.py`, `part.py`, `materials.py`, `operation.py`, and `cad_part_cost.py` were **not modified**; every costing formula is unchanged.
- Added 30 tests (74 → **104**): STEP volume/surface area/bounding box against an exactly-known box, `.stp` extension handling, file-path extraction, determinism, inch-authored-STEP unit conversion, invalid/empty/geometry-less STEP rejection, unsupported-format rejection, STEP-vs-STL mass equality, unit-conflict rejection, STEP-derived mass flowing into the existing costing pipeline, per-part mass under quantity scaling, and API coverage of all three endpoints with STEP. STEP fixtures are generated locally via OCCT's writer (no downloads), with a guard asserting the fixture really is ISO-10303-21 text.
- Updated two Step 5 tests that had used `.step` as their example of an *unsupported* format — now `.iges`, preserving their original intent.
- Verified: full suite 104/104 passing; live server checks of `/cad/geometry`, `/cad/mass`, and `/cost/cad-part` with a real 15 KB STEP file; STL and `/cost/part` regressions confirmed unchanged; STEP and STL uploads of the same box produce an identical `total_part_cost` (1367.0434666666667); zero temp-file leakage across 30 success/failure iterations.

### Step 8 — overhead, scrap allowance, and tooling
- Added `additional_costs.py`: `AdditionalCostInputs` (overhead rate %, scrap rate %, tooling cost per batch), `AdditionalCostsResult`, and pure deterministic `calculate_additional_costs(direct_cost_base, inputs)`. Kept as its own module so this policy can change without touching material or operation costing.
- **Formulas** — `direct_cost_base = total_material_cost + total_operation_cost`; `overhead_cost = overhead_rate_percent / 100 * direct_cost_base`; `scrap_cost = scrap_rate_percent / 100 * direct_cost_base`; `tooling_cost = tooling_cost_per_batch`; `total_additional_cost = overhead_cost + scrap_cost + tooling_cost`; `total_part_cost = direct_cost_base + total_additional_cost`.
- **No compounding**: both percentages apply to the *same* base, which excludes overhead, scrap, and tooling — so overhead is never charged on scrap, scrap never on overhead, and neither on tooling. Covered by explicit tests.
- **Quantity semantics**: all three components are batch-level. The percentages inherit batch scaling from `direct_cost_base` (already quantity-scaled), and `tooling_cost_per_batch` is a one-time batch cost like operation setup cost. Neither is re-scaled by quantity.
- **Validation bounds**: `overhead_rate_percent` ∈ [0, 1000] (pragmatic guard against a fraction being passed where a percent was meant), `scrap_rate_percent` ∈ [0, 100) (principled — at 100% scrap no good parts are produced), `tooling_cost_per_batch` ≥ 0. `calculate_additional_costs` also rejects a negative cost base.
- **Explainability**: the result exposes each component's amount and rate plus the formulas and assumptions used, so "why is the final part cost this amount?" is answerable from the response alone.
- Extended `PartCostEstimateRequest`/`Result` and `calculate_cad_part_cost`, and added three optional form fields to `/cost/cad-part`, keeping the JSON and CAD-driven paths at feature parity. `/cost/material`, `/cost/operation`, `/cad/geometry`, `/cad/mass` needed no changes; `costing.py`, `operation_costing.py`, `part.py`, `materials.py`, `operation.py`, `cad_geometry.py`, and `cad_mass.py` were **not modified**.
- Added 34 tests (104 → **138**): each component individually, all combined, non-compounding, tooling excluded from the percentage base, zero-valued inputs, negative values, scrap ≥ 100%, absurd overhead, negative cost base, batch-level quantity semantics, total equals the sum of exposed components, backwards compatibility with the field omitted, and API coverage of `/cost/part` and `/cost/cad-part` including validation failures.
- Includes a hand-checkable **worked example** (Steel 2.5 kg × qty 4 + a milling operation → direct base 968.00; overhead 10% = 96.80, scrap 5% = 48.40, tooling 250.00 → **total 1363.20**) asserted in tests, documented in `README.md`, and confirmed live.
- Verified: full suite 138/138 passing; live server reproduced the worked example exactly (1363.20), returned 968.00 when `additional_costs` was omitted, applied the components correctly on a STEP upload via `/cost/cad-part`, and rejected a 150% scrap rate (422 on the JSON endpoint, 400 on the multipart one). `/health`, `/cost/material`, and `/cad/geometry` regressions all unchanged.

### Step 9 — named, versioned rate sets
- Added `rate_sets.py`: `RateSet` (identified by `rate_set_id` + `version`), `MaterialRate`, `OperationRate`, `OperationRequirement`, `RateSetReference`, `RateSetSummary`, a static illustrative catalog, `list_rate_sets`/`get_rate_set`, and pure resolvers `resolve_material`/`resolve_operation`.
- **The rate layer calculates nothing.** Its resolvers build the existing `Material` and `ManufacturingOperation` input models, which the existing unmodified calculators consume. `operation_costing.py` and `additional_costs.py` were **not modified**; `costing.py` gained only an optional `material` parameter so a caller can inject an already-resolved material (omitting it preserves the original catalog lookup exactly).
- **Identity/versioning**: the catalog holds `poc-default` v1.0.0 plus two versions of `poc-machine-shop` (v1.0.0 and v2.0.0) so version identity is genuinely exercised. Resolving an id without a version selects the highest; the resolved version is always stamped onto the estimate. `get_rate_set` returns a deep copy, so the static catalog cannot be mutated by callers.
- **Precedence rule** — *explicit per-request values always win; the rate set supplies only what the request omits.* Material comes from the rate set when one is selected (error if unpriced), otherwise the built-in catalog; `operations[]` keep their own rates and are never overridden; `operation_requirements[]` resolve rates by process type and require a rate set; an explicit `additional_costs` object replaces the rate set's wholesale. Precedence keys off **structural presence, never a value**, so a defaulted `0.0` can never silently override a rate set — this is why `/cost/cad-part`'s three additional-cost form fields changed from `0.0` defaults to unset.
- **Why `OperationRequirement` exists**: times and quantity describe the part's process plan, rates describe the shop. Splitting them let `ManufacturingOperation` keep its rates strictly required, leaving `/cost/operation` and every existing test untouched.
- Added `GET /rates` and `GET /rates/{rate_set_id}` (optional `?version=`, 404 on unknown id/version), and rate-set selection on both `/cost/part` (JSON) and `/cost/cad-part` (form fields).
- **Provenance**: results carry a `rate_set` reference (id, name, version, illustrative flag) and a `rate_precedence` string. The rates *actually used* needed no new fields — they were already visible via `material.material_cost_per_kg`, each operation's machine/labor rates, and the additional-cost rates.
- **Persistence deliberately deferred**: the catalog is a static in-memory tuple, nothing is stored. Frozen models with stable ids and explicit versions mean a future SQLite/SQLAlchemy layer can store rows keyed by (`rate_set_id`, `version`) with `get_rate_set` as the single lookup seam to replace.
- All catalog values are marked **ILLUSTRATIVE POC VALUES ONLY** and were chosen to keep the existing worked example numerically stable — no invented realistic pricing.
- Added 80 tests (138 → **218**): rate-set model validation (ids, versions, negative rates, percentage bounds, frozen-ness), catalog behaviour (known/unknown id, unknown version, latest-version resolution, version identity, determinism, copy-on-read), resolution (case-insensitive material, unpriced material/process rejection, requirement validation parity), cost integration (worked example, provenance, resolved rates visible, multiple operations resolving different process rates, quantity semantics unchanged, additional-cost semantics unchanged, determinism), precedence, and full API coverage.
- Verified: full suite 218/218 passing with **zero regressions to the pre-existing 138**; live server confirmed `/rates` discovery, version pinning, the worked example resolving to **1363.20 through the rate set**, `/cost/cad-part` with a STEP file plus a rate set, unknown rate sets (400 on costing endpoints, 404 on discovery), and the legacy explicit-rate request still returning 1363.20 with `rate_set: null`.

### Step 10 — SQLite + SQLAlchemy persistence
- Added `db.py` (configurable `CAD_COSTING_DATABASE_URL`, default `sqlite:///./cad_costing_agent.db`; lazily-cached engine; `get_session` FastAPI dependency; `init_db` run from the app lifespan), `db_models.py` (`RateSetRow`, `EstimateRow`), and `repository.py` (`RateSetRepository`, `EstimateRepository`, `StoredEstimate`).
- **Kept the calculators pure.** `calculate_part_cost` used to look up its own rate set, which would have dragged a database dependency into a costing module. The dependency was inverted instead: the endpoint owns the session, resolves the `RateSet` via the repository, and injects it — `calculate_part_cost(request, rate_set=...)`, mirroring Step 9's `calculate_material_cost(part, material=...)`. A request naming a rate set with none supplied is now an explicit error rather than a silent lookup. No SQLAlchemy import exists in any costing module.
- **Schema**: `rate_sets` with a unique `(rate_set_id, version)` and the rate detail in one JSON column; `estimates` keyed by a uuid4 `estimate_id` with identity, the four totals, and three JSON snapshots. Rate detail is JSON because a version is immutable and always read/written whole — child tables would add joins and partial-update risk for nothing, while identity stays in real indexed columns.
- **Immutability is structural**: the unique constraint rejects duplicates and neither repository exposes an update method, so changed rates require a new version.
- **Seeding is idempotent** — `init_db` skips any `(rate_set_id, version)` already stored, so restarts never duplicate or overwrite. `SEED_RATE_SETS` is now explicitly seed data; the database is authoritative afterwards.
- **Audit snapshots** (`request_snapshot`, `rate_set_snapshot`, `result_snapshot`) make a stored estimate self-contained, so publishing a newer rate set version leaves historical estimates byte-identical — covered by a test.
- Added `GET /estimates/{estimate_id}` (404 on unknown); `/cost/part` and `/cost/cad-part` now return `estimate_id` alongside every pre-existing field.
- **Test isolation**: an autouse fixture gives each test its own freshly-seeded in-memory SQLite database (StaticPool) and overrides `get_session`, so estimates never leak between tests.
- Updated 13 domain tests in `test_rate_sets.py` to inject a rate set rather than rely on the calculator looking one up, and moved the catalog-lookup tests (unknown id/version, latest-version resolution, listing) into `test_persistence.py` where they now exercise the repository. Their assertions and intent are unchanged.
- Added `tests/test_persistence.py` (25 tests). Suite: 218 → **241 passing**, with every pre-existing endpoint still working.
- Verified live against a real uvicorn server: SQLite file created on startup, `/rates` served from the database, the worked example returning **1363.20** with an `estimate_id`, `/estimates/{id}` returning the full snapshots, a CAD-driven estimate persisted and retrieved, and — **after a server restart** — the earlier estimate still retrievable with its original timestamp while the rate set table stayed at 3 rows (proving seeding is idempotent across restarts).

### Step 11 — estimate history and comparison
- Added `estimates.py`, a **pure** module (no SQLAlchemy import): `EstimateSummary`, `EstimateListPage`, `CostDelta`, `EstimateComparison`, and `compare_estimates`. The comparison arithmetic is testable without a database; `repository.py` builds summaries from rows.
- Added `GET /estimates` — history newest-first, with `limit`/`offset` pagination (1–100, 422 outside) and optional filtering by `part_id`, `rate_set_id`, and an inclusive `created_after`/`created_before` UTC range. Filters combine, and `total` counts everything matching, not just the page. Summary rows deliberately **exclude** the audit snapshots.
- Added `GET /estimates/compare?baseline=&comparison=` — both identities and timestamps, the four cost components side by side, deltas (`comparison - baseline`) with percent change, and explicit `older_estimate_id`/`newer_estimate_id`. Newer/older is decided by timestamp **independently of argument order**; `percent_change` is `null` against a zero baseline rather than infinite; unknown ids return 404.
- Declared `/estimates/compare` **before** `/estimates/{estimate_id}` so "compare" is not captured as an id — covered by a test.
- **Schema change**: `estimates` gained an autoincrement `id` primary key, with `estimate_id` becoming a unique indexed column. History is ordered by that monotonic key rather than `created_at`, because the clock's granularity (~15 ms on Windows) means estimates created in quick succession genuinely share a timestamp — which was making pagination order non-deterministic. This was caught by a failing pagination test and fixed properly rather than worked around.
- Date filters normalise timezone-aware bounds to naive UTC before comparison, matching how `created_at` is stored.
- **Historical integrity**: comparison reads persisted totals and never recalculates. Tested by publishing a 999/kg rate set version and asserting both the listing and the comparison are byte-identical afterwards, and by doctoring a stored total and confirming the comparison reports it unchanged.
- No costing formula, costing module, or existing endpoint was changed. Sessions stay dependency-injected; SQLAlchemy stays in the persistence layer.
- Added `tests/test_estimate_history.py` (32 tests). Suite: 241 → **273 passing**, with the pre-existing 241 confirmed green before and after.
- Verified live: server start, two estimates created and individually retrieved, listing, filtering by part/rate set/date, pagination across pages, comparison with correct deltas (+423.20 total, +31.0%), publishing rate set v9.0.0 at 999/kg, then **a server restart** — after which the listing, the stored estimate (still v1.0.0, steel 0.80/kg) and the full comparison JSON were all identical to before. All nine pre-existing endpoints re-checked.

### Step 12 — Alembic migrations
- Added `alembic.ini` and `src/cad_costing_agent/migrations/` (`env.py`, `script.py.mako`, `versions/0001_initial_schema.py`), plus `alembic>=1.13` and package-data config so the scripts ship with the package.
- Revision `0001` was **autogenerated from the ORM models** rather than hand-written, so it matches them exactly — including the `estimates` autoincrement `id` primary key and the unique, indexed `estimate_id` introduced in Step 11. A test compares the migration-built schema against `create_all` output to catch future drift.
- `db.run_migrations` branches on database state: empty → `upgrade head`; already under Alembic → `upgrade head`; **pre-Alembic (tables but no `alembic_version`) → `stamp head`**. That third branch is why adoption is safe — running the migration would fail on existing tables, and dropping/recreating would destroy stored rate sets and estimates.
- `init_db` now migrates then seeds. **`Base.metadata.create_all` is gone from all application code**; Alembic owns the schema. Seeding behaviour and estimate persistence are unchanged.
- The database URL stays configured in exactly one place: `alembic.ini` deliberately omits `sqlalchemy.url`, and `env.py` reads `db.database_url()`, so the CLI cannot drift from the app.
- `env.py` honours a caller-supplied `config.attributes["connection"]` — essential for the in-memory SQLite used in tests, where a second connection to `sqlite://` would be a different, empty database.
- Added `tests/test_migrations.py` (14 tests): fresh-database upgrade, `estimates` PK/unique-index shape, schema-vs-models parity, `init_db` migrate-then-seed, idempotent re-runs, **pre-Alembic upgrade preserving all rate sets and estimates including snapshot contents**, stamping running no DDL, a stamped database still reading and writing, file-backed upgrade in place, single-head check, and a parameterised scan asserting no revision's `upgrade()` contains `drop_table`/`drop_column`/`delete`.
- Tests exercise the real migration path rather than a `create_all` shortcut; the suite went 273 → **287 passing** and ~10.5s → ~14s, which is worth it.
- Verified live: CLI `history`/`current`/`upgrade head` against a fresh database; then the **genuine Step-11 database** (4 rate sets, 4 estimates, no `alembic_version`) adopted at server startup — afterwards stamped at `0001` with all 4 rate sets and 4 estimates intact, legacy estimates and comparisons still returning identical values, new estimates still writable, and a restart applying no further changes and re-seeding nothing. All pre-existing endpoints re-checked.
- Audited for data-loss paths: no `drop_all`/`drop_table`/`DELETE` in application code, no `create_all`, and nothing invokes `downgrade` automatically.

### Step 13 — cost presentation and currency policy
- Added `money.py`: `DEFAULT_CURRENCY = "INR"`, `round_money`/`round_percent` (half-up via `Decimal`, since Python's built-in `round` is half-to-even and would make 0.125 into 0.12), and the `Money`/`Percent` pydantic serializer annotations.
- **Rounding is presentation only, by construction.** `Money` is a `PlainSerializer` annotation, not a numeric type: a field holds the exact float in memory and is rounded only when serialized. Python-level arithmetic therefore always reads full precision, which makes it *structurally impossible* to round an intermediate and reuse it. **No costing formula changed.** `1367.0434666666667` now reports as `1367.04`.
- Applied `Money` to computed cost amounts across `MaterialCostResult`, `OperationCostResult`, `AdditionalCostsResult`, `PartCostEstimateResult`, `StoredEstimate`, `EstimateSummary`, and `CostDelta`. **Input rates are deliberately not rounded** — a legitimate rate like 0.125/kg would be distorted — so the policy covers computed amounts, not echoed rates.
- **Currency**: `RateSet.currency` (3-letter, validated, default INR) flows into `RateSetReference`, `RateSetSummary`, `PartCostEstimateResult`, and the persisted estimate. An estimate is denominated in its rate set's currency; without a rate set it reports the default. **No conversion anywhere.** A stored estimate keeps the currency it was created with, even if a later rate set version changes it.
- **Per-unit cost**: `per_unit_cost = total_part_cost / part.quantity`, computed from the exact batch total. The batch total already includes quantity, so dividing it back out is the only place quantity is undone — it is never applied twice. One-time batch costs (setup, tooling) amortise across the batch, so per-unit cost falls as quantity rises; both properties are tested.
- **Migration `0002`** adds `currency` to both tables with `server_default='INR'`, so every pre-existing row is backfilled in place. Purely additive — no table dropped or recreated, no stored value modified. SQLite batch mode rewrites each table, copying every row across.
- Added `tests/test_money.py` (41 tests): half-up rounding including the 0.125 and 2.675 edge cases, exact-in-memory versus rounded-on-serialize, totals rounded from the exact value rather than from rounded parts, every monetary field in a response at 2dp, rates left unrounded, INR defaults and rejection of malformed codes, currency propagation through rate set to estimate to history to comparison, a non-INR rate set, historical currency immutability, per-unit arithmetic including quantity-independence and batch-cost amortisation, a zero-cost estimate, and **migration of a populated pre-currency database with backfill and full data preservation**.
- Updated 10 existing API test expectations to the new presentation (for example `166.666...` to `166.67`, `0.3768` to `0.38`) and added explicit penny tolerances where a test compared a total against a sum of independently-rounded components. Domain-level tests needed no change, since the calculators still return exact values.
- **Two real bugs found and fixed by these tests**: (1) `RateSetRepository` persisted `currency` but `_row_to_rate_set` never read it back, so every stored non-INR rate set silently reverted to INR; (2) an earlier scripted patch had silently failed to apply because of an indentation mismatch, so later patches assert their anchors.
- Suite: 288 to **329 passing**, no regressions.
- Verified live: the **real Step-12 database** migrated `0001` to `0002` in place with all 4 rate sets and 5 estimates preserved and currencies backfilled to INR; the worked example returning **1363.20 INR** with `per_unit_cost` **340.80**; `/cost/cad-part` returning **1367.04** where it used to return `1367.0434666666667`; currency present on `/rates`, estimates, history and comparison; a rate set published in EUR round-tripping as EUR; a historical estimate unchanged after a newer EUR rate set version; and everything stable across a server restart. All pre-existing endpoints re-checked.

### Step 14 — rate-set authoring
- Added `POST /rates` (create a rate set version) and `POST /rates/{rate_set_id}/versions` (same, with the id from the path). Both funnel into one `_create_rate_set` helper, so validation, the duplicate rule and the read-back live in exactly one place.
- Added `RateSetVersionCreate` — a `RateSet` minus `rate_set_id`, reusing the same constraint constants, with a test asserting its fields stay in sync with `RateSet`. It exists only so the convenience route can take the id from the path; the constructed `RateSet` re-validates everything.
- **No repository change and no Alembic migration were needed.** `RateSetRepository.add` already validated identity, rejected duplicates with an immutability message and inserted, and `rate_sets` already stored every field — so the endpoint calls `add` then `get`, and head remains `0002`. This was the smallest correct change rather than new abstractions.
- **Immutability is structural**: the unique `(rate_set_id, version)` constraint, no update/delete/upsert method on the repository, and no `PUT`/`PATCH`/`DELETE` route (they return 405). A re-post returns **409 Conflict** and the stored rates are provably unchanged — tested, including against the seeded sets.
- Defaults preserved: `currency` defaults to **INR**, `is_illustrative` defaults to **true** so an authored set is presumed a placeholder unless marked otherwise; the flag flows into every estimate's provenance stamp.
- Added `tests/test_rate_set_authoring.py` (46 tests): creation and retrieval, persistence, INR default, explicit currency, `is_illustrative` default, minimal payloads, ~22 validation-failure cases across ids/versions/currency/material/operation/additional rates, rejected payloads leaving nothing persisted, duplicate rejection, tamper-proofing, absence of edit routes, multiple coexisting versions, numeric latest-version resolution, the versions endpoint (path id, duplicate rule, invalid path id, defaults), a hand-checkable estimate from authored rates, and repository-level round-trip/duplicate/no-mutator checks.
- Suite: 329 to **375 passing**, no regressions.
- Verified live: authored `acme-machine-shop` v1.0.0 in INR (steel 72.5/kg, milling 1850/420 per hour, overhead 18%, scrap 3%, tooling 4500) via `POST /rates` (201); costed a 10-off bracket at **9764.51 INR**, 976.45 per unit; a re-post returned 409; published v2.0.0 through the versions endpoint (201) with higher rates; a new estimate used v2 and came to **10845.45 INR** while the original stayed at **9764.51** with its v1 snapshot (steel 72.5/kg) intact; after a server restart both versions and both estimates were still present and unchanged.

### Step 15 — first usable costing UI
- Added `src/cad_costing_agent/static/` (`index.html`, `app.js`, `styles.css`), mounted at `/ui` with `/` redirecting to it. Plain HTML/CSS/JS — **no build step, no bundler, no second server**. Mounted under `/ui` so it cannot shadow an API route.
- Workflow: upload CAD → part details → rate set → optional operations → Calculate → result. Geometry is read on file selection via `/cad/geometry`, so an attached file is visibly distinct from a processed one; costing goes through the existing `/cost/cad-part`.
- **No costing logic in the frontend** — it collects inputs and formats responses. A test greps `app.js` for formula fragments to keep it that way.
- Three API semantics the page respects exactly, each of which would otherwise be a silent costing bug: blank additional-cost fields are **omitted, never sent as `0`** (a zero overrides the rate set); `rate_set_version` is sent **only when explicitly pinned**, so "Latest" resolves server-side; operation quantity is **derived** from part quantity, which the API requires to match.
- The page adapts to the backend rather than the reverse: STEP uploads lock the unit selector to `mm` (the reader normalises STEP and rejects other units), material and process dropdowns are populated from the selected rate set so an unpriced choice is impossible, and the stored `result_snapshot` is read from either shape (`/cost/part` stores the part cost directly; `/cost/cad-part` nests it under `part_cost`).
- Money is rendered `₹1,234.56` for INR and `CODE 1,234.56` otherwise — **never converted**. An illustrative rate set raises an unmissable warning on the result.
- **One small API addition**: `EstimateSummary` gained `quantity`. The history table has a Qty column that could never fill without it, and a batch total is unreadable without the batch size. The column already existed on the row, so there was **no schema change and no migration**.
- Added `tests/test_ui.py` (33 tests): UI and assets served, `/` redirect, API not shadowed, only supported formats advertised, no arithmetic in `app.js`, rate-set/version/material/process loading, geometry preview and its error path, the exact multipart form the page builds, omitted-vs-entered additional costs, latest-vs-pinned versions producing different totals, operation requirements, the quantity-mismatch guard, STEP upload and the non-mm rejection that justifies locking the control, both error shapes the page formats, and history/detail/compare including per-unit from both snapshot shapes.
- Suite: 375 to **408 passing**, no regressions.
- Verified live end to end: authored an INR rate set, uploaded a real STEP file, and calculated a 10-off bracket at **₹8,051.33 total / ₹805.13 per unit**; a 50-off run came to **₹15,541.13 / ₹310.82 per unit**, showing setup and tooling amortising. Both appeared in history with quantity and rate-set version, opened correctly from history, and compared cleanly. The displayed total matched the API response exactly.

### Step 16 — interactive 3D CAD viewer
- Added `cad_mesh.py` and `POST /cad/mesh`: one upload returns the geometry metadata *and* a browser-renderable triangle mesh. **Nothing in the costing engine changed** — the only Python files touched this step were `main.py` (the new route, plus pinning the `.js` MIME type) and the new module.
- **Two independent branches from one upload**: `cad_geometry → cad_mass → costing`, and `cad_geometry → cad_mesh → viewer`. Both call the same extractor, so the figures beside the model are the ones costing uses, not a second opinion measured from the triangles. `viewer.js` contains no costing code and a test enforces that.
- **One common representation for both formats** (flat `positions` + `indices`), so the browser never parses STEP or B-Rep and the viewer does not care which format the file was.
- Normals are derived in the browser rather than transmitted, which is correct only because the backend deliberately **does not merge vertices across surface boundaries**: STEP vertices are shared within a B-Rep face but never between faces (smooth across a curved surface, hard at a real edge), and STL triangles stay unmerged so the surface shades flat, as the file actually describes.
- **Tessellation is relative to the model**: 0.1% of the bounding-box diagonal plus 0.5 rad angular, so a 10 mm bracket and a 2 m frame get comparable fidelity. Settings used are reported in the response. Uploads are capped by `MAX_UPLOAD_BYTES` (raised to 500 MB to accept large real-world exports) and previews at 80,000 triangles; an over-budget STEP model is re-tessellated once at 0.5%, and anything still too dense returns geometry with `mesh: null` and a `mesh_error` — **a part too dense to draw is still a part you can cost**.
- **three.js r169 (MIT) is vendored**, not loaded from a CDN, so the viewer works offline and behind a proxy. It is the ES-module build imported by relative path, so there is still **no build step, no bundler and no npm**. Camera controls are hand-written, since fit/reset/keyboard support would have been needed on top of `OrbitControls` anyway.
- Relaid out the page as viewer-left / inputs-right / result-full-width, and moved the geometry readout beside the model.
- Fixed a real CSS collision found during live verification: the overlay's `error` modifier was being repainted by the page-level `.error` form-error rule (equal specificity, defined later), so the viewer's failure state took on a pink form-error box. Modifiers are now `is-`-prefixed, with a regression test.
- Pinned the `.js` and `.css` MIME types in `main.py`. Windows resolves these from the registry, and a stray `text/plain` mapping would make browsers refuse to execute the ES module — breaking the viewer on someone else's machine with nothing but a console message.
- Added `tests/test_cad_viewer.py` (44 tests): both formats produce a renderable mesh; every index references a real vertex; **the mesh encloses the correct volume by the divergence theorem**, which catches inverted winding that would render the part inside-out; a curved face really is tessellated; the reported volume comes from the exact B-Rep and not from the triangles; the mesh endpoint's geometry is byte-identical to `/cad/geometry`; invalid files fail cleanly; the size and triangle budgets behave; assets (including the vendored library) are served as JavaScript; and costing is unaffected.
- Suite: 408 to **452 passing**, no regressions. No schema change, so no migration — Alembic is still at `0002`.
- **Verified live in a real browser** (headless Chrome driving the actual `/ui/` page): a STEP bracket (plate + boss + through bore + four corner holes) rendered correctly with round holes and sharp edges; rotate, pan, zoom in/out, Fit, Reset and arrow-key orbit each verified by screenshot; Fit re-framed at the rotated angle while Reset returned to the default isometric. Geometry shown matched the backend exactly (volume 42,675.066 mm³, bbox 80×50×22 mm, mass 0.335 kg/part), and a 10-off estimate came to **₹8,473.29 total / ₹847.33 per unit**, matching a direct API call to the paisa. The same part as STL rendered flat-shaded at 900 triangles and cost ₹8,473.26 — three paise less, because an STL *is* the faceted approximation. A corrupt file produced a clean error in both the viewer and the page.

### Step 17 — user-configurable costing setup
- Added a **Costing setup** tab (`static/setup.js`) with a profile listing and a five-step wizard: General, Materials, Processes, Additional costs, Review & publish. Real shop rates can now be entered without touching the API.
- **No new costing concepts.** A "costing profile" is the existing rate set and a profile version is the existing `(rate_set_id, version)` pair; the wizard publishes through the existing `POST /rates` and `POST /rates/{id}/versions` unchanged. **No costing formula changed.**
- **Immutability preserved by construction**: no `PUT`/`PATCH`/`DELETE` exists on any rate route and the UI offers no edit path. "Create new version" copies the previous version so only the change has to be typed, and the new version must be numerically higher. Tests assert both the absence of mutating routes and of mutating verbs in `setup.js`.
- **Drafts live only in the browser.** No server-side draft state, so an abandoned edit leaves nothing behind and there is exactly one kind of stored rate set — a published one. Validation happens before persistence.
- Two small API additions, both backward compatible: `RateSetSummary` gained `material_count`/`process_count` so the listing can say "3 materials · 3 processes" without fetching every version, and `POST /rates/validate` checks a candidate rate set without storing it, so the review step's verdict comes from the backend rather than the browser. **No schema change and no migration** — the counts are derived from the existing JSON column. Alembic is still at `0002`.
- Process types are **not** a fixed list: the model already keys operations by an arbitrary string, so broaching, honing or shot peening work end to end. The form's dropdown only suggests common ones.
- Materials and processes are edited as **arrays** and folded into the keyed maps only at publish time, so two rows sharing a name are visible and reported against the offending row rather than silently collapsing into one.
- The listing replaced a developer-oriented list of slugs and version strings with profile cards: name, currency, current version, counts, real-vs-illustrative badge, and every published version marked **Current** or **Superseded**. Seeded `poc-*` profiles are plainly labelled illustrative. Estimate and history views now show the profile **name** rather than its slug.
- **Bug found and fixed during live verification — a self-authored profile could not actually be used.** CAD mass always resolved density through the built-in two-material catalog in `materials.py`, ignoring the selected rate set, so a profile defining "EN8 steel" failed with *"Unknown material 'EN8 steel'. Known materials: Steel, Aluminium"* even though it priced it. The same gap meant a profile redeclaring a material's density had that density silently overruled while its price was honoured. `calculate_cad_part_cost` now resolves the material from the rate set and injects it into `calculate_mass_from_geometry` — the same dependency inversion already used for `RateSet`. The mass formula is unchanged; only where the density comes from. With no rate set the built-in catalog is still the fallback, so that path is untouched.
- Added `tests/test_costing_setup.py` (53 tests): publishing a profile, INR default, custom process types costed end to end, the listing counts, validate-without-storing, every field-level validation failure rejected by both validate and publish, a new version copying the old one and changing only what was edited, previous versions untouched, republishing rejected as a conflict, no mutating routes, the profile reaching the estimate with hand-checkable numbers, a new version changing new estimates but never old ones, pinning an older version, the density-resolution regressions, and profiles plus estimates surviving a restart.
- One existing test changed: `test_unknown_material_returns_a_readable_string` asserted the old built-in-catalog wording. With a profile selected the message now names the profile's materials, which is strictly more useful, so the test asserts that intent instead.
- Suite: 452 to **505 passing**, no regressions.
- **Verified live end to end** in a real browser against a running server: created "Pune Machine Shop" from the UI with three materials (EN8 steel at ₹72.50/kg, EN24, Aluminium 6061) and three processes (milling, turning, broaching) plus 18% overhead, 3% scrap and ₹4,500 tooling; the review step got a backend "Ready to publish" verdict and published v1.0.0. A 10-off STEP bracket in EN8 steel costed **₹8,473.29 total / ₹847.33 per unit**. "Create new version" copied v1.0.0, only EN8 steel was changed to ₹145.00/kg, and v2.0.0 published — every other value carried across identically. A new estimate used v2.0.0 and came to **₹8,767.16**, with material cost exactly doubled (₹242.87 to ₹485.75), while the first estimate stayed at ₹8,473.29 on v1.0.0. Attempting to republish v2.0.0 was refused with *"Version 2.0.0 is not newer than the current v2.0.0."* The server was restarted twice; both versions, both estimates and their version stamps survived intact.

### Step 18 — deterministic CAD feature recognition
- Added `src/cad_costing_agent/features/`: `model.py` (the vocabulary), `topology.py` (one B-Rep scan), `grouping.py` (coaxial cylinders), and six recognizers under `recognizers/`. Plus `POST /cad/features`, a **Detected features** panel (`static/features.js`), and `tests/step_fixtures.py` + `tests/test_features.py`.
- **A third branch off the same B-Rep**, independent of the other two: `CAD → geometry → mass → costing` (unchanged), `CAD → tessellation → viewer`, `CAD → features`. **No costing formula, rate-set rule or persistence schema changed**, and no Alembic migration was needed — Alembic is still at `0002`.
- **Removed the STEP-parsing duplication Step 16 introduced.** `cad_geometry.read_step_shape` is now the only STEP reader; geometry, tessellation and recognition all call it, so there is one definition of a readable STEP file.
- **One scan, then pure rules.** `topology.py` is the only module in the feature layer that imports OpenCASCADE. Recognizers are pure functions over plain dataclasses, so each is independently testable and the model is traversed once rather than six times. Nothing downstream — API, UI, later steps — sees an OCP class.
- **Face attribution is exclusive**, most-specific rule first: holes → pockets/slots → fillets → chamfers → cylinders → planes. A bore is a hole, not also a cylinder; a pocket's rounded corners are the pocket's, not stray fillets. Faces nobody claims are counted as `unclassified_face_count` rather than dropped.
- **How a hole is identified**: internal cylindrical faces are grouped by axis, radius and axial overlap (one bore is not always one face), and a group sweeping a full turn is a bore. Each end's circular edge is then read — a planar face carrying that circle on its **outer** wire is a floor (closed end); on an **inner** wire the bore breaks through (open end). Two open ends is *through*, one closed is *blind*. Counterbores and countersinks fall out of the same evidence rather than needing separate scans.
- **Confidence is deterministic**, not a probability: high = every condition checked and satisfied and every dimension measured; medium = something inferred rather than measured; low = classification ambiguous. Chamfers are capped at medium because a narrow oblique face is good evidence, not proof.
- **Traceability preserved**: every feature carries the 1-based face and edge indices from `TopExp::MapShapes`, stable for a given file — what a later step needs to highlight a feature in the viewer.
- **STEP-first, and honest about it.** STL is refused for recognition with an explanation that also says what still works, because a hole in a triangle mesh cannot be told from a many-sided prism. STL support elsewhere is untouched.
- **Known gaps are documented rather than papered over** (see `ARCHITECTURE.md`), and two were verified empirically: a slot cut clean *through* a part has no floor and is not detected — its rounded ends are reported as concave fillets, a real false positive; and a stepped pocket comes back as nested recesses, one per floor level.
- Added 131 tests: 13 locally generated STEP fixtures with manually stated expected features, exact dimension assertions, determinism over every fixture, face-exclusivity, traceability, "no unexplained claims", recognizer isolation, a recognizer that raises not sinking the others, robustness against corrupt/empty/oversized/unsupported input, and regressions proving geometry, the viewer, STL and costing are unchanged.
- Suite: 505 to **636 passing**, no regressions.
- **Verified against real third-party CAD, not only generated fixtures.** `cadmodels/model1_1.stp` (a 43-face Creo/ST-Developer bracket) yields 6 through holes (2 × Ø25 × 16, 1 × Ø16 × 80, 2 × Ø8 × 40, 1 × Ø8 × 16), 11 fillets and 22 planes, with **4 faces honestly reported as unclassified** — they are `SurfaceOfExtrusion`, exactly the documented non-analytic limitation. `cadmodels/model2.stp` yields 4 through holes, 2 shafts, 2 fillets and 19 planes across **2 correctly separated solids**, 0 unclassified.
- **A real failure found and fixed during final verification.** `cadmodels/Figure 3.stp`, a 198 MB Creo export, is built entirely from `FACETED_BREP` polygons — 2,855 of them, zero `ADVANCED_FACE`. OpenCASCADE read it without error but transferred **no faces at all**, and recognition returned an empty feature list, which reads as "this part has no features" when the truth is "there was no B-Rep here to look at". `recognize_features` now rejects a face-less shape with an explanation naming the likely cause and the fix (re-export as solid geometry). Over HTTP it returns a clean `400` after ~19 s with the server still healthy. Covered by a test that needs no large file.
- **Verified live** against a running server and the real browser UI: the Step-16 bracket analysed to 4 × Ø6.5 through holes (depth 10), 1 × Ø14 through hole (depth 22), 1 × Ø28 shaft (length 12) and 8 planes with **0 unclassified faces**, in ~90–150 ms; the 3D viewer and geometry panel still worked; `/cost/cad-part` returned **₹3,483.52 / ₹348.35 per unit** identically before and after analysis; the same file analysed twice returned byte-identical results; an STL showed the viewer and geometry normally with the Analyse button disabled and explained; corrupt and empty files returned clean 400s with the server still healthy. A 100-hole plate found all 100 holes in ~700 ms.

### Step 18.5 — faceted CAD fallback
- **Investigated the two real Creo exports and found they are the same representation.** `Figure 3.stp` and `Figure 4.stp` are both 198 MB, both `AUTOMOTIVE_DESIGN` AP214, both **272,113 polygons in 6,473 `FACETED_BREP` bodies**, both with **zero** analytic surfaces. They differ only by 113 `CARTESIAN_POINT` entities. Neither is the "better" export and neither contains information the other loses.
- **Diagnosed why OpenCASCADE returned nothing from them.** Both write `FACETED_BREP` over an `OPEN_SHELL`; ISO 10303-42 requires a `CLOSED_SHELL`, so the reader rejects all 6,473 solids and transfers zero faces. Proven by repairing the text: substituting `CLOSED_SHELL` makes 310,601 faces appear — but takes 110 s and yields no solids, so the repair is a diagnosis rather than a fix.
- Added `features/step_text.py`: classifies a file as `brep` / `faceted` / `wireframe` / `empty` by counting entity keywords in the raw bytes, in **1.9 s on 198 MB** against ~20 s for OpenCASCADE merely to read it. It also names the `OPEN_SHELL` defect.
- Added `features/faceted.py` (a polygon parser that needs no OpenCASCADE, welding vertices by position because some writers emit one point per facet) and `features/mesh_recognition.py` (coplanar facets → flat patches → bands joined at under 40° → closed-form circle fit).
- **Both paths produce the same `Feature` model**, distinguished by `source_type` (`brep` / `faceted`) and by `source_facets` instead of `source_faces`. No OCP class reaches the API, which a test asserts.
- **Two deterministic tests decide whether a band is a cylinder**, both stated in every feature's evidence: the circle-fit residual, and the sagitta — how far the flat strips stand inside the circle, `1 - cos(π/N)`. Below 6% it is a tessellated circle; above it is a designed polygon. **An octagonal hole is left unclassified rather than reported as round**, which a test enforces.
- **What faceted reconstruction deliberately does not attempt**: pockets, slots, fillets and chamfers. On polygons a fillet cannot be told from a chamfer or a coarse curve, and a pocket floor cannot be told from any other flat region. Reported as a warning on every faceted result.
- Added `limits.py`: upload, B-Rep face, facet and viewer-triangle budgets are now **four separate, environment-configurable values** rather than the upload cap living inside the viewer's module. A model over the viewer budget is still costable, which a test enforces.
- **A real bug found live and fixed**: `/cad/geometry` answered **HTTP 200 with volume 0.0** for these files — a silent wrong answer that only surfaced much later as "volume must be positive" during costing. A shape with neither volume nor surface area is now refused with the `OPEN_SHELL` diagnosis.
- Added 50 tests (`tests/test_faceted_features.py`) plus a faceted-STEP fixture writer that tessellates a known shape and discards its analytic surfaces, so expected diameters are knowable.
- Suite: 637 to **688 passing**, no regressions.
- **Verified live.** Synthetic faceted plate → **4 × Ø10.000 through holes, depth 10, confidence high, `source_type: faceted`**, 52 facets each, in 114 ms — the worked example, reconstructed with nothing hard-coded. Faceted blind hole → Ø16 × 12 deep, correctly *blind*. Faceted boss → Ø30 shaft, not a hole. Both real 198 MB files → `/cad/features` refuses in **3 s** with the polygon and body counts plus the `OPEN_SHELL` diagnosis, and `/cad/geometry` now refuses with an explanation instead of returning zero. Analytic models unchanged (`model1_1.stp`: 6 holes, 11 fillets, 22 planes; `model2.stp`: 4 holes, 2 shafts, 2 fillets, 2 solids). STL unchanged. Costing **₹4,467.00 / ₹446.70 per unit identical** before and after analysing both an analytic and a faceted model. Repeated recognition byte-identical on both paths.

### Step 18.6 — faceted CAD made costable
- Added `cad_faceted.py` (polygon parsing, positional welding, connected components, closure and signed volume — no OpenCASCADE) and wired it into `cad_geometry`. `GeometryProperties` gained `geometry_source`, `volume_method`, `volume_confidence`, `facet_count`, `closed_component_count` and `open_component_count`. **No costing formula changed**; `mass_kg = volume_m3 × density` is untouched and the Step 17 rule that a rate set's material density wins is covered by a test.
- **The `OPEN_SHELL` declaration turned out to be a red herring.** The real files declare all 6,473 shells `OPEN_SHELL` — which is why OpenCASCADE rejects them — but welding shows those shells are face *groups* forming only **100** connected components, 63 of them watertight. Closure is now decided from topology, never from the entity name.
- **Closure is checked properly**: every edge shared by exactly two triangles *and* consistent winding. The directed-edge check is not redundant — flip one triangle and the mesh stays watertight by edge count while its signed volume is silently wrong. A test proves it is caught.
- **Units are read, not assumed.** The real exports are in **metres**; their `LENGTH_UNIT` is parsed and converted to millimetres, and a file with no declarable unit is refused rather than guessed, because guessing scales every mass by a thousand.
- **No volume is invented.** No bounding box, no convex hull, no guessed thickness, no STEP-text repair. A volume is reported only when *every* component is closed; otherwise nothing is reported and the refusal carries the counts.
- The viewer now draws faceted files straight from their polygons, replacing a cryptic "no transferable geometry" — but remains independent: a preview that fails or is refused for size never affects volume, mass or cost.
- Added `CAD_COSTING_MAX_GEOMETRY_FACETS` (1,000,000), far above the feature budget, because deriving a volume is one weld and one scan while reconstructing features fits geometry to every body.
- Added 35 tests (`tests/test_faceted_geometry.py`): exact closed-mesh volume, open-mesh rejection, welding, flipped-triangle detection, inward winding, multiple closed bodies, mixed closed/open refusal, unit conversion and undeclared units, determinism, faceted mass from a rate-set density, and analytic/STL/costing regressions.
- Suite: 688 to **723 passing**, no regressions.
- **Live verification.** The decisive result: the *same real Creo bracket*, costed as analytic B-Rep and again as a faceted export with its surfaces discarded, gives **220,621.078 mm³ / 1.731875 kg** and **220,606.972 mm³ / 1.731765 kg** — 0.006% apart — and an **identical ₹4,467.00**. All five endpoints work for a closed faceted file, and repeated volumes are byte-identical. `Figure 3.stp` and `Figure 4.stp` are confirmed the same export twice (identical polygon counts and welded topology) and remain **not costable**: 63 of 100 components watertight holding 27% of the polygons, the largest open component carrying 923 separate boundary chains and 66 non-manifold edges (characterised properly in Step 18.7). Both now fail in ~25–35 s with the counts and the reason rather than a bare "no surfaces". Analytic models and STL are unchanged.

### Step 18.7 — repairing faceted geometry, and refusing to
- **The "cracks between independently tessellated faces" claim from Step 18.5/18.6 was an inference, and it is wrong.** Checking it properly: only **120 of 9,039** boundary edges (1.3%) have a boundary vertex lying on them, so T-junctions are not the cause; the nearest unrelated boundary vertex is typically 0.25 mm away, so there is no opposing crack face to weld to; and **1,359 of 1,687 boundary chains are three-vertex closed loops** — the footprint of one absent triangle. The export is **incomplete**, not cracked. The error message and both documents have been corrected.
- Added `restore_missing_facets` in `cad_faceted.py`: the one repair the evidence supports. A three-edge boundary loop over three existing vertices admits exactly one triangle, oriented by the three unmatched directed edges, and **uses no tolerance at all** — it is topology, not fitting.
- **Three guards, all required**: the chain is exactly three mutually-joined vertices; each edge is used exactly once (so no non-manifold edge can be created); and the three edges come from **three different triangles**. The third guard exists because testing found a real false positive — a lone triangle also has three free edges, and filling it folds a copy back onto itself into a **zero-volume shell that then reports as watertight**. Exactly the kind of confident-but-wrong answer this system must not produce.
- Everything larger is refused: four-edge holes (a choice of diagonal), large missing faces (inventing surface), T-junctions, open sheets, and non-manifold topology. Two triangular holes touching at a corner merge into one five-vertex chain and are also left alone — conservative, and the safe direction to fail in.
- `GeometryProperties` gained `repair_applied`, `repair_method`, `repaired_facet_count` and `repair_tolerance_mm`; `volume_confidence` now reports `repaired` as well as `high` and `unavailable`. A repaired volume is never presented as a pristine one, and the wording is explicit that the volume is exact for the reconstructed mesh while the mesh is an approximation of the original surfaces.
- Added 39 tests (`tests/test_faceted_repair.py`): every one of a cube's twelve facets recovered individually, separate holes each repaired, exact volume match against the intact model, and the whole refusal set — two-facet holes, large missing faces, T-junctions, open sheets, lone triangles, non-manifold edges, touching holes, and weld tolerances that are too small, sufficient, or dangerously large.
- Suite: 723 to **762 passing**, no regressions. No costing formula changed.
- **Live outcome for the real files.** Both `Figure 3.stp` and `Figure 4.stp` restore **1,346** facets each, taking watertight components from 63 to **68** of 100 and the closed share from 27% to 32%. Both remain **not costable**: 32 components are still open, including whole absent faces (planar loops 44 mm across, one 356-vertex non-planar boundary) and 66 edges shared by three or four polygons. The two files agree exactly on every figure. The honest answer is that the information needed is not in the files — the fix is a solid B-Rep re-export. (Step 18.8 makes them costable anyway, by asking the user to supply the missing volume as an explicit assumption.)

### Step 18.8 — user-configurable fallback for incomplete CAD
- **Unmeasurable geometry is now a question, not a failure.** `GeometryProperties` gained `geometry_status` (`complete` / `incomplete_requires_input`) and an `incomplete` report listing component counts, the watertight-only volume, the unresolved components and the supported resolutions. `/cad/mass` and `/cost/cad-part` answer **422 `needs_geometry_input`** rather than an opaque 400.
- **Volume stays 0 until the user answers.** The watertight-components-only volume is offered as a starting point but never used on its own, so anything that ignores `geometry_status` fails loudly instead of quietly pricing a fraction of the model.
- Two resolutions, both explicit: **`total_volume`** (used verbatim) and **`missing_volume`** (added to what was measured). The third option, uploading a corrected CAD file, needs no API at all. No bounding box, no convex hull, no guessed thickness.
- **The assumption travels with the result and cannot be mistaken for a measurement**: `geometry_source: faceted_user_completed`, `volume_method`, `volume_confidence: user_supplied`, and the supplied figure. It is persisted both inside the geometry and as an explicit `geometry_completion` entry on the estimate, so history shows the original CAD state, the assumption, and the volume it produced.
- UI: an amber **Incomplete CAD geometry** panel showing the known volume and the three choices. *Calculate estimate* stays disabled until an assumption is applied; applying one calls the backend for the resulting volume and mass rather than computing them in the browser; the result then reads *"Volume assumed, not measured."*
- Added 32 tests (`tests/test_geometry_completion.py`): complete analytic/faceted/STL geometry asking for nothing, incomplete geometry detected and reported, the needs-input state, both resolution methods, invalid and non-positive values, an assumption refused for measurable geometry, mass and cost using the completed volume, the rate set's density still winning, persistence, a later assumption not altering an earlier estimate, survival across restart, determinism, and viewer independence.
- Suite: 762 to **794 passing**, no regressions. No costing formula changed.
- **Live verification on the real files.** Both report `incomplete_requires_input` with 68 of 100 components watertight, 1,346 facets restored, and a known volume of 1,981,004.3 mm³ (32% of polygons). Costing without an assumption returns 422 `needs_geometry_input`. With one, both cost: `Figure 3.stp` via `missing_volume` = 2,000,000 mm³ and `Figure 4.stp` via `total_volume` = 3,981,004.3 mm³ both reach **3,981,004.3 mm³ → 31.2509 kg → ₹651.81 (₹65.18 per unit)** — the two methods converging on the same answer. Both estimates persist their assumption, appear in history, survive a restart, and a later 9,000,000 mm³ assumption left the first estimate untouched. Analytic STEP (220,621.078 mm³, ₹4,467.00) and STL are unchanged and ask for nothing.

### Step 18.9 — large models made viewable, without touching a number
- **The viewer's budget no longer vetoes a model.** A mesh over `MAX_VIEWER_TRIANGLES` used to come back as `mesh: null` with a red error. It is now decimated server-side to fit the budget and drawn. Only if nothing usable survives the decimation, or the model is past the separate `MAX_VIEWER_INPUT_TRIANGLES` read budget (2,000,000), is the preview reported unavailable — and that still never blocks costing.
- **New module `mesh_simplify.py`** — Rossignac & Borrel vertex clustering, chosen for being deterministic (no randomness, so the same bytes always give the same picture), linear, and bounded in memory. The grid resolution is found by bracketing and bisection; the search never returns a mesh over budget.
- **The simplification is quarantined from every engineering number.** It happens in `cad_mesh._finish`, the one place all source formats converge, and only *after* the full-resolution mesh has been measured. Volume, surface area, bounding box, mass, feature recognition and cost all continue to come from the full data. The costing modules do not import `mesh_simplify` at all, and a test asserts that by reading their source.
- **The response says what happened**: `display_mesh_simplified`, `original_triangle_count`, `display_triangle_count`, `simplification_method`, `simplification_ratio`. The UI shows the display count with the original beside it — *"79,273 triangles (original: 2,72,113) — Simplified for display"* — plus a note that engineering geometry and costing use the full CAD data, so a reduced count can never be read as the model's own.
- Added 23 tests (`tests/test_display_simplification.py`): the below-budget path left byte-identical, reduction within budget, a dense faceted STEP previewable, determinism, a grep against any future "just sample some triangles" shortcut, mesh validity and preserved extents, an unsatisfiable budget degrading to no-preview while costing still succeeds, and — the two that matter most — the `geometry` block being identical whether or not the preview was simplified, and the same file costing the same at any display budget.
- Suite: 794 to **818 passing**, no regressions. No costing formula changed.
- **Live verification in a real browser** on the 198 MB `Figure 3.stp`: it renders as a recognisable assembly with no error overlay, at **272,113 → 79,273 triangles**, ratio 0.291, method `deterministic_vertex_clustering`. Served at two different display budgets (80,000 and 15,000 triangles) the display mesh changed (79,273 vs 14,527) while the entire `geometry` block stayed **identical** — same surface area 2,470,825.294 mm², same bounding box, same 273,459 full-resolution facet count — and the cost was unchanged at ₹49,136.81. The Step 18.8 workflow is intact underneath it: the amber *Incomplete CAD geometry* panel still appears beside the drawn model, a supplied total volume of 6,000,000 mm³ still yields 47.1 kg and a full INR estimate reading *"Volume assumed, not measured."* Analytic STEP (`model1_1.stp`, 220,621.078 mm³; a 239 KB real assembly, 37,942 triangles, not simplified) and STL are unaffected and ask for nothing.
- **`Figure 4.stp` could not be re-tested**: it is no longer present on this machine (an exhaustive search of the user profile and both drives found only `Figure 3.stp`). It was verified under Step 18.8 and nothing in this step is specific to it, but the Step 18.9 viewer claim rests on `Figure 3.stp` alone.

### Step 19 — features → suggested operations
- **New module `operation_suggestions.py`** and **`POST /cad/operations/suggest`**, which reuses the existing recognition (one upload, one parse) and applies a deterministic rule table to the normalized features. Response carries `features`, `suggestions`, `declined`, `summary`, `warnings`, `rules` and `suggestion_counts`. No OCP or topology class is exposed.
- **Supported rules**, each keyed to evidence the recognizers actually produce: `through`/`blind`/countersunk hole → drilling; counterbore → drilling (offering milling); external cylinder (`shaft`) → turning (offering milling); internal cylinder (`bore`, ends never classified) → drilling capped at medium; pocket and slot → milling; a planar face at or above 10% of the largest planar face on its solid → milling/facing capped at medium; a fillet or chamfer recognized at `high` → finishing milling capped at medium.
- **Deliberate refusals**, each returned with its reason: holes whose ends are `ambiguous` or `enclosed`, unknown subtypes, planar faces below the significance threshold or with no measured area, and blends recognized below `high`. Nothing is invented where the evidence does not reach.
- **Confidence can only go down.** A suggestion's confidence is the weakest of the feature's own confidence, a per-rule ceiling, and a per-source ceiling — `faceted` caps everything at medium, because a feature fitted to polygons is weaker evidence than one read from exact surfaces. `source_type` and the feature's own confidence are both carried through.
- **Every suggestion explains itself and traces back**: `feature_id`, `feature_label`, `rule_id`, `reason`, `evidence` (dimensions, CAD face numbers, the recognizer rule, the source kind) and the `source_faces`/`source_facets` the viewer marks. There is no opaque "milling, drilling, turning" list.
- **Nothing can become money by itself**, enforced three ways rather than documented: the costing modules cannot import the layer (asserted by reading their source, both directions); every suggestion reports `costable: false` with `missing_for_costing`; and `to_operation_requirement` — the only bridge into costing — raises unless a cycle time is supplied and refuses a rejected suggestion outright.
- **Corrections are preserved for a later learning loop.** `user_action` (`pending`/`accepted`/`edited`/`rejected`/`added`) and `user_operation_type` sit beside the untouched `operation_type`, so the pair *(suggested, chosen)* survives. Editing back to the suggested process records an acceptance, not a correction. Nothing is persisted — a suggestion is not a manufacturing decision.
- **UI**: the estimate page is now numbered 1 CAD → 2 Part → 3 Rate set → 4 Recognized features → 5 Suggested operations → 6 Additional costs → 7 Calculate. Suggestions appear in an editable table (Feature / Suggested operation / Confidence / Source / User action) with Accept, a process dropdown, Remove, plus Accept all and Clear all. An accepted suggestion lands in the operation plan **with blank times** and is excluded from the estimate until one is typed, with the plan saying so. An edit displays `milling  was turning`.
- **Viewer**: selecting a feature — or a suggestion — marks it on the 3D model with a dot at its location and a line along its axis, using the backend's own coordinates verbatim. Face-level highlighting is still not possible: the wire mesh carries no per-face id.
- Added 73 tests (`tests/test_operation_suggestions.py`). Suite: 818 to **891 passing**, no regressions. No costing formula changed.
- **Live verification.** `model1_1.stp`: 39 features → 31 suggestions, **6 through holes → 6 drilling at high confidence**, 11 fillets and 14 planes → milling at medium, 8 minor planes declined. `model2.stp`: 27 features → 22 suggestions, 4 holes → drilling, **2 shafts (Ø60 and Ø49.66) → turning**, 14 planes → milling, 5 declined. Both byte-identical across repeated runs. Faceted STEP exports produce suggestions capped at medium with `source_type: faceted`. Against `poc-machine-shop`, which prices no turning, the two turning suggestions are flagged `priced_by_rate_set: false` with a warning — and no rate is invented. In the browser: accepting a suggestion produced a plan row with empty times, operations cost **₹0.00**; entering setup 15 / cycle 2 produced **₹1,216.67**; *Clear all* emptied the plan and a hand-entered operation still costed exactly as before. Costing on `model1_1.stp` was **INR 13.86 before and after** suggesting.
- **`Figure 3.stp` is refused by feature recognition**, unchanged from Step 18.5: 272,113 polygons across 6,473 bodies is above the 200,000 feature-reconstruction budget, so it produces no suggestions. Its geometry, mass, costing and 3D preview are unaffected. This is a pre-existing budget, not a Step 19 regression.

### Step 20 — operation times, an editable plan, and the feedback record
- **New module `operation_time.py`** and **`POST /operations/estimate-time`**: deterministic machining-time formulas for drilling, milling (facing, pockets/slots, blends) and turning, built on `rpm = clamp(1000·Vc/(π·D))` plus feed per revolution or per tooth. Every constant lives in a configurable `TimeAssumptions` model — there are no unexplained numbers in the code — and every parameter that affected a result is echoed back in the estimate's `assumptions`.
- **The endpoint takes no CAD file.** It accepts the normalized operations `/cad/operations/suggest` returns, so estimating never re-parses geometry; a 200 MB export is parsed once, and a time costs microseconds.
- **No time is invented.** Missing dimensions, an operation with no formula, or units that were never established all return `needs_user_input` with the gap named, and the times come back **null rather than zero** so nothing downstream can add a missing time into a total. Faceted exports report their units as "as written in the file", so times for them are refused rather than guessed at a possible factor-of-1000 error.
- **No time is confident.** Estimates are capped at `medium` however cleanly the geometry was measured — the parameters describe a plausible shop, not the user's — and fall to the operation suggestion's confidence when that is lower.
- **Time assumptions are separate from rate sets**: assumptions decide duration, rates decide money. A test asserts the two models share no fields beyond `is_illustrative`. The built-in baseline is flagged illustrative exactly as the rate sets are, and warns accordingly.
- **New module `manufacturing_plan.py`.** `PlannedOperation` carries `system_suggested_operation/_setup_time/_cycle_time`, `system_time_method/_confidence/_status`, `user_operation_type/user_setup_time/user_cycle_time`, `user_action`, `sequence_position` and `origin`. **The system's proposal is never overwritten** — an edit writes to the `user_*` side. What gets costed resolves in one direction: the engineer's value, else the system's *if they accepted it*, else nothing. Generating an estimate does not accept it, and changing an operation's process invalidates a time estimated for the old one.
- **Sequence is explicit and persisted, and changes no number**: no sequence-dependent time, no setup consolidation, no routing optimisation, and no enforced precedence. A test costs a plan forwards and backwards and asserts identical results.
- **New module `operation_feedback.py`**: one `OperationFeedback` row per operation recording suggested vs final operation and times, what changed, the confidences, the user action and a timestamp. Removed suggestions are recorded too; a hand-added operation is not counted as a correction, because there was no prediction to correct. Nothing is trained, nothing adapts, nothing is aggregated.
- **Persistence**: one nullable `plan_snapshot` JSON column, added by Alembic revision `0003`, holding the plan, its feedback and the ids of operations left uncosted. Verified against the real 35-estimate development database — all rows and rate sets intact, old rows reading back `null`. A later edit creates a **new** estimate rather than rewriting an old one.
- **UI** renumbered to `1 CAD → 2 Part → 3 Rate set → 4 Features → 5 Manufacturing plan → 6 Sequence → 7 Additional costs → 8 Calculate`. The plan is one table (`# · Operation · Feature · Setup · Cycle · Source · Confidence`) with move up/down, a process dropdown, **Use estimate**, Remove and manual add. An estimated time appears as a **placeholder, never as the field's value**, and the row reads *Needs input* until committed, then *System estimate* or *User value*. Clicking a row shows why the operation was proposed and how its time was derived; clicking its feature marks it on the 3D model.
- Added 122 tests across `tests/test_operation_time.py`, `tests/test_manufacturing_plan.py` and `tests/test_plan_costing.py`. Suite: 891 to **1013 passing**, no regressions. No costing formula changed.
- **A real bug found in live verification and fixed**: the plan was dropping `feature_type`/`feature_subtype`, so through-holes lost their breakthrough travel (0.19 min instead of 0.23) and every fillet fell through to the area formula and got no time at all. Both are now part of `PlannedOperation`, with regression tests.
- **Live verification** on `model2.stp` through the real UI: 27 features → 22 suggestions → 22 time estimates, all `medium`. Engineer accepted one estimate (drilling, 5/0.75), **overrode a time** (drilling 5/0.23 → **6/3.2**), **changed an operation** (turning 12/0.44 → **milling 9/5.5**) and reordered the plan. The estimate came to **₹3,704.78** with operations **₹3,674.17**, costed at the *final* values — 3.2 not 0.23, milling not turning — while the persisted feedback kept both sides with `operation_changed`/`cycle_time_changed` set. 19 operations with no committed time were left out, and the sequence persisted with the moved operation at position 1.

### Step 21 — user-configurable time assumptions
- **New module `time_profiles.py`** and five endpoints mirroring the rate-set pattern: `GET /time-profiles`, `GET /time-profiles/{id}`, `POST /time-profiles/validate`, `POST /time-profiles`, `POST /time-profiles/{id}/versions`. Identity is `(time_profile_id, version)`; omitting a version resolves the highest numerically; re-posting a published version returns **409**. There is no update or delete method anywhere, which is what makes immutability structural rather than a convention.
- **A time profile is not a rate set.** It has no currency and no rate of any kind. A test asserts the two models share only the four fields every versioned thing has, and that the profile layer imports no costing arithmetic. The parameters themselves stay in `operation_time.TimeAssumptions`, beside the formulas that read them, so a parameter and its arithmetic cannot drift apart.
- **All 21 parameters are exposed**: drilling (cutting speed, feed/rev, approach, breakthrough, setup), milling (cutting speed, feed/tooth, teeth, cutter diameter, stepover ratio, axial depth of cut, approach, setup), turning (cutting speed, feed/rev, depth of cut, stock allowance, approach, setup) and the shared spindle limits. A test compares the editable fields in the UI against the model's own fields and fails if the estimator ever reads something nobody can edit.
- **Validation refuses, and separately advises.** Non-positive and absurd values are a 422 — the new upper bounds are typo guards, since a feed of 800 mm/rev is a slipped decimal point — and a cross-field check refuses a spindle range whose minimum is not below its maximum. Values that are legal but unusual come back as advisory notes and still publish: the parameters belong to the person who knows the shop. The spindle *clamp* is kept, because it is part of the deterministic model rather than input handling.
- **Provenance on every prediction**: `time_profile_id`, `time_profile_version`, `time_profile_name` and a `parameters` dict holding the values that actually entered that calculation — the process's own parameters plus the resolved rpm and feed rate, not the whole profile. Enough for one operation to answer "why 2.37 minutes?" on its own.
- **Explicit assumptions clear the attribution.** An API caller may estimate under parameters they have not published; the result then records no profile, because none produced it. Attributing a prediction to parameters it was not made with would be worse than attributing it to nothing.
- **Persistence**: a new `time_profiles` table plus a nullable `time_profile_snapshot` JSON column on `estimates`, both in Alembic revision `0004`. The snapshot is taken from the plan's own profile references rather than a separate field that could disagree with them. Verified against the real development database — 38 estimates and 4 rate sets intact, the new column reading back `null` on every existing row.
- **The Step 20 plan is unchanged in substance**: `system_suggested_*` versus `user_*` still never mix, an estimated time is still not committed until accepted or typed, and an untimed operation is still omitted rather than defaulted. What is new is that the prediction side now names the profile version behind it, and so does every `OperationFeedback` row — without which a correction is uninterpretable.
- **UI**: a **Time setup** tab beside Costing setup, with profile cards, a parameter editor grouped by process, Validate and Publish, and *Publish new version* (which copies the previous version and bumps it). The estimate page gains **4. Time profile** with a version picker and badges reading *Real parameters* / *Illustrative parameters* / *No currency — duration only*. Steps renumbered to 1–9. Each plan row's explanation now names the profile version its time came from.
- Added 58 tests (`tests/test_time_profiles.py`). Suite: 1017 to **1075 passing**, no regressions. No costing formula changed.
- **Live verification** through the real UI on `model2.stp`. Published `live-shop-time v1.0.0` (drilling feed 0.15 mm/rev) → Ø16 × 12 through hole estimated at **0.23 min**; costed an estimate under it at **₹1,675.45**, accepting one estimate and overriding another (5/0.75 → 6/3.2). Published **v2.0.0** with feed 0.05 through the *Publish new version* button; the same CAD then estimated the same hole at **0.68 min** (3× slower, as the feed change implies). The v1 estimate was re-fetched and was **byte-identical** — still v1.0.0, still feed 0.15, still 0.23 min, still ₹1,675.45. Under v2 an engineer override of 7/4.5 was costed at exactly 7/4.5 while the feedback kept `suggested_cycle_time: 0.68` against `live-shop-time v2.0.0`.

### Step 21b — setup-aware manufacturing plan and costing
- **The error being fixed.** Every operation carried its own setup time into costing, so six drilled holes charged six setups. Measured on `model1_1.stp`: 31 suggested operations would have charged **280 minutes** of setup; the part needs **10**.
- **New `ManufacturingSetup`**, holding a setup id, its place in the routing, a name, an optional process context, the operations it contains, its own setup time, and its provenance. `ManufacturingPlan` is now setups-of-operations, with `system_setup_id` / `user_setup_id` making membership explicit rather than inferred from a name or a process type.
- **Setup time is charged once per setup**, cycle time per operation. `to_operation_requirements` attaches each setup's whole time to the first costable operation in it and zero to the rest — not a division, not an average; the setup is a real object and the bridge only decides where to hang its time so the existing unmodified formulas total it correctly. `time_summary()` reports the two separately so the distinction is never buried.
- **The default grouping is conservative and honest**: everything in one setup, `system_assumed`, **low** confidence, assumptions spelled out ("the part can be held in one fixture in one orientation for these operations"; "geometry alone cannot prove it"). The system never claims two operations share a setup because they share a process type.
- **The setup's estimated time is the largest per-process baseline among its operations** — never their sum, which is the mistake, and never an average, which is a number no process has. It is a proposal until accepted or replaced; an uncommitted setup charges zero and says so.
- **Full editing**: create, rename, remove an empty setup (refused while it holds work), reorder setups, move an operation between setups, reorder within a setup, edit or accept the setup time. Moving an operation back to where the system put it stops counting as a correction.
- **The system's grouping is never overwritten.** `system_setup_id` stays put and the move is recorded in `user_setup_id`; `OperationFeedback` gained `system_setup_id`, `final_setup_id`, `setup_changed`, `system_setup_grouping`, `system_setup_time_minutes`, `final_setup_time_minutes`, `setup_time_corrected` and `setup_operation_count`.
- **Two levels of sequence**, both editable and persisted. Neither changes a number — a test costs a plan with operations and setups reordered and asserts an identical total — while setup *membership* does, which is the point.
- **New `POST /operations/plan-cost-preview`** prices a plan through the same unmodified calculators an estimate uses, splitting setup cost from run cost, and persists nothing. It is what lets the UI show the effect of a regrouping before anyone commits, without the browser doing any costing arithmetic.
- **UI**: the flat operation list became a setup-grouped table with a setup band per fixturing (name, setup time, System estimate / User value, System grouping · low confidence / User grouping), a per-row *move to setup* control, add/remove/reorder for setups, and a live impact panel reading `2 setups · Setup time 20 min · Cycle time 149.1 min · ₹5,763 = ₹600 setup + ₹5,163 run` with `Since your last change: +1 setup · +10 min setup time · +300 INR`. Selecting a setup points at the features its operations touch.
- **No migration needed.** The plan already persisted whole in `plan_snapshot`; setups travel with it. A plan stored before setups is read back as one system-assumed setup holding everything, which is what it already meant.
- Added 51 tests (`tests/test_manufacturing_setups.py`). Suite: 1075 to **1127 passing**, no regressions. No costing formula changed.
- **Live verification** on `model1_1.stp` through the real UI. One setup, 31 operations: setup **10 min**, cycle 149.1 min, operation cost **₹5,463 = ₹300 setup + ₹5,163 run**. Added a second setup named *Side fixture* with the real buttons, moved one drilled hole into it with the row's dropdown and gave it 10 min: **₹5,763 = ₹600 setup + ₹5,163 run** — exactly **+₹300**, which is 10 min at the drilling machine rate of ₹1,800/h, with cycle time unchanged at 149.1 min. Reordering operations and reordering setups changed nothing. The estimate persisted at ₹5,776.86 with 20 setup-minutes charged across 31 costed operations, and after genuinely restarting the server both setups, the moved operation's `system_setup_id: SETUP-001` / `user_setup_id: SETUP-002`, the 30/1 split and the feedback row all came back intact.

### Presentation — the estimate page split into sections (not a roadmap step)

- **The problem, measured before changing anything.** `#tab-estimate` held **18
  cards**, **10 of them visible with nothing uploaded**, and 2,502 px of page when
  empty. Uploading a real 63 KB analytic part and analysing it produced **6,621 px
  — about 12.4 screens** at the browser's 534 px viewport, and **91 px of
  horizontal overflow**: the operations table was 600 px wide in a 487 px column,
  so process names rendered as "drilli" and features as "Drill ho". The
  per-feature table — 78 rows of identifiers, axes and CAD face counts for 39
  features — sat on the primary page, above the costing inputs.
- **Six sections, and the cards were moved rather than rewritten.**
  `#tab-estimate` carries `data-flow`; a new `estimateflow.js` toggles it. Each
  existing card moved verbatim into one `.flow-pane`, and the splice was gated on
  a check that **not one of the 121 element ids disappeared** (16 were added),
  because every handler in every other file addresses its elements by id. No card
  was rewritten and no render function was rehomed.
- **Result, on the same part.** Overview **1,415 px**, CAD **1,462 px**,
  Manufacturing **4,507 px**, Cost **1,860 px**, Assembly **777 px**, Quote
  **588 px**, and **no horizontal overflow** (-14 px) in any of them. The empty
  state went from 2,502 px to 1,196 px. Manufacturing was taken further in the
  polish pass below.

### Presentation — the Manufacturing routing made scannable (not a roadmap step)

- **Where the height actually was.** Measured on the same 39-feature part with 31
  operations in one setup: the pane was **4,217 px**, the operations table
  **2,169 px** of it, and **2,077 px of that was 31 rows at 67 px each**. A row was
  67 px because the operation cell stacked the process select over the name input
  with a `<br>` and the sequence cell stacked its two move buttons under the
  number — which also made the `#` column **126 px**, wider than Feature and
  Confidence together. Content was never the problem: the tallest thing in any
  cell was 31 px.
- **One line per operation.** Process and name share a flex `.op-cell`; the move
  buttons sit beside the number; cell padding went from 6 px to 4 px and the first
  column's 110 px minimum — sized for the old stacked buttons — was dropped. Rows
  are now **35 px, uniform** (a feature id was breaking at its hyphen, so
  `FIL-001` rows had been 12 px taller than `H-001` rows). The `#` column is
  **67 px** and the Operation column grew from 184 px to 362 px.
- **A labelled `Details` control.** Clicking a row had always revealed its
  explanation, rule id and parameters, and nothing on screen said so.
  `toggleEvidence` is now shared by the row click and a `Details` button that
  reports `aria-expanded` and toggles its own label. The evidence row itself is
  unchanged.
- **Figures before prose.** The charging-rules paragraph, the numbered sequence
  list and the per-feature suggestion breakdown are folded into disclosures —
  prose, or a repeat of the table's own order — so the section opens on
  `#plan-impact`, the backend-computed setup summary: **1 setup, setup time 0 min
  (charged once each), cycle time 93.9 min over 10 parts, operation cost ₹3,047.00
  = ₹0.00 setup + ₹3,047.00 run**, with the delta since the last change and the
  warning that the setup has no committed time. All three folded panels are intact
  and reachable; the suggestion breakdown still lists all 22 lines.
- **No per-operation cost was added, and none was invented.**
  `/operations/plan-cost-preview` returns the plan's `total_operation_cost`,
  `setup_cost` and `run_cost`, not a figure per line. Showing one would have meant
  multiplying a rate by a time in the browser, which is exactly what this codebase
  forbids, so the cost is reported where the data supports it — per setup and per
  plan.
- **Result.** Manufacturing pane **4,217 px → 2,071 px (-51%)**; the page
  **4,542 px → 2,396 px** with times accepted, 2,931 px before they are. Operations
  table 2,169 px → 1,167 px, sequence card 798 px → 150 px, suggestions card
  636 px → 184 px. **Horizontal overflow: none** (-14 px), and the row's Remove
  button ends at 1,105 px of a 1,160 px viewport.
- **Nothing about the plan changed.** 31 operations, sequence positions 1 to 31,
  one setup with all 31 members, the same process and feature on every line. The
  estimate comes to **₹3,060.86 total, ₹306.09 per unit, ₹13.86 material,
  ₹3,047.00 operations, 1.7319 kg** — identical to before the pass. `assembly.js`
  renders a component's plan through the same `renderPlan`, so component plans get
  the same layout; this was verified live in the pass below, once an assembly with
  measurable component geometry was available.
- Added 2 tests to `tests/test_manufacturing_plan.py`. Suite: 1,721 to **1,723
  passing**. No Python file changed.

### Fix — an assembly roll-up could not be quoted (not a roadmap step)

- **The bug.** `/cost/assembly` has always persisted its own immutable estimate,
  but `AssemblyCostSummary` carried no reference to it — 34 fields, none of them
  the id. With nothing to point at, the Quote screen fell back to
  `state.result`, which only `/cost/cad-part` ever sets. So an assembly whose
  components had just been costed at ₹2,221.62 was quoted as a **material-only
  part estimate**, because that is what the same file costs when treated as one
  lump of steel. Both numbers were correct; the screen was showing the wrong
  estimate.
- **The fix, in three places.** `AssemblyCostSummary` gained
  `estimate_id: str | None`; `/cost/assembly` stopped discarding the id
  `EstimateRepository.save()` already returns; and the Quote screen now chooses
  through `quotableEstimate()` — the roll-up when the upload is an assembly and
  one exists, the part estimate otherwise. Both quotation buttons issue whichever
  was chosen, and the screen says which.
- **No arithmetic changed.** The two Python edits are a model field and one
  assignment. Verified: the same roll-up run twice produces identical figures and
  differs only in the id, per-assembly and production totals still separate, and
  `assembly_quantity` still multiplies exactly once.
- **Incompleteness is still reported.** The quotation for a roll-up whose plans
  are not fully committed carries "3 operation(s) carry no committed time"
  alongside the illustrative-rate warnings, and uncommitted operations are still
  excluded rather than zeroed.
- **The roll-up total is now labelled "Assembly total (one assembly)"**, because
  it sat beside a production-quantity field and invited being read against it.
- Live: assembly quoted at **₹2,221.62** with `subject.kind: "assembly"` and the
  component section populated; the competing part estimate for the same file
  (₹1,209.13) correctly not quoted; single part unchanged at **₹5,805.40 /
  ₹580.54** and still quoting its part estimate. No console errors, no overflow.
- Added 7 tests and reworked one determinism test to exclude the per-call id the
  way it already excludes `elapsed_ms`. Suite: 1,724 to **1,731 passing**.

### Fix — the browser kept running the previous UI, and Cost had no estimate of its own (not a roadmap step)

The previous fix was correct and shipped, and the mismatch was still on screen.
Two separate causes, found by inspecting the running system rather than the
source.

- **Cause 1: Chrome was executing a superseded `estimateflow.js`.** There is no
  build step, so no asset filename carries a content hash — every release is
  served as `app.js`, `estimateflow.js` and so on at the same URLs. Starlette's
  `StaticFiles` sends `etag` and `last-modified` but no `cache-control`, which
  leaves a browser free to apply heuristic freshness and reuse its stored copy
  without asking. It did: a freshly loaded page from the corrected server had
  `isAssemblyUpload` but no `quotableEstimate`, and fetching the same URL twice
  returned **13,403 cached bytes against 16,178 on disk**. Every symptom of that
  looks exactly like an application bug. `RevalidatedStaticFiles` now serves the
  `/ui` mount with `cache-control: no-cache`, which requires revalidation rather
  than forbidding caching — the etag still turns an unchanged file into an empty
  304. Confirmed live: an edit made seconds earlier was picked up on an ordinary
  navigation, with no hard reload.
- **Cause 2: only the Quote screen had been taught which estimate was current.**
  The Cost section still showed whatever `/cost/cad-part` last returned, with no
  indication that an assembly's estimate is the roll-up, and the context bar and
  the Overview tiles read that same panel. So an assembly rolled up to ₹2,221.62
  displayed ₹1,209.13 above every section of the flow, and the recommended next
  step was *Calculate the estimate* — the button that produces the wrong one.
  Worse, `markRollUpStale()` discarded the roll-up when a classification changed
  and the Quote screen then quietly fell back to the part estimate, which is the
  original defect returning through a different door.

**The fix.** `quotableEstimate()` became `authoritativeEstimate()`: one function
naming the estimate that describes what is loaded, read by the Quote screen, the
new Cost banner, the context bar, the Overview tiles and the next-step
recommendation. For an assembly it returns the roll-up or nothing at all — there
is deliberately no fall-back to the part estimate, because for an assembly that
estimate is not an approximation of the answer, it is the answer to a different
question. It remains saved, and remains quotable from History, where choosing it
is a decision rather than an accident. Staleness became a state of its own
(`state.assemblyCostStale`) so "discarded because its inputs moved" and "never
costed" can say different things.

- `#cost-authority` leads the Cost section for an assembly: which estimate this
  is, its id, its per-assembly and production figures, and — when a part
  estimate is also on screen — what the panel below actually is, with its own id
  beside it. For a single part the banner is not rendered at all.
- The Overview's quantity tile now belongs to the estimate above it
  (*Assemblies to build 1*, not the part form's *Quantity 10*), and the
  next-step recommendation routes an assembly through Decompose → Cost every
  component → Quote instead of through whole-file feature analysis.

**No costing arithmetic changed.** The only Python change is the static-files
subclass; `roll_up`, every calculator and every rate path are untouched.
Verified rather than asserted: the same assembly costed four times across two
browser sessions returned ₹758.2160020607175, ₹38.38150633167858 and
₹772.5416315057277 per component every time, and ₹2,221.6247475366595 in total,
including once after a classification was changed to `purchased` and back.

**Live verification** (fresh throwaway database, port 8050, `poc-default`
v9.0.0, Chrome at 100% zoom):

| | |
|---|---|
| Assembly roll-up | **₹2,221.62**, `estimate_id` returned with it |
| Cost section | names that same id, and labels the ₹1,209.13 part panel as a different estimate with its own id |
| Context bar | ₹2,221.62 — previously the part figure |
| Quote screen | both quotation links carry the roll-up id; neither carries the part id |
| Quotation | `subject.kind: "assembly"`, ₹2,221.62, component section populated, `engineering` omitted from the customer view |
| Warnings | all three still shown, including "3 operation(s) carry no committed time" |
| Stale roll-up | classification changed → zero quotation links, an explanation, and the part estimate sitting in state **not** substituted |
| Single part | `model1_1.stp` unchanged at **₹5,805.40 / ₹580.54**, still quoting its part estimate, banner absent |
| Layout | no console errors; horizontal overflow 0 px on all six sections |

- Added 10 tests (8 in `tests/test_ui.py` replacing the 2 provisional ones, plus
  a cache-header test and 2 backend tests in
  `tests/test_component_classification.py`). Suite: 1,731 to **1,740 passing**,
  1 skipped.
- **Note for anyone running the app:** a browser that visited it before this
  change may still hold the old assets. One hard reload (Ctrl+Shift+R) clears it
  permanently; every load after that revalidates on its own.

### Fix — an assembly's plans are part of decomposing it, and a partial total says so (not a roadmap step)

Two defects with one cause: the manufacturing half of an assembly's cost was
opt-in, and skipping it produced a number that looked finished.

- **The plans were behind an undeclared dependency.** *Cost every component*
  reads the component plans in the browser's hand; if *Generate component plans*
  had never been pressed, there were none, and each manufactured component was
  costed for its material and its tooling alone. Nothing on the screen connected
  the two buttons. Diagnosed from a real estimate in the demo database:
  `plan_snapshot` was `NULL` — that field is written whenever any plan reaches
  `/cost/assembly` — with `total_operation_cost: 0.0` and a ₹902.57 total, beside
  a ₹2,221.62 rehearsal of the same file.
- **The roll-up called that state complete.** `is_complete` meant "every
  definition was costed or excluded", which was true, so the summary read *"Every
  one of the 3 component definitions was costed"* over a total with nothing in it
  for making anything.

**Plans now arrive with the structure.** Decomposing an assembly builds every
manufactured component's plan through the endpoint that already existed —
`/cad/assembly/plans`, the same feature recognition, the same suggestion rules,
the same time estimator, the same setup model. There is no second pipeline and
no new endpoint; a structural test asserts the browser calls that URL exactly
once. It is not awaited by the decompose handler, so the tree, the bill of
materials and the classifications stay usable while the plans are built (2.2–3.0 s
on `Assembly.stp`), and it is skipped entirely when nothing is plannable, so a
102-component export that carries no component geometry is not sent away for a
minute to discover it can plan nothing.

**Generating is still not accepting.** Every operation comes back with the
system's estimated time and `system_time_accepted` false, exactly as a single
part's suggestions do; the plans read *needs_user_input* until a person accepts
or types a time, and the status line says so every time it is written. A test
asserts the generation path contains none of the four fields that could commit
one.

**Reclassification keeps the plans honest without destroying committed work.** A
component that stops being manufactured loses its plan with no round trip — the
answer is already known. One that becomes manufactured gets a plan built, merged
so that any plan already held is kept: a rebuild returns freshly estimated times
with nothing accepted, and adopting those wholesale would silently discard every
time the engineer had committed. Verified live: with Drawn Cup and cage
committed, reclassifying Needle Roller to purchased and back left 4 and 79
committed operations untouched and gave Needle Roller a fresh, uncommitted plan.

**A partial total now says which kind of partial it is.** `unplanned_definition_count`
counts manufactured components that were costed with no committed manufacturing
time; they carry `manufacturing_committed: false` and the existing reason
vocabulary's new `no_committed_operations`, and `is_complete` requires their
absence. The note leads *"Partial assembly estimate — manufacturing plans
incomplete"* and names the components. The quotation refuses it —
`assembly_manufacturing_incomplete`, alongside the blockers that were already
there — while the engineering view is never withheld.

The distinction that keeps this from over-reaching: `manufacturing_committed` is
**tri-state**. `None` means the caller supplied no plan facts at all, which is
the pre-plan contract `roll_up` has always honoured and says nothing about; only
facts that show nothing committed make it `False`. A partly committed component
is *complete* here and reported by the existing `partial_costing` warning — the
demo assembly has two operations the estimator cannot time, and blocking on those
would refuse a quotation the engineer has genuinely finished reviewing.

**Nothing about the arithmetic moved.** No formula, rate, tooling rule, quantity
multiplication or setup charge was touched, and the two figures are the ones the
existing calculation path produces:

| State | Total | Status |
|---|---|---|
| Plans generated, nothing committed | **₹902.5716225366596** | `is_complete: false`, 3 unplanned, quotation refused |
| Two of three committed | **₹1,831.46** | `is_complete: false`, 1 unplanned, names *Needle Roller* |
| All three committed | **₹2,221.6247475366595** | `is_complete: true`, quotation available |

₹902.5716225366596 is exactly the stored figure the old workflow produced, to
sixteen digits, and every per-component figure in the committed run matches the
rehearsal identically (₹758.2160020607175 · ₹38.38150633167858 ·
₹772.5416315057277). Neither number is hard-coded anywhere.

**Live verification** (fresh throwaway database, port 8060, `poc-default` v9.0.0,
Chrome at 100% zoom): one *Decompose* click produced 4 definitions, 20
occurrences and 3 component plans with 4, 5 and 79 operations, all uncommitted
with their estimates shown; costing before committing gave ₹902.57 labelled
partial on the Assembly panel, on the Cost banner and on the Quote screen, with
the customer quotation refused and the reason given per component; committing the
three plans gave ₹2,221.62, complete, with one estimate id across Cost, Quote and
the quotation, `subject.kind: "assembly"`, the component section populated and the
`partial_costing` warning still shown for the three operations the estimator
cannot time. `model1_1.stp` is unchanged at **₹5,805.40 / ₹580.54**, generates no
component plans, shows no assembly banner, and carries no stale assembly state
from the previous file. No console errors; horizontal overflow 0 px on all six
sections.

**UX follow-up: the Edit button now goes where the plan is shown.** *Edit this
component's plan* retargeted the operations table correctly and left it
invisible — the table lives in the Manufacturing section, and
`scrollIntoView` on an element inside a hidden pane scrolls nowhere, so the
click looked inert and the user had to find the tab themselves.
`editComponentPlan` now calls `setFlow("manufacturing")`, guarded forward
because `estimateflow.js` loads after `assembly.js`, with the previous
scroll as the fallback. It lands at the top of the section, where the
`plan-scope` banner names the component before the routing appears. Only the
explicit button navigates: clicking a component's *name* still opens its
detail beside the tree and isolates it in the viewer, which is a different
intention. Verified live — Assembly → detail → Edit switched to
Manufacturing with Drawn Cup's four operations and the banner "Editing the
manufacturing plan for Drawn Cup, a component of Assembly"; repeated for the
cage (79 operations); committing times there still mutates the component's
own plan object (`entry.plan === state.plan`); single-part navigation across
all six sections unchanged with no scope banner and no console messages at
all. One test added. Suite: **1,760 passing**, 1 skipped.

- Added 16 tests (11 in `tests/test_component_plans.py`, 5 in `tests/test_ui.py`)
  and corrected 8 that encoded the old meaning of "complete" — each now asserts
  the honest state, and the arithmetic assertions in them were left untouched.
  `commit_every_time` moved to `tests/assembly_fixtures.py` beside a new
  `committed_plans` helper, so a fully reviewed assembly is built one way.
  Suite: 1,740 to **1,759 passing**, 1 skipped.

### Presentation — the setup band, and component plans verified (not a roadmap step)

- **The last wrap, and why no column width could fix it.** The setup row borrowed
  the operations' columns: its setup-time input sat under *Cycle*, its "Needs
  input" pill and "Use estimate" button under *Setup* (89 px, so the button broke
  onto a second line), and its grouping pill under *Source* and *Confidence*.
  Widening *Setup* would only have taken width from *Operation*. A setup heads the
  operations beneath it rather than being one of them, so `setupHeaderRow` now
  emits a single `colspan="8"` cell holding a flex band. Every class and handler is
  unchanged — `wireSetupRows` finds its controls by class, never by cell position,
  and the tests that pin `setup-name`, `setup-time`, `setup-use-est`, `setup-up`,
  `setup-down`, `setup-remove`, "System grouping" and "User grouping" all still
  pass.
- **Measured.** At a desktop width (1,146 px shell, 1,065 px table) the band is
  **47 px on one line** — sequence and move buttons 48 px, identity 434 px, timing
  284 px, grouping 172 px, Remove 60 px, totalling 998 px in 1,053 px — and "Use
  estimate" is **25 px** rather than two lines. Below about 1,000 px the band wraps
  *between groups* while the button itself stays 25 px at every width tested.
  Operation rows remain **35 px, uniform**; horizontal overflow **none**.
- **Nothing was lost.** Checked element by element: the band still carries the
  sequence number, both move buttons, the name input, the member/costable count,
  the setup-time input with its estimate as placeholder, the source badge, "Use
  estimate" when one applies, the grouping pill and Remove. An operation row still
  carries all twelve of its controls plus its evidence row. All four disclosures on
  the section open to their full content.
- **Component plans verified live — the gap left open by the previous pass.** No
  assembly with measurable component geometry had been available; `Figure 3.stp`
  has structure but no geometry, so it yields zero plans. A real analytic assembly
  (**20 component placements across 4 product definitions**, 3 with measurable
  geometry) was used instead, and it exercised the whole path: decompose → 4
  definitions / 20 occurrences → classification (7 rows) → **3 component plans
  built in 2.4 s** → opening one loads it into the shared table with the
  `plan-scope` banner naming *"Drawn Cup, a component of Assembly. Its setups are
  its own — no other component shares them"*, 4 operations at **35 px**, a **47 px**
  band, 4 Details buttons → roll-up **₹378.42** with the sub-assembly reported as
  not costed rather than counted twice. The file was staged temporarily, read, and
  removed; it is not in the repository.
- **Costing untouched.** The single part still comes to **₹3,060.86 total, ₹306.09
  per unit, ₹13.86 material, ₹3,047.00 operations, 1.7319 kg** over 31 operations
  in one setup, sequence 1 to 31.
- Suite: **1,723 passing** (1,724 collected, 1 skipped). No Python file changed, no
  endpoint, no schema, no migration, and no per-operation cost was introduced.
- **Evidence is one click away, not gone.** The per-feature table, its provenance
  note and the selected-feature panel are inside a *View detailed feature analysis*
  disclosure labelled with its row count; the features card is **725 px closed and
  2,909 px open** (page 1,462 px → 3,646 px). The "not suggested" list is behind a
  disclosure that hides itself when nothing was declined. The CAD-format capability
  matrix, previously permanent, folds away after upload.
- **The engineering views left the primary flow.** Top-level navigation is now
  *Estimate · History* then *Rate sets · Time profiles · Engineering*. All seven
  feedback sub-views (Summary, Operations, Times, Setups, Make/buy, Confidence,
  Evidence) are intact under Engineering with their filters and Reset.
- **`estimateflow.js` arranges the page and computes nothing.** It reads the
  strings `app.js` already rendered rather than reformatting the API responses a
  second time, so the overview cannot disagree with the breakdown it summarises,
  and it borrows `plan.js`'s `isCostable` instead of repeating the three-branch
  time rule a fourth time. A test greps it for costing arithmetic, for a format
  list, and for `system_time_accepted`.
- **Five defects found in the browser and fixed.** (1) The context bar read "No CAD
  file yet" for the whole ~30 s geometry parse of the 198 MB export, because the
  only refresh ran after the geometry render; identification finishes in ~1.4 s and
  now refreshes it. (2) The overview's geometry-gap warning and its recommended
  next step were stale, because `state.gap` is set *after* the render the hook was
  attached to. (3) The Assembly section stayed dimmed, and the subtitle read
  "single part", for a file declaring 102 occurrences — both now read
  `structure_state` through one helper. (4) `nextStep` treated `capabilities` as a
  keyed object when it is a list of reports, which silently made every capability
  look supported. (5) Selecting a new file left the previous estimate's cost on
  screen — survivable down a long page, misleading in an always-visible bar — so
  the result panel is retired on upload while `state.result` is kept as the
  baseline for "Why did this change?".
- **Live verification.** Single part: upload → CAD → Manufacturing → Cost → Quote,
  39 features, 31 suggestions, 63 plan rows, **₹3,060.86 total and ₹306.09 per
  unit — identical to the pre-restructuring figure** — and the quotation opens and
  renders that total. Assembly, on the real 198 MB `Figure 3.stp`: identified in
  seconds, 43 definitions and 102 occurrences read in 26.2 s, 73 classification
  rows, **0 component plans built in 51.5 s** (every component blocked for want of
  geometry, which is the correct answer), and a roll-up that still refuses to count
  a sub-assembly twice. The bill of materials was 618 px in a 579 px pane and now
  scrolls inside its own panel. History, Rate sets, Time profiles and Engineering
  all still render.
- Added 4 tests and one parametrize case to `tests/test_ui.py`; rewrote
  `test_the_ui_workflow_is_numbered_in_order` as
  `test_the_ui_workflow_is_split_into_ordered_sections` and updated two heading
  assertions that pinned the old numbering. Suite: 1,716 to **1,721 passing**
  (1,722 collected, 1 skipped). One file added, `static/estimateflow.js`. **No
  Python file changed, no endpoint, no schema, no migration.**

### Iteration 7 — wall thickness, setups from tool access, sheet-metal unfolding

Three agents, each in its own worktree from the pushed branch, reviewed and merged into
`timed-welding`. Two merges conflicted only because each agent appended a section to the end of
`main.py`; the file was rebuilt as the sections in sequence, after confirming neither branch
changed anything else there except one message wording. Full suite on the merged code: **2491
passed, 8 skipped** in about 2.5 min (Linux, Python 3.13, no `cadmodels/`). The merged app was
started on a scratch database and the new endpoints exercised together on generated fixtures:
the sheet analysis of an L-bracket gave t = 2.0 mm and a 251.94 mm cut perimeter at K = 0.4, the
independent wall measurement of the same part also gave 2.0 mm, `/cad/dfm` ran five rules, and
the page loaded with every new panel and no script error. The numbers below were observed by each
agent against a running server on a scratch database before the merge. **No real model was
measured**: `cadmodels/` is absent from this environment, so all three were verified on generated
fixtures only.

**Measured wall thickness.** `features/wall_thickness.py` casts a ray along each face's inward
normal from a fixed 5x5 grid on every face of every exact B-Rep solid; the wall at a point is the
distance to the face the ray leaves through. Exact for parallel planes and coaxial cylinders:
fixtures measured 2.0 (pocket floor), 3.0 (tube), 10.0 (plate) and 0.6 mm (rib), each the known
value. Every response carries its limits: the minimum is the thinnest wall *found* — the true one
can only be thinner (an over-budget 906-face part sampled at 4x4 reported 3.64 mm against a real
2.0, with the budget note) — and a tapering wall reads thick. Faceted STEP, STL and IGES surface
models are refused. `CAD_COSTING_MAX_WALL_RAYS` (20,000) coarsens the grid and says so; a 406-face
part measured in 1.4 s. `POST /cad/wall-thickness` is read-only. Its one consumer is the DFM rule
`dfm_min_wall` in a new seeded profile version, `dfm-general-guidelines` 1.1.0 (1.0.0 unchanged;
fresh and existing databases checked identical): 0.8 mm, quoted from the same CNC guide 1.0.0
cites — the quote was re-fetched and confirmed during review. It is the guide's *metal* figure
applied to every material, because a profile has no material; the guide gives 1.5 mm for
plastics, which a plastics shop must publish as its own version. The flag names no feature and
carries a marker the viewer draws on the thinnest wall. Wall thickness feeds no cost, time, route
or sheet detection yet.

**Setups proposed from tool approach directions.** `features/approach.py` reads from the B-Rep
which way a tool must approach each milled or drilled feature — a blind hole out through its open
end, a through hole either end, a pocket's floor normal, a face's outward normal, a blend's
blended planes, and a planar face's side by the tool's flank along a direction another feature
already needs — and checks each direction with straight rays (5 per hole end, a 3x3 grid per face)
that must all leave the part. `setup_proposal.py` groups operations by a deterministic greedy
cover, one setup per direction on a 3-axis mill, said not to be guaranteed minimal. **Nothing is
applied by default**: `build_plan` still starts with one assumed setup, `POST /cad/setups/propose`
returns the proposal beside the plan, and `POST /cad/setups/apply-proposal` recomputes it on the
server and regroups through the existing `with_operation_in_setup` (`user_setup_id` written,
`system_setup_id` untouched); new setups carry `system_grouping: "approach_direction"` and an
uncommitted baseline estimate. Unreachable or rule-less operations are listed and left where they
were; confidence is medium at most, low when anything is unplaced. Live: holes on top and one side
gave +Z and +X setups; with both 10-minute estimates accepted the operations went from 360.65 to
660.65, exactly one extra setup at 10 min x 1,800/h. An overhung bracket reported "+Z: 5 of 5
ray(s) hit material, the nearest 29.990 mm away" and used -Z. Limits stated on every proposal: a
ray is not a tool (no holder or shank collision, tool length, undercut or fixture check). Because
the cover minimises setups it leans on flank milling — an irregular bracket's +X setup side-mills
about 8 faces — and the suggested plan faces the bottom of every block, so "holes on top only"
proposes two setups. Applying counts in the feedback as the engineer moving those operations.

**Sheet-metal flat pattern, cut and bend times.** `features/sheet_metal.py` decides from the
exact B-Rep whether a part is a constant-thickness sheet (opposite skins at one t, coaxial r / r+t
bend cylinders, every other face square through t, volume equal to t x the unfolded area within
0.1%), reports each bend and unfolds the flange tree with BA = θ(r + K·t). **K is the engineer's**,
a request field on `POST /cad/sheet-metal` (0 < K <= 0.5) with no default; without it the flat
pattern is null and `needs_input` is `["k_factor"]`. Cut perimeter, contours and pierces come from
the unfolded outline, and a flat pattern is claimed only when its area and perimeter (Green's
theorem over the exact lines and arcs) agree with the flanges plus bend allowances. Hems, closed
rings of bends, sharp corners and cut-outs across bends are reported unsupported, never
approximated. Seven fixtures matched the hand flat size, perimeter and pierces to 1e-5 (e.g. an
L-bracket 40 x 85.969 mm, perimeter 251.938 mm, 1 pierce). Sheet cutting (perimeter / table speed
+ pierces x pierce time, interpolated in the table, never extrapolated) and bending (bends x time
+ repositioning) join the time model, all None by default, with an illustrative profile
`illustrative-sheet-2026` (estimates, no published source). `sheet_cutting` and `bending` are now
timeable stages; `POST /operations/sheet-metal-lines` proposes unaccepted cut and bend lines from
the measurement, each in its own setup. Live: L-bracket x 10, both stages timed: cut 422.50, bend
410.00, deburring by stage rate 60.00, total 892.93 — the same in the API, the browser and the hand
calculation. Material is still the finished part's CAD mass: the blank is reported, not charged,
and there is no nesting or utilisation.

**Debt this iteration left.** Three paths now obtain "the exact shape plus its recognized
features" — `recognize_features_with_shape` (walls), `features.approach.shape_and_features`
(setups) and `analyse_sheet_metal_from_bytes` (sheet). Each calls the single STEP/IGES readers, so
none is a second parser, but they repeat the faceted/IGES gating and should become one function.

### Iteration 6 — tool life, cost drivers, weld prep and finishing (built in parallel)

Three agents, each in its own worktree from the pushed branch, reviewed and merged into
`timed-welding`; one merge conflict in `main.py` (two independent new helpers) was resolved by
keeping both. Full suite on the merged code: **2316 passed, 8 skipped** in about 2 min
(Linux, Python 3.13, no `cadmodels/`). The merged app was started on a scratch database: both
new endpoints present, the page loaded with no script error, and a costed part's drivers summed
to its stored total. The live numbers below were observed by each agent against a running
server on a scratch database before the merge.

**A stage's one-off charge amortised over the tool's life.**
`stage_inputs[stage].amortisation_quantity` — a whole number of pieces the pattern or die is
expected to make, stated by the engineer, never defaulted — charges this estimate only
`batch_charge x quantity / amortisation_quantity`, unrounded until output. On such a line
`batch_charge` is the share charged (so `total_cost = run_cost + batch_charge` still holds) and
`full_batch_charge`, `amortisation_quantity`, `batch_charge_basis: "amortised_share"` and a note
say the rest is carried by pieces outside this estimate; without one a line has exactly its
previous keys. A tool life below the quantity (that would silently buy a second tool), one on a
stage with no one-off charge, one on a timed stage, or a non-whole number is a 400. The
comparison names the change; the quotation's route-stages line carries the note **in both
views**, because a customer total that includes only a share of a tool should say so. The page
offers the input where the selected rate set's stage rate has a batch charge. Live
(`block_with_counterbore`, 5 off, casting 120/kg + 15,000, 3.0 kg): the casting line went from
16,800.00 to 2,550.00 (a 750.00 share) at a tool life of 100; in a matrix, quantities 1/40/100
gave stage totals 540/21,600/54,000 and 150 was refused with the reason. The route card's stage
inputs, never seen in a browser after Iteration 5's yield input, were found stacked in one tall
column and now sit side by side (checked at 1280 and 900 px). Not done: bulk preview sends no
stage inputs; the rate set's top-level `tooling_cost_per_batch` is unchanged; no cavity count
(flat stage rates have no cycle for it to act on).

**Cost-driver breakdown.** `GET /estimates/{id}/cost-drivers` breaks one stored estimate's
total into the stored figures that built it, each with its share of the stored total: material
(0.00 with the stage's note when a casting or purchase supplies it), each priced route stage,
operations grouped by process type (setup, machine and labour as stored — never rate x time),
overhead, scrap and tooling. Build-up order, never sorted, nothing called a hotspot. Any
remainder is its own `reconciliation` driver; an absent field is "not recorded", never zero.
An assembly roll-up breaks down by its stored categories and per component; an incomplete one
is `partial` with the roll-up's own reason. `cost_drivers.py` imports `money` only. UI: a card in
the Cost section, a disclosure in History and in the roll-up card (`drivers.js`), share bars
whose width is the server's percentage. Live (`block_with_counterbore`, `poc-machine-shop`, 5
off): seven drivers summing to the stored 3,304.05 exactly — milling 63.56%, drilling 13.12%,
overhead 9.59%, tooling 9.08%. Setup sits on whichever process's operation the plan charged it
to, and the row says so. Not seen in a browser: the assembly disclosure (API only).

**Weld preparation and finishing from the typed weld.** Two optional time-profile rates, absent
by default: `welding_butt_prep_mm_per_min` (butt welds only — a fillet is never charged
preparation) and `welding_finishing_mm_per_min`. Each turns the weld length into minutes, added
to the weld's cycle as its own named component. `illustrative-welding-2026` 1.2.0 adds prep 100
and finishing 200 mm/min (estimates, no published source). Travel speed by weld size was
deliberately not added: passes already scale with size. Live: a 600 mm x 8 mm MIG weld is 10.39
min under 1.1.0, and 13.39 as a fillet / 19.39 as a butt weld under 1.2.0.

**Tacking and grinding are each charged once.** The cutting + welding route carries `fit_up`
("Fit-up and tacking") and `weld_finishing` stages priced by stage rate, while the profile can
put tack and finishing time inside each weld. Both stages may now be priced by weld time, only
together with welding; `/cost/cad-part` refuses a system-timed weld carrying either component
beside that stage's rate (`422 fit_up_charged_twice` / `weld_finishing_charged_twice`), and a
timed stage whose welds carry none (`422 timed_stage_not_costed`). A typed weld time is the
engineer's whole figure. Repositioning is no stage's work and stays in the weld. **Behaviour
change:** under the illustrative 1.1.0/1.2.0 profiles, timing welding alone on `cutting_welding`
is now refused until fit-up (and, under 1.2.0, finishing) is timed too. The test that had
accepted the double charge was changed to expect the refusal. Live (5 off, welding 550/h, stage
rates cutting 25, fit-up 40, finishing 50 per piece): a fillet weld under 1.1.0 with welding
alone timed is refused; with fit-up timed too it costs 852.66 with no fit-up line. Not done:
the route comparison still labels every stage's pricing as stage rate.

### Iteration 5 — five aPriori gaps closed (built in parallel)

Five agents each built one feature in its own worktree; each was reviewed, re-tested and merged
into `timed-welding`. Full suite on the merged code: **2236 passed, 8 skipped** in about 2 min
(Linux container, Python 3.13, no `cadmodels/`, hence 8 skips rather than 1). The merged app
was started against a scratch database: migration head `0008`, the new endpoints present,
`/ui/matrix.js` served `no-cache`. Each feature's live numbers below were observed by its
agent against a running server on a scratch database, before the merge.

**Yield on the material-supplying per-kg stage.** `stage_inputs[stage].yield_percent` (above 0,
at most 100; the engineer's number — no default, no hidden allowance) says what share of the
metal poured or billet bought ends up in the blank. The stage is charged on base mass /
(yield/100), the base being the entered blank mass or else the finished CAD mass. The line
states `base_mass_kg`, `base_mass_source`, `yield_percent`, the charged `mass_kg`,
`mass_source: "yield_applied"` and a note; with no yield a line has exactly its previous keys.
A later per-kg step (fettling) still works on the blank. A yield on a per-piece, batch-only,
non-supplying or timed stage is a 400. `/process-routes` lists `yield_stages` so the page
keeps no list. Live (`block_with_counterbore`, quantity 5, casting 120/kg + 15,000 per batch):
casting line 15,218.42 with no yield and 15,273.02 at 80% (0.364 kg -> 0.455 kg); a 3.0 kg
blank at 75% is charged on 4.0 kg, 17,400.00, and the comparison attributes the 600.00 change
as `route_stage` "yield none -> 75%" with nothing unexplained. Not checked in a browser: the
yield input's layout on the route card. Not done: the quotation does not print the yield; bulk
preview sends no stage inputs.

**Matrix costing preview.** `POST /cost/matrix-preview` prices one CAD part under each listed
rate-set version at each listed quantity, one `price_cad_part` call per cell with the plan
exactly as sent (no time is accepted). `kind: preview`, `stored: false`, `chosen: null`; rows
and cells in the caller's order, never sorted or marked; a cell that cannot be priced carries
the pricing path's reason and null figures; currencies are never converted. Capped by
`CAD_COSTING_MAX_MATRIX_CELLS` (default 24; a larger grid is a 400). `matrix_preview.py`
imports `money` only. UI: a *Matrix preview* disclosure in the Cost section (`matrix.js`).
Live: `block_with_counterbore` under `poc-machine-shop` 1.0.0 at quantity 10 gave 5,275.02
total / 527.50 per unit in both the matrix cell and `/cost/cad-part`; `stepped_shaft` under
`poc-machine-shop` was refused in every cell for want of a turning rate; the estimate count
was unchanged by previews. Not done: CSV export, an assembly matrix, geometry caching (each
cell re-reads the file).

**Supplier quote vs should-cost.** A recorded cost now states what kind of figure it is:
`figure_kind` is `actual` or `supplier_quote`, **required with no default** (as `cost_basis`
is), plus optional `supplier_name` and `quote_reference` for a quote only. **This is a
breaking change to `POST /estimates/{id}/actuals`: a payload without `figure_kind` is a 422.**
Migration `0008` adds the columns and marks every existing row `actual` — what each was
entered and shown as — leaving no default in the schema. An actual is still a variance
(estimate - actual); a quote is reported in separate fields as a gap (quote - should-cost,
percent of the should-cost) with a sentence giving direction and size and no verdict.
`GET /estimates/{id}/actuals` also returns the current quotes side by side in the order
entered — not sorted, none preferred. Live: a 242.00/unit should-cost against quotes of 100,
400 and 850 gave gaps -142.00 (-58.68%), +158.00 (+65.29%) and +608.00 (+251.24%), in entry
order. Not done: supplier master data, any ranking, a per-category comparison for assemblies.
A legacy row that was really a quotation must be corrected by hand (a correction may change
the kind).

**Per-weld handling time.** A time profile may state `welding_reposition_minutes_per_weld`
and `welding_tack_minutes_per_weld`, both None by default. Where stated, each is added to
that weld's cycle time (per weld, per part; never the setup) and named in the estimate's
parameters and explanation; without them the estimate is exactly the earlier one. Passes were
already `ceil(size / welding_mm_per_pass)`. The publish script now also publishes
`illustrative-welding-2026` 1.1.0 (reposition 0.5 min, clamp-and-tack 1.0 min — both
estimates with no published source) and is idempotent. Live: a 600 mm, 8 mm-leg MIG fillet is
8.89 min under 1.0.0 and 10.39 under 1.1.0 (4.00 / 0.45 + 0.5 + 1.0). **Known overlap:** the
arc-on factor's remainder is already described as "handling and cleaning", and 1.1.0 keeps
0.45 while adding fixed handling, so the two may double count; whoever publishes real figures
must decide the split. Not done: robotic programming time. **Corrected in Iteration 6:** the
clamp-and-tack time charged tacking a second time beside the `fit_up` stage rate on the cutting +
welding route; such a request is now refused (`422 fit_up_charged_twice`) unless fit-up is also
priced by weld time. Weld prep and finishing were added in Iteration 6.

**DFM flags pointed at on the 3D model.** `/cad/dfm` now returns `flagged_features` beside
`flags` — the recognized features the flags name, unchanged — so a flag row marks its
feature(s) in the viewer whether or not the features panel has been analysed. *Show all
flagged features* and *Clear markers* do what they say. A flag marker is a magenta diamond
with an axis line, drawn over the part (a through hole's location is on the far face), one
colour because a flag has no severity. It is a pointer at the recognized location, **not a
face highlight**. Live: a block with a deep hole (5.0 x diameter against 4), a deep slot
(6.0 x width against 4) and 7 hole diameters (against 6) gave 3 flags, 8 features, 8 markers.
Not seen: flags on an assembly (the fixture raises none) or a faceted file.

**Two environment fixes.** `cadquery-ocp` is pinned below 8: the 8.x bindings drop
`TopTools_Indexed*` and `TDF_LabelSequence`, so a fresh install failed feature recognition
with an ImportError. And one exploded-view test compared nested tuples with `pytest.approx`,
which current pytest refuses; it now compares flat.

### Welding priced by time (Iteration B)

Following the research into aPriori and Teamcenter Product Cost Management, welding can now
be costed the way those tools cost it: as operations with a time and an hourly rate, instead
of one per-piece stage rate. On the route card, the welding stage's *Priced by* choice
switches to "weld time × hourly rate"; its stage row then carries **+ Add weld**, and each
weld is a row where the engineer types type (fillet/butt), process (MIG/TIG), length and leg
or plate size — no weld is detected from CAD. The server estimates its time under the
selected profile (passes = ceil(size / mm per pass); arc = length × passes / travel speed;
cycle = arc / arc-on factor), and as with every estimate nothing is costed until it is
accepted or typed. The welding stage is then left out of stage pricing, so it is charged
once. A timed stage with no committed weld is refused (422 `timed_stage_not_costed`); asking
to time any other stage is a 400; sending no choice is the stage-rate behaviour unchanged.
The `welding_*` time parameters default to *absent*, so the seeded `poc-baseline-time` still
describes a machining shop and times no weld. Only welding is timeable; cutting, fit-up and
weld grinding stay on stage rates.

Illustrative data, published by script and never seeded: `illustrative-india-2026` **v1.3.0**
adds an hourly `welding` rate (machine ₹250/h from a built-up MIG-cell estimate — set,
power, wire, gas, tips — and labour ₹90/h, the same skilled-minimum basis; ESTIMATED), and
`scripts/publish_illustrative_welding_time_profile.py` publishes `illustrative-welding-2026`
(the baseline's 26 machining parameters unchanged plus MIG 300 mm/min, TIG 100 mm/min,
6 mm per pass, arc-on 0.45, setup 15 min; ESTIMATED). On the scratch database it is
**v1.0.1**: a v1.0.0 published while the server still predated the welding fields was stored
without them and, being immutable, stays there unused; the script now reads the server's
schema first and refuses instead.

Verified live in headless Chrome on `model1_1.stp`, 10 off, India rates v1.3.0, welding
profile v1.0.1, cutting + welding route:

| Welding priced by | Stages | Operations | Total |
|---|---|---|---|
| stage rate | cutting 250 + fit-up 400 + welding 1,000 + grinding 500 = 2,150 | none | ₹3,920.31 |
| time, nothing timed yet | — refused: "Welding / joining is priced by time, and the plan has no operation of it with a committed time" | | |
| time, one 600 mm × 8 mm MIG fillet, estimate accepted | cutting 250 + fit-up 400 + grinding 500 = 1,150 | welding: 2 passes, 4.00 min arc / 0.45 = **8.89 min**, setup 10 = ₹545.43 | ₹3,365.74 |

With only the length entered the weld said it was missing `weld_size_mm` and had no time.
Not done: the setup's own time is the plan's existing estimate (10 min, the largest machining
baseline when the plan was built), not the weld's 15 min — the engineer types it if the weld
fixture differs; and a weld added beside 31 parked operations is numbered after them.

### Route stages shown as rows of the operations table

Asked why a decided cutting + welding route showed none of its steps among the operations,
the answer was the rule that a stage is not an operation: a stage rate prices it, so it has
no time, no feature and no hourly rate. Research on aPriori, Teamcenter Product Cost
Management and Costimator (public pages and search snippets; aPriori's help refused direct
reads) found those tools cost every route step as an operation with a time and a rate —
aPriori times each weld from its type, length and size. The user chose to show the stages
in the table now and to plan timed welding as a later iteration.

`plan.js`'s `routeStageRows` adds one row per non-machining stage of a *decided* route, in
route order — those before the first machining stage above the setups, the rest below —
each with "Step n of m", the stage rate as published and the mass basis. They are display
rows: no `plan-row` class, nothing pushed into the plan, so no cost, no setup or cycle total
and no feedback record changes. On a route with no machining stage an empty setup band is
no longer shown (it stays in the plan). Verified live in headless Chrome on `model1_1.stp`
with `illustrative-india-2026` v1.2.0: casting + machining shows Casting and Fettling above
the setup (20 operations, 11 as cast); sheet cutting + bending shows its three steps; cutting
+ welding its four (₹25, ₹40, ₹100, ₹50 per piece), with the 31 operations parked below and
still 31 in the plan each time. `tests/test_ui.py` 57 passed; the full suite was not rerun
for this JavaScript-only change.

### Routes broken into the steps a shop quotes

At the user's request each non-machining route now carries the steps that follow its forming
stage, priced separately from the rate set: casting → **fettling, knock-out and shot
blasting**; forging → **flash trimming and shot blasting**; sheet cutting → **deburring**
(→ bending); cutting → **fit-up and tacking** → welding → **weld grinding and cleaning**;
moulding → **degating and trimming**; additive build → **support removal**. These *route
steps* are not detected from geometry: a recommended route gets them from the catalog with a
basis saying exactly that, and the route lookup ignores them, so no recommendation changed. A
per-kg step after a casting or forging with no mass of its own is charged on the same blank
(`blank_mass`). Every route still prices every stage or none, so a rate set without the new
steps now refuses those routes with the step named; `illustrative-india-2026` v1.2.0 adds
**ESTIMATED** rates for all seven. On `model2.stp`, 10 off, a 4.5 kg blank: casting ₹28,825 +
fettling ₹540 (12/kg × 4.5 × 10) = ₹29,365 of stages; cutting + welding ₹250 + ₹400 + ₹1,000 +
₹500 = ₹2,150. Full suite after the change: 2100 passed, 1 skipped.

### An illustrative India rate set (not client data)

`scripts/publish_illustrative_india_rates.py` publishes `illustrative-india-2026` v1.0.0
through `POST /rates`, flagged illustrative: 11 materials, milling/turning/drilling hourly
rates, overhead 20 % and scrap 2 %, and stage rates for every route stage except
*Purchased complete* (a bought part's price is part-specific, so no generic figure is
given). Figures came from public web research on 2026-10-27 — BigMint trade indices for MS
plate (₹61.4/kg) and EN8 (₹72.75/kg), otherwise mostly undated marketplace asking prices —
and every stage rate's note says whether it is SOURCED, DERIVED (a published figure plus a
stated assumption, e.g. laser cutting ₹450/m × an assumed 1 m per part) or PLACEHOLDER
(forging, moulding and drilling: no usable Indian figure was found). Labour is ₹90/h from
the Maharashtra statutory skilled minimum (no PF/ESI loading). The research found **no
public or partner API** for CostMasters or SIAM: both are login or subscription products,
and SIAM's statistics may not be redistributed without written permission. Published to a
scratch database only. On `model2.stp`, 10 off, Steel, no committed machining times:
milling + drilling ₹2,866.37 (material ₹2,349.49), casting with a 4.5 kg blank ₹4,666.50,
cutting + welding ₹4,391.37.

**v1.1.0** (published the same day, at the user's request "for demo purposes only") fills
every gap with the assistant's own **ESTIMATED** figures, each labelled so in its note:
casting pattern ₹25,000/batch, forging ₹130/kg + ₹75,000 die, moulding ₹15/piece + ₹2,50,000
mould, drilling ₹150 + ₹90/h, a new grinding rate ₹400 + ₹90/h, and a demo-only purchased
part at ₹500/piece. v1.0.0 is unchanged. With v1.1.0 every route on `model2.stp` prices:
casting + machining ₹3,516.65 per part at 10 off falling to ₹527.65 at 500 off as the
pattern spreads, purchased ₹6,100.00 for 10.

### Iteration 4 — route comparison, DFM flags and bulk preview (built in parallel)

The user asked for the three candidates from the aPriori comparison to be built at once by
separate agents, and decided: compare the recommended route plus the candidates; DFM
thresholds as cited, editable defaults; bulk costing as an unsaved preview; and commit the
current work to a new branch so the agents' worktrees could start from it.

**Preparation.** Iterations 1–3 and the turned-part work were committed to a new branch,
`iterations-1-3` (2a65b41). The `/cost/cad-part` pricing body was then extracted, unchanged,
into `main.price_cad_part` returning `PricedCadPart` (c6a0d1b; full suite unchanged at 2020
passed, 1 skipped), so that two agents would not each extract it differently. Three agents
then worked in separate git worktrees from c6a0d1b, each verifying live on its own scratch
database and port and committing to its own branch; none edited the shared docs.

**Route comparison** (`feature/route-compare`, e73fcde). `POST /cost/route-comparison`
prices the recommended route and every catalog route of each candidate family, each as if
decided, through `price_cad_part`. `route_comparison.py` (imports `money` only) selects and
orders them: recommended first, then catalog order — never by cost. A route that cannot be
priced is listed with the pricing path's code and message and null figures. The response is
`kind: preview`, `stored: false`, `chosen: null`; "Use this route" in the route card calls
`/cad/process/decide`. 17 tests. Live (agent, `model1_1.stp`, 5 off, every operation 2 min, a
test rate set with casting at ₹120/kg + ₹15,000): milling + drilling ₹9,806.93; casting and
casting + machining refused for want of a blank mass, then ₹16,800 and ₹23,300 with 3 kg;
estimate count 0 throughout.

**DFM flags** (`feature/dfm-flags`, 963fe4c). `dfm.py` (imports `features.model` only)
evaluates four rules against a versioned, immutable `DfmProfile` — table `dfm_profiles`,
**migration `0007`** (additive), seeded `dfm-general-guidelines` 1.0.0, illustrative:

| Rule | Threshold | Source |
|---|---|---|
| `dfm_deep_hole` | depth / diameter > 4 | "A Practical Design Guide for CNC Machining", Yijin Solution (vendor guide) |
| `dfm_deep_narrow_pocket_or_slot` | depth / width > 4 | same |
| `dfm_small_internal_radius` | concave fillet radius < 0.5 mm | same |
| `dfm_many_distinct_hole_diameters` | > 6 distinct diameters | **no published source found — illustrative** |

Other vendor pages give different ratios, which is why the profile is editable
(`POST /dfm-profiles/{id}/versions`; an uncited "published" threshold is refused). Wall
thickness and pocket corner radii are not measured, so no rule uses them. `POST /cad/dfm` is
separate from `/cad/operations/suggest` so advice cannot reach a plan. 28 tests. Live
(agent): `model1_1.stp` gives three deep-hole flags — H-002 80 mm deep at Ø16, H-004 and H-005
40 mm at Ø8, each ratio 5.0; a published 1.1.0 at ratio 6 gives none, and pinning 1.0.0
brings them back. Migration `0007` on a read-only copy of the real database: 0006 → 0007,
estimates 137, quotations 3, rate sets 8, time profiles 7 unchanged, `dfm_profiles` 1.

**Bulk preview** (`feature/bulk-preview`, 8682b64). `POST /cad/bulk/preview` takes a CSV
(`file, part_id, part_name, material, quantity` required; `rate_set_id, rate_set_version,
route_id, source_unit` optional; an unknown or missing column refuses the batch) plus the
CAD files. Each row is imported, recognized, suggested, planned and timed, and priced through
`price_cad_part` with the system's times accepted on an in-memory copy only — nothing is
stored. Every row, the response and every exported CSV row say "Unreviewed system estimates
— not an estimate, not quotable". Statuses `priced` / `refused` / `error`; a failed row
carries no figure; totals are summed per currency only. Limits in `limits.py`:
`MAX_BULK_ROWS` 100, `MAX_BULK_FILES` 50, `MAX_BULK_TOTAL_MB` 300, `MAX_BULK_CSV_BYTES` 1 MB.
27 tests. Live (agent): 7 rows — `model1_1.stp` ₹2,257.84 (5 off), `model2.stp` ₹1,488.91,
`model1_1_solid.igs` ₹691.57, a turned bush ₹1,260.84; an unknown material and a missing file
as errors; a casting route refused; estimate count 0 before and after.

**Merging.** Each branch appended a section to `main.py` and a script tag to `index.html`;
both conflicts were resolved by keeping every side. Two problems surfaced only on merge and
were fixed: git folded the route-comparison section's closing parenthesis into the shared
context (`main.py` would not compile until it was restored), and two structural tests had
assumed their section was last — the route-comparison load-order test (now "after
`actuals.js`, before `estimateflow.js`") and the bulk section test, which read to the end
of `main.py` and so saw the DFM section's `session.commit()` (it now stops at the next
section banner and still asserts one `price_cad_part` and one in-memory time acceptance).

**Tests.** Full suite on the merged code, main tree: **2094 passed, 1 skipped** (2095
collected), up from 2020. The agents' own full runs showed 8 skips because a worktree has no
git-ignored `cadmodels/`; the main-tree run is the one recorded.

**Combined live check** (scratch database, port 8730): DFM gave the same three deep-hole
flags; route comparison priced milling + drilling and refused both casting routes for want
of stage rates in `poc-default` (no plan was sent, so the priced figure is material only, as
the response says); bulk priced both rows at ₹3,746.75 together, exactly the agent's two
figures summed; estimates stored 0. In headless Chrome every tab opened with **no JavaScript
exceptions**, and the rate-set editor still draws its processes table and stage-rate table.

**Limitations.**
- Route comparison re-parses the upload once per route, and only offers candidate families
  the classifier listed; with no recommendation and no candidates it compares nothing.
- DFM: no profile selector or authoring in the UI (API publishing only); flags are not
  stored on estimates; *Show feature* needs *Analyse features* first; faceted files yield
  only hole flags; `dfm.js` reassigns the global `resetFeaturesPanel`.
- Bulk preview: synchronous and sequential, no progress feedback; the omitted-operation path
  never occurred live (covered by a test); a single assumed setup, the suggested drilling
  sequences and no confirmed bars, unlike a reviewed estimate; assemblies are costed as one
  part; per-kg stage masses cannot be supplied in the CSV.
- All three agents verified against the illustrative seeded or test rate sets, not client
  rates.

### Iteration 3 — non-machining route stages priced from stage rates

Iteration 2 let an engineer confirm *Casting followed by CNC machining* and then refused
to cost it. The user asked for costing-model improvements based on client inputs and
confirmed routes, and decided four questions before anything was built: rates per kg /
per piece / per batch; the per-kg mass entered by the engineer; a casting, forging or
purchase rate includes the metal; all non-machining stages in scope.

**What was built.**
- `rate_sets.StageRate` and `RateSet.stage_rates`, keyed by stage id: `unit_basis`
  (`per_kg`/`per_piece`) with `unit_rate`, and/or `batch_charge`, plus a `note`. Stored in
  the existing JSON column only when present, so **no migration** and seeded rows keep their
  exact shape. Keys are checked against the route catalog at publish and validate (an
  unknown stage or a machining stage is refused, 422). `RateSetSummary.stage_rate_count`.
- `stage_costing.py` (imports `money` and `rate_sets` only): `price_stages` returns lines or
  per-stage problems (`no_rate_set`, `no_stage_rate`, `needs_mass`), never a partial price;
  `material_supplying_stage` names the first stage when it is casting, forging, moulding,
  an additive build or a purchase.
- `process_classification`: `unsupported_stages` → `stage_rate_stages`, read from `STAGES`
  by stage id rather than from the payload; `costing_status` `cost_model_not_available` →
  `stage_rates_required`; `blocks_costing` now only for a route marked unknown or an
  *unreviewed* recommendation needing stage rates.
- `/cost/cad-part`: `stage_inputs` (`{"casting": {"mass_kg": 2.5}}`), recorded in the request
  snapshot; `422 route_stages_not_priceable` with a reason per stage; a confirmed bar on a
  material-supplying route is refused (400).
- `part_cost`: `stage_costs`, `total_stage_cost`, `material_charged`, `material_note`;
  `direct_cost_base = material + operations + stages`, so overhead and scrap apply to stages.
  Defaults leave every other estimate's figures unchanged.
- `quotation`: a `route_stages` line only when there are stages; the material line carries
  the note. `estimates.decompose`: a material not charged enters at a zero rate, and stage
  changes are `route_stage` impacts.
- UI: a stage-rate table in the rate-set editor's *Processes* step (stage list from
  `/process-routes`) and in its review; per-stage rates and a mass input on the route card;
  a *Route stages* row and its formula in the result panel; the route strip and stage pills
  say "priced from stage rates".

**A pre-existing defect, found and fixed.** `feedback.js` (Step 29) declared a global
`renderOperations`, which silently replaced the rate-set editor's: opening *New profile* or
*Create new version* threw before the processes table was drawn, and the next step click read
the empty table back into the draft — a version published that way would have had no
machine or labour rates. Found only because this iteration's editor rows would not render.
Renamed to `renderOperationAnalytics`; a new test fails if any two classic scripts declare
the same top-level function (shown to fail on the old name). A second, new race — the stage
table being read back before its catalog round trip had drawn it — was also fixed.

**Tests.** Baseline before this iteration **1974 passed, 1 skipped** (1975). 45 new in
`tests/test_stage_costing.py` and 1 in `tests/test_ui.py`; three tests in
`test_process_classification.py` updated for the intended change (the route-refusal test now
expects `route_stages_not_priceable` for decided routes priced with `poc-default`, which has no
stage rates; the "browser cannot declare costability" test also tries relabelling the casting
stage as milling). Final full run: **2020 passed, 1 skipped** (2021 collected, 511 s); the
route strip's wording in `plan.js` changed after it started, and the four files that cover it
were re-run afterwards (294 passed).

**Live verification** (scratch database, port 8712, a rate set published for the check and
**marked illustrative**: casting ₹120/kg + ₹15,000/batch, purchase ₹900/piece). `model1_1.stp`,
10 off, every operation timed at 2 min:
- *Casting followed by CNC machining*: no mass → 422 `needs_mass`; 1.0 kg → 422 (finished part
  1.732 kg); 2.5 kg → casting ₹18,000.00 (₹120 × 2.5 × 10 + ₹15,000), operations ₹13,000.00
  (fillets as cast), material ₹0.00 "included in the stage rate", total ₹31,000.00.
- 2.5 → 3.0 kg compared: one impact, `route_stage` +₹600.00, nothing unexplained.
- *Purchased complete*: ₹9,000.00, no operations, no material. `poc-default` (no stage rates):
  422 naming Casting. *Milling + drilling* with the stage-rate set: ₹19,613.86, identical to
  sending no classification.
- Quotation: a `route_stages` line of ₹18,000.00. Its ₹0.02 reconciliation note is the
  existing rounding of per-operation run costs — a machining-only estimate shows the same.
- In headless Chrome: the editor copies both stage rates into a new version and lists them in
  review; the route card shows the casting rate and a required mass field; Calculate without
  a mass shows the per-stage refusal; with 2.5 kg the result panel reads *Route stages
  ₹18,000.00 — Casting: ₹18,000.00 (₹120.00/kg × 2.5 kg × 10 + ₹15,000.00 per batch)*. The
  first screenshots showed *+ Add process* displaced under the new table; moved back.

**Limitations.**
- No seeded rate set has stage rates and none was added: until a client's rates are
  published, every non-machining route is refused, as before, now with the reason.
- One mass per stage, entered by hand; nothing estimates a casting's or forging's blank
  weight. A per-kg finishing rate is charged on the mass entered for it, not on surface area.
- Stage costs carry overhead and scrap like material; a client who applies neither to
  bought-out work will see that difference. No per-stage scrap or yield.
- Moulding and additive builds were taken to include their material along with the three
  the user named; the constant is `MATERIAL_SUPPLYING_STAGES`.
- Sheet, cut-and-weld and other stock-based routes still cost material from the finished
  mass — no sheet blank or nesting.
- The *Assumptions* panel still lists the material's cost per kg without saying it was not
  charged; the result panel and the quotation say so.
- Assembly components are not classified, so assembly roll-ups have no stage costs.
- Actual-cost variance (Iteration 1) compares material/machining/additional only; stage costs
  sit inside the total.

### Iteration 2 — manufacturing family and process route, recommended and confirmed

**Audit first.** No family or route classification existed. `operation_suggestions`
proposes `milling`/`drilling`/`turning` per feature; `component_classification` is
make/buy for assembly components only; nothing checked whether a part is machined at
all before machining rates were applied to it. Signals the pipeline genuinely carries:
feature axes and on-axis locations, plane normals, centres and areas, per-solid volume,
solid count, unclassified-face count and the representation. Nothing detects draft,
parting lines, weld preparations or lattices.

**What was built.**
- `process_classification.py` — a pure module importing only `features.model` and
  `operation_suggestions`. `classify_process(recognition, suggestions)` reads three
  signals from the existing features (no second parse): *features cut into a solid*
  (pockets, slots, blind and counterbored holes), *rotational form* (every external
  cylinder on one axis and every planar face square to it) and *constant wall
  thickness* (one opposite-plane separation holding at least 75% of the planar area,
  thin against that area, and agreeing with the measured volume to 10%). Each signal is
  reported observed, not observed or not assessable, with the reason and the constant
  that decided it. Rules, in order: no features → `unknown`; a constant wall with
  nothing cut into it → `sheet_metal` (cut, or cut + bend when the wall runs in more
  than one direction) at **low**; faces and blends only → `unknown`; otherwise
  `machining`, with the route built from the suggested processes. On a rotational part
  the faces the per-feature rules proposed milling for are faced in the turning stage;
  on a prismatic part a turning suggestion for a boss is treated as milling at route
  level (the per-feature suggestion itself is unchanged). Family confidence is capped
  at `medium`, and lowered to `low` for non-B-Rep evidence, several solids, or
  unrecognized faces.
- A catalog of 19 routes across the nine families; each stage states whether a cost
  model exists for it. Only milling, drilling and turning stages do.
- `record_route_decision` writes `user_action` / `user_route` / `user_note` beside the
  untouched `system_*` fields; choosing the recommended route is recorded as an
  acceptance; `unknown` marks the route as needing information; accepting when nothing
  was recommended is refused. `family`, `route`, `decision_status`, `costing_status`,
  `unsupported_stages`, `blocks_costing` and `costing_note` are computed, so a payload
  round-tripped through the browser cannot declare itself costable.
- API: `/cad/operations/suggest` now also returns `process_classification` (same
  request, same parse); `GET /process-routes`; `POST /cad/process/decide` (persists
  nothing). `/cost/cad-part` takes an optional `process_classification` form field: a
  blocking route is refused with `422 process_route_not_costable` before anything is
  priced and nothing is stored; otherwise the classification and any plan-vs-route
  disagreement (`process_route_differences`, reported, never corrected) are stored in
  the request snapshot. Those keys are absent when no classification is sent.
- UI: a *Manufacturing process* card at the top of the Manufacturing section
  (`static/process.js`, loaded after `plan.js`) showing the recommendation, stage
  basis, cost-model badges, the costing note, and the evidence behind a disclosure;
  route list from `/process-routes`; confirm / use selected route / mark unknown /
  withdraw. `app.js` sends the classification with the cost request and now shows the
  `message` of a structured refusal instead of "The request failed (HTTP 422)".

**Not blocking, by design.** An unreviewed `unknown` does not block costing: the
engineer's own operations are priced as before and the snapshot records that the
route was not established. An unreviewed *sheet* recommendation does block, with a
note asking for a confirmation or an override.

**Tests.** Baseline 1802 passed, 1 skipped. New `tests/test_process_classification.py`
(84 tests) and six STEP builders in `tests/step_fixtures.py` (`PROCESS_FIXTURES`,
kept out of `ALL_FIXTURES`): a stepped shaft, the shaft with an axial blind hole, the
shaft with a cross hole, a 2 mm L-bracket with two holes, a flat 2 mm blank, and the
blank with a milled pocket. Final full run: **1886 passed, 1 skipped** (1887 collected, 291 s), with no existing test changed or failing.

**Live verification** (scratch database, port 8711).
- Classification of the real models: `model1_1.stp` and `model1_1_solid.igs` agree —
  machining, milling + drilling, low (no feature cut into the solid; 4 unrecognized
  faces; an 8 mm wall holds 72% of the planar area, below 75%). `model2.stp`:
  machining, milling + drilling, low (two solids; its two external cylinders are not
  coaxial, so not rotational; fabrication offered as a candidate).
  `model1_1_surfaces.igs`: `unknown` — only planar milling suggestions survive from a
  surface-mode export. Each took 0.5–3.9 s including recognition.
- Costing `model1_1.stp`, 10 off, `poc-default`, every suggested operation at 2.0 min:
  **₹19,613.86** with no classification, with the unreviewed recommendation, and
  confirmed — identical. Overridden to casting + machining: 422
  `cost_model_not_available`; marked unknown: 422 `route_unknown`; estimate count
  unchanged after each. Overridden to milling + drilling: ₹19,613.86.
- The bent bracket: sheet cutting + bending recommended at low; 422 with "has not been
  reviewed" until overridden to milling + drilling, then costed (₹3,933.82 with the
  same 2.0 min times).
- Browser (headless Chrome over DevTools, screenshots inspected): uploaded the bracket,
  analysed, suggested — the card showed the recommendation, both stages with *no cost
  model* badges and the refusal note; *Calculate estimate* showed the 422 message in
  the page and no result panel; after *Use selected route* (milling + drilling, with a
  note) the card showed the recommendation unchanged beside the engineer's route, and
  the estimate was produced (₹0.49, material only, because no operation time had been
  committed in the browser — the existing omit-don't-default rule); its stored snapshot
  held both routes, the note, and the plan-vs-route differences. Defects found and
  fixed during this: a doubled full stop after the sheet reason, and the surface-mode
  IGES signal wrongly describing its features as fitted to polygons.

**Follow-up: a decided route regroups the manufacturing plan** (requested after
review — changing the route used to leave the plan below unchanged). Once the
engineer confirms, overrides or marks the route unknown, the plan is arranged by it:
- `ProcessClassification.plan_processes` (computed; `None` until a decision) lists the
  processes the route includes; `ManufacturingPlan.route_processes` carries it
  (default `None`, the old behaviour). An operation whose process is outside the route
  stays in the plan untouched, is shown under **Not in this route — kept but not
  costed**, is left out of `to_operation_requirements`, and is not reported as missing
  a time. Changing its process into the route, or switching/withdrawing the route,
  brings it back. Setup time is still charged once, on the first in-route costable
  operation of the setup.
- A strip above the operations table lists the route's stages in order: machining
  stages with their operation count (or "no operations yet — add one with + Add
  operation"), other stages as "no cost model — needs inputs — not costed". A line
  added by hand starts as one of the route's processes.
- `/cost/cad-part` sets `route_processes` from the classification and ignores the
  browser's copy; the plan snapshot records `out_of_route_operation_ids` (only when a
  route was decided).
- 18 more tests in `tests/test_process_classification.py` (102 in the file). Full run: **1904 passed, 1 skipped** (1905 collected, 393 s).
- Live, in Chrome against a scratch database: `model1_1.stp` overridden to Turning +
  drilling showed the strip (turning: no operations yet; drilling: 6) and 25 milling
  operations under *Not in this route*; Casting followed by CNC machining put all 31
  in its machining stages with Casting marked *no cost model*; *Withdraw decision*
  restored the plan exactly. Costed under Turning + drilling with all estimated times
  accepted: **₹1,371.86**, from the 6 drilling operations only; the snapshot recorded
  the route, the 25 out-of-route operations and "the plan costs no turning operation".

**Follow-up: large holes are drilled smallest first** (requested after review — a
senior engineer: a big hole is not drilled in one go; "Ø20 needs 3"). Searched first:
**no ISO or DIN standard prescribes a pilot or step-drilling sequence.** What is
published is a practice — size the pilot so the larger drill's chisel edge does not
cut, step very large holes through successively larger pilots (Wikipedia, *Pilot
hole*); the NIMI *Fitter Trade Practical* drills a 6 mm pilot before 8–16 mm holes;
CAM software (Autodesk FeatureCAM) takes the pilot list as a shop setting; forum rules
of thumb put the threshold at about ½" (13 mm). ISO 2306/DIN 336 (tap drills) and
ISO 273 (clearance holes) are not about this. The user approved a rule built from these:
- Three new time-profile parameters, illustrative and editable on the Time profiles
  screen: `drilling_pilot_threshold_mm` 13, `drilling_pilot_diameter_mm` 6,
  `drilling_max_step_ratio` 2.5. Above the threshold a hole is drilled from the pilot,
  each drill at most 2.5× the previous: Ø14 → 6, 14; **Ø20 → 6, 15, 20**; Ø25/Ø30 →
  6, 15, final; Ø40 → 6, 15, 37.5, 40. At or below it, one pass, exactly as before
  (`drilling_time_baseline_v1`); stepped holes are `drilling_time_stepped_v1`. A stored
  profile without the parameters reads with the defaults.
- A stepped hole's cycle time is the sum of one full-depth pass per drill. The estimate
  also returns `drilling_options` — single drill, pilot + drill, and the rule's
  sequence — each with its own system-computed time, one marked `suggested`.
- Each drilling row has a dropdown of those options. The choice goes to
  `user_drilling_steps` beside the untouched `system_drilling_steps`; picking the
  suggestion again records no choice; any change un-accepts the estimate and marks the
  row edited, as a change of process does. Once accepted, the chosen option's own
  system time is costed (`PlannedOperation.system_cycle_for_choice`, mirrored in
  `plan.js`); a sequence the system never offered has no system time and needs one
  entered. `system_suggested_cycle_time` keeps its meaning, so feedback, quotation and
  assembly code are unchanged (the assembly panel's accepted-time line now reads the
  resolved time).
- 29 tests in the new `tests/test_drilling_steps.py`; no existing test changed. Full run: **1933 passed, 1 skipped** (1934 collected, 591 s).
- Live, in Chrome against a scratch database, `model1_1.stp`: Ø25 holes suggested
  6 → 15 → 25 (0.81 min), Ø16 × 80 mm 6 → 15 → 16 (2.63 min), Ø8 holes one pass. One
  Ø25 hole switched to a single drill and accepted: costed at 0.44 min, while the other
  Ø25 kept 0.81; the stored plan recorded both the system's and the engineer's
  sequence. The first screenshots showed the dropdown squeezing the operation name to a
  few characters; labels were shortened and the row is back to one 35 px line.

**Follow-up: turned components** (after a client meeting that asked for costing logic
to start with simple part families, and for raw material to be judged from the
part's form and size). The client did not share CAD files, so six representative
turned parts were generated with OpenCASCADE into the git-ignored `cadmodels/`
(stepped shaft, bush, flanged pin, spacer ring, hub with a bolt circle, shaft with a
cross hole) and run through the app before anything changed. All six were classified
rotational / turning correctly, and that run found three faults for turned parts:
material costed from the finished mass (0.67 kg for a shaft cut from a 1.59 kg bar);
every diameter turned from a fixed 2 mm allowance (a Ø20 step from Ø45 bar has
12.5 mm to come off); and end faces suggested as *milling*, so under a confirmed
turning route they fell out of the cost altogether. What was built:
- `features/rotational.py` — the rotational test, moved out of the classifier so the
  suggestions, the classifier and the bar proposal share one implementation; it also
  measures the largest diameter and the overall length along the axis.
- Facing: on a rotational part, a significant planar face (square to the axis by
  definition) is suggested as **turning — "Face …"** (`turned_part_face_to_facing`,
  medium, milling offered as the alternative), timed by a new facing formula across
  the radius (`facing_time_baseline_v1`) with a new time-profile parameter
  `turning_facing_allowance_mm` (1 mm, illustrative, editable). Parts that are not
  rotational are unchanged.
- `raw_stock.py` — a **bar proposal** for a rotational part: largest Ø + 2 × 2.5 mm,
  rounded up to a 5 mm step; length + 2 mm facing each end + 3 mm parting
  (illustrative). Returned as `raw_stock` with the suggestions; `/cad/stock/decide`
  confirms it, takes the engineer's bar (kept beside the proposal; a bar smaller than
  the part is refused) or withdraws. **Unconfirmed, it changes nothing.** Confirmed,
  `/cost/cad-part` costs material from the bar's volume at the same density and price;
  `mass` stays the finished part's, and `stock_mass` / `material_basis` report the bar.
- **Turning passes from the bar**: `/operations/estimate-time` takes
  `stock_diameter_mm`; each diameter's passes are then (bar Ø − Ø)/2 ÷ depth of cut
  (`turning_time_from_bar_v1`). The browser estimates with the *proposed* bar, so the
  time stays the system's; entering a different bar shows that the turning times were
  estimated for the proposed one.
- UI: a *Raw material* card in Manufacturing (`static/stock.js`); the result panel
  names the bar mass beside the part mass when it was used.
- 26 tests in the new `tests/test_turned_parts.py` (full run: **1959 passed, 1 skipped**, 1960 collected); two existing tests updated for the
  intended changes (the classifier's import set now includes `features.rotational`;
  the suggest response also carries `raw_stock`).
- Live, in Chrome against a scratch database, the generated Ø40/Ø30/Ø20 × 120 mm
  stepped shaft: bar proposed Ø45 × 127 mm; turning 0.34 / 1.04 / 1.01 min from the
  bar (the Ø20 step was 0.23 min on the fixed allowance); four faces suggested as
  facing; route confirmed CNC turning with nothing left out of it; bar confirmed and
  costed — 10 off, ₹1,242.02, material ₹12.68 = 1.5856 kg × 10 × ₹0.80/kg, and the
  result read "0.6715 kg per part · material costed from the bar: 1.5856 kg per part".
  Entering Ø50 was recorded as the engineer's bar with the proposal beside it and the
  stale-times warning; Ø30 was refused as smaller than the part. Screenshots could not
  be captured in this session (Chrome's capture timed out repeatedly while the test
  suite was running); these results were read from the page itself.

**Fix: a casting route changed nothing in the plan, and two records misreported**
(reported by the user — "even if I select casting the operations don't change" —
followed by a review of every change since the Iteration 2 commit).
- **Casting/forging followed by machining now leaves the blank's blends as formed.**
  The route filter only compared processes, and milling and drilling are both in
  that route, so `model1_1.stp` under *Casting followed by CNC machining* kept all 31
  operations, 11 of them fillet millings — exactly the "discard many irrelevant
  machining items by hand" the client warned about. The classification now reports
  `as_formed_feature_types` (fillets and chamfers) and `as_formed_label` ("As cast" /
  "As forged") for a *decided* route that starts by casting or forging and then
  machines; `ManufacturingPlan.as_formed_feature_types` carries it (`None` on every
  other route, the old behaviour), and a row's own `user_machined` overrides it either
  way. Faces, holes, pockets and cylinders stay machined by default — the geometry
  cannot tell a machined datum from an as-cast wall. The plan shows an **"As cast —
  not machined"** group with *Machine it* on each row and an *As cast* button on the
  machined rows; the stage strip says how many features the blank leaves unmachined.
  `/cost/cad-part` sets the default from the classification, and the plan snapshot
  records `as_formed_operation_ids`. Measured: 11 fillets as cast, 20 operations
  machined.
- **Feedback said "costed" for operations the plan did not cost.** `operation_feedback`
  recorded `costed = operation.costable`, but the route (and now the as-formed
  default) lives on the plan, so every operation moved to *Not in this route* was
  recorded as costed — and the analytics count those as accepted. On `model1_1.stp`
  under Turning + drilling it recorded 31 costed where 6 were. There is now one test,
  `ManufacturingPlan.counts_toward_cost`, read by `to_operation_requirements`, the time
  summary and the feedback record alike.
- **The bar's mass was shown as the part's.** With material costed from a bar, the
  assumptions panel and the quotation took "Mass" from the material line — the bar —
  and printed it in the CAD section beside the finished volume and as "kg per part".
  `EstimateAssumptions` (and the quotation's provenance) now keep `mass_kg` as the
  finished part's own mass and carry `material_basis`, `stock_description` ("round
  bar Ø35 × 77 mm") and `stock_mass_kg` beside it; the comparison lists a switch to
  bar stock as a material input, and does not report the part's mass as changed.
- 15 more tests (full run: **1974 passed, 1 skipped**, 1975 collected) — as-formed defaults and overrides, feedback costing, the mass
  separation in the assumptions, the quotation and the comparison).
- Live, in Chrome against a scratch database: `model1_1.stp` overridden to Casting
  followed by CNC machining — the band read "As cast — not machined — 11 operations",
  the strip "Casting … leaves 11 features unmachined below", 20 operations remained to
  machine; *Machine it* on FIL-001 brought it back and *As cast* on a face moved it
  into the group.

**Limitations.**
- As-formed defaults are by feature type only (fillets, chamfers). Which faces of a
  casting are machined is a drawing and tolerance question the model cannot answer;
  the engineer moves faces individually. Rows left as formed still offer *Use
  estimate*, which records an acceptance but costs nothing while they stay there.
- Turned parts: the bar rule's allowances are illustrative; tube stock, hexagon bar
  and standard bar-size tables are not modelled; parting off, grooves, threads,
  knurls, boring and second-operation (flip) setups are not recognized or timed;
  a face with a bore through it is faced as if solid; cross holes stay declined (the
  recognizer reports them ambiguous). Tolerances and finishes — which the client
  stressed come from the drawing — are not read or entered yet.
- Shape does not determine process, and this layer says so rather than solving it.
  The thresholds behind the two geometric signals are stated constants, not measured
  against a population of real parts. `model1_1.stp` (an 8 mm bracket) is classified
  machining because its wall holds 72% of the planar area against the 75% threshold;
  if it is in fact a bent plate, the engineer has to say so.
- No cost model exists for any stage but milling, drilling and turning. This iteration
  refuses those routes; it does not price them. *(Superseded by Iteration 3: a decided
  route's other stages are now priced from the rate set's stage rates.)*
- Assembly components are not classified. `/cad/assembly/plans` and `/cost/assembly`
  are unchanged.
- The step rule is applied literally, which produces one sequence no machinist would
  use: Ø16 is just over 2.5× the 6 mm pilot, so it becomes 6 → 15 → 16, a 1 mm last
  step. Intermediate sizes are not rounded to stock drills (Ø40 gets a 37.5 mm drill).
  Drill changes are not timed, and every pass is assumed to run the full depth at the
  same speed and feed. These are for the shop to correct, not the rule to hide.
- A decided route regroups the plan but never creates operations: a stage the
  suggestions produced nothing for (turning chosen for a part with no external
  cylinder) is shown empty, to be filled with *+ Add operation*. Non-machining stages
  are lines in the route strip, never plan operations.
- A hole the suggestions decline (for example a cross hole opening onto a cylinder,
  which the recognizer reports as ambiguous) is named in the evidence but not added to
  the route.
- STL is refused here as it is for feature recognition (400).

### Iteration 1 — actual costs recorded against estimates, with variance

**What was built.** `POST` and `GET /estimates/{estimate_id}/actuals`; a pure module
`cost_actuals.py` (input validation, the estimate-figures carrier, `compute_variance`)
that imports only `money`; `CostActualRepository` with `record`, `get` and
`for_estimate` and no update or delete; migration `0006_add_cost_actuals` (one new
table, nothing else touched); and an *Actual cost (client data)* panel in the History
detail (`static/actuals.js`, loaded after `app.js`) that lists every record with its
variance, records a new one and starts a correction from an existing one.

**Decisions, as agreed:**
- Total required; material, machining and additional optional; absent means
  `not_supplied`, never zero.
- `cost_basis` is `per_unit` or `per_batch` with no default; `batch_quantity` is
  required for `per_batch`. Normalisation to per unit happens only from those stated
  values.
- Linked to an existing estimate. Optional `client_part_number` and
  `drawing_revision` are plain columns on the record, stored as entered and not
  matched against anything.
- Immutable; a correction names the record it supersedes. `supersedes_actual_id` is
  unique, so a record is corrected once (a second attempt is `409`).
- Currency mismatch refused (`422`), never converted.

**What the variance compares.** Per unit, `estimate - actual`, percentage relative to
the actual and omitted when the actual is zero. Material vs the estimate's material
cost, machining vs its operation cost (machine + labour + setup), additional vs
overhead + scrap + tooling. The part of the actual total that no supplied category
covers is reported as *unattributed*, not assigned. A batch size different from the
estimate's, or not stated, is a warning and is not adjusted for.

An **assembly** is compared on its total only: its material/operation/additional
figures cover manufactured components alone. An assembly estimate's row stores
`0.0` where the roll-up had no total, so the comparison reads the snapshot's
`is_complete`, `is_aggregatable` and `production_total_cost`, and an incomplete or
mixed-currency assembly reports the variance `unavailable` with the reason.

**Tests.** Baseline 1760 passed, 1 skipped (1761 collected). Final full run: **1802 passed, 1 skipped** (1803 collected, 537 s):
41 new in `tests/test_cost_actuals.py`, plus the existing no-destructive-operations
check, which is parametrised per migration file and now also covers `0006`.
`tests/test_migrations.py`'s head constant moved to `0006`. The first full run had
**2 failures** in `tests/test_money.py`: its fixture rebuilds a revision-`0001`
database from today's models and must drop every table a later revision adds, as it
already did for `quotations`; it now drops `cost_actuals` too. One new structural test
initially failed because it grepped for the module name and matched the identically
named table in the ORM model and the migration; it now parses imports with `ast`. The
no-arithmetic regexes for `actuals.js` were checked against three deliberately
arithmetic lines to confirm they can fail.

**Migration verified against a copy of the real database.** The real
`cad_costing_agent.db` was copied through a read-only SQLite backup and the copy
migrated: path `upgraded`, Alembic `0005 -> 0006`; `estimates` (137), `rate_sets` (8),
`time_profiles` (7) and `quotations` (3) identical by content hash before and after;
`cost_actuals` added empty; `PRAGMA integrity_check` ok. The original file's SHA-256
was unchanged afterwards. **The real database itself has not been migrated** — that
happens the next time the app starts against it.

**Live verification** (scratch database, port 8711). `model1_1.stp`, 10 off,
`poc-default`, milling 10 min setup + 6 min cycle and drilling 3 min: estimate
₹3,163.86, ₹316.39 per unit. A per-batch actual of ₹3,400 for 10 with material ₹600
and machining ₹2,500 gave total -23.61 (-6.95%), material -58.61 (-97.69%), machining
+65.00 (+26.00%), additional *not supplied*, ₹30.00 unattributed. A correction to
₹3,450 left the original listed and marked superseded; a total-only per-unit actual
warned that its batch size was not stated. Refused as specified: USD (422), per-batch
without a quantity (422), breakdown above the total (422), no basis (422), correcting
the same record twice (409). `GET /estimates/{id}` was identical before and after. The
`pump_assembly` fixture costed without plans was `is_complete: false`, and its actual
came back `unavailable / assembly_incomplete`.

The History panel was driven in headless Chrome over the DevTools protocol and
inspected from screenshots: records listed with the superseded one dimmed, a new
record posted from the form, the per-batch refusal shown in the page, and a
correction prefilled with its banner. The first screenshots showed two defects that
were fixed before finishing: the shared `table.mini th { width: 40% }` crushed a
per-row note column so a *not supplied* row was about 150 px tall (notes moved under
the table, column widths scoped to the new table), and a previous error stayed
visible when a correction started.

The material line of -97.69% is the seeded ₹0.80/kg steel price showing through, not
a fault of the comparison. It is the kind of gap this feature exists to expose.

**Limitations.**
- One actual is compared with one estimate. There is no report across many parts yet.
- The categories map onto this application's cost structure. A client whose
  "machining" excludes labour, or whose overhead sits inside machine rates, will see
  variance that comes from the mapping, and nothing here can detect that.
- No adjustment for batch size, and no import of actuals from a file — each is typed in.
- `cost_date` is optional, so records can always be ordered by when they were
  recorded here, but not necessarily by when the cost was incurred.
- Pydantic's validation messages reach the page with their `Value error, ` prefix.

### Step 30 — closing the feedback data gaps, and measuring which could not be closed

- **Inspected before changing anything.** Each of Step 29's three reported gaps was
  traced to its source of truth, its lifecycle point and its persistence location
  before a line was written, and then measured against the real database. Two turned
  out to be correct absences that the report mislabelled, which is the finding this
  step mattered most for.
- **Feature confidence — a real gap, now closed going forward.** The recognizer has
  exposed `OperationSuggestion.feature_confidence` since Step 19, and Step 29 recorded
  it on `PlannedOperation` and `OperationFeedback`. Yet all 713 stored records lacked
  it, because **`plan.js` builds the plan payload in the browser** rather than calling
  `build_plan`, and a field it does not copy never reaches the server. `plan.js` now
  carries `system_source_type` and `system_feature_confidence`, and the display-only
  `_source_type` duplicate was removed rather than left as a second source of truth.
  Verified end to end on a real file: every operation of a new estimate persists the
  recognizer's answer.
- **Component identity — already complete where it can exist.** Every one of the 264
  assembly records carries it; the 449 without one are all `cost/cad-part`, and a
  standalone part has no component definition. `component_identity_applicable` is set
  from the shape of the snapshot the record came out of, so the distinction is read
  rather than guessed. A definition id is never confused with an occurrence id, and a
  test asserts no occurrence id ever appears as an identity.
- **Paired cycle time — 302 of 308 correctly unpaired.** An operation nobody committed
  a time for is `not_applicable`, which is exactly what Step 29 requires. `time_pairing`
  distinguishes `paired`, `unfinished` and `not_predicted`, derived from the two halves
  that were already persisted separately — the prediction is never overwritten by the
  correction, and a chosen time is never manufactured because an operation was accepted.
- **A fourth gap corrected during live verification.** `time_profile_version` was
  reported as 198 permanently unknown. Checking the data showed all 205 such records
  had no `time_profile_id` either: the plan ran under the built-in baseline, which has
  no published version. That is `not_applicable`, and the field now reads **514/514 =
  100%** with **zero** permanent unknowns.
- **Four states, not two.** `EvidenceField` carries `known`, `not_applicable`,
  `unrecorded`, `unrecoverable`, `applicable`, `percent_known`, the denominator's basis,
  the source of truth, whether the gap is closeable at all, and Step 29's `SampleGuard`
  unchanged. The four counts are exhaustive and sum to the total, asserted on the real
  data. `percent_known` divides by `applicable`, never by the total.
- **Zero safe backfills, and that is the honest answer.** Every recoverable value had
  already been recovered by Step 29's plan join. Nothing was written to a historical
  record, no migration was added, and the schema head stays `0005`.
- **A new `GET /feedback/evidence`** and an **Evidence** sub-view, added without
  disturbing Step 29's six views, filters, pagination, drill-down, excluded/unfinished
  counts, reset behaviour or bounded window. The server computes every count,
  percentage, threshold and readiness decision; a test greps the page for arithmetic on
  any of them.
- Added 49 tests (`tests/test_evidence_completeness.py`) and updated two Step 29 tests
  the new semantics made stale. Suite: 1667 to **1716 passing** (1717 collected, 1
  skipped). No costing formula, quotation, plan, rate set or time profile changed.
- **Live verification on the real database** (127 estimates before, 128 after the probe
  estimate). Evidence report in 0.33 s. Feature confidence **0 known / 712 permanently
  unrecoverable** historically, **7/7 on the new estimate**; component identity
  **264/264 = 100%** with 456 not applicable; paired cycle time **412/412 = 100%** with
  308 not applicable, exactly matching the 405→412 time samples the time report uses;
  rule id and evidence type **719/719**; time-profile version **514/514**. The four
  states summed to the total on every field. Summary still matched all 720 paged records
  with **exact agreement on every correction kind**; acceptance denominator + never
  finished == reviewed; three confusion cells drilled down to matching counts; six
  filter combinations behaved; Reset cleared all eight controls. Publishing a rate set
  at ₹9,999/kg and a new time-profile version left the evidence report, the summary and
  the records **byte-identical**. Two reads identical.
- **Three defects found and fixed during the step.** The `time_profile_version`
  misclassification above. A JavaScript name collision where a loop variable called
  `field` shadowed the `EvidenceField` meaning, which made a field-name check read
  `field.replace` as an API field. And a lazy test assertion (`permanently_unknown !=
  safe_backfill_available`) that asserted a coincidence rather than the property, now
  rewritten to check each count is attributable to a different field.
- **Two more structural greps matched their own explanations** — a Python check
  forbidding `calibrat` matched a note *refusing* to claim calibration, and a JS
  field-name check matched the comment explaining the collision it was checking for.
  That is seven occurrences across three languages; `CLAUDE.md` now says so with the
  count, and the Step 29 field-name check was hardened the same way.

### Step 29 — manufacturing feedback and correction analytics

- **New `feedback_analytics.py`**, whose entire design is about what it cannot do. It
  imports **nothing** from the package: it is handed plain snapshot dictionaries and
  returns plain models, so it cannot reach a rule, a profile, a rate set or a
  repository. Three structural tests hold that — the empty import set, that no module
  but `main.py` mentions it, and that its unparsed source contains no `session`,
  `Repository`, `save`, `commit` or `fit(` — plus a behavioural one that runs every
  report and asserts every stored estimate, rate set and time profile is
  byte-identical afterwards. Every endpoint is a `GET`, asserted by walking the route
  table.
- **Snapshots are read as they were written**, by explicit `.get()` on dicts rather
  than by validating into today's models: coercing an old record would fill absent
  fields with defaults that read exactly like recorded data. Absent is `unknown` and
  is counted in the readiness section.
- **One source of truth, both snapshot shapes.** The flat `feedback` list a part
  estimate stores and the per-component `component_feedback` map an assembly estimate
  stores since Step 28.
- **Feedback rows are joined to the plan beside them.** `rule_id`, `feature_type` and
  `source_type` reached the feedback record only in Step 29, so all **713** historical
  corrections reported them as `unknown` — found in live verification. The plan travels
  in the same snapshot and carries all three, so joining by `operation_id` recovered
  `rule_id` and `source_type` for **712 of 713**. The join fills absent fields only,
  never overrides a recorded one, and flags what it filled; a test proves the
  `source_type` fallback agrees with what the recognizers actually report on both a
  real B-Rep and a real faceted model.
- **The denominators are the design, and one of them was wrong.** An operation nobody
  committed a time for was never finished — 302 of 713 here, usually because the rate
  set prices no rate for the process suggested — and counting those as rejections made
  `external_cylinder_to_turning` (100 suggestions, **one** correction, 91 unfinished)
  read as **8% acceptance**. It reads **89%** now with the 91 shown beside it. Every
  `CountedRate` carries `numerator`, `denominator`, `excluded` and a `basis` sentence
  naming the population it describes.
- **Medians, outliers, and a relative-error floor.** Medians carry every headline so one
  correction cannot define a bias; outliers are listed separately and still counted
  (17 in this history), because an order-of-magnitude miss is the most informative
  record in the set; and relative error skips predictions below 0.05 min — dividing 0.5
  by 0.0008 gives a 62,400% error that says nothing — counting them as
  `near_zero_excluded` and keeping them in every other statistic.
- **Small samples are labelled.** `SampleGuard` on every metric: `sample_count`,
  `minimum_required`, `is_actionable` and the sentence to print instead of the number.
  The threshold is configurable (`CAD_COSTING_MIN_FEEDBACK_SAMPLE`, default 8) and
  explicitly **not** a significance test. A directional bias is claimed only above it,
  and a median correction smaller than the error floor is `no_clear_bias` rather than a
  direction.
- **Seven read-only endpoints** — summary, operations, times, setups, classifications,
  confidence, records — with eight filters (component, process, feature type, material,
  evidence type, confidence, time-profile version, rate-set version, rule id,
  corrections-only), pagination on the drill-down, and a bounded history window
  (`CAD_COSTING_MAX_FEEDBACK_ESTIMATES`) whose response says when it truncated.
- **A Feedback tab** with six sub-views, loaded on demand so opening the application
  reads no history. Every rate shows its two counts, what it excluded and its sample
  guard; thin evidence is dimmed and labelled rather than hidden; and clicking a
  confusion cell lists the individual corrections behind it. The disclaimer the API
  returns is printed verbatim, not paraphrased.
- Added 79 tests (`tests/test_feedback_analytics.py`). Suite: 1588 to **1667 passing**.
  No costing formula changed; no migration needed, Alembic head stays `0005`.
- **Live verification on the real 127-estimate database.** 713 reviewed operations in
  0.1–0.2 s per report. Operation accuracy **99%** (708/712) with three changed cells;
  cycle time corrected on 19 of 405 paired records, median signed error 0.00 min and no
  clear directional bias on any process; setups left as the system grouped them **98%**
  of the time (562/572), 9 of 35 single-setup estimates split; **42 of 222** make/buy
  suggestions overruled — manufactured→purchased 19, →standard 11, →excluded 4. Rule
  performance across five rules. Empirical acceptance 87% at high confidence and 96% at
  medium — reported as an association, with the note refusing to call it calibration.
  Readiness: 712 potential training rows, biggest gaps `feature_confidence` (713) and
  `component_identity` (449). Summary counts verified against all 713 paged records:
  **exact agreement**. Publishing a rate-set version at ₹9,999/kg and a new
  time-profile version left the summary, the records and the time report
  **byte-identical**. Two reads of every report identical.
- **Six defects live verification found that the tests did not.** The `source_type`
  derivation could never fire, because it read the feedback row — which never carried
  the viewer linkage — instead of the plan beside it. The acceptance denominator
  included unfinished operations, inverting what a rule's rate meant. The browser did
  not show the excluded count, so 91% over 411 looked like 91% over 713. The filter bar
  stacked into a 377-pixel column because `.feedback-filters` set `flex-wrap` without
  `display: flex` — and the first fix guessed the wrong cause before measuring found it.
  Reset left the sample threshold in place, leaving every figure dimmed with nothing
  saying why. All are fixed and tested.
- **A structural grep matched its own explanation, twice more.** A JavaScript test
  forbidding `median` matched the comment saying no median is computed, and a Python
  test forbidding `train` matched the word *constraint*. That is now five occurrences
  across three languages, so `CLAUDE.md` states the rule for all of them: strip
  comments first, and prefer a precise token over a substring.

### Step 28 — per-component manufacturing plans

- **New `assembly_plan.py`**: one `ManufacturingPlan` per manufactured component
  definition. `AssemblyManufacturingPlan` is a mapping of definition to plan with **no
  operations and no setups of its own**, which is what makes an assembly-wide routing
  unrepresentable rather than merely discouraged. Its only package imports are
  `manufacturing_plan` and `component_classification`; a structural test asserts that
  set and the reverse direction, so `manufacturing_plan` remains the sole bridge to
  money and each component plan emits its own `OperationRequirement`s for the unchanged
  costing to consume.
- **Why not one flat plan.** Setup time is charged once per setup, so a single
  assembly-wide plan would put a housing's facing operation and a shaft's turning
  operation in the same setup — a fixturing they cannot physically share — and the
  assembly would be under-costed by exactly the setups nobody charged for. The Step 21
  error, one level up. A test asserts no two components share a setup id.
- **Ids are namespaced at birth.** `build_plan` gained an `id_prefix`, so a component's
  plan is *born* with `HOUSING/OP-001` and `HOUSING/SETUP-001` and the plan's existing
  validator checks it like any other. Relabelling afterwards would have meant keeping
  `system_setup_id`, `user_setup_id` and the setup's membership list in step by hand —
  the kind of bookkeeping that fails silently. Feature ids were already prefixed by
  Step 24, so `assembly → component → feature → operation → setup` is traceable by
  stable id rather than by list position.
- **Recognition per component, never on the aggregate.** Each component's own OCCT
  shape goes through the same recognizers a standalone part does, keeping
  `source_faces`, `source_facets`, `source_type` and confidence, so a housing's holes
  are never attributed to a shaft. A component whose shape the reader could not
  transfer is skipped with its reason.
- **Three different absences, three different answers.** `not_applicable` (bought,
  standard, excluded — the absence of a plan is correct), `not_generated` (plannable,
  nobody has), `blocked` (unresolved, or no geometry). `not_generated` carries
  `plan=None`, never an empty plan, because a component nobody planned and one that
  genuinely needs no work must never look alike. `MANUFACTURED` is the explicit gate: a
  classification the vocabulary does not recognise is refused rather than falling
  through into planning.
- **Estimated and committed time are separate fields.** `time_summary` reports
  *committed* time and is legitimately 0.00 for a fresh plan; `estimated_setup_time_minutes`
  and `estimated_cycle_time_minutes` carry the system's predictions beside it, with
  `committed_time_is_complete` saying which to trust. Missing inputs are named one by
  one, never counted — and `_missing_inputs` reads `setup.setup_time_minutes` (the
  committed time) rather than `charged_setup_time`, which is 0.0 by design when nothing
  is committed and would therefore never have reported a gap.
- **Costing from the component's own plan.** `/cost/assembly` takes `component_plans`
  and costs each manufactured component from its own — material plus the operations
  carrying a committed time, that component's setups charged once each.
  `ComponentEstimate` gained `setup_per_unit`/`run_per_unit`, split from figures the
  engine already produced (`setup_cost` is its own field on every
  `OperationCostResult`), and a test asserts setup + run comes back to the operation
  cost, which is what makes it a split rather than a new formula. A component whose plan
  commits nothing is costed for material only and its line reports the plan status.
- **Feedback carries the component.** `OperationFeedback` gained
  `component_id`/`component_name`, because "the system said milling and the engineer
  said turning" is uninterpretable across an assembly without knowing which component
  it was about. Nothing is trained on it; a test re-runs the suggestion after a
  correction and gets the same prediction.
- **Persistence adds no table and no migration.** The component plans and their
  per-component feedback travel in the existing `plan_snapshot`; Alembic head stays
  `0005`.
- **The browser reuses the operations table rather than copying it.** Selecting a
  component's plan points `state.plan` at that component's plan object and calls
  `renderPlan()`, so all twelve editing operations work unchanged and mutate the
  component's plan in place, with a banner naming the component being edited. A second
  editing UI would have been a place for the Step 20/21 system-versus-user semantics to
  drift. The panel's status is re-read live through `plan.js`'s own
  `finalCycle`/`setupTime` — the single implementation of which time counts.
- Added 68 tests (`tests/test_component_plans.py`). Suite: 1520 to **1588 passing**. No
  costing formula changed; geometry, mass, features, suggestions, time estimation,
  setup-aware costing, classification, the exploded view, cost impact and the quotation
  are unchanged.
- **Live verification on the pump assembly.** Three component plans built in 119–130 ms:
  HOUSING 6 features / 6 operations / 1 setup, COVER 4/4/1, PIN 3/3/1, with 13
  operations across 3 setups and **no id shared between components**. Nothing committed
  on generation: HOUSING listed all seven missing inputs by name. Committing them
  through the existing operations table moved HOUSING from `needs_user_input` to
  `generated` and its cost from ₹32,454.03 (material only) to ₹41,075.31 — setup
  ₹1,666.50 + run ₹1,226.54 = operations ₹2,893.04 exactly. Setup time reported as
  **10 min, not 60**, for 6 operations in one setup; splitting into two setups raised
  the setup cost to ₹2,999.70. Ten assemblies gave ₹6,45,856.30 against a per-assembly
  ₹64,585.63 with the setup time still 10 min. Reclassifying a component to purchased
  while sending its plan was refused with 400 and the reason.
- **Live verification on `Figure 3.stp`.** 43 definitions in 51.6 s: 36 parts all
  `blocked` on `no_geometry_in_file`, 7 sub-assemblies `not_applicable`, **0 plans
  built and 0 components reporting zero operations as though planned**. The roll-up
  stayed "no total, nothing reported as zero"; structure (43/102/depth 4) and the
  exploded view (depth 3, 83 groups, 423.690 mm) unchanged, and the exploded state
  mentions no plan, setup or operation.
- **Real-part regression.** `model1_1.stp` (220,621.078 mm³, 39 features, 31
  suggestions) and `model2.stp` (487,455.564 mm³, 27 features, 22 suggestions) are
  unchanged: **no feature id is component-prefixed and no suggestion names a
  component**, and `/cad/assembly/plans` refuses them with an explanation pointing at
  the single-part flow. A real part still costs (₹5,58,823.00 for 10) and quotes.
- **Five defects live verification found that the tests did not.** A freshly generated
  plan printed `0.00 min` of committed time, which reads as "this component takes no
  time to make" — the estimate is now shown labelled as one. A refusal reused a column
  label as a clause and read "Component 'HOUSING' is no manufacturing plan and cannot
  carry a manufacturing plan". After committing every time in the operations table the
  detail panel still listed them as missing, because it read a server-computed status
  that goes stale on the first edit. The status column then lagged the detail panel,
  because the render hook refreshed only one of the two. And a component costed with an
  uncommitted plan printed `0 min` from the roll-up's genuine 0.00. All five are fixed
  and tested.
- **A layout defect measured rather than guessed.** The eight-column plan table
  overflowed the 486 px assembly panel by **111 px** and pushed the whole page
  sideways — the same defect the classification table hit in Step 26, and the same fix:
  three columns with the counts and times on a full-width sub-row. Measuring also
  established that the operations table's own overflow (600 px in a 487 px card for a
  *single part*, 605 px with component ids) is **pre-existing and not from this step**.

### Step 27 — professional quotation / costing report

- **New `quotation.py`**: one estimate's snapshots rendered as a document. Its only
  package imports are `estimates` — where `EstimateDetail` and the existing
  `extract_assumptions` provenance reader already live — and `money`. Two structural
  tests hold the boundary: one asserts that exact import set, the other unparses the
  module and asserts it contains no `calculate_`, no `resolve_material(`, no
  rate-per-hour multiplication and no `RateSet`, `Part` or `ManufacturingOperation`.
  There is no second costing path because there is nowhere for one to come from.
- **The cost lines partition the total, and the partition is checked.** Regrouping is
  the one place a document could quietly disagree with the estimate it presents, so no
  line contains part of another, they sum, and the sum is compared against the figure
  the estimate stored. A remainder is reported as `reconciliation` — the same rule
  the cost-impact decomposition follows, and a test breaks a stored line to prove the
  report fires.
- **Setup and run are split from figures that already existed.** `setup_cost` is
  separate on every `OperationCostResult`, and the plan hangs a setup's whole time on
  the first costable operation in it, so summing gives the part's setup cost rather
  than one per operation. No new formula.
- **The time source is a label, not a third resolution rule.** The numbers come from
  the *costed* operations — the ones that reached the money — and the source is a
  two-branch test on the plan, because an operation only reaches the cost if it had a
  committed time. A third copy of the three-branch rule would have drifted from
  `manufacturing_plan` and `plan.js`.
- **An assumption is never printed as a measurement.** For a supplied volume the
  document leads with "Volume assumed, not measured from CAD." and prints the
  user-supplied figure beside the volume used; the phrase "CAD volume" appears
  nowhere near it, asserted by a test. It is the most consequential sentence on the
  page: a mass is a volume times a density and a price is a mass times a rate.
- **Refusal is a feature.** An assembly with components needing costing input, one
  priced in more than one currency, an estimate with no total, or geometry that was
  never resolved all give `available=False` with a blocker naming the reason, and
  `POST` refuses with 409. The engineering view still renders — the person fixing it
  needs to see it — and nothing is ever filled in with a zero to make a total appear.
- **The customer view omits the engineering appendix entirely** rather than hiding it.
  A field a template does not print is still a field in a forwarded payload, so a test
  serializes a customer quotation and greps it for `classification_feedback`,
  `system_suggested_operation`, `system_time_parameters` and the rest.
- **Immutability needs no second copy of the figures.** `QuotationRow` stores the
  metadata and the totals as issued; the figures are re-read from the estimate's own
  snapshots every render, so there is one source rather than two that could drift, and
  `total_at_issue` exists to be *checked* against a re-render. No update method, like
  a rate set version: a correction is a new quotation with a new number.
- **Migration `0005`** adds the `quotations` table and touches nothing existing.
  Applied to the real 105-estimate database with every row intact and the revision at
  `0005`; the pre-currency legacy-database fixture was extended to drop it, exactly as
  its own comment says every later revision requires.
- **A standalone report page.** `quotation.html` / `.css` / `.js` share no globals and
  no stylesheet with the application shell. Print is the primary target: repeating
  table headers, `break-inside: avoid` on rows, notices and setups, controls hidden,
  margins in millimetres and **no forced paper size** — forcing `size: A4` would
  override the reader's print dialog and make a Letter printer scale or clip. A test
  greps the page's script for arithmetic and for assignments to any money field.
- Added 67 tests (`tests/test_quotation.py`). Suite: 1453 to **1520 passing**. No
  costing formula changed.
- **Live verification.** A 25-off bushing estimate (₹3,867.31 stored) quotes with
  lines summing to exactly ₹3,867.31, per unit ₹154.69, the illustrative-rate warning
  prominent, and the volume described as measured. Issued as `Q-0001`; then a rate set
  version was published with steel at ₹9,999/kg and every machine rate at ₹9,999/hour,
  and `Q-0001` still read **₹3,867.31**, still named **POC Machine Shop v2.2.0**, with
  every cost line and the whole provenance block byte-identical and `total_at_issue`
  still matching the re-render. A 4-assembly pump quote showed manufactured ₹32,454.03
  + purchased ₹250.00 + standard ₹25.00 = ₹32,729.03 per assembly and ₹1,30,916.14 for
  four, with the sub-assembly absent from the component table. An incomplete assembly
  and a mixed-currency one both refused, both named their reason, both refused to
  issue with 409, and both still rendered their engineering view. A supplied volume
  printed "Volume assumed, not measured from CAD. User-supplied assumption:
  39,81,004.3 mm³. Volume used: 39,81,004.3 mm³". Two renders of the same quotation
  were identical.
- **Print verified as far as a browser session honestly allows**: the print rules were
  read back out of the parsed stylesheet (`@page` margins, `display:
  table-header-group`, `break-inside: avoid`, `.no-print { display: none !important }`),
  the sheet measures 210 mm with **zero horizontal overflow**, and a one-part quotation
  comes to 316 mm of content — two A4 pages. The native print dialog was **not**
  opened: it is a modal that blocks the automation channel, so "Print → Save as PDF"
  is verified by its rules and geometry rather than by a produced PDF file, and that
  limitation is stated rather than papered over.
- **Six defects live verification found that the tests did not.** A per-unit figure
  times the quantity came to ₹3,867.25 against a stated ₹3,867.31 with nothing to
  explain the six paisa — the money policy's own consequence, now stated on the
  document. "0 setups" printed beside 20 minutes of setup time, because a hand-entered
  estimate has no plan to count setups from. An assembly reported itself as a
  "material-only estimate" using a sentence written for a part. `[hidden]` was defeated
  by `.no-print { display: flex }`, so the metadata form showed on an already-issued
  quotation. A part number equal to its component's name printed the same string
  twice. And "Quotation unavailable" appeared three times on a blocked page, because
  every blocker carries that title for API consumers. All six are fixed and tested.
- **A structural test that matched its own explanation, twice.** The print-stylesheet
  test greps for `size: A4` to assert it is *absent* — and matched the comment
  explaining why it is absent; the blocked-page test counted a phrase and matched the
  comment about that phrase. Both now strip comments first, which is the rule
  `CLAUDE.md` already states for Python source and evidently needs stating for CSS and
  JavaScript too.

### Step 26 — component material, make/buy and assembly cost classification

- **New `component_classification.py`**: the five classifications, the costing
  profile that carries a component's decisions, and the feedback record that pairs
  what the system suggested with what the engineer chose. It imports nothing at
  all — a structural test asserts it is a leaf — and `assembly_costing.py`'s only
  package import is this vocabulary, so the roll-up still cannot reach a
  calculator, a rate set or a repository.
- **The suggestion reads two facts, and neither is a name.** `suggest_classification`
  looks at whether the definition is a sub-assembly and whether the file carries
  measurable geometry. A structural test unparses *that function alone* — the
  module's own prose about bolts and bearings would match a naive grep — and
  asserts it cannot see a name, a part number, a material, a volume or a colour;
  a behavioural test gives the same component three different names and three
  different materials and gets the same suggestion each time.
- **What it will claim is narrower than make/buy, and it says so.** "The file
  carries measurable solid geometry, which is what this application knows how to
  cost. That is evidence about what can be costed here, not evidence that the
  part is made rather than bought." Low confidence, with the caveat attached to
  the profile and printed in the UI.
- **The suggestion is applied and labelled, never silently.** Without that an
  assembly would cost nothing the moment this shipped, so the suggestion is the
  effective classification until someone answers — exactly as an assumed setup
  grouping is used until an engineer regroups it — and every line reports
  `classification_source` in the API, the UI and the stored snapshot. The
  engineer's answer never overwrites the suggestion: both sit on the estimate for
  its life.
- **A purchased component genuinely leaves the manufacturing path.** It is not
  costed and then hidden: no geometry is costed for it, no material is applied,
  no operations are considered, and its `estimate_id`, `material_extended` and
  `operation_extended` are null. Verified live on the pump: reclassifying the
  housing dropped the assembly's material cost from ₹152.51 to ₹20.63, which is
  exactly the housing's ₹131.88 leaving.
- **Occurrence quantity and production quantity meet exactly once.**
  `total_quantity = quantity x assembly_quantity` is computed in one place and
  `production_extended_cost` derives from it, rather than re-multiplying an
  already-extended figure. `assembly_quantity` defaults to 1, so a caller that
  does not care about a production run gets the previous behaviour byte for byte
  — asserted by comparing the two serializations.
- **The one legitimate zero is an exclusion**, because a person authorised it: it
  contributes 0, states its reason, keeps its place in the tree and the picture,
  and does *not* make the total incomplete. A component classified as bought with
  no price, one priced with no currency, and one nobody has classified each get a
  *different* reason and each do make it incomplete — separate because telling
  someone "no unit cost" about a component whose cost they had entered sends them
  to re-type a number that is already there.
- **Currencies are reported, never converted.** More than one among the components
  sets `is_aggregatable` false, names them, and covers the base currency only.
- **Persistence adds no table and no migration.** The decisions travel in the
  estimate's request snapshot, the contributions in its result snapshot, and one
  `ClassificationFeedback` record per component the system had an opinion about
  travels beside them — frozen, written once, and trained on by nothing. Alembic
  head stays `0004`.
- **UI**: a classification panel with a per-component selector that asks only for
  what that classification needs — a material for a manufactured part, a unit cost
  and supplier for a purchased one, a reason for an excluded one — plus a detail
  panel whose fields follow the component's kind, coloured classification chips in
  the roll-up, a separate line for each contribution including the excluded zero,
  and an "assemblies to build" input. Changing a classification marks the existing
  roll-up stale rather than leaving a number that no longer matches beside it.
- Added 67 tests — 66 in `tests/test_component_classification.py` and one asserting
  the new vocabulary module is an import leaf — and rewrote two Step 24 assertions
  the new model made stale. Suite: 1386 to **1453 passing**. No costing
  formula changed; geometry, mass, features, plans, setups, cost impact, the
  exploded view and the viewer mesh are unchanged.
- **Live verification on the pump assembly**, in the browser: the suggested
  classification for all three parts is manufactured at low confidence; changing
  COVER to purchased (₹250, "Test Supplier", TS-6204) and PIN to standard (₹12.50)
  gives manufactured ₹456.28 + purchased ₹250.00 + standard ₹25.00 = **₹731.28**,
  every line's `per unit x qty` matching its extended figure exactly, and the same
  figures from the API. Excluding PIN gives it ₹0.00 with "Reason: shipping
  hardware, not supplied" and leaves the total complete; marking COVER unresolved
  gives "Assembly total incomplete — 1 component(s) need costing input". Ten
  assemblies gives ₹4,562.78 against a per-assembly ₹456.28. Selection, isolation
  and the explode slider still work — 0.5 ms per move, and a component selected at
  70% still reports "drawn at (-25.8, 29.9, 47.2), CAD places it at (10, 20, 25)".
- **Live verification on `Figure 3.stp`.** All 36 part definitions come back
  `unresolved` with the reason "the file carries no geometry", 7 sub-assemblies get
  no suggestion at all, no material is read because the file states none, and the
  roll-up is still "no total, **0 lines reported as zero**". Then the point of the
  step: classifying two of them as purchased (₹1,450) and standard (₹18) produces
  a real partial total of **₹1,468.00** — ₹4,404.00 across three assemblies —
  covering exactly the two definitions someone priced and saying so. **A component
  nothing can measure can now still be costed.**
- **Three bugs live verification found that the tests did not.** The currency field
  displayed INR but never stored it, so an entered price came back as "no unit
  cost"; the "you confirmed it" label read a server-computed field the browser
  never refreshed, so it appeared on a classification the user had just changed;
  and a sixth column of prose in a 484-pixel panel produced twenty-line rows and a
  horizontally scrolling page. All three are fixed and now have tests.
- **A structural test that had been checking nothing.** While extending
  `test_the_browser_computes_no_assembly_arithmetic` its pattern turned out to
  contain a literal backspace byte where a word-boundary escape was meant, so it
  could never match — and two Step 25 assertions about the bill-of-materials panel
  had the same defect. All three now run, and the patterns are written out rather
  than interpolated so a stray escape cannot make them vacuous again.

### Step 25 — exploded view and assembly visualization

- **The mesh representation had to change first.** Step 24 sent an assembly as one baked vertex
  buffer with an index range per component. That is impossible to explode — every component's world
  position is welded into its vertices, so moving one means rewriting geometry — and it transmitted a
  repeated part once per placement. `AssemblyMesh` now carries one `ComponentMeshDefinition` per
  definition, in its own local space, and one `ComponentInstance` per occurrence holding the CAD
  placement. A part used twelve times is one geometry and twelve transforms.
- **That also removed a Step 24 caveat.** Decimating the old shared index buffer renumbered its
  triangles and invalidated every component range, so per-component selection had to be withdrawn
  when the display budget bit. With a geometry per definition, decimation reduces each definition in
  place and every component stays selectable; a floor stops any definition being reduced to nothing,
  because a component that vanishes reads as a missing part rather than a coarse one.
- **New `exploded_view.py`**: a deterministic, hierarchy-aware arrangement. It finds the shallowest
  depth at which the tree actually *branches* — skipping chains of single children, which is
  essential, because the real export's top two levels are single children and a naive explosion at
  depth 1 would move all 102 occurrences together and separate nothing. Each group at that depth
  moves radially away from the centre of the group centres by a distance taken from how far apart
  they already are, and **everything below a group inherits its offset exactly**, so a
  sub-assembly's parts keep their positions relative to each other rather than scattering. A group
  sitting on the centre gets a direction from a golden-angle spiral over the sphere, indexed by a
  sorted position, so the fallback is reproducible and co-located groups fan out.
- **The server/browser split is deliberate.** The interesting part — grouping, directions, fallback,
  scale — is computed once in Python where it is unit-tested; the browser receives each occurrence's
  CAD placement and the offset it would take at full explosion, and the slider is
  `drawn = original + offset x factor`. So the algorithm is testable and the interaction needs no
  request, no reparse, no re-tessellation and no recalculation.
- **The CAD placement is never overwritten.** The drawn position is rebuilt from the stored placement
  and the offset every time, so returning to 0% restores the file's arrangement *exactly* rather than
  approximately. Selecting a component while exploded reports both: "drawn at (-25.8, 29.9, 47.2),
  CAD places it at (10, 20, 25)".
- **Structurally unable to reach an engineering number.** `exploded_view.py` imports nothing from
  this package, and no module except `main.py` — which only serves it — so much as mentions it; tests
  parse the imports and walk the source tree. It is never persisted: a test asserts a stored estimate
  contains no exploded position and that its transforms are the CAD ones.
- **Never described as a disassembly sequence.** The state carries that disclaimer, the viewer panel
  prints it under the slider, and a test asserts the wording. Sequencing is a manufacturing decision;
  the geometry does not imply it.
- **Viewer and panel**: explode slider, reset explosion, fit, reset camera, component selection,
  isolation, show-all, and faint connector lines from a component's CAD position to where it is
  drawn — labelled a reading aid, not a path a part travels. The assembly tree distinguishes all four
  things that are easy to confuse: part vs sub-assembly, which definition a row is, how many times
  that definition is used, and which placement this row is. A single part shows none of it.
- Added 30 tests (`tests/test_exploded_view.py`) and rewrote the 7 Step 24 mesh tests for the
  instanced representation, preserving their intent. Suite: 1354 to **1384 passing**. No costing
  formula changed; geometry, mass, features, plans, setups, cost impact and the roll-up are unchanged.
- **Live verification on `Figure 3.stp`.** Loads in 28.8 s; the tree matches the normalized structure
  (43 definitions, 102 occurrences, depth 4, 7 sub-assemblies, every child's parent present); the
  arrangement computes in **2.9 ms** with **83 rigid groups at depth 3**, all travelling exactly
  423.68955 mm, and the assembly's extent grows 706 -> 1008 -> 1315 -> 1934 mm at 0/25/50/100%.
  Reset returns a maximum displacement of **0.000000000 mm**. Its components still cannot be *drawn*
  — the file carries no transferable geometry, unchanged since Step 23 — and that is reported rather
  than worked around. Geometry stayed `incomplete_requires_input` with 273,459 facets and the cost
  roll-up stayed "no total, nothing reported as zero". On the pump assembly in the browser: volume,
  surface area and bounding box read **identically at 0% and 100%**, and each slider move took
  **0.1-2 ms**.
- **A bug found by live verification, not by the tests.** One `result.mesh.components` reference
  survived the rename in the bill-of-materials row builder. It threw inside a template literal, so
  the panel rendered *blank* — no error, no missing-field message, just nothing. Two structural tests
  now check that the panel reads only fields the API models actually declare.

### Step 24 — assembly and component decomposition

- **The problem.** `read_step_shape` returns one shape for a whole file, which is what geometry, the
  viewer and recognition have always wanted — and is also what made an assembly look like a part.
  Step 23 could *detect* an assembly and warn that it was being measured as one lump; it could not
  take it apart.
- **A second reader, used in one place.** `cad_assembly.py` is the only user of OpenCASCADE's
  `STEPCAFControl_Reader`, which populates an XCAF label tree rather than one shape. It translates
  labels into plain values before anything else sees them: no `gp_Trsf`, `TopLoc_Location` or
  `TDF_Label` crosses the boundary, and a test greps the serialized document for those names.
- **Definitions and occurrences are separate.** A STEP assembly stores each part once and places it
  as many times as it is used, so geometry hangs off a `ComponentDefinition` and a
  `ComponentOccurrence` is an id, a parent, an instance name and a transform. Conflating them would
  either lose eleven of twelve bolts from the bill of materials or hold the same geometry twelve
  times. An occurrence id is its parent's id joined to the component label's own entry, so ids stay
  unique when one definition is placed at several depths.
- **Quantity is an occurrence count, and says so in its own field description.** How many times the
  assembly uses a component is in the file; how many assemblies are to be built is not, and the two
  are never conflated. Costing one assembly is what this step does.
- **Both placements are kept** — local to the parent and accumulated to the assembly — as twelve
  plain floats each. Losing either would lose orientation, and a future exploded view has to
  displace from the world one.
- **A part is still a part.** A file with several solid bodies and no assembly entities is reported
  as a single part; multi-body is a property of a part and calling its bodies components would
  invent structure. Verified against `model2.stp`: two `MANIFOLD_SOLID_BREP`s, one `PRODUCT`, zero
  `NEXT_ASSEMBLY_USAGE_OCCURRENCE`, read back as one part. And the document now states
  `structure_state` at upload, so nobody discovers afterwards that an assembly was costed as a part.
- **Per-component geometry, never the aggregate.** Each component is measured from its own shape
  through `measure_brep_shape`, the same function that measures a whole part. A component the file
  carries no geometry for is marked `no_geometry_in_file` and left unmeasured; a sub-assembly is
  marked `not_measured`, because its shape is a compound of its children's and measuring it too
  would count the same material twice.
- **Component feature recognition** runs the unchanged recognizers on a component's own shape and
  prefixes every feature id with the component (`HOUSING/H-001`), so two components that each have a
  first hole stay distinguishable. `component_id` is carried on the feature and propagated onto the
  operation suggestion, which keeps component -> feature -> operation unbroken through decomposition.
- **New `assembly_costing.py`**: `sum(per-part cost x quantity)` and nothing else. It imports nothing
  from this package — a structural test parses its imports — so it cannot reach a calculator, a rate
  set or a repository; the existing engine remains the authority for a part. A component that cannot
  be costed is carried with its reason (`no_geometry_in_file`, `not_estimated`, `sub_assembly`,
  `currency_mismatch`), counted separately, and leaves the total marked incomplete. **Nothing is ever
  counted as zero**, because a total that absorbs "we do not know" is wrong in the one direction
  nobody checks.
- **New endpoints**: `POST /cad/assembly` (structure, `?measure=false` for structure alone,
  `?with_mesh=true` for a selectable mesh), `POST /cad/assembly/features`, `POST /cost/assembly`.
  Decomposition is separate from `/cad/import` because it costs a different order of magnitude —
  1.4 s to identify the 198 MB export against about 20 s to read its structure — and one request
  reads the structure once and reuses it for the document, the measurements and the mesh.
- **Viewer** (*superseded by Step 25*): the assembly was drawn as one baked buffer with an index
  range per component, each definition tessellated once and instanced at every occurrence. Selecting
  a component isolated it by narrowing the draw range. When the display budget forced decimation the
  triangles were renumbered and the ranges would have addressed the wrong ones, so per-component
  selection was **withdrawn** with a note rather than left silently wrong. Step 25 replaced the baked
  buffer with one geometry per definition and a transform per occurrence, which removed that caveat.
- **BOM panel** in the browser: component, part number, type, quantity, geometry status and volume,
  indented by depth, with the roll-up beneath it. A test greps `assembly.js` to confirm the browser
  multiplies and sums nothing.
- **Persistence adds no table.** The structure, the quantities, the placements and every component's
  cost travel in the existing estimate snapshots, so a historical assembly estimate stays
  reconstructible and a later CAD change cannot rewrite it.
- **`Figure 3.stp`, re-read through the new layer rather than trusted from Step 23.** 43 definitions
  (7 sub-assemblies, 36 parts), 102 occurrences, **4 levels deep**, 99 of 102 placements away from
  the origin, real Creo part numbers with quantities up to x12 (`31161800SD00018_PRT`). Step 23's
  "102 occurrences across 43 products" was right; its "182 shape representations" was a substring
  artifact — the file has 43 `SHAPE_REPRESENTATION`, 32 `FACETED_BREP_SHAPE_REPRESENTATION` and 5
  wireframe ones. **Zero components measurable**, so `/cost/assembly` returns no total at all and
  says "Nothing is reported as zero". Structure read in 25 s.
- Added 59 tests (`tests/test_assembly.py`, with `tests/assembly_fixtures.py` generating real nested
  STEP assemblies through OCCT's XCAF writer). Suite: 1295 to **1354 passing**. No costing formula
  changed, and single-part geometry, mass, features, plans, setups and cost impact are unchanged.
- **Three bugs found by this work.** A sub-assembly was drawn on top of its own children, doubling
  their triangles and overlapping the viewer's ranges. `main.py` compared a format name directly,
  breaking the Step 23 boundary that the registry owns format knowledge — the adapter now declares
  `reads_assembly_structure`. And the roll-up measured its costed count against *all* occurrences
  including sub-assembly placements, so a complete assembly read as "5 of 6".

### Step 23 — expanded CAD file support and a unified import pipeline

- **The question being answered.** The application read STL and STEP, and the upload panel listed
  extensions. An extension list says nothing about what a format can do, and the three readers
  plus three UI files that each kept their own list could disagree with each other. They did: the
  first attempt at adding IGES left the *Analyse features* button disabled for a perfectly
  analysable IGES file, telling the user it was a triangle mesh.
- **New `cad_import` registry and normalized `CADDocument`.** One adapter per format declares its
  capabilities; `import_cad_document` identifies a file, normalizes it and reports what can be done
  with it. `main.py` parses no file extension anywhere — a structural test reads its source and
  fails if a format name appears in it — and no library type reaches the API, so a future adapter
  using a different library changes nothing downstream.
- **Eight capabilities at four levels, and a reason is mandatory.** `geometry`, `mass`,
  `surface_area`, `features`, `viewer`, `assembly`, `drawings`, `costing`, each `supported`,
  `partially_supported`, `requires_user_input` or `unsupported`. Anything short of `supported`
  **must** carry an explanation, enforced in the model's validator rather than left to diligence: a
  degraded capability with no stated reason reads exactly like a working one. Declarations are then
  refined per file — a faceted STEP weakens features, a wireframe STEP drops geometry and costing,
  an IGES file with no unit declaration drops everything that needs measuring.
- **New `GET /cad/formats`** (the whole matrix, no file) and **`POST /cad/import`** (one file:
  format, representation, units and their provenance, components, header metadata, capabilities,
  warnings). `?with_geometry=false` identifies without parsing. `detect_ms` and `parse_ms` are
  separate, so the import layer's own cost is visible rather than folded into the geometry time.
- **IGES implemented, in the common abstraction.** It shares `GeometryProperties`, the recognizers,
  the budgets and the costing pipeline unchanged, and the STEP tessellation path was refactored into
  `_brep_shape_mesh` and reused so the two formats cannot drift into looking different. Two things
  could not be shared, and are why `cad_iges` exists rather than a rename.
- **IGES finding 1 — a volume must not be reported for surfaces that do not enclose one.** IGES
  usually exports loose trimmed surfaces (entity 144) with no solid, and `BRepGProp` returns a
  number for loose faces regardless: measured on three faces of a 40x30x20 box it returns
  10666.67 mm3, which is the volume of nothing. So closure is established first — the file carried a
  solid, or the sewn shell satisfies `BRep_Tool.IsClosed` — and a file that fails goes down the
  existing `incomplete_requires_input` path with its surface area, which is real, still reported.
  Sewing is recorded in the existing repair fields with its tolerance, so a reconstructed volume is
  never presented as a plain measurement.
- **IGES finding 2 — a surface export loses analytic surface identity, and sewing cannot restore
  it.** Measured on `model1_1.stp`, a real 43-face NX part: the solid-mode IGES kept all 17
  `Cylinder` faces and recognition matched STEP **exactly** at 39 features (6 holes, 11 fillets,
  22 planes); the surface-mode IGES of the same part returned those faces as `SurfaceOfRevolution`
  and recognition found **22 planes and no holes**, while the volume was identical to six decimal
  places. Closure is therefore the wrong question for features. Which kind of export a file is comes
  from scanning its Directory section for entity 186, which costs a byte scan and gives the same
  answer whether or not geometry was parsed — so the upload panel is right before anything heavy
  runs. The degradation is stated prominently rather than left looking like a part with no holes.
- **IGES finding 3 — the reader guesses inches when no unit is declared.** Measured: OCCT honours a
  declared unit correctly (40 units declared INCH read back as exactly 1016.0 mm), but a well-formed
  file with the unit flag *and* units name removed is read as inches silently — a 25.4x error in the
  mass and therefore in the price, with no error and no warning. So the unit flag is parsed from the
  file's own Global section first, and an undeclared unit is refused with that explanation.
  `ensure_readable` applies both guards in one place so geometry and feature recognition cannot
  diverge; a structural test asserts both call it.
- **Native Creo: architecture only, and said so plainly.** The formats are proprietary and
  undocumented, the installed OpenCASCADE ships readers for STEP, IGES, STL, VRML and glTF and
  nothing else, and no open-source Python parser reads `.prt`/`.asm`/`.drw` reliably — checked, not
  assumed. The three adapters declare everything unsupported with a reason that says what the file
  actually is: that `.prt` is not even unique to Creo (NX and Solid Edge use it too), that an
  `.asm` references its members by name rather than containing them, and that a `.drw` is a drawing
  and will never yield a volume. A future licensed reader implements `inspect` and `geometry` behind
  an interface that is already wired; costing, the viewer, recognition and planning read
  `CADDocument` and would not change.
- **Assemblies prepared, not implemented.** `CADDocument.components` is a `CADComponent` tree and a
  part file imports as one component holding the document's geometry — the truth about a part file,
  not a placeholder. What is implemented is *detection*: `NEXT_ASSEMBLY_USAGE_OCCURRENCE` counted in
  the raw bytes, so an assembly says so and warns that it is measured as one lump of material. On
  the real 198 MB Creo export that is 102 component placements across 43 product definitions.
- **Two refusals, two statuses.** A format nothing can read answers **415** with the adapter's
  explanation — a `.drw` is not a malformed file, it is a drawing. A format that reads perfectly but
  cannot do one thing answers **400**, which is the status the STL-features path has always
  returned and still returns.
- **The UI is rendered from the backend's declarations.** The hardcoded `accept` list and the
  separate STEP-only lists in `features.js` and `suggestions.js` are gone; the upload panel, the
  file picker's accepted types, the unit lock and the *Analyse features* gate all read
  `/cad/formats` and the imported document. The old test asserting a hardcoded list was replaced by
  one asserting there is no list. A per-file panel shows every capability that is short of full
  support with the backend's own reason.
- Added 118 tests (`tests/test_cad_import.py`, with `tests/iges_fixtures.py` generating real IGES
  locally through OCCT's own writer in both modes, plus a well-formed file with its unit declaration
  removed). Six existing tests that encoded "IGES is unsupported" were updated, and one UI test was
  inverted. Suite: 1171 to **1291 passing**. No costing formula changed, and a regression test
  asserts that routing STEP or STL through the new layer yields a byte-identical
  `GeometryProperties` to calling the extractor directly.
- **Live verification** on the running app. `model1_1.stp` and `model2.stp`: unchanged volumes
  (220621.0784 and 487455.5638 mm3) and unchanged feature counts. Both exported to IGES both ways
  and re-imported: volumes agreed with STEP to within 4.3e-5 mm3 in every case, areas to 1e-6, and
  costing `model1_1` at quantity 10 gave **INR 3,891.48 from the STEP, the solid IGES and the
  surface IGES alike**. Features: 39 from STEP, 39 from solid IGES, 22 from surface IGES with the
  warning shown. `Figure 3.stp` (198 MB): identified in **1.3 s with no parsing**, correctly as
  faceted, Creo-authored, and an assembly of 102 occurrences. `.prt`/`.asm`/`.drw` each answered 415
  with their own explanation while `/cad/import` still described them. A unit-stripped IGES was
  refused; the same file relabelled INCH read back at exactly 25.4x. In the browser: the
  capability panel, the per-file caveats, the IGES model in the 3D viewer, the surface-model warning
  on the features panel, and a completed estimate from an IGES file.
- **Two bugs found by this work.** The first attempt tied the IGES features claim to closure, which
  the measurement above disproved; the second was the UI's own STEP-only lists disabling analysis
  for a valid IGES file — the reason the browser now reads the capability report instead of keeping
  its own answer.

### Step 22 — cost and assumption impact analysis
- **The question being answered.** The same CAD file can produce very different costs — a rate set version, a time profile version, the material, the quantity, the operation times, the setup grouping, the additional costs all feed it. An estimate that cannot say which of those moved is not trustworthy however correct its arithmetic.
- **New `EstimateAssumptions` and `GET /estimates/{id}/assumptions`**: CAD file, geometry source, volume, mass; material, density, cost per kg; costing profile id/version/currency; time profile id/version; quantity, setup count, per-setup times, costed operations, setup and cycle minutes; overhead, scrap, tooling. Read back from the estimate's own snapshots, so a stored estimate keeps reporting what actually produced it even after both profiles have been superseded. A test publishes a new rate set version and asserts the stored assumptions are byte-identical afterwards.
- **New `CostImpact` decomposition.** Most components are `rate × driver`, and a two-factor variance split accounts for a change exactly: `rate effect = (r_a−r_b)·d_b`, `driver effect = r_b·(d_a−d_b)`, `interaction = (r_a−r_b)·(d_a−d_b)`. The three sum to the delta with no remainder — an algebraic identity, not an apportionment.
- **Categories**: `material_rate`, `material_quantity`, `operation_rate`, `operation_time`, `operation_added`, `operation_removed`, `setup_rate`, `setup_time`, `overhead`, `scrap`, `tooling`, `combined`, `rounding`, `unexplained`.
- **No false attribution.** The interaction term is its own `combined` line rather than folded into either side. The parts are checked against the whole and any remainder is reported — `rounding` within a small allowance (stored amounts are rounded to two decimals), `unexplained` beyond it. Attribution counts *inputs* rather than fields, because one edit usually moves several: adding a setup moves the count and the minutes; changing the costing profile moves the material price, the machine rates and the overhead percentages, since it supplies all of them. Only genuinely separate inputs yield `multiple_inputs`, which then names them and shows every before/after with **no causal percentages**.
- **Operation-level diff** matched by operation id, preserving the original ids: added, removed, changed, moved between setups, with before/after cycle, setup and cost. **Sequence** differences are reported with a cost impact of exactly zero and a note saying the current model has no sequence-dependent cost — the limitation made visible rather than hidden.
- **Different currencies are never subtracted.** The comparison flags the mismatch, omits the money impacts and keeps the input differences.
- **`/estimates/compare` extended, not replaced.** It keeps the totals it always returned and adds the new sections; `?detail=false` gives the old cheap answer. The comparison layer still imports no calculator and no repository — snapshots reach it through an `EstimateDetail` carrier that `main.py` builds — and a test asserts that by reading the source.
- **UI**: an **Assumptions behind this estimate** panel on every result, with the profile versions as links to the exact rates and feeds that produced the number; a **Why did this change?** panel comparing against the previous estimate; and the history comparison expanded into the full side-by-side — Overview, Geometry, Material, Costing profile, Time profile, Manufacturing plan, Additional costs, Cost drivers, What changed, Sequence, Operations — with differing rows highlighted. The browser derives no delta of its own; a test greps `impact.js` to keep it that way.
- Added 44 tests (`tests/test_cost_impact.py`), several of which assert the impacts sum to the actual delta across a sweep of change kinds. Suite: 1127 to **1171 passing**, no regressions. No costing formula changed.
- **A units bug caught by the guard.** The first decomposition multiplied a per-hour rate by *minutes*, making every time effect sixty times too large. The `unexplained` remainder surfaced it immediately with a −₹213,953 line rather than shipping a plausible wrong answer; rates are now paired with hours and the minutes are display-only. A second fix stopped quantity leaking into the plan's cycle-minute figure, which had made a quantity change look like a second, independent change to the operations.
- **Live verification** on `model1_1.stp` through the real UI, one change at a time. Operation cycle time 1.32 → 9.9 min: **+₹3,289.00**, `single_input` — "the operations (Cycle minutes per part 8.28 → 16.86)", one driver. Setup grouping 1 → 2 setups: **+₹300.00**, `single_input` — "the setup grouping (Setup count 1 → 2; Total setup minutes 10 → 20)". Costing profile poc-default v1.0.0 → poc-machine-shop v2.0.0: **+₹2,690.25**, `single_input` — "the costing profile", decomposed into material rate +₹2.60, operation rates +₹843.00, setup rate +₹66.66, overhead, scrap and tooling. Then quantity, another cycle time and a third setup changed together: **₹9,767.11 → ₹24,633.34 (+₹14,866.23)**, `multiple_inputs`, naming the operations, the quantity and the setup grouping and showing each driver with no invented percentage. The history side-by-side rendered all eleven sections with the ten differing rows highlighted.

## What has NOT been done (explicitly out of scope so far)

- Editing or deleting an existing rate set version — deliberately impossible; publish a new version instead
- CAD file parsing for formats other than STL, STEP and IGES. **Native Creo `.prt`/`.asm`/`.drw` are recognized and refused with an explanation, not supported**: the formats are proprietary, the installed OpenCASCADE has no reader for them, and no reliable open-source parser exists. Parasolid, ACIS, JT and NX native are in the same position
- Drawing information of any kind. No views, dimensions, tolerances or PMI are read from any format, STEP included
- Automated STEP or IGES export from Creo (producing the neutral file is currently a manual export)
- Assembly *manufacturing*. Components are separated, measured, classified and rolled up, but there is no assembly labour, no fastening or joining time and no assembly-level routing. An assembly total is the sum of each component's contribution and every summary says so
- PDF generation of its own. The quotation prints through the browser, which is deliberate: a PDF library would be a second rendering path to keep in agreement with the first
- Emailing, sending or hosting a quotation. It is a page that prints; distributing it is the user's business
- A customer or contact record. The quotation metadata is six fields typed onto one document, with nothing inferred from them and no address book behind them
- Any quotation figure that is not read from the estimate. Taxes, freight, margin, discount and payment terms are not calculated, defaulted or shown — adding any of them would mean a second costing path
- Any new capability in the UI restructuring. It moved cards between sections, folded evidence behind disclosures and fixed layout; it added no feature, no endpoint and no calculation, and the costed figures are identical
- Any parametric feature history in the interface. `Figure 3.stp` is a faceted export with no feature tree, and no part of the UI implies otherwise
- Any backfill of a value that is not provable from persisted data. Zero were available: the only deterministic recovery — a component definition id from the feedback map's own key — was already performed by Step 29
- Any recovery of the 712 historical feature confidences. The CAD file is not stored and no snapshot holds the recognizer's output, so they are permanently unrecoverable and reported as such rather than estimated
- Any inference of an identity from a display name, of a confidence from acceptance frequency or rule performance, or of a cycle time from a cost or a rate. Tests unparse the classifiers to enforce each
- Any claim that more evidence means a model is ready. Dataset readiness is an evidence-quality report; the note says so in those words
- Any learning, of any kind. No model is fitted, no rule updated, no time assumption adjusted, no confidence recalibrated, and no future estimate is affected by a past correction. The analytics layer imports nothing and cannot reach anything it could change
- Any statistical claim the dataset cannot support. There is no significance test, no confidence interval and no distributional assumption — the sample guard is a configurable floor, and below it the finding is a sentence rather than a number
- Any claim that the system's confidence is calibrated. Acceptance rates by band are reported as measured associations; confidence here is a three-way classification of which rule branches fired, never a probability
- `feature_confidence` on any historical record. Step 30 fixed the capture — the plan carries it, `plan.js` now sends it, and a new estimate records it on every operation — but unlike `rule_id` and `source_type` there is nothing in a stored snapshot to recover it from, so the **712** records written before the fix report it as permanently unrecoverable and the readiness section counts them there
- An assembly-level manufacturing plan or routing. Each component is planned on its own; nothing knows what order components are made or joined in, and no setup is ever shared between two components
- Automatic setup generation. Every component starts with one assumed setup at low confidence, exactly as a single part does, and splitting it is the engineer's decision
- Any component plan for a component the file carries no geometry for. `Figure 3.stp`'s 36 parts are all reported `blocked` with the reason, and not one is given a fabricated plan
- Any inference of make/buy. The classification is a decision a person makes; the system's only contribution is to say whether the file carries geometry it could cost
- Supplier price lookup, live market rates, a standards catalogue, or a parts database. A purchased or standard cost is exactly what someone typed, and there is nowhere for it to come from otherwise
- Automatic learning from a classification correction. The pairs are recorded; no model is fitted and a correction never changes a later suggestion
- Currency conversion. More than one currency in an assembly is reported as not aggregatable, never reconciled
- Assembly-level manufacturing: no assembly labour, no fastening or joining time, no routing, no make/buy economics beyond the stated unit cost
- An assembly or disassembly **sequence**. The exploded view is a visualization derived from placement and hierarchy; nothing infers what has to come off before what, or whether a component can be withdrawn along the direction it is drawn moving
- Collision-aware or path-aware explosion. Components move radially and may pass through one another on the way; the arrangement is read at its endpoints, not animated as a motion
- Exploding an assembly whose components carry no geometry. `Figure 3.stp`'s arrangement is computed for all 102 occurrences and can be inspected numerically, but nothing can be drawn, so there is no picture to explode
- Per-component materials. Nothing in a STEP file says what a component is made of, so one material is applied to every component and recorded as an assumption. Material is never inferred from a name, a density or a shape
- Production quantity for an assembly. The roll-up costs **one** assembly; how many are to be built is a separate number the file does not contain, and applying it would need a decision about batch economics (tooling per batch, in particular) that this step does not make
- Component structure from anything but STEP. STL and IGES carry no product structure at all, and native Creo remains unreadable
- Per-component geometry from a faceted assembly. `Figure 3.stp`'s components can be named, counted and placed but not measured, because OpenCASCADE transfers nothing from its schema-invalid `FACETED_BREP` entities
- Feature recognition from a surface-mode IGES export. Measured: cylinders arrive as generic surfaces of revolution, so holes and fillets cannot be identified. Reported rather than worked around — no attempt is made to re-fit an analytic surface to a spline
- A unit override for a file that declares none. An IGES file with no unit declaration is refused rather than measured; the capability reports `requires_user_input`, but there is no input to supply yet
- **Learned or calibrated** machining times. Step 20 estimates times from a fixed parameter model; nothing is fitted to real data, and a recorded correction never changes a later prediction
- Real cutting parameters *out of the box*. The built-in baseline profile is illustrative in exactly the sense the rates are. Step 21 makes it replaceable — publish a profile with your shop's own feeds and speeds — but nothing verifies that what you publish is right
- Tool selection, tool-change time, rapid traverse, dwell, coolant, or any second-order machining effect. The formulas are one line each and deliberately coarse
- Setup consolidation. Setup is charged per operation *line*, so six holes listed separately carry six setups; whether they share a fixture is a decision the model cannot make
- Automatic operation selection. Operations are suggested for review, never applied: nothing reaches an estimate without an explicit acceptance
- Sequence *consequences*. Both levels of order are recorded and editable, and a comparison now says explicitly that reordering costs nothing, but nothing is sequence-dependent: no routing optimisation, no alternative routes, no enforced precedence, and no automatic setup generation. Setup *membership* changes cost; setup *order* does not
- Currency conversion. Two estimates in different currencies are compared on their inputs only; their money is never converted or subtracted
- Aggregate reporting across estimates. A single pair can be compared and explained; nothing rolls many estimates up into a trend
- A validation report across many historical parts. Iteration 1 compares one actual with one estimate; nothing aggregates the variances
- Importing actual costs from a file. Each is entered through the History panel or the API
- Adjusting a variance for a difference in batch size. It is reported as a warning only
- Matching an actual's client part number or drawing revision to anything. They are stored as entered
- Fixture recognition. The system cannot tell from geometry whether two operations can share a fixturing; it groups conservatively, says so at low confidence, and leaves the decision to the engineer. Since Iteration 7 it can *propose* a grouping by tool approach direction, checked only with straight rays, which the engineer applies or ignores
- A training pipeline. Step 20 *persists* the prediction/decision pairs an eventual learning loop would need, but nothing reads them back, aggregates them, or learns from them
- Face-level highlighting in the 3D viewer. A selected feature is marked with a point and its axis; lighting up its exact faces needs a per-face id the wire mesh does not carry
- AI/agent components
- Creo integration
- Windchill integration
- Manufacturing route *optimisation*. Iteration 2 recommends one family and route for review from feature evidence; it does not compare alternative routes, choose between them on cost, or sequence stages
- A cost model for any non-machining stage: sheet cutting, bending, casting, forging, welding, additive, moulding, finishing or purchase. Such routes are refused, not priced
- Any process inferred from material, tolerance, finish, quantity or a part name. None of these is read; the family comes from geometry signals alone and is capped at medium confidence
- Classification of assembly components into a manufacturing family
- Multi-part assemblies / BOM handling
- Engineering and inspection cost (overhead, scrap allowance, and tooling now exist; these do not)
- Yield-based scrap modelling (`1 / (1 - scrap_rate)`) — scrap is currently a linear allowance, which understates cost at high scrap rates
- Real/customer pricing anywhere — the rate-set catalog is illustrative placeholders only
- Real/customer material or rate pricing (current values are illustrative placeholders only)
- Production-grade persistence: schema changes are now migrated with Alembic, but there is still no backup/retention policy, no auth, and no concurrency handling beyond SQLite's own
- Data migrations (Alembic handles schema; nothing back-fills or transforms existing rows)
- Frontend/UI
- Authentication, CI/CD, deployment
- Higher-quality display decimation (quadric edge collapse). Vertex clustering preserves silhouettes and proportions but can pull corners inward by up to half a grid cell and loses detail below the cell size — acceptable for a preview, and never used for a measurement, but it is visibly coarser than the CAD data

## Next step

The roadmap agreed at Iteration 0, in order: (2) a raw-stock material basis, since
material is costed from finished mass today and the first live comparison showed
material at -97.69%; (3) bulk import of a client's rates; (4) per-piece and per-kg
pricing for outsourced processes; (5) confirming cost-model assumptions with the
client (labour during setup, linear scrap); (6) a validation report across many
parts; (7) housekeeping, including the stale assembly capability text in
`cad_import` that still says make/buy classification is unimplemented.

Iteration 2 (manufacturing family and route classification) was taken ahead of these
at the user's request. Since then: (2) is done for **turned parts** (bar stock, a
follow-up to Iteration 2) but not for milled or sheet parts; (4) is done by Iteration 3
(stage rates per kg / per piece / per batch). Still open: raw stock for prismatic and
sheet parts (plate/block stock, sheet blank and nesting); (3) bulk import of a client's
rates; (5) the cost-model assumptions; (6) the multi-part validation report; (7)
housekeeping, now also the *Assumptions* panel's material line on a stage-supplied
route and the quotation's ₹0.02 per-operation rounding remainder.

Iteration 4 added route comparison, DFM flags and a bulk preview. They open three
natural follow-ups: profile authoring for DFM in the UI; a hand-off from a bulk preview
row into the normal review flow; and pricing a route comparison with the plan the
engineer is editing (it already accepts one).

The work is on branch `iterations-1-3`, not on `main`; merging it into `main` is the
user's decision. The three agent worktrees under `.claude/worktrees/` can be removed with
`git worktree remove` once their branches are no longer wanted.

Each needs the user's go-ahead. Prismatic stock needs the client's convention for
stock size and allowance before anything is built.
