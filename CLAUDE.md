# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup (Python >= 3.11; Windows shown, use source .venv/bin/activate elsewhere)
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"

# Run the app — UI at http://127.0.0.1:8000/, API docs at /docs
uvicorn cad_costing_agent.main:app --reload

# Live verification against a throwaway database, which is usually what you want. The
# default is the real SQLite file ./cad_costing_agent.db — it holds the history that
# every "never delete the database" rule below is about, and startup migrates and seeds
# whatever it is pointed at.
CAD_COSTING_DATABASE_URL=sqlite:///./scratch.db uvicorn cad_costing_agent.main:app --port 8710

# Tests (2633 collected; measured without cadmodels/: 2625 pass, 8 skip; full run about 3-12 min)
# cadquery-ocp must stay below 8 (pinned): 8.x drops names this code imports.
# A git worktree has no cadmodels/ (git-ignored), so tests needing real models
# skip there (8 skips instead of 1). Judge a branch by a run in the main tree.
pytest
pytest tests/test_operation_suggestions.py                          # one file
pytest tests/test_features.py::test_a_through_hole_is_found_with_its_diameter_depth_and_axis
pytest -k "faceted and not viewer"                                  # by expression

# Migrations. Reads CAD_COSTING_DATABASE_URL; alembic.ini deliberately does not
# set sqlalchemy.url so the CLI can never drift from what the app uses.
alembic current
alembic upgrade head
alembic revision --autogenerate -m "..."   # after changing db_models.py

# Demo. `DEMO.md` is the runbook (PowerShell — this is a Windows machine, and a
# bash-syntax command line in it is a parse error, not a portability nit). The
# pre-flight publishes a rate set through the ordinary API and warms
# OpenCASCADE; it adds no demo mode and fakes nothing.
python scripts\demo_preflight.py --base-url http://127.0.0.1:8000
```

There is no linter or formatter configured, and no build step for the frontend.

**Two rate sets and one time profile are seeded at startup, and they are not interchangeable.**
`poc-default` prices milling, drilling **and turning**; both versions of `poc-machine-shop` price
milling and drilling only. Any plan containing a turning operation — which the `pump_assembly`
fixture's components and most real parts do — fails against `poc-machine-shop` with *"has no rate
for process type 'turning'"*, a 400 that reads like a bug in whatever you just wrote. It has cost
two debugging detours: once in the demo pre-flight, once in a test. The time profile is
`poc-baseline-time`. All are flagged illustrative and the UI says so unmissably.

Real CAD test models live in `cadmodels/` which is **git-ignored** — `Figure 3.stp` alone is
~198 MB. Never add them to the repository. `tests/step_fixtures.py`,
`tests/faceted_fixtures.py` and `tests/iges_fixtures.py` generate their files locally with
OpenCASCADE's own writers instead, so fixtures are deterministic and their expected volumes are
known exactly. The IGES fixtures deliberately generate **both** export modes, because the two behave
differently in ways that matter (see below).

## Architecture

A FastAPI app where `main.py` is a thin HTTP layer: it reads uploads, resolves a rate set from the
database, and calls pure domain functions. All arithmetic lives in modules that take values and
return values.

Dependencies run one way — `main.py` → `repository.py` → domain — and the domain never reaches
back. Where a pure module needs persisted data it is handed a plain carrier that `main.py` builds:
`estimates.compare_estimates` takes `EstimateDetail`s rather than importing `repository`, which is
what keeps the comparison layer free of both the database and the calculator.

### The branches that must never meet

One CAD upload fans out into independent branches. Keeping them apart is the central design
constraint of the codebase, and **structural tests enforce it by reading module source** — cross a
boundary and a test fails by name:

```
CAD file ──→ cad_import                                    format detection + capabilities
              │  (the one place an extension is read)
              ▼
          CADDocument
              │
          ─┬─→ cad_geometry → cad_mass → costing            authoritative: the only path to money
          │       (for a component: only if it is classified `manufactured`)
          ├─→ cad_geometry → cad_mesh → mesh_simplify      viewer; display only
          ├─→ features/                                    recognition; analysis only
          └─→ features/ → operation_suggestions            proposals only
                        → process_classification           family + route; recommendation only
                        → operation_time                   times only, never money
                          (parameters from time_profiles — versioned, immutable)
                        → manufacturing_plan               where a person commits a proposal
                        → operation_feedback               predicted vs chosen, recorded only
```

- `cad_import` is the only place a file extension is read. `main.py` parses none, and a structural
  test fails if a format name appears in its source. The upload UI renders `/cad/formats` rather
  than keeping its own list — before Step 23 six places held their own answer and they disagreed.
- `cad_geometry.extract_geometry_from_bytes` is the single entry point every branch starts from, so
  the numbers shown beside the 3D model are the numbers costing uses, never a second opinion. The
  adapters route to it; they never measure anything themselves, and a structural test greps them
  for `BRepGProp` and `trimesh`.
- `read_step_shape` and `read_iges_shape` are the single places STEP and IGES are parsed, so there
  is one definition of "readable" per format and one set of failure messages. `read_step_assembly`
  is the single place STEP's *product structure* is read, through OpenCASCADE's other reader.
- `measure_brep_shape` is the one implementation of "measure exact geometry", shared by whole-file
  extraction and by a single assembly component, so a component cannot disagree with the part it is.
- Costing modules must not import `mesh_simplify`, `operation_suggestions`, `operation_time`,
  `manufacturing_plan` or `operation_feedback` — and none of those may import costing arithmetic.
  `manufacturing_plan` is the sole bridge: it emits `OperationRequirement`s that the existing
  unmodified costing consumes exactly as it does a hand-entered operation.

### STEP comes in two representations, and this drives most of the complexity

`features/step_text.py` classifies an upload before anything heavy is attempted:

- **Analytic B-Rep** — exact surfaces. OpenCASCADE reads it; `BRepGProp` integrates volume and
  area over the real geometry; recognizers fit rules to actual faces and edges.
- **Faceted** (`FACETED_BREP`, typical of large Creo exports) — flat polygons only. OpenCASCADE
  transfers *no faces at all* from these, so `cad_faceted.py` parses, welds and closure-checks the
  polygons instead, and `features/mesh_recognition.py` reconstructs features by fitting.

Both paths produce the *same* `GeometryProperties` and `Feature` objects, distinguished only by
`geometry_source` / `source_type` / `representation`. Anything reconstructed from polygons is
weaker evidence than something read from an exact surface, and the code says so everywhere: a
faceted feature caps its operation suggestion at `medium` confidence.

STL is fully supported for geometry, the viewer and costing, but **feature recognition refuses it**
— a hole in a triangle mesh is indistinguishable from a many-sided prism.

### IGES is not STEP with a different extension

Both differences below were measured, and both are load-bearing:

- **IGES usually carries no solid.** The common export is loose trimmed surfaces (entity 144), and
  `BRepGProp.VolumeProperties` returns a plausible number for loose faces — 10666.67 mm³ for three
  faces of a box. `cad_iges.analyse_iges_shape` therefore establishes closure first (a solid in the
  file, or a sewn shell that `BRep_Tool.IsClosed` confirms) and reports no volume otherwise.
- **A surface-mode export loses analytic surface *identity*.** Its cylinders come back as
  `SurfaceOfRevolution`, so holes and fillets cannot be recognized even after sewing restores the
  topology. Measured on a real 43-face part: 39 features from the solid export, 22 planes and no
  holes from the surface export, identical volume. So the `features` capability is decided by
  scanning the Directory section for entity 186 (`cad_iges.carries_a_solid`) — *not* by closure, and
  not after the parse, so the answer is the same with or without geometry.
- **The reader guesses inches when no unit is declared** — 40 mm silently becomes 1016 mm. Never
  hand an IGES file to the reader without `cad_iges.ensure_readable`, which applies the
  not-IGES and no-unit guards in that order.

### Assemblies: a definition is not an occurrence

`cad_assembly.py` reads a STEP file's product structure with `STEPCAFControl_Reader` — a *different*
reader from `read_step_shape`, and the reason an assembly can be taken apart at all.

- **Geometry hangs off the definition, never the occurrence.** Twelve bolts are one
  `ComponentDefinition` measured once and twelve `ComponentOccurrence`s. Both models exist because
  they have different cardinality; merging them loses eleven bolts or duplicates their geometry.
- **Structure survives when geometry does not.** Measured on the 198 MB export: OCCT transfers zero
  faces from it, yet XCAF recovers all 43 definitions, 102 occurrences and 4 levels. So a component
  can be named, counted and placed while being unmeasurable, which is `geometry_status =
  no_geometry_in_file`. Never substitute the aggregate for a component's own measurement.
- **A sub-assembly is neither measured nor drawn nor costed.** Its shape is a compound of its
  children's, so doing any of those counts the same material — or the same triangles — twice. All
  three were bugs during development and are now tests.
- **Both transforms are kept** (local to the parent, world accumulated) as twelve plain floats. No
  OpenCASCADE handle leaves the reader; a test greps the serialized document for `gp_Trsf` and
  friends.
- **A multi-body part is not an assembly.** `model2.stp` has two solids under one product and no
  `NEXT_ASSEMBLY_USAGE_OCCURRENCE`, and reads back as one part. Do not treat separate solids as
  components.
- **`assembly_costing.py` imports nothing from this package**, so it cannot reach a calculator or a
  rate set — it multiplies and sums figures it is handed. A component it cannot cost is carried with
  its reason and leaves the total incomplete; **never write a zero there**.

### An empty field has four possible meanings, not two

`feedback_analytics.evidence_fields` classifies each evidence field as `known`,
`not_applicable`, `unrecorded` or `unrecoverable`. Asking only "is it filled in?" is
how Step 29 came to report 449 single-part records as missing a component identity
and 302 unfinished operations as missing a paired time.

- **Applicability comes from the snapshot's shape, never a guess.**
  `component_identity_applicable` is set only for records from an assembly's
  per-component feedback map. `time_profile_version` is not applicable when no
  `time_profile_id` was recorded — the plan ran under the built-in baseline.
- **`percent_known` divides by `applicable`.** Never by the total.
- **Backfill only what is provable from persisted data.** The one recovery here is
  taking a component definition id from the feedback map's own key. Never infer an
  identity from a display name when a stable id exists, and never turn `unknown` into
  `probably X` to improve a number.
- **`unrecoverable` is a limitation, not a backlog.** The CAD file is not stored, so a
  feature confidence that was not written down is gone. Say so.
- **Never manufacture a confidence.** Not from acceptance frequency, not from rule
  performance, and a binary rule match is not a confidence score. Tests unparse the
  classifiers to enforce it.

### The browser's plan builder is a persistence path

`plan.js` builds the plan payload in JavaScript rather than calling `build_plan`, so
**any field it does not copy never reaches the server**. That is how all 713 stored
records came to lack a feature confidence the backend was populating correctly. When
you add a field to `PlannedOperation`, add it to `plan.js` too — and do not leave a
display-only `_`-prefixed duplicate of a real field beside it.

### The feedback analytics reads; it must never be able to write

`feedback_analytics.py` reports the recorded corrections. Its design is about what
it cannot do.

- **It imports nothing from the package.** Keep it that way: that is what makes
  "analytics changes nothing" structural. Three tests hold it — the empty import
  set, that no module but `main.py` mentions it, and that its source has no
  `session`, `Repository`, `save`, `commit` or `fit(`. Every endpoint is a `GET`.
- **Read snapshots with `.get()` on plain dicts, never by validating into today's
  models.** A default that fills an absent field reads exactly like recorded data.
  Absent is `unknown`, and it is counted in the readiness section.
- **An acceptance denominator is the operations someone *decided about*** — accepted,
  changed or rejected. An operation with no committed time was never finished (302
  of 713 in this project's own history) and counting it made a rule with one
  correction read as 8% acceptance. Every `CountedRate` carries `numerator`,
  `denominator`, `excluded` and a `basis` sentence.
- **Medians carry every headline; outliers are listed apart and still counted;
  relative error skips predictions below `RELATIVE_ERROR_FLOOR`.** Do not trim an
  outlier to calm an average.
- **`SampleGuard` on every metric that needs a sample.** The threshold is a
  configurable floor, explicitly not a significance test. Below it, print the
  sentence, not the number.
- **Confidence is an association, never calibration.** Confidence here is a
  three-way classification of which branches fired, not a probability.
- **`LEARNING_DISCLAIMER` on every response.** A page of correction rates is where a
  reader starts assuming something adapts.
- **Nothing may learn.** No rule update, no profile update, no confidence change, no
  model. If a future step trains something, it does not live in this module.

### A manufacturing route is recommended, never concluded

`process_classification.py` recommends a family and a route of stages from the features
recognition already produced. Shape does not determine process, and the module is built
around saying so.

- **Confidence is capped at `medium`.** No rule may raise it; a test checks every fixture.
  Non-B-Rep evidence, several solids or unrecognized faces make it `low`.
- **Never propose a family nothing can detect.** Casting, forging, moulding, welding,
  additive and purchased are listed in `not_assessed` and can only be chosen by a person.
  Do not add a rule for one without a recognizer that produces evidence for it.
- **Thin evidence is `unknown`**, with `needs_information`. Accepting it is refused.
- **Costability is a computed field**, recomputed from the route's stages on the server, by
  stage id against `STAGES` — never from the payload's `process_type`. Stages whose process
  type is `milling`, `drilling` or `turning` are priced by the hour; every other stage
  (`stage_rate_stages`) is priced from the rate set's stage rates (`stage_costing.py`, below).
  `costing_status` is `stage_rates_required` for such a route; `blocks_costing` holds only for
  a route marked unknown or an *unreviewed* recommendation needing stage rates, and
  `/cost/cad-part` refuses those with `422 process_route_not_costable`. Never price the
  machined stages of a mixed route as the whole.
- **An unreviewed `unknown` does not block costing**; an engineer's `unknown` does. Sending no
  classification must stay byte-for-byte the old behaviour — the snapshot keys are absent, not
  null.
- **It imports `features.model` and `operation_suggestions`; only `main.py` imports it.** A
  route stage never becomes an operation — a casting or bending stage is a line in the
  route strip and a display row in the operations table (`routeStageRows`, no `plan-row`
  class), never a `PlannedOperation`, because it has no hourly rate. The one exception is a
  *timeable* stage the engineer chose to price by time (welding, below): the stage stays a
  display row, and its welds are ordinary operations the engineer adds.
- **A decided route regroups the plan; it never edits it.** `plan_processes` (computed, `None`
  until someone decides) becomes `ManufacturingPlan.route_processes`; `in_route` keeps an
  operation whose process is outside it in the plan, unchanged, but out of
  `to_operation_requirements`, out of `uncosted_operations` (it is excluded, not missing a
  time) and listed in `out_of_route_operations`. `/cost/cad-part` sets `route_processes`
  from the classification itself and ignores the browser's copy. `route_processes = None`
  must stay the pre-route behaviour exactly — assembly component plans never get one.
  `plan.js`'s `inRoute` mirrors `in_route`; change one, change both.
- **The UI decides nothing.** `process.js` renders the server's object, takes the route list
  from `/process-routes`, and sends every decision through `/cad/process/decide`.

### A non-machining stage is priced from a rate someone published, or not at all

`stage_costing.py` prices a decided route's casting, forging, sheet, finishing, purchase
(every non-machining) stages from `RateSet.stage_rates`: per kg or per piece, plus an
optional one-off batch charge (pattern, die).

- **Every stage or none.** A stage with no rate, or a per-kg stage with no entered mass,
  makes `/cost/cad-part` answer `422 route_stages_not_priceable` with a reason per stage,
  and nothing is stored.
- **The per-kg mass is the engineer's (`stage_inputs`) or, if none was entered, the finished
  part's CAD mass** (volume x density) — a derivation, labelled `mass_source:
  "cad_finished_mass"` with a `mass_note`, and editable. Never add a hidden allowance to it:
  on a route that machines afterwards the note says the real blank is heavier by material the
  CAD does not record. An *entered* blank lighter than the part is refused. `mass` stays the
  finished part's own. (Changed at the user's request after Iteration 4; it was "entered only".)
- **A route that starts with casting, forging, moulding, an additive build or a purchase
  does not charge material again** (`MATERIAL_SUPPLYING_STAGES`): `material_charged` is
  false, `total_material_cost` is 0 with `material_note` saying which stage carries it. A
  confirmed bar on such a route is a contradiction and is refused (400).
- **Route steps** (`ROUTE_STEPS`: fettling, forge finishing, deburring, fit-up, weld
  finishing, degating, support removal) are stages a route always carries after its forming
  stage, never detected from geometry. `_ROUTE_BY_STAGES` keys on the *evidence* stages only and
  `_route_for` fills the steps in from the catalog with `ROUTE_STEP_BASIS` as their basis — so
  adding a step never breaks a recommendation. Never put one first: the first stage decides
  material-included and as-formed. A per-kg stage after the forming stage with no mass of its
  own takes the blank's (`mass_source: "blank_mass"`).
- **Stage costs join the direct cost base**, so overhead and scrap apply to them as to
  material. `stage_costs` is empty and `total_stage_cost` 0 for every other estimate.
- **It imports `money` and `rate_sets` only**; stage ids reach it as strings from `main.py`.
  `rate_sets.py` may not import `process_classification`, so stage-rate keys are checked
  against `STAGES` in `main.py` (`_stage_rate_problems`) at publish and validate.
- **No seeded rate set has stage rates**, and none should be added to a seeded version: it
  would make a fresh database disagree with an existing one. Client rates are published.
- **Welding may be priced by time instead** (`TIMEABLE_STAGES = {"welding": "welding"}`, the
  only stage with a time model). `stage_inputs[stage].pricing = "timed"` takes the stage out
  of `price_stages` and adds its process to `route_processes`, so it is charged once — by its
  weld operations at the rate set's hourly `welding` rate — and never also by the stage rate.
  A timed stage with no committed weld time is `422 timed_stage_not_costed`, never zero; any
  other stage asked to be timed is a 400. Absent `pricing` is the stage rate, byte-for-byte.
- **A weld is typed, never detected.** `PlannedOperation.weld` (`WeldSpec`: type, MIG/TIG,
  length, size) is the engineer's; a weld line has `system_suggested_operation = None` (no
  rule proposed it) and `system_time_operation_type = "welding"`, which `estimated_for` reads
  so its system time can apply. Describing the weld again re-estimates it and clears the
  acceptance (`with_weld`; `plan.js`'s `estimateWeld`). The `welding_*` `TimeAssumptions`
  default to **None** — the seeded profile times no weld; `illustrative-welding-2026` is
  published by `scripts/publish_illustrative_welding_time_profile.py`. `plan.js`'s
  `TIMED_STAGE_PROCESSES` and `estimatedFor` mirror the backend; a test holds each.
- **Per-weld handling is cycle time, and optional on its own.**
  `welding_reposition_minutes_per_weld` / `welding_tack_minutes_per_weld` are added to a weld's
  cycle when a profile states them and are *not* in `_WELDING_PARAMETERS`: a profile without
  them times welds exactly as before. Never move them to the setup. The illustrative profile
  grows by versions in the publish script's `VERSIONS` table; never edit a listed one.
  (`test_no_machine_learning_is_involved` greps `operation_time.py` for `torch` — do not write
  that word in a welding description.)
- **A yield is the engineer's, and only the material-supplying per-kg stage takes one.**
  `stage_inputs[stage].yield_percent` divides the base mass (entered blank, else CAD finished
  mass); the line's `mass_kg` is the mass *charged*, `base_mass_kg` / `base_mass_source` what
  it came from, `mass_source: "yield_applied"`. Never default one. An entered mass is the
  *blank*, so mass plus yield is not a contradiction; a later per-kg step takes the blank's
  mass, never the poured mass. Absent yield means absent keys (a test pins the key set). A
  yield on a per-piece, batch-only, non-supplying or timed stage is a 400; one for a stage
  the route lacks is ignored, because `stage_inputs` is shared across a comparison's routes.
  The page offers the input from `/process-routes`'s `yield_stages` and never divides.
- **A tool's life is the engineer's, and only a stage with a one-off charge takes one.**
  `stage_inputs[stage].amortisation_quantity` makes the line's `batch_charge` the share
  `full_batch_charge x quantity / amortisation_quantity`, so `total_cost = run_cost +
  batch_charge` still holds. Never default one, and never let one below the quantity through —
  that silently buys a second tool. Absent means absent keys (a test pins the key set). The
  page offers the input where the selected rate set's stage rate has a `batch_charge`, and
  never divides.
- **Tacking and weld grinding are each charged once: by the route stage's rate or inside the
  welds' time.** `fit_up` and `weld_finishing` are in `TIMEABLE_STAGES` (-> welding), timeable
  only with welding (`TIMED_ONLY_WITH`). `_WELD_COMPONENT_STAGES` in `main.py` maps each to the
  weld component it reads (`welding_tack_minutes_per_weld`, `weld_finishing_minutes`) and its
  `*_charged_twice` error; a timed stage whose welds lack the component, or with no plan, is
  `timed_stage_not_costed`. A typed weld time is the engineer's whole figure. Repositioning
  has no stage and no guard. Edge preparation (`welding_butt_prep_mm_per_min`) is butt-only
  and no stage prices bevelling. Any new weld time component that some route stage also
  prices must join that table, or it is a double charge. `plan.js`'s `TIMED_IN_WELDS` keys
  equal `TIMED_ONLY_WITH` (a test holds it).
- The comparison (`estimates.decompose`) reads a material not charged at a zero rate and
  attributes stage changes as `route_stage`; the quotation adds a `route_stages` line only
  when there are stages.

### What reaches the cost is one test, and a cast blank is not machined all over

- `ManufacturingPlan.counts_toward_cost` decides whether an operation is costed: active, in
  the route, not as formed, with a committed time. Costing, the time summary and the feedback
  record's `costed` all read it. Do not test `operation.costable` alone anywhere that means
  "was costed" — that is how the feedback came to call out-of-route operations costed.
- `as_formed_feature_types` exists only for a *decided* route that casts or forges and then
  machines; it defaults the blank's blends (fillet, chamfer) to as formed. Never extend the
  default to faces or holes from geometry alone — which faces are machined is a drawing
  question. `user_machined` is the engineer's override, `None` meaning "the default".
- `plan.js`'s `isAsFormed` mirrors `is_as_formed`; change one, change both. The assumptions
  and quotation report the finished part's `mass_kg` and the bar separately
  (`material_basis`, `stock_description`, `stock_mass_kg`); never fill `mass_kg` from the
  material line when material came from stock.

### A turned part's material is its bar, once a person says so

- `features/rotational.py` is the only rotational test; the suggestions, the classifier and
  `raw_stock` all call it. Do not reintroduce a copy.
- `raw_stock.propose_bar_stock` proposes; `record_stock_decision` records the engineer's bar
  beside it. An **unconfirmed** bar changes no cost. A confirmed one reaches costing only as a
  volume (`material_stock_volume_mm3`); `mass` must stay the finished part's own, and the bar's
  is `stock_mass`. Never present a bar volume as the part's.
- Turning estimates use the *proposed* bar (`stock_diameter_mm`), so they stay system
  estimates; a different bar is flagged as making them stale, never silently re-estimated.

### A large hole is drilled smallest first, and the choice of sequence is the engineer's

- `drilling_steps` (threshold 13, pilot 6, ratio 2.5 — illustrative `TimeAssumptions`, no
  standard behind them) decides the *suggested* sequence. Holes at or below the threshold
  stay one pass and keep `drilling_time_baseline_v1`; do not change that path.
- Every option offered carries its own system-computed time in `drilling_options`. A choice
  goes to `user_drilling_steps`; `system_drilling_steps` and `system_suggested_cycle_time`
  are never overwritten. Choosing changes no time until the estimate is accepted again, and
  a sequence the system did not offer has no system time.
- `system_cycle_for_choice` (backend) and `systemCycleForChoice` (`plan.js`) are the one
  resolution of which time a chosen sequence uses; keep them identical.

### A manufacturing plan belongs to a component, never to an assembly

`assembly_plan.py` gives each manufactured component definition its own
`ManufacturingPlan`. `AssemblyManufacturingPlan` is a mapping of definition to plan
with **no operations and no setups of its own** — keep it that way.

- **Two components must never share a setup.** Setup time is charged once per setup,
  so a shared one would under-cost the assembly by the setups nobody charged for. A
  shared process type is not evidence of shared fixturing, here or anywhere else in
  this codebase.
- **There is no assembly-level sequence.** Nothing here knows what order components
  are made or joined in. Do not add one.
- **One plan per definition, never per occurrence.** Twelve bolts are planned once;
  the per-part cost is multiplied by the occurrence count in the roll-up and by the
  production quantity once after that. Duplicating the plan multiplies its setup time.
- **Ids are namespaced by `build_plan(id_prefix=...)`, at birth.** Do not relabel a
  plan's ids afterwards: `system_setup_id`, `user_setup_id` and the setup's membership
  list would have to be kept in step by hand.
- **Three different absences, three different answers.** `not_applicable` (bought,
  standard, excluded — the absence is correct), `not_generated` (nobody has planned it
  yet), `blocked` (unresolved, or no geometry). Never collapse the first two: unplanned
  work would then be costed as no work. And `not_generated` carries `plan=None`, never
  an empty plan.
- **`time_summary` is *committed* time and is legitimately 0.00 for a fresh plan.**
  Show `estimated_*_time_minutes` beside it, labelled — a bare 0.00 reads as "this
  component takes no time to make". Never combine setup and cycle into one figure.
- **`_missing_inputs` reads `setup.setup_time_minutes`, not `charged_setup_time`** —
  the latter is 0.0 by design when nothing is committed and would never report a gap.
- **It plans; it does not cost.** Only `manufacturing_plan` and
  `component_classification` may be imported, and no costing module may import it.
  `manufacturing_plan` stays the sole bridge to money.
- **The browser reuses the operations table.** `editComponentPlan` points `state.plan`
  at the component's plan object; do not write a second editing UI. The panel's status
  is re-read through `plan.js`'s `finalCycle`/`setupTime`, because a server-computed
  status goes stale on the first edit — and a fourth copy of the three-branch rule
  would drift from `manufacturing_plan`, `plan.js` and `quotation.py`.

### Previews price through `price_cad_part`, and store nothing

`main.price_cad_part(session, ...)` is the one pricing path for a CAD part: `/cost/cad-part`
stores what it returns; route comparison and bulk preview only show it. Never add a second
path — a preview must give the figure the estimate would.

- **Route comparison** (`route_comparison.py`, imports `money` only; `POST
  /cost/route-comparison`) prices the recommended route plus the candidate families'
  catalog routes as if each were decided. It never sorts, ranks, recommends, stores or
  chooses: `kind: preview`, `stored: false`, `chosen: null`, recommended first then catalog
  order. A route that cannot be priced is listed with the pricing path's reason and null
  figures. The engineer chooses through `/cad/process/decide`. Response figures are `Money`
  (exact in memory, rounded on output) or they will not equal the estimate's.
- **Matrix preview** (`matrix_preview.py`, imports `money` only; `POST /cost/matrix-preview`)
  prices one part across rate-set versions x quantities, one `price_cad_part` call per cell
  with the plan as sent. It **accepts no time** — that is bulk preview's labelled special
  case, and a test forbids `system_time_accepted`, `model_copy` and `build_plan` in its
  section. Caller's order; nothing sorted, marked or converted; a refused cell has null
  figures and the pricing path's reason. Capped by `CAD_COSTING_MAX_MATRIX_CELLS`.
- **Bulk preview** (`bulk_preview.py`, imports nothing from the package; `POST
  /cad/bulk/preview`) prices a CSV of parts with the system's estimated times accepted on an
  **in-memory plan copy only** — `system_time_accepted` is set in exactly one place in that
  section, and a test counts it. Every row, the response and every exported CSV row carry
  "Unreviewed system estimates — not an estimate, not quotable". A failed row carries no
  figure (a validator forbids it); totals are summed only within one currency. Its section
  in `main.py` is checked from its banner to the *next* banner, not to the end of the file.

### DFM flags are advice, with a cited or admittedly uncited threshold

`dfm.py` (imports `features.model` only; imported by `main.py`, `repository.py`, `db.py`)
checks recognized features against a versioned, immutable `DfmProfile` (table
`dfm_profiles`, migration `0007`, seeded `dfm-general-guidelines` 1.0.0, illustrative).
`POST /cad/dfm` is separate from `/cad/operations/suggest` on purpose, so advice can never
reach a plan, a time or a cost.

- **A rule needs a measured input.** Never add one for a quantity no recognizer measures
  (pocket corner radius). Wall thickness (`features/wall_thickness.py`) feeds `dfm_min_wall`;
  draft and undercuts (`features/draw_direction.py`) feed `dfm_min_draft` and `dfm_undercut`. A feature missing the dimension is not checked; a
  rule absent from a profile is reported as not evaluated.
- **A threshold cites a source or says none was found** (`no_published_source_found_illustrative`).
  Publishing one marked published without a title and URL is refused (422). Never present
  a vendor rule of thumb as a standard.
- **No severity, no confidence.** A flag states the rule, the measured value, the threshold,
  the profile version and the source; faceted evidence is labelled weaker.
- **A flag is marked on the model from the DFM response alone.** `flagged_features` are
  recognition's own objects handed back unchanged; `dfm.js` never looks one up in
  `state.features` and never picks a feature by a threshold. One flag colour, a different
  shape, and the row and `#dfm-marked` say it in words — no severity scale. It is a pointer
  drawn without depth testing, not a face highlight; never describe it as one.
- Adding a seeded catalog changes `init_db`'s return count and the head revision: update
  `test_persistence`, `test_migrations`, `test_cost_actuals` and `test_money`'s helper.

### Geometry measured for Iteration 7: walls, approach directions, sheet

Analytic B-Rep only, all three — faceted STEP, STL and IGES surface models are refused with
the reason. Each reads the shape through the single STEP/IGES readers.

- **A wall is measured, never inferred, and the minimum is the thinnest *found*.**
  `features/wall_thickness.py` (rays along inward normals, fixed 5x5 grid per face) imports
  `limits`, `features.model` and `features.topology` only; only `main.py` imports it, and
  `dfm.py` receives a plain `WallEvidence`, never the module. The true minimum can only be
  thinner; `limitations` travels with every result. A DFM flag may carry a `marker` instead of
  `feature_ids` — draw it with the viewer marker, never through `selectFeatures`. Adding a rule
  means a new DFM profile version in `SEED_DFM_PROFILES`; never edit 1.0.0 or 1.1.0.
- **An approach grouping is offered, never applied by default.** `features/approach.py` reads
  directions and casts rays and imports no plan or costing; `setup_proposal.py` groups and
  applies and only `main.py` imports it. Apply is recomputed on the server and goes through
  `with_operation_in_setup` — never write `system_setup_id`. New setups carry
  `system_grouping="approach_direction"` and an *uncommitted* estimate. Never place an
  operation without evidence; confidence is medium at most (a validator enforces it). A ray is
  not a tool: never present "reachable" as proof of access. Flank coverage of a planar face is
  only along directions some feature already needs, so it never creates a setup of its own.
- **K is the engineer's.** `/cad/sheet-metal`'s `k_factor` (0 < K <= 0.5) has no default;
  without it `flat_pattern` is null and `needs_input` is `["k_factor"]`. `features/sheet_metal.py`
  imports only `features.topology`; what sheet cannot make is handed in from `main.py`
  (`sheet_excluding_features`), never copied, and its 10% thinness ratio equals the classifier's
  (a test holds it). A flat pattern is claimed only when the outline's area and perimeter agree
  with flange area + sum(BA x L); anything else is `unsupported` with a reason. A cutting speed
  is never extrapolated past the profile's table.
- **Cut and bend lines come from the measurement, never auto-accepted.** `sheet_operations.py`
  (read only by `main.py`) gives them fixed ids (`OP-SHEET-CUT`, `OP-SHEET-BEND`) so measuring
  again replaces them. `sheet_cutting` and `bending` are timeable stages; `plan.js`'s
  `TIMED_STAGE_PROCESSES` mirrors all five timeable stages and `TIMED_STAGE_WORK` holds display
  words only. The blank is reported, not charged — material is still the finished CAD mass.
- `sheet.js` declares no global functions (an IIFE) and wraps `renderProcessClassification` /
  `resetFeaturesPanel` the way `dfm.js` does. The time-setup editor must round-trip
  `sheet_cutting_table`.

### Iteration 8: pull direction, moulding time, sheet nesting

- **One ray caster: `features/rays.py`.** `approach.py`, `draw_direction.py` and the wall
  measurement's spans use it; `blocked_along` must stay equal to `first_hit` (a test holds it).
  Never add a ray caster to a feature module.
- **Draft and undercuts are measured, never evidence of a process.** `draw_direction.py` imports
  `limits`, `features.model` and `features.rays` only; only `main.py` reads it. The recommended
  axis is a ranking by two counts — never call it the pull. `dfm_min_draft` / `dfm_undercut` run
  only for a stated forming process (`forming_process`, or a decided route starting with
  casting, forging or moulding); `_stated_forming_process` reads those two request fields and
  nothing measured. DFM profile 1.2.0 is immutable; the next version is 1.3.0.
- **The governing wall is the largest inscribed ball found at sampled wall midpoints**, never the
  largest per-face minimum (that reads a rim or plate edge as its length). It can only
  under-report. `measure_governing_wall` uses `measure_wall_thickness(spans=...)`; never a
  second set of rays.
- **Moulding thermal data is a profile row keyed by material**; never default it or borrow
  another material's. **Cavities are the engineer's**: absent is needs-input, never 1.
  `_check_moulding_line` refuses a system-timed line timed for other cavities or another
  material's row, and cavities on a stage-rate route. A moulding cycle is rounded to 0.0001 min.
- **Timed moulding keeps its stage line** (`TIMED_KEEPS_MATERIAL_AND_TOOL`) for the material
  (the rate set's material price, `unit_rate_source: "material_rate"`, the engineer's runner
  yield) and the mould's one-off charge (amortisable — the one timed stage where yield and tool
  life are allowed). Its stage unit rate is never charged. Never drop the line when timing
  moulding (the material would vanish) and never also charge the material line. `process.js`
  reads `/process-routes`' `timed_material_stages`; do not add a JS mirror of it.
- **A sheet part's material is its sheet share, once a person says so.** The nest is a
  bounding-rectangle grid estimate, never presented as a real nest; sheet size, spacing and
  margin are the engineer's, with no defaults; `user_parts_per_sheet` sits beside the nest and
  never overwrites it. A confirmed share is re-measured from the file in
  `_sheet_stock_for_costing` — never trust the browser's blank. `sheet_nesting.py` imports
  nothing from the package; `SheetStock` reuses the bar's fields (`material_stock_volume_mm3`,
  `stock_mass`, `material_basis`, `stock_description`). One part has one confirmed stock.

### A cost-driver table reads one estimate; it never ranks

- `cost_drivers.py` imports `money` only, and only `main.py` imports it; it is handed the
  result snapshot as a plain dict. Never let it import `estimates`, `quotation` or a
  calculator, and never re-derive a figure from a rate and a time.
- The drivers partition the stored total *as shown*; the remainder is `reconciliation`, never
  absorbed, and an absent field is `recorded: false`, never 0.
- Build-up order, never sorted, never "biggest" or "hotspot". `drivers.js` passes the server's
  `share_percent` into a CSS width and prints it; a test greps it for arithmetic.
- A process row's setup is wherever the plan charged it (the first costed operation of the
  setup). Do not distribute it.

### An actual cost is a recorded fact, never an input

`cost_actuals.py` compares a client's stated cost with an estimate, per unit.

- **It imports `money` only, and only `main.py` and `repository.py` import it.**
  The test parses imports with `ast`: the table is also named `cost_actuals`, so a
  substring grep matches the ORM model and the migration.
- **Not supplied is not zero.** Only the total is required; an absent category is
  `not_supplied`. `actuals.js` sends a blank field as `null` — keep it that way.
- **The basis is never guessed.** `cost_basis` has no default; a per-batch cost
  without `batch_quantity` is refused. A different batch size is a warning, never
  an adjustment.
- **An assembly row stores `0.0` where the roll-up had no total.** Read
  `is_complete` / `is_aggregatable` / `production_total_cost` from the snapshot
  (`figures_from_estimate` does), and compare an assembly on its total only.
- **The kind is never guessed, and a quote and an actual never share a field.**
  `figure_kind` (`actual` / `supplier_quote`) has no default in the model or the schema and is
  never read out of the free-text `source`. A quote fills `quote_per_unit` / `gap` /
  `gap_percent`; an actual `actual_per_unit` / `variance` / `variance_percent`. Anything that
  ever aggregates actuals must filter on it. A gap is not a verdict: no judgement, no sort, no
  "best" — `compare_supplier_quotes` lists current quotes in the order entered.
- A new migration means updating the head revision in `test_migrations.py`, `test_dfm.py` and
  `test_cost_actuals.py`.
- **Immutable.** No update or delete in `CostActualRepository`; a correction names
  `supersedes_actual_id`, which is unique so the chain stays one line.

### A quotation presents an estimate; it never calculates one

`quotation.py` renders a stored estimate as a printable document.

- **It imports `estimates` and `money`, and nothing else.** A structural test
  asserts that exact set, and a second unparses the module to assert it contains no
  `calculate_`, no rate resolution, no rate-per-hour multiplication and no
  `RateSet`/`Part`/`ManufacturingOperation`. Keep it true: the moment this layer can
  compute a cost, a document can disagree with the estimate it claims to present and
  nobody can tell which number is real.
- **The stored total is the authority.** `totals.total` is exactly what the estimate
  recorded. The lines are stored figures regrouped so that they *partition* it, and
  any remainder is reported as `reconciliation` rather than absorbed.
- **A per-unit figure times a quantity will not always equal the total** — every
  amount is rounded from its own exact value under the money policy. `quantity_note`
  says so, because a reviewer will multiply the two and find the difference.
- **An assumption is never printed as a measurement.** `ASSUMED_VOLUME_HEADLINE`
  leads the statement for a supplied volume, and the phrase "CAD volume" must never
  appear anywhere near it. A test asserts both.
- **Refusal is a feature.** An incomplete assembly, mixed currencies, no total or
  unresolved geometry produce `available=False` with a blocker. Never fill a gap with
  a zero to make a total appear, and never withhold the *engineering* view — the
  person fixing it needs it.
- **The customer view omits `engineering` entirely**, not merely unrendered. A field
  a template does not print is still a field in a forwarded JSON payload.
- **It reads no clock**: `issued_at` is a parameter so a preview and the document it
  previews are identical. A test greps for `datetime.now`.
- **Do not restate the three-branch time-resolution rule here.** The numbers come
  from the *costed* operations, and the source label is a two-branch test on the
  plan. A third copy would drift from `manufacturing_plan` and `plan.js`.
- **An issued quotation is immutable** and has no update method, like a rate set
  version. Only metadata and the totals-as-issued are persisted; the figures are
  re-read from the estimate every render.
- **The report page is standalone** — no shared globals, no shared stylesheet, no
  forced `@page size` (that would override the reader's print dialog). It computes no
  arithmetic, and a test greps it.

### A component's classification decides where its cost comes from

`component_classification.py` says whether a component is `manufactured`,
`purchased`, `standard`, `excluded` or `unresolved`, and that decides everything
downstream of it.

- **No CAD format states make/buy.** Never infer a classification from a name, a
  part number, a shape, an appearance, a material or a file name. A purchased
  bearing is modelled with the same kind of solid as a machined housing. A
  structural test unparses `suggest_classification` *on its own* — the module's
  prose about bolts and bearings would match a naive grep — and asserts the
  function cannot see any of those things. It reads exactly two: whether the
  definition is a sub-assembly, and whether the file carries measurable geometry.
- **The suggestion is applied and labelled, not applied silently.** It is the
  effective classification until a person answers, the same way an assumed setup
  grouping is used until an engineer regroups it, and every line reports
  `classification_source`. Do not remove the default — the roll-up would go blank;
  do not stop reporting the source — that is what makes it honest.
- **The engineer's answer never overwrites the suggestion.** `user_classification`
  sits beside `system_classification`, and `classification` is a computed field so
  the two cannot disagree. `ClassificationFeedback` records the pair. Nothing is
  trained on it.
- **A purchased component must genuinely leave the manufacturing path** — not be
  costed and then hidden. `/cost/assembly` skips it before `calculate_cad_part_cost`,
  and its `estimate_id`, `material_extended` and `operation_extended` stay null.
- **Only user-owned fields cross the request boundary.** `USER_OWNED_FIELDS` is the
  whole list `build_profiles` will apply; the suggestion, the identity and the
  quantity are rebuilt from the assembly, so a browser round trip cannot rewrite
  them.
- **Missing information is never zero, and the reasons are distinct.** `no_unit_cost`,
  `no_currency`, `unresolved_classification`, `no_geometry_in_file` and
  `currency_mismatch` each send a person somewhere different. The one legitimate
  zero is `excluded`, because a person authorised it: it contributes 0, states a
  reason, and does *not* make the total incomplete.
- **Occurrence quantity x production quantity happens once**, in
  `total_quantity`. `assembly_quantity` defaults to 1 and a test asserts the
  default serializes identically to the pre-Step-26 result.

### The exploded view is a display transform, and must stay one

`exploded_view.py` computes where to *draw* an assembly's components when it is pulled apart.

- **It is not a disassembly sequence.** Never describe it as one, in code, in the UI or in docs.
  Nothing here knows what has to come off before what. `ExplodedViewState.note` carries the
  disclaimer and a test asserts its wording.
- **It imports nothing from this package, and nothing but `main.py` reads it.** Two tests enforce
  both directions — one parses its imports, one walks the source tree. That is the proof an exploded
  position cannot reach a mass, a feature, a time or a cost, and it is worth more than any assertion
  about behaviour. Keep it true.
- **Never persist an exploded position.** A slider position is a viewing preference; a historical
  estimate keeps the placements the file states.
- **The CAD placement is never overwritten.** A component carries its placement *and* an offset, and
  the drawn position is rebuilt from both, so 0% restores the file's arrangement exactly.
- **Groups are rigid.** Everything below a group inherits its offset unchanged, which is what keeps a
  sub-assembly's parts in their relationship to each other. The explosion happens at the shallowest
  *branching* depth — skip chains of single children, or the 198 MB export (single children at
  depths 1 and 2) separates nothing.
- **The split is deliberate**: the grouping and directions are computed in Python where they are
  tested; the browser only does `drawn = original + offset x factor`. Do not move the algorithm into
  JavaScript, and do not make the slider hit the server.

### An assembly mesh is definitions plus placements

`build_assembly_mesh` sends one geometry per definition and one transform per occurrence, not a baked
buffer. That is what makes explosion possible at all, keeps a repeated part from being transmitted
once per placement, and lets decimation shrink each definition in place without invalidating
component identity. A definition is never decimated to zero triangles — a component that vanishes
reads as a missing part.

### Capabilities are declarations, and a limitation must always say why

`cad_import/model.py` reports eight capabilities at four levels, and `CapabilityReport`'s validator
**refuses** anything below `supported` that carries no `detail`. That is the honesty rule of the
layer, enforced rather than trusted: a degraded capability with no stated reason reads to a user
exactly like a working one. When adding a format or weakening a capability, write the reason.

Refinement happens during a cheap inspection (a keyword count and a header read — about a second on
a 198 MB file), never by parsing, so `/cad/import?with_geometry=false` stays fast. `detect_ms` and
`parse_ms` are separate on purpose.

Two refusals, two statuses: **415** when nothing can be read from the format at all (the Creo
adapters), **400** when the format reads fine but cannot do this one thing (an STL asked for
features — the status that path has always returned).

### Invariants worth knowing before editing

- **A volume is never invented.** If faceted polygons do not enclose a solid, `geometry_status`
  becomes `incomplete_requires_input`, volume stays `0`, and `/cad/mass` and `/cost/cad-part`
  answer `422 needs_geometry_input`. The user then supplies `total_volume` or `missing_volume`
  explicitly; it is recorded as an assumption (`volume_method`, `volume_confidence:
  user_supplied`) and never presented as a measurement. No bounding box, no convex hull, no
  guessed thickness.
- **The display mesh is never measured.** A model over the viewer's triangle budget is decimated
  by `mesh_simplify` (deterministic vertex clustering) *after* every measurement has been taken
  from the full-resolution mesh. `cad_mesh._finish` is the one place this happens.
- **A suggestion is never a decision or a cost.** `operation_suggestions` proposes a process and
  its link back to a feature — no setup time, no cycle time, no rate.
- **An estimated time is not a committed one.** `operation_time` produces times from a fixed
  parameter model; `manufacturing_plan` decides whether one counts. Resolution runs in exactly one
  direction: the engineer's typed value → the system's value *if `system_time_accepted`* →
  **nothing**. Generating an estimate does not set that flag, and changing an operation's process
  clears it. An operation with no committed cycle time is *omitted* from the cost, never zeroed or
  averaged. `plan.js` reimplements the same three branches so the table and the backend cannot
  disagree — change one, change both.
- **Missing inputs produce `needs_user_input`, never a default.** Times come back `None` rather
  than `0` so nothing can add a missing time into a total. Non-millimetre units (a faceted export
  reports "as written in the file") are refused rather than scaled — guessing is a 1000× error.
- **Accepting an upload is not supporting it.** A recognized-but-unparseable format is refused with
  an explanation of what the file actually is; native Creo support is architecture only, and nothing
  in the codebase may imply otherwise. Do not add a parser for `.prt`/`.asm`/`.drw` — the formats are
  proprietary and the environment has no reader for them.
- **An assembly roll-up is its own estimate, and carries its own id.**
  `/cost/assembly` persists an estimate separate from the `/cost/cad-part` one for the
  same file, and returns it as `AssemblyCostSummary.estimate_id`. `state.result` is set
  only by `/cost/cad-part`, so anything that reads it alone shows a part estimate for an
  assembly — which is not an approximation of the roll-up but the answer to a different
  question, ₹1,209.13 of steel against ₹2,221.62 of components. **One function decides**:
  `estimateflow.js`'s `authoritativeEstimate()`, read by the Quote screen, the
  `#cost-authority` banner, the context bar, the Overview tiles and the next-step
  recommendation. For an assembly it returns the roll-up or `null` — never the part
  estimate, not even when the roll-up has gone stale (`state.assemblyCostStale`, set by
  `markRollUpStale`). The part estimate stays quotable from History, where picking it is
  deliberate. A per-call id also means a determinism test must exclude it, as it already
  excludes `elapsed_ms`.
- **The UI is served `cache-control: no-cache`, and that is load-bearing.** There is no
  build step, so no asset filename carries a content hash and every release is served at
  the same URL. Without the header a browser applies heuristic freshness and runs the
  previous JavaScript against the current server — this happened, cost a full debugging
  session, and made a shipped fix look like it had never worked. `RevalidatedStaticFiles`
  in `main.py` sets it; the etag still makes an unchanged file a 304. Do not remove it,
  and do not conclude a UI change "did not work" without checking the served bytes.
- **An assembly's component plans are built when it is decomposed, not on request.**
  `assembly.js`'s `decompose` handler calls `generateComponentPlans({automatic: true})`,
  which is the same `/cad/assembly/plans` endpoint the button always used — there is no
  second suggestion or time-estimation pipeline and a structural test asserts the URL
  appears once. It was opt-in until a real estimate came out at ₹902.57 against a
  ₹2,221.62 rehearsal of the same file: *Cost every component* reads the plans in the
  browser's hand, and with none it costs material and tooling only. Generating still
  commits nothing. A reclassification calls `reconcileComponentPlans()`, which drops a
  plan a component is no longer entitled to without a round trip and **merges** any
  rebuild, because a fresh plan has nothing accepted and would silently discard every
  committed time. `editComponentPlan` ends with `setFlow("manufacturing")` (guarded —
  `estimateflow.js` loads later): the operations table is in that section, so retargeting
  it from the Assembly section left it correct and invisible.
- **A manufactured component costed with no committed time makes the roll-up incomplete.**
  `unplanned_definition_count`, `manufacturing_committed` and the `no_committed_operations`
  reason carry it; `is_complete` requires their absence and the note leads "Partial
  assembly estimate — manufacturing plans incomplete"; `quotation.py` refuses it with
  `assembly_manufacturing_incomplete`. **`manufacturing_committed` is tri-state on
  purpose**: `None` means the caller passed no plan facts, which is the pre-plan `roll_up`
  contract and claims nothing, and only supplied facts showing nothing committed make it
  `False`. A *partly* committed component stays complete — the existing `partial_costing`
  warning covers it — because the demo assembly has two operations the estimator cannot
  time, and blocking on those would refuse a finished review.
- **An assembly total is not a quotation.** It is the sum of each component's contribution with no
  assembly labour and no fastening time, and every summary says so. Purchased and standard costs are
  what a person entered — no price is looked up, and a purchased part's cost is never derived from
  its weight. An occurrence count is still not a production quantity: they meet once, in
  `total_quantity`.
- **Decomposition is opt-in and separately budgeted.** Identifying the 198 MB export costs 1.4 s and
  reading its structure about 20, so `/cad/import` never does it; `/cad/assembly` does, under
  `CAD_COSTING_MAX_ASSEMBLY_COMPONENTS`. One request reads the structure once and reuses it for the
  document, the measurements and the mesh.
- **The system's proposal is never overwritten.** Edits go to `user_*` fields beside untouched
  `system_suggested_*` ones, and `operation_feedback` persists the pair on the estimate. Nothing
  is trained on it; a recorded correction never changes a later prediction.
- **Setup time belongs to a setup, never to an operation.** A `ManufacturingSetup` is one
  fixturing; its time is charged **once** however many operations it holds, while cycle time is
  charged per operation. `to_operation_requirements` hangs each setup's whole time on the first
  costable operation in it and zero on the rest — the existing `OperationRequirement` has no setup
  field, so this is where the arithmetic lands; it is not a division or an average, and
  `time_summary()` reports setup and cycle totals separately. Charging setup per operation was a
  real costing error (280 min vs 10 on `model1_1.stp`), so do not reintroduce it.
- **Grouping is assumed, not proven.** The default is everything in one setup, `system_assumed` at
  **low** confidence with its assumptions stated. A shared process type is *not* evidence of shared
  fixturing. `system_setup_id` is never overwritten; a move writes `user_setup_id` beside it.
- **`sequence_position` affects no number** — at either level. No sequence-dependent time, routing
  optimisation, or enforced precedence; a test costs a plan with operations and setups reordered
  and asserts the same total. Setup *membership* does change cost, and is meant to.
- **Time assumptions are not rate sets.** `TimeAssumptions`/`TimeProfile` decide duration,
  `RateSet` decides money; a test asserts the two profile models share only `name`, `version`,
  `description`, `is_illustrative`, and that a time profile has no currency. Both catalogs are
  versioned, immutable (no update/delete anywhere) and seeded at startup, and both flag
  illustrative defaults.
- **A predicted time names the assumptions behind it.** Every estimate records the profile id,
  version and the parameter values that entered *that* calculation; the full profile is snapshotted
  once per estimate in `time_profile_snapshot`. Explicit `assumptions` override a profile and then
  clear the attribution — `build_plan` therefore passes a profile *instead of* its assumptions,
  never alongside them. `time_profiles` imports `operation_time`, never the reverse, which is why
  `estimate_operation_time(profile=…)` is untyped and read with `getattr`.
- **Cost changes are explained, never guessed.** `estimates.py` decomposes a delta with an exact
  two-factor variance split (`rate effect`, `driver effect`, `interaction`), reports the
  interaction as its own `combined` category rather than folding it into a side, and reports any
  remainder as `rounding` or `unexplained` so a gap can never pass as an explanation. Pair a
  per-hour rate with *hours* — pairing it with minutes silently multiplies every time effect by 60.
  Attribution counts *inputs* (`_COMPARABLE_FIELDS` carries a group per field), because one edit
  usually moves several fields. The comparison layer imports no calculator and no repository, and
  `impact.js` derives no delta of its own; tests assert both.
- **Rate-set versions are immutable.** Editing one is impossible by design; publish a new version
  (which copies the last). Every estimate records the exact `rate_set_id` + `version` it used and
  stores request / rate-set / result snapshots, so a historical estimate is self-contained.
- **INR is the default currency and USD must not appear as a default.** Currencies are never
  converted; an estimate is denominated in its rate set's currency.
- **Costing is deterministic — no AI, LLM or randomness anywhere in the calculation path.**
  Confidence values are three-way classifications of which rule branches fired, not probabilities.
- **Placeholder rates must stay unmistakably labelled.** `is_illustrative` drives an unmissable UI
  warning.
- **Never delete or recreate the database.** Startup `run_migrations` distinguishes empty /
  already-Alembic / pre-Alembic (which is *stamped*, not migrated) so adopting Alembic cannot lose
  data. Any schema change needs a migration that works against both a fresh and an existing
  database.

### Resource budgets

`limits.py` holds eleven separately configurable budgets (including the matrix preview's cell cap, `CAD_COSTING_MAX_WALL_RAYS` and `CAD_COSTING_MAX_DRAW_RAYS`) (`CAD_COSTING_MAX_*`) because they answer
different questions, plus one threshold that is not a budget at all —
`CAD_COSTING_MIN_FEEDBACK_SAMPLE`, the observation count below which the analytics prints a sentence
instead of a number. The module docstring lists seven of the nine; if you add one, list it there. The ordering that matters: **a model too complex to draw or
analyse is still costable.** Exceeding a viewer budget degrades only the preview; exceeding a
recognition budget degrades only feature analysis. Neither blocks an estimate. The geometry budget
is far higher than the feature budget on purpose — deriving a volume is one weld and one closure
scan, while reconstructing features fits geometry to every body.

### Frontend

Plain static files served by the same app under `/ui`, with **no build step**. Load order in
`index.html` is significant, because everything except the viewer is a classic script sharing
globals (`$`, `api`, `state`, `escapeHtml`, `number`, `money`, and cross-file functions like
`selectFeature`, `renderPlan`, `planPayload`, `impactMarkup`, `featureCapability`):

```
app.js        upload, the cost request, the result panel, the history table
features.js   the feature list and selection  (selectFeature / selectFeatures)
suggestions.js  suggested operations and their explanations
plan.js       the setup-grouped operations table and `+ Add operation`
process.js    the recommended manufacturing family and route, and the engineer's answer
setup.js      rate-set authoring (money)
timesetup.js  time-profile authoring (duration)
impact.js     the assumptions panel and "Why did this change?"
feedback.js   the feedback and correction analytics panel
actuals.js    actual costs and their variance, in the History detail
routecompare.js  route comparison table in the route card; "Use this route"
              goes through /cad/process/decide
drivers.js    the cost-driver table (Cost section, History detail, roll-up card)
draw.js       the pull-direction card (CAD): axis, draft, undercut markers
sheet.js      the sheet-metal card (Manufacturing): K, bends, flat pattern, cut/bend lines
matrix.js     the Matrix preview disclosure in the Cost section; shows the server's grid
bulk.js       the Bulk preview tab; shows the server's rows and CSV
dfm.js        the advisory DFM panel in the CAD section
assembly.js   the component tree, the bill of materials, the classification
              panel, the component manufacturing plans, their detail views,
              and the roll-up
estimateflow.js  which of the estimate's six sections is showing, the overview
              panel and the quote panel; loaded last so every helper it borrows
              already exists
viewer.js     ES module; publishes only `window.CadViewer`
```

### The estimate is six sections, and every card belongs to exactly one

`#tab-estimate` carries `data-flow`, and `estimateflow.js` toggles it: **Overview, CAD,
Manufacturing, Cost, Assembly, Quote**, in the order the work happens. Before this it was
eighteen cards on one page, ten of them visible with nothing uploaded; analysing a 39-feature
bracket produced 6,621 px of page, about twelve screens, and the operations table was clipped
mid-word.

- **The cards did not change.** They were moved into `.flow-pane` wrappers verbatim, and a
  script asserted that not one element id disappeared, because every handler in every other
  file addresses its elements by id. When adding a card, put it in one pane; do not
  reintroduce a page that shows everything at once.
- **`estimateflow.js` arranges; it must not work anything out.** The overview shows a cost, so
  it is exactly the file that could grow a total of its own. It reads the strings `app.js`
  already rendered (`#r-total`, `#mi-volume`) rather than reformatting the API responses a
  second time, so a summary cannot disagree with the detail it summarises. A test greps it for
  arithmetic and for a format list, and it borrows `plan.js`'s `isCostable` rather than
  repeating the three-branch time rule a fourth time.
- **One viewer, kept in the DOM.** `.stage-viewer` is shown by CSS for Overview, CAD and
  Assembly and hidden for the rest, rather than the viewer being moved or rebuilt — moving it
  would break feature highlighting and the exploded view's state. Cost and Quote get the full
  width, which is what un-clipped the operations table.
- **Evidence goes behind a `<details>`, never away.** The per-feature table with its ids, axes
  and CAD face counts, and the "not suggested" list, are one click from view and complete.
  Hiding them was the point; deleting them is not allowed.
- **A figure is shown only while it describes what is loaded.** Selecting a new file hides the
  previous result panel, and the context bar reads a cost only while that panel is visible.
  `state.result` is deliberately kept, because `app.js` uses it as the baseline for "Why did
  this change?".
- **Applicability comes from the file's own declaration.** The Assembly section is offered when
  `/cad/import` reports `structure_state === "structure_available"` — known at identification,
  about 1.4 s even on the 198 MB export — not when a decomposition happens to exist, or the
  section you must visit to decompose would be the one dimmed.
- **A routing row is one line, and the figures come before the prose.** Stacking the process
  select over the name input with a `<br>`, and the move buttons under the sequence number, cost
  67 px a row and a 126 px `#` column; the same controls in a flex `.op-cell` come to **35 px**.
  Per-operation explanation, rule id and parameters stay in the `evidence-row`, now reachable
  through a labelled `Details` button as well as the row click that was always there and never
  advertised. The charging-rules paragraph, the numbered sequence list and the per-feature
  suggestion breakdown are folded into disclosures — all three are prose or repeats of the table,
  and they were sitting between the reader and `#plan-impact`, which is the backend-computed
  setup summary (setup count, setup time charged once, cycle time, cost split into setup + run).
  **There is no per-operation cost to show**: `/operations/plan-cost-preview` returns
  `total_operation_cost`, `setup_cost` and `run_cost` for the plan, not per line, and the browser
  must not multiply a rate by a time to invent one.
- **A setup row is a band, not a row of the operations' columns.** `setupHeaderRow` emits one
  `colspan="8"` cell holding a flex band, because sizing a heading with the columns of the rows it
  heads is what made its "Use estimate" button wrap: the button sat in the 93 px *Setup* column
  while the setup-time input sat under *Cycle* and the grouping pill under *Source*. The band keeps
  every class (`setup-name`, `setup-time`, `setup-use-est`, `setup-up`, `setup-down`,
  `setup-remove`), which is what `wireSetupRows` finds them by — it never reads a cell position.
  `.setup-timing` is `nowrap` so the pill and its button stay together; the band itself may wrap
  between groups below about 1000 px.
- **`assembly.js` renders a component's plan through this same table** (`state.plan = entry.plan`
  then `renderPlan()`), so anything done to a row is done to component plans too. Verified live: a
  4-definition, 20-occurrence analytic assembly gives a named `plan-scope` banner, 35 px rows and a
  47 px band, identical to a standalone part.

`quotation.html` / `quotation.css` / `quotation.js` are deliberately **outside** that
load order: a standalone report page sharing no globals and no stylesheet with the
shell, because a document that gets printed and sent must not inherit an interactive
layout.

A file may only call into one loaded before it, so `app.js` guards its calls forward
(`typeof renderImpactAgainstPrevious === "function"`). `plan.js` owns the operations table;
`app.js` only sends the plan. three.js is vendored under `static/vendor/` rather than loaded from a
CDN so the app works offline and behind a corporate proxy.

The browser measures nothing, **decides nothing about formats**, and **adds nothing up**. Every figure displayed comes
from a backend response, and tests grep the UI files to assert they contain no costing arithmetic
and no hardcoded list of CAD formats. Which formats appear in the upload panel, which extensions the
file picker accepts, whether the unit control is locked, and whether *Analyse features* is enabled
all come from `/cad/formats` and the imported document's capability report — `features.js` and
`suggestions.js` used to keep their own STEP-only lists, and those lists disabled analysis for a
perfectly analysable IGES file.

### Tests

**Two classic scripts must never declare the same top-level function.** They share one
global scope and the later declaration silently wins: `feedback.js`'s `renderOperations`
replaced the rate-set editor's, so opening the editor threw before its processes table was
drawn — unnoticed from Step 29 until Iteration 3. `test_ui.py` now fails on any duplicate.

`tests/conftest.py` gives every test its own freshly-seeded in-memory SQLite database via an
autouse fixture and `app.dependency_overrides`, so estimates written by one test are invisible to
the next. Sessions are injected per request rather than reached for globally, which is what makes
that swap possible.

The in-memory database is injected, and `CAD_COSTING_DATABASE_URL` is *also* pointed at a
throwaway file, so a code path that reaches for the configured engine instead of the injected
session still cannot touch the real database.

Beyond behaviour, the suite deliberately contains **structural tests** that read source files to
assert boundaries (which module may import which) and **determinism tests** that run the same input
twice and compare, excluding only `elapsed_ms`. When adding a feature, expect to keep both kinds
honest.

**A structural grep must strip prose first — in every language, not just Python.**
This has now bitten seven times: a CSS test grepping for `size: A4` matched the comment
explaining why it is absent; a JS test counting a phrase matched the comment about that
phrase; a JS test forbidding `median` matched a comment saying no median is computed; a
Python test forbidding `train` matched the word *constraint*; a Python test forbidding `calibrat`
matched a note *refusing* to claim calibration; and a JS field-name check matched the comment
explaining the name collision it was checking for. Strip comments, then grep. And
prefer a precise token (`_cost`, `def save`) over a substring — the package is called
`cad_costing_agent`, so any test for "cost" matches its own name.

A structural test that greps source must strip docstrings and comments first —
`code_without_prose()` in `tests/test_manufacturing_plan.py` does it with `ast`. The prose in this
codebase explains at length *what it deliberately does not do*, so a naive grep for a forbidden
name matches the paragraph promising the name is absent. Where even stripped module source would
match — `component_classification.py` names bolts and bearings in a user-facing string constant —
unparse the one function under test rather than the module.

**A quotable assembly in a test is a *reviewed* one.** Since manufacturing completeness joined the
roll-up, an assembly whose manufactured components have committed no time is refused as a customer
document however exact its arithmetic — so a test about the *document* has to accept the estimates
first. `tests/assembly_fixtures.committed_plans(client, rate_set_id=...)` builds every manufactured
component's plan through the real endpoint and accepts every system estimate (`commit_every_time`
does the accepting, and is the single implementation of what the operations table's "use all
estimates" does). Pass the same classification decisions to it that the cost call will use, or the
plans endpoint refuses the contradiction — planning a component another call excludes. And use
`poc-default`, per the rate-set note above.

And **write a structural pattern out literally rather than interpolating it**. Three assertions in
this suite carried a literal backspace byte where a `\b` escape was meant, so their patterns could
never match and they had been passing on anything; they were only found when one of them was
extended. A structural test that cannot fail is worse than no test, because it is counted.

## Documentation discipline

`README.md`, `ARCHITECTURE.md` and `PROJECT_STATUS.md` are maintained to describe **only what has
actually been built** — as are `DEMO.md`, whose every number was measured against the running
application, and `CHECKPOINT.md`, a point-in-time handoff that is *not* updated in place — — `PROJECT_STATUS.md` records each numbered step with its real test counts
and live-verification numbers, and keeps an explicit "What has NOT been done" list. Do not add
aspirational claims, and do not describe this proof-of-concept as production-grade. When a step
changes behaviour, correct any statement the change made stale rather than only appending.

Unit tests alone are not treated as evidence here: changes are expected to be verified against the
actually running application, with the observed numbers reported.
