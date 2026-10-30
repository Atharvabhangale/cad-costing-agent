# Architecture

This document describes the intended technical direction for the CAD Costing Agent. Only the foundation described under "Current State" has actually been built; everything else is a forward-looking plan and will change as the project develops.

## Goal

Estimate the zero-base manufacturing cost of mechanical parts directly from CAD models, eventually integrating with Creo and Windchill.

## Technology stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Standard for both scripting/backend work and the CAD/scientific-computing ecosystem (numpy, trimesh, pythonocc, etc.) that later geometry-processing steps will need. |
| Web framework | FastAPI | Minimal to start with (a single route today), but gives typed request/response models, automatic OpenAPI docs, and async support for the REST API layer planned later. |
| App server | Uvicorn | Standard ASGI server for FastAPI. |
| Packaging/dependencies | `pyproject.toml` (setuptools + pip) | Single standard, tool-agnostic file for metadata and dependencies; no extra tooling to install. |
| Testing | pytest + FastAPI's `TestClient` (httpx) | Simple, widely used, integrates directly with FastAPI. |
| Database | SQLite via SQLAlchemy 2.x | Zero-config to start locally; SQLAlchemy allows moving to Postgres later without rewriting data-access code. Still POC-grade: no auth, no backup story. |
| Migrations | Alembic | Lets the schema evolve without deleting stored history, and adopts a pre-Alembic database by stamping rather than rebuilding. |
| CAD geometry processing — mesh | `trimesh` (STL) | Pure-Python, computes volume/surface area analytically from a mesh (deterministic), needs no external CAD kernel/license — a good fit for a neutral triangulated format like STL. |
| CAD geometry processing — B-Rep | `cadquery-ocp` (STEP) | OpenCASCADE bindings. See the selection rationale below. |

### STEP library selection

Options evaluated against this project's constraints (Python 3.12, Windows, pip/venv — no conda, commercial-friendly licensing, and a need for solid volume, surface area, and bounding box):

| Option | Outcome |
|---|---|
| `pythonocc-core` | **Rejected — not distributed on PyPI** (conda-forge only), so it cannot be installed into this project's pip/venv workflow. |
| `gmsh` | **Rejected on licensing.** pip-installable and lighter, and it does embed OpenCASCADE, but it is GPL-licensed — an unacceptable risk for a POC intended to grow into commercial Creo/Windchill integration. |
| `cadquery`, `build123d` | **Rejected as redundant.** Both are modeling DSLs layered *on top of* OCP, so they carry all of OCP's weight plus an API we do not need — this project only reads a file and queries mass properties. |
| **`cadquery-ocp` (OCP)** | **Selected.** Ships a native `cp312`/`win_amd64` wheel; exposes OCCT's mature STEP reader (AP203/AP214/AP242); `BRepGProp` returns analytically integrated volume/surface area over the exact B-Rep rather than a meshed approximation; and OCCT's licence (LGPL-2.1-style with a linking exception, bindings Apache-2.0) is far safer commercially than GPL. |

Two costs of this choice, accepted deliberately:

- OCP transitively installs `vtk` and `matplotlib` (a few hundred MB) even though this project renders nothing, because `OCP/__init__.py` unconditionally registers VTK's DLL directory at import time. There is no supported way to install OCP without them.
- Importing OCP takes ~2.4 s and loads large native libraries, so `cad_geometry` imports it **lazily inside the STEP code path**. STL-only usage never pays that cost, and a missing OCP install surfaces as a clear error on STEP uploads instead of breaking startup.

## Current state

A FastAPI app (`src/cad_costing_agent/main.py`) with:

- `GET /` and `/ui/` — the browser UI (static files served by this same app).
- `GET /health` — liveness check.
- `GET /rates` and `GET /rates/{rate_set_id}` — discover and inspect the stored rate sets.
- `POST /rates` and `POST /rates/{rate_set_id}/versions` — author a new rate set version.
- `POST /rates/validate` — check whether a candidate rate set would publish, without storing it.
- `GET /estimates` — browse estimate history with pagination and filtering.
- `GET /estimates/compare` — compare two persisted estimates: totals, the assumptions each rested on, every input that differs, an exact decomposition of the money into the factors that moved it, and an operation-level diff. Reads only what each estimate stored.
- `GET /estimates/{estimate_id}/assumptions` — everything one estimate rested on (CAD, material, both profile versions, the plan, additional costs), read back from its own snapshots.
- `GET /cad/formats` — every CAD format this application recognizes and what each can do, with a stated reason for anything less than full support. No file involved; the upload UI renders this.
- `POST /cad/import` — identify one uploaded CAD file and report honestly what can be done with it: format, representation, units and where they came from, component structure, header metadata, capabilities and warnings. `?with_geometry=false` skips the parse entirely.
- `POST /cad/assembly` — decompose an assembly into definitions, occurrences, hierarchy, quantities and placements, with each component measured in its own right, plus a display-only exploded arrangement and a suggested costing classification per component. `?measure=false` for the structure alone; `?with_mesh=true` for a viewer mesh of one geometry per definition and one placement per occurrence.
- `POST /cad/assembly/plans` — a manufacturing plan for every **manufactured** component: its own feature recognition, the same suggestion rules, the same time estimator and the same setup model a standalone part goes through. No assembly-level plan and no assembly-level sequence. A purchased, standard or excluded component comes back `not_applicable` with the reason; an unresolved or geometry-less one comes back `blocked` with what is missing. Neither is reported as a plan with zero operations.
- `POST /cad/assembly/features` — recognize features on one component, with ids prefixed by that component so they stay unique across the assembly.
- `GET /feedback/summary` — where engineers disagree with the system, in aggregate. Every rate carries its numerator, denominator, what it excluded and a sample guard; a figure resting on too few observations says so instead of showing a percentage. Read-only, and nothing it reports changes a rule, a profile or an estimate.
- `GET /feedback/operations` — the system → engineer confusion matrix and each suggestion rule's hit rate. Where a systematically wrong rule becomes visible; measuring one and editing one are different steps.
- `GET /feedback/times` — calibration per process and per time-profile version, with outliers listed apart and a directional bias claimed only above the sample threshold.
- `GET /feedback/setups` — how often the system's fixturing assumption was changed, reported as a correction frequency and never as an error rate.
- `GET /feedback/classifications` — which make/buy suggestions engineers changed and to what. It reports what was chosen, nothing about what a component truly is.
- `GET /feedback/confidence` — empirical acceptance by confidence band and by evidence type. B-Rep and faceted are never combined silently, and the response refuses to claim calibration.
- `GET /feedback/evidence` — how completely each evidence field is captured: known, not applicable where the value cannot exist for that record, unrecorded where something persisted could still recover it, and unrecoverable where it never can be. An evidence-quality report, not a training system.
- `GET /feedback/records` — the individual records behind a statistic, filtered and paginated, so a percentage is checkable.
- `GET /estimates/{estimate_id}/quotation` — preview a quotation rendered entirely from that estimate's stored snapshots. `?view=customer` (default) or `engineering`. An estimate that must not become a customer-facing document comes back with `available: false` and a blocker naming the reason.
- `POST /estimates/{estimate_id}/quotation` — issue one, recording the metadata a person typed and the totals as issued. Refused with 409 when the quotation is unavailable.
- `GET /quotations/{quote_number}` — an issued quotation, re-rendered from the estimate it presents; this is where historical accuracy is demonstrable rather than promised.
- `POST /cost/assembly` — roll each component's contribution up by classification: a manufactured component through the unchanged pipeline, a purchased or standard one at the unit cost the caller stated, an excluded one at nothing on purpose. `components` carries the per-component decisions and `assembly_quantity` the production run; every component that could not be costed is listed with the reason and none is counted as zero.
- `GET /estimates/{estimate_id}` — retrieve a persisted estimate with its audit snapshots.
- `POST /cost/material` — deterministic material cost calculation for a single part.
- `POST /cost/operation` — deterministic cost calculation for a single manufacturing operation.
- `POST /cost/part` — deterministic part-level cost estimate combining material cost with a list of operation costs.
- `POST /cad/geometry` — extracts geometry properties (volume, surface area, bounding box) from an uploaded STL or STEP file. Not connected to costing.
- `POST /cad/mesh` — geometry properties plus a browser-renderable triangle mesh for the 3D viewer. Visualization only; not connected to costing.
- `POST /cad/features` — deterministic feature recognition on a STEP B-Rep. Analysis only; not connected to costing, and it infers no manufacturing operations.
- `POST /cad/operations/suggest` — deterministic feature-to-operation *suggestions*. Proposes; never decides and never prices. Carries no time and no rate, reports `costable: false`, and persists nothing.
- `GET /time-profiles`, `GET /time-profiles/{id}`, `POST /time-profiles`, `POST /time-profiles/{id}/versions`, `POST /time-profiles/validate` — the versioned, immutable catalog of machining-time assumptions. The exact counterpart of the rate-set endpoints, and separate from them: a rate set decides what an hour costs, a time profile decides how long the work takes.
- `POST /operations/estimate-time` — deterministic setup/cycle time estimation from *normalized* operations, under a named time-profile version.
- `POST /operations/plan-cost-preview` — prices a manufacturing plan's operations through the existing unmodified calculators, splitting setup cost from run cost, without creating an estimate. What lets the UI show the cost of a regrouping before anyone commits to it. Takes no CAD file, so it never re-parses geometry. Returns the formula, evidence and every parameter used, or `needs_user_input` with what is missing. Costs nothing and persists nothing.
- `POST /cad/mass` — converts extracted CAD volume into a mass using a material's density (mass only, no cost).
- `POST /cost/cad-part` — the CAD-driven counterpart to `/cost/part`: derives mass from an STL upload and a material, then reuses the existing material/operation/part-level costing calculations unchanged.

Domain logic is kept separate from the API layer:

- `part.py` — `Part` model (pydantic), with validation (positive mass, positive quantity, etc.).
- `materials.py` — `Material` model and a small illustrative material dataset (`Steel`, `Aluminium`), kept separate from the calculation logic. **All cost figures in this file are illustrative POC values, not real pricing.**
- `costing.py` — pure, deterministic `calculate_material_cost(part)` function: `material_cost = mass_kg * cost_per_kg`, `total_material_cost = material_cost * quantity`. No AI/LLM involved.
- `operation.py` — `ManufacturingOperation` model (pydantic): `operation_id`, `operation_name`, `process_type`, `machine_rate_per_hour`, `labor_rate_per_hour`, `setup_time_minutes`, `cycle_time_minutes`, `quantity`, `is_illustrative_rate`. Rates are supplied per-request rather than looked up from a dataset, since they vary by operation/shop rather than falling into a small fixed catalog like materials. Deliberately left extensible (as plain additional fields) for later additions such as tooling, inspection, or scrap cost.
- `operation_costing.py` — pure, deterministic `calculate_operation_cost(operation)` function implementing the machine/setup/labor cost formulas below. No AI/LLM involved.
- `rate_sets.py` — `RateSet` (identified by `rate_set_id` + `version`), `MaterialRate`, `OperationRate`, `OperationRequirement`, `RateSetReference`, the illustrative `SEED_RATE_SETS` seed data, and the pure resolvers `resolve_material`/`resolve_operation`. This layer **configures inputs and calculates nothing**, and has no database dependency: the resolvers build the existing `Material` and `ManufacturingOperation` models, which the existing unmodified calculators then consume. Lookup lives in `repository.py`. No costing formula is duplicated here.
- `additional_costs.py` — `AdditionalCostInputs` (overhead rate %, scrap rate %, tooling cost per batch — all caller-supplied, all defaulting to zero), `AdditionalCostsResult`, and pure, deterministic `calculate_additional_costs(direct_cost_base, inputs)`. Kept in its own module so the overhead/scrap/tooling policy can change without touching material or operation costing. No AI/LLM involved.
- `part_cost.py` — `PartCostEstimateRequest` (a `Part`, operations, operation requirements, `AdditionalCostInputs`, and a rate set selection), `PartCostEstimateResult`, and pure, deterministic `calculate_part_cost(request, rate_set=None)`: reuses `calculate_material_cost`, `calculate_operation_cost`, and `calculate_additional_costs` internally, sums their (already quantity-scaled) totals, and keeps every component separately visible in the result — see "Quantity semantics" below. The rate set is injected by the caller, so this module has no database dependency. No AI/LLM involved.

This is still a small slice: a part-level estimate is material cost, plus a flat list of independently-costed operations, plus percentage-based overhead and scrap and a flat tooling charge. There is no BOM/assembly handling, no manufacturing route (ordered/optional/alternative sequences of operations), and no engineering or inspection cost. Rate sets and completed estimates are persisted in SQLite.

CAD geometry extraction is a separate, self-contained module with no dependency on costing:

- `cad_geometry.py` — `GeometryProperties` (volume, surface area, bounding box, source format, detected unit, a units note) and `BoundingBoxDimensions`, plus pure functions `extract_geometry_from_bytes(data, file_format)` and `extract_geometry_from_file(path)`. These dispatch on the declared file extension to a per-format reader — `trimesh` for STL, OCCT for STEP — and return the same `GeometryProperties` shape either way, so everything downstream is format-agnostic. Deterministic for a given input. Still has no knowledge of material density, material cost, machine rates, labor rates, or any costing formula.
- `cad_mass.py` — the one module where geometry and material data meet. `LengthUnit` (`mm`/`cm`/`m`/`inch`), `GeometryMassResult`, and `calculate_mass_from_geometry(geometry, material_name, source_unit)`. Converts a `GeometryProperties.volume` into m³ using a **cubic** conversion factor (`meters_per_unit ** 3` — never a linear factor applied directly to a volume), looks up the material via the existing `materials.get_material`, and computes `mass_kg = volume_m3 * material.density_kg_m3`. Returns the mass for a *single* part; does not calculate cost.
- `cad_part_cost.py` — `CadPartCostEstimateResult` (`geometry`, `mass`, `part_cost`) and `calculate_cad_part_cost(...)`, which composes `calculate_mass_from_geometry` with the existing, unmodified `calculate_part_cost`: it builds a `Part` from the CAD-derived mass and calls `calculate_part_cost` exactly as `/cost/part` does — no costing formula is duplicated.

- `time_profiles.py` — `TimeProfile`, `TimeProfileReference`, `TimeProfileSummary`, `TimeProfileValidation`, `advisory_notes(...)` and the illustrative `SEED_TIME_PROFILES`. Identity, versioning and immutability for the machining assumptions; the exact counterpart of `rate_sets.py`. It **configures inputs and calculates nothing**, has no database dependency, carries no currency, and imports no costing arithmetic. The parameters themselves stay in `operation_time.TimeAssumptions`, next to the formulas that read them, so a parameter and its arithmetic cannot drift apart.
- `operation_time.py` — `TimeAssumptions`, `OperationTimeEstimate`, `TimeStatus`, and the pure functions `estimate_operation_time(...)` / `estimate_operations(...)`. Deterministic, parameter-based machining-time formulas. Produces **times only**: it imports no costing arithmetic and reads no rate. Every estimate names its formula, its evidence and every parameter that affected it, and returns `needs_user_input` rather than a default when the inputs are not there. No AI/ML involved.
- `manufacturing_plan.py` — `PlannedOperation`, `ManufacturingPlan`, `TimeSource`, `build_plan(...)`, `manual_operation(...)`, `estimate_missing_times(...)`. The editable plan: what the system proposed, what the engineer chose, and the sequence. Holds the invariant that the system's proposal is never overwritten, and decides which operations carry a **committed** time and may therefore be costed. Produces `OperationRequirement` objects for the existing unmodified costing; calculates no money itself.
- `operation_feedback.py` — `OperationFeedback` and `feedback_from_plan(...)`. A pure projection of a plan into one prediction/decision pair per operation, persisted with the estimate. Records corrections; trains nothing and adapts nothing.
- `operation_suggestions.py` — `OperationSuggestion`, `DeclinedSuggestion`, `OperationSuggestionResult`, `UserAction`, `SuggestionOrigin`, and the pure functions `suggest_operations(recognition, ...)`, `record_user_decision(...)` and `to_operation_requirement(...)`. Maps recognized features onto **suggested** processes by a deterministic rule table. It calculates nothing: no time, no rate, no cost. It imports no costing arithmetic and no costing module imports it; `to_operation_requirement` is the only bridge into costing and raises rather than defaulting a missing time. No AI/LLM involved.

CAD geometry extraction is exposed via `POST /cad/geometry` purely to prove it works standalone. `cad_mass.py` and `cad_part_cost.py` complete the intended pipeline (geometry → density → mass → material cost → part cost) via `POST /cad/mass` and `POST /cost/cad-part`, while `POST /cost/part` remains the unmodified manual-mass path. Feature recognition (Step 18), operation *suggestions* (Step 19) and *time estimates* (Step 20) branch off geometry without touching costing. Times are now estimated from a deterministic parameter model, but **an estimate is not a commitment**: an operation reaches a cost only when a human accepts its estimate or types their own, and one with neither is omitted rather than defaulted.

### Operation cost formulas

```
machine_cost = machine_rate_per_hour * (cycle_time_minutes * quantity / 60)
setup_cost   = machine_rate_per_hour * (setup_time_minutes / 60)
labor_cost   = labor_rate_per_hour * (cycle_time_minutes * quantity / 60)
total_operation_cost = machine_cost + setup_cost + labor_cost
```

Setup cost is a one-time cost per batch (not multiplied by quantity); machine and labor run cost scale with quantity via cycle time.

### Persistence

SQLite via SQLAlchemy 2.x, in three files that nothing in the costing modules imports:

- `db.py` — configurable URL (`CAD_COSTING_DATABASE_URL`, default `sqlite:///./cad_costing_agent.db`), a lazily-cached engine, and `get_session`, the FastAPI dependency that yields one session per request. `init_db` migrates to head and seeds idempotently, and runs from the app's lifespan.
- `db_models.py` — SQLAlchemy ORM rows (`RateSetRow`, `EstimateRow`), kept strictly separate from the Pydantic domain models.
- `repository.py` — `RateSetRepository` and `EstimateRepository`, the only place the two representations are translated.

**Keeping the calculators pure.** Persisting rate sets created a real risk of dragging a database dependency into the costing modules, since `calculate_part_cost` used to look up its own rate set. The dependency is inverted instead: the endpoint owns the session, resolves the `RateSet` through the repository, and *injects* it into the pure calculator. `calculate_part_cost(request, rate_set=...)` mirrors `calculate_material_cost(part, material=...)` from Step 9, and it refuses to guess — a request naming a rate set with none supplied is an error, not a silent lookup.

**Schema.**

```
rate_sets   unique (rate_set_id, version)
            name, description, is_illustrative, currency, rates (JSON), created_at

estimates   id (autoincrement PK, orders history)
            estimate_id (uuid4 hex, unique + indexed)
            created_at, source, part_id, part_name, material, quantity,
            rate_set_id, rate_set_version, currency,
            total_material_cost, total_operation_cost,
            total_additional_cost, total_part_cost,
            request_snapshot (JSON), rate_set_snapshot (JSON), result_snapshot (JSON)
```

Rate detail lives in one JSON column rather than child tables. A rate set version is immutable and is always read and written whole, so splitting materials and operations across tables would add joins and partial-update risk while buying nothing; identity stays in real indexed columns because that is what gets queried.

**Immutability** is enforced structurally: the unique constraint rejects a duplicate `(rate_set_id, version)`, and neither repository exposes an update method at all. Changing rates means publishing a new version.

**Audit snapshots** are what make a stored estimate self-contained — what was asked for, which rates were resolved, and the full breakdown. A historical estimate therefore stays byte-identical when a newer rate set version is published later, which is covered by a test.

**Test isolation**: an autouse fixture gives every test its own in-memory SQLite database (StaticPool so all connections share it), seeds it, and overrides `get_session`. Estimates written by one test cannot leak into another.

### Migrations

Alembic owns the schema; `Base.metadata.create_all` is no longer used anywhere in application code. `init_db` calls `run_migrations`, which branches on the database's state:

- **Empty** → `upgrade head`, creating the tables.
- **Under Alembic** (`alembic_version` present) → `upgrade head`, applying outstanding revisions.
- **Pre-Alembic** (tables exist, no version table) → **`stamp head`**, recording the revision without running DDL.

That last branch is the one that matters. A Step-10/11 database already has exactly the schema revision `0001` describes; running the migration would fail on the existing tables, and dropping-and-recreating would destroy stored rate sets and estimates. Stamping adopts it in place, and a test asserts the schema fingerprint is byte-identical before and after.

Two deliberate departures from the stock Alembic template, both in `migrations/env.py`:

- The URL comes from `db.database_url()`, not from `alembic.ini` (which intentionally omits `sqlalchemy.url`). There is one place to configure the database, and the CLI cannot drift from the app.
- If the caller supplies a live connection via `config.attributes["connection"]`, migrations run on it. This is essential for the in-memory SQLite databases used in tests — a second connection to `sqlite://` is a *different, empty* database, so migrations would otherwise run somewhere the tests never see.

Migration scripts live inside the package (`src/cad_costing_agent/migrations/`) and are declared as package data, so they are found regardless of the working directory and survive a real install. Tests run the genuine migration path rather than a `create_all` shortcut, which costs the suite a few seconds and is worth it. A parameterised test scans every revision's `upgrade()` for `drop_table`/`drop_column`/`delete`, so a future migration that would silently destroy stored estimates fails CI.

### Estimate history and comparison

`estimates.py` is a pure module — no SQLAlchemy import — holding `EstimateSummary`, `EstimateListPage`, `CostDelta`, `EstimateComparison`, and the `compare_estimates` function. `repository.py` builds summaries out of rows; the comparison arithmetic stays independently testable without a database.

**Ordering.** History is ordered by the `estimates.id` autoincrement key, not by `created_at`. This is deliberate: the system clock's granularity is coarse enough (~15 ms on Windows) that estimates created in quick succession genuinely share a timestamp, which would make "newest first" ambiguous and, worse, make pagination unstable. A monotonic surrogate key gives a total order that matches real insertion order.

**Filtering** covers `part_id`, `rate_set_id`, and an inclusive `created_after`/`created_before` range. Because `created_at` is stored as naive UTC, an aware datetime arriving from a query string is converted to naive UTC before comparison — otherwise the bound would be compared against a different notion of time.

**Comparison** reads only the totals persisted with each estimate and never recalculates. Deltas are `comparison - baseline`; which side is newer is decided by timestamp independently of argument order; and `percent_change` is `null` against a zero baseline rather than infinite. This is what makes history trustworthy: publishing a newer rate set version, or changing the catalog, cannot retroactively alter a past estimate or a past comparison.

### Rate sets: identity, versioning, and precedence

A rate set is identified by (`rate_set_id`, `version`) and holds material prices/densities, machine and labor rates keyed by process type, and the overhead/scrap/tooling inputs. Resolving an id without a version selects the highest version; the resolved version is always stamped onto the estimate as a `RateSetReference`, so a result is never ambiguous about what produced it. Pinning `rate_set_version` gives full reproducibility — the trade-off of the "latest" default is that a catalog revision could change an unpinned result, which is why the version is echoed back rather than left implicit.

Precedence is deliberately keyed off **structural presence, never off a value**, so a field that happens to default to `0.0` can never silently override a rate set:

| Input | With a rate set | Without a rate set |
|---|---|---|
| Material rate | From the rate set; an unpriced material is an error | Built-in material catalog (original behaviour) |
| `operations[]` | Carry their own rates, never overridden | Unchanged |
| `operation_requirements[]` | Rates resolved by `process_type` | Rejected — they require a rate set |
| `additional_costs` | Present → replaces the rate set's wholesale; absent → the rate set's | Present → used; absent → zeros |

`OperationRequirement` exists because times and quantity describe the *part's process plan* while rates describe the *shop*. Splitting them let `ManufacturingOperation` keep its rates strictly required, so `/cost/operation` and every existing test were untouched.

The only change needed inside the calculators was an optional `material` parameter on `calculate_material_cost`, letting a caller inject an already-resolved `Material` instead of consulting the module-level catalog. Omitting it preserves the original behaviour exactly, and supplying it removes hidden global state from the rate-set path.

Rate sets now live in SQLite. `SEED_RATE_SETS` in `rate_sets.py` is **seed data**, not a live catalog — it is written into the database once, idempotently, after which the database is authoritative and editing that tuple does not retroactively change stored versions.

### The browser UI

`src/cad_costing_agent/static/` holds a single-page app (`index.html`, `styles.css`, twelve
classic scripts, `viewer.js` as a module, and `vendor/`) mounted at `/ui`, with `/` redirecting
to it. It is deliberately plain HTML/CSS/JS: no build step, no bundler, no second process to
run, and nothing to keep in sync with the Python dependencies. It is mounted under `/ui` rather
than `/` so it cannot shadow an API route.

Top-level navigation separates the work from the catalogs that support it: **Estimate** and
**History**, then **Rate sets**, **Time profiles** and **Engineering** (feedback, correction and
evidence analytics).

**The estimate itself is six sections rather than one page.** `#tab-estimate` carries a
`data-flow` attribute that `estimateflow.js` toggles between Overview, CAD, Manufacturing, Cost,
Assembly and Quote — the order the work actually happens in. Every card belongs to exactly one
section. This replaced a single scrolling page of eighteen cards, ten of which were visible
before anything had been uploaded, and which reached about twelve screens once a 39-feature part
had been analysed: the capabilities were right and the presentation was not. One viewer stays in
the DOM and CSS shows it for the three sections that are about the model, so feature
highlighting and the exploded view keep their state while Cost and Quote get the full width.

**One function decides which estimate is current.** A file can have produced two stored
estimates that answer different questions: `/cost/cad-part` costs the whole upload as a single
solid, and `/cost/assembly` costs an assembly component by component from each component's own
plan. `estimateflow.js`'s `authoritativeEstimate()` is the only place that chooses between them,
and the Cost banner, the Quote screen, the context bar, the Overview tiles and the next-step
recommendation all read it. For an assembly it returns the roll-up or nothing — the part estimate
is never substituted, including when the roll-up has been discarded because a classification
changed, because a wrong answer offered without comment is worse than no answer. That estimate is
still saved and still quotable from the History table, where choosing it is deliberate.

**An assembly's manufacturing plans are part of decomposing it.** Every manufactured
component's plan is built as soon as the structure is read, through the same
`/cad/assembly/plans` endpoint, the same feature recognition, the same suggestion rules and
the same time estimator a standalone part uses. It was previously a separate button whose
connection to *Cost every component* was undeclared, and an assembly costed without it came
out at material plus tooling with nothing for the making — a plausible figure at less than
half the real one. Generating remains distinct from accepting: the times are the system's
estimates with nothing committed, and a manufactured component that has committed none of
them now makes the roll-up `is_complete: false`, names itself in the completeness note, and
blocks the customer quotation while leaving the engineering view intact.

Because there is no build step, the `/ui` mount is served with `cache-control: no-cache`
(`RevalidatedStaticFiles`). No asset filename carries a content hash, so without the header a
browser is free to keep running a previous version of the JavaScript against the current server —
which is not hypothetical: it happened, and every symptom of it looked like an application bug.
The etag is still sent, so an unchanged file costs one conditional request and an empty 304.

Engineering evidence is folded behind `<details>` disclosures rather than removed — the
per-feature table with its identifiers, axes and CAD face counts, the features that produced no
suggestion, and the CAD-format capability matrix. An engineer reaches all of it in one click; a
demo audience is not made to read it first.

**The frontend contains no costing arithmetic.** It collects inputs, calls the existing endpoints,
and formats what comes back; a test greps `app.js` for formula fragments to keep it that way.
`estimateflow.js` is held to the same rule and is the file most likely to break it, because its
overview panel shows a cost: it mirrors the strings `app.js` has already rendered rather than
reformatting the API response a second time, so a summary cannot disagree with the detail it
summarises, and it borrows `plan.js`'s `isCostable` rather than repeating the three-branch
time-resolution rule. The
layering is unchanged: browser → existing FastAPI API → deterministic costing/CAD modules →
SQLite.

Three API semantics the page has to respect exactly, each of which would be a silent costing bug
if it got them wrong:

- **Blank additional costs are omitted, never sent as `0`.** The API treats a supplied value as an
  override, so sending zero would replace the rate set's overhead/scrap/tooling with nothing.
- **`rate_set_version` is only sent when a version is explicitly pinned**, so "Latest" resolves
  server-side rather than being frozen by the browser.
- **Operation quantity is derived from the part quantity**, which the API requires to match; it is
  not a field the user can desynchronise.

The page also adapts to two backend realities rather than asking the backend to change: STEP
uploads lock the unit selector to `mm` (the reader normalises STEP to millimetres and rejects
anything else), and the stored `result_snapshot` is read from either shape, since `/cost/part`
stores the part cost directly while `/cost/cad-part` nests it under `part_cost`.

### Geometry and mass from polygons

```
                        ┌ brep     → OCCT mass properties ─┐
STEP bytes → classify ──┤                                  ├→ volume → mass → costing
                        └ faceted  → weld → closure → mesh ┘   (formulas unchanged)
```

`cad_faceted.py` sits beside `cad_geometry.py` rather than inside the feature package, because a
volume is a geometry concern: **feature recognition and mass extraction are independent**, and a
faceted model may give limited features and a completely valid mass.

**The `OPEN_SHELL` trap.** The real exports declare all 6,473 shells `OPEN_SHELL`, which is what
makes OpenCASCADE reject them — but it says nothing about the polygons. Welded, those 6,473 shells
are face *groups* forming only 100 connected components, 63 of them watertight. So shell grouping
is ignored when measuring: connectivity decides what a body is, the entity names do not.

**One repair, and only one.** A surface missing a single facet leaves a boundary of exactly three
edges over three existing vertices; exactly one triangle fills it, oriented by the three unmatched
directed edges. It uses no tolerance at all. Three guards must hold: the chain is exactly three
mutually-joined vertices, each edge is used exactly once (so no non-manifold edge is created), and
the three edges come from three *different* triangles — a lone triangle also has three free edges,
and filling it would fold a copy onto itself into a zero-volume shell that would then read as
watertight. That false positive was found in testing, which is why the third guard exists.

Everything larger is refused: a four-edge hole offers a choice of diagonal, and a hundred-edge hole
would mean inventing a face. T-junction subdivision is deliberately not attempted.

**What the real files actually suffer from.** Not cracks — that was checked rather than assumed,
and disproved. Only 1.3% of boundary edges have a vertex lying on them, the nearest unrelated
boundary vertex is a quarter of a millimetre away, and 1,359 of 1,687 boundary chains are
three-vertex loops. The export is simply **incomplete**: individual polygons were never written.
Restoring the 1,346 recoverable ones closes five more components, leaving openings that are whole
absent faces (planar loops 44 mm across, and one 356-vertex non-planar boundary) plus 66
non-manifold edges. Those cannot be repaired without inventing surface.

**When geometry is missing, ask rather than guess.** Some exports simply do not contain enough
polygons to enclose a solid, and no deterministic repair can invent the missing surface. That is
returned as a *state*, not an error: `geometry_status` becomes `incomplete_requires_input`, `volume`
stays **0**, and an `incomplete` report carries what is known, which components are unresolved, and
the supported resolutions. `/cad/mass` and `/cost/cad-part` answer `422` with
`needs_geometry_input` so a caller knows to ask the user.

The user answers with either a total volume or the volume missing from the watertight part that
*was* measured. `complete_geometry` applies it verbatim and stamps the result:
`geometry_source: faceted_user_completed`, `volume_confidence: user_supplied`, and the supplied
figure itself. Volume stays 0 until then, so anything that ignores `geometry_status` fails loudly
instead of quietly costing a fraction of the model — the watertight-only volume is never used
unless the user explicitly chooses to add to it.

The assumption is persisted in the estimate's request snapshot, both inside the geometry and as an
explicit `geometry_completion` entry, so history shows what was assumed and a later assumption
creates a new estimate rather than changing an old one.

**Closure, not approximation.** Vertices are welded by position (exporters write a separate point
per facet corner, so without welding nothing is adjacent to anything); the mesh is split into
connected components; a component is closed when every edge is shared by exactly two triangles
**and** the winding is consistent. The directed-edge check matters: flip one triangle and the mesh
is still watertight by undirected edge count while its signed volume is wrong.

A volume is reported only when **every** component is closed. There is no bounding-box volume, no
convex hull, no guessed thickness and no repair. Summing the closed components and ignoring the
rest would quietly price a fraction of an assembly, so the refusal carries the counts instead.

**Units are read, not assumed.** A faceted file's coordinates are used verbatim, so its
`LENGTH_UNIT` is parsed and converted to millimetres; the real exports are in metres, and guessing
would scale every mass by a thousand. An undeclared unit is refused.

The viewer reads faceted files too, straight from their polygons with no tessellation — but that is
a separate branch, and a preview that fails or is refused for size never affects the volume.

### The unified CAD import layer

```
CAD file
   |
format detection            (extension, cross-checked against the bytes)
   |
CAD adapter                 (declares its capabilities)
   |
normalized CADDocument      (geometry, metadata, components, capabilities)
   |
downstream systems          (mass, viewer, features, planning, costing)
```

**What this replaced.** Three modules each asked their own question about the
file extension — `cad_geometry`, `cad_mesh` and `features` — and the upload UI
kept a fourth list of its own, plus `features.js` and `suggestions.js` each kept
a fifth and sixth. Six places had to agree about what a format could do and
nothing checked that they did. The concrete symptom, found while building this:
adding IGES to the backend left the *Analyse features* button disabled for a
perfectly analysable IGES file, telling the user it was a triangle mesh.

Now `cad_import` answers "what is this file and what can honestly be done with
it" once. `main.py` parses no extensions at all — a structural test reads its
source and fails if a format name appears in it — and the upload panel is
rendered from `format_catalog()`, the same declarations the endpoints gate on.
The interface therefore cannot advertise a capability the backend does not have.

**The capability model is the point, not the routing.** Eight capabilities
(`geometry`, `mass`, `surface_area`, `features`, `viewer`, `assembly`,
`drawings`, `costing`) at four levels (`supported`, `partially_supported`,
`requires_user_input`, `unsupported`), and **anything short of `supported` must
carry a reason**. That is enforced in `CapabilityReport`'s validator rather than
left to diligence, because a degraded capability with no explanation attached
reads to a user exactly like a working one. A test constructs one without a
reason and asserts it is refused.

Declarations are refined per file. A faceted STEP export weakens `features` to
partial; a wireframe STEP drops geometry, mass, viewer and costing to
unsupported; an IGES file with no unit declaration drops everything that depends
on measurement to `requires_user_input`. Refinement happens during a cheap
*inspection* — a keyword count and a header read, about a second on a 198 MB
file — so `/cad/import?with_geometry=false` describes a very large export in
milliseconds and nothing heavy runs unless a measurement was actually asked for.
`detect_ms` and `parse_ms` are reported separately so the import layer's own
cost is visible rather than hidden inside the geometry time it precedes.

**Detection is by extension, cross-checked against the bytes.** The extension
chooses the adapter and the content gets the final say: a STEP file saved as
`.stl` is read as the STEP file it is, with a warning naming the mismatch.
Refusing it would be unhelpful when the content is unambiguous, and parsing it
as STL would fail with a message about triangles that explains nothing.

**Two refusals, two statuses**, because they are different problems. A format
nothing here can read answers **415** with the adapter's explanation — a `.drw`
upload is not a malformed file, it is a drawing. A format that reads perfectly
but cannot do one particular thing answers **400**: an STL asked for feature
recognition is fine as a file, and that path returns the status it always has.

**The layer is a router, not a second measurer.** An adapter delegates to the
existing `extract_geometry_from_bytes`, `build_renderable_mesh` and
`recognize_features_from_bytes` and implements no geometry of its own; a
structural test greps the adapters for `BRepGProp`, `trimesh` and friends. It
also imports no costing arithmetic, and costing imports nothing from it — the
same one-directional rule every other branch follows. A regression test asserts
that routing a STEP or STL file through the new layer produces a
byte-identical `GeometryProperties` to calling the extractor directly, and the
viewer is handed the geometry the import already measured, so a request parses
the file once rather than twice.

**Assemblies are prepared, not implemented.** `CADDocument.components` is a
`CADComponent` tree, and a part file imports as one component holding the
document's geometry — which is the truth about a part file rather than a
placeholder. Step 24 added the decomposition itself (see *Assemblies* above);
what Step 23 contributed was the model and the *detection*: `NEXT_ASSEMBLY_USAGE_OCCURRENCE` is counted in the raw bytes, so an
assembly says so and warns that it is being measured as a single lump of
material. On the real 198 MB Creo export that is 102 component placements across
43 product definitions — a file whose volume is the whole assembly's, which is
worth knowing before costing it as a part.

**Native Creo support is architecture only, and deliberately so.** The formats
are proprietary and undocumented; the OpenCASCADE build in use ships readers for
STEP, IGES, STL, VRML and glTF and no others; no open-source Python package
reads them reliably, and none is installed. The three adapters exist so that a
future licensed native reader is a matter of implementing `inspect` and
`geometry` behind an interface that is already wired — costing, the viewer,
feature recognition and planning all read `CADDocument` and would not change.
No parser was fabricated and no signature was guessed at.

### Assemblies: decomposition and traceability

**The problem.** `read_step_shape` returns one shape for the whole file, which
is what geometry, the viewer and feature recognition have always wanted — and is
also what makes an assembly look like a part. The real 198 MB export arrived as
a single volume with 43 components and 102 placements collapsed into it.

**The reader.** OpenCASCADE has a *second* STEP reader,
`STEPCAFControl_Reader`, which populates an XCAF document: a labelled tree that
keeps the product structure. `cad_assembly.py` is the single place it is used,
and it translates labels into plain values — no `TopLoc_Location`, no `gp_Trsf`
and no `TDF_Label` crosses the boundary. A test greps the serialized document
for those names.

**Structure survives when geometry does not.** Measured on `Figure 3.stp`:
OpenCASCADE transfers **no faces at all** from it, because its `FACETED_BREP`
entities sit on an `OPEN_SHELL` that ISO 10303-42 forbids. The XCAF reader
nonetheless recovers the entire product structure from the same file — 43
definitions, 102 occurrences, four levels, every placement. So an assembly can
be named, counted and placed even when not one of its components can be
measured, and that is worth reporting rather than collapsing into "unreadable".
The reverse is never assumed: a definition that yielded no shape is marked
`no_geometry_in_file`, and the aggregate is never substituted for it.

**Definitions and occurrences are separate models**, because they answer
different questions and have different cardinality. `ComponentDefinition` holds
the geometry, the quantity and the capabilities; `ComponentOccurrence` holds an
id, a parent, an instance name and a `ComponentTransform`. An occurrence id is
its parent's id joined to the component label's own entry, which keeps ids
unique when the same definition is placed at several depths.

**Both transforms are kept.** The local placement is what the file states; the
world placement is the accumulation down the tree, composed by
`cad_assembly._compose`. Deriving one from the other later would mean re-walking
the tree, and losing either would lose orientation — which is what a future
exploded view is going to displace from.

**A sub-assembly is not measured and not costed.** Its shape is a compound of
its children's, so measuring it as well would count the same material twice, and
drawing it as well would send every nested component's triangles twice and make
the viewer's ranges overlap. Both were bugs during development; both are now
tests.

**Component costing reuses the engine and adds only multiplication.**
`assembly_costing.py` takes per-part figures the unchanged pipeline already
produced, unit costs a person typed, and computes each component's contribution.
Its only package import is `component_classification`, which is itself a leaf
with no imports at all — two structural tests assert both — so it still cannot
reach a calculator, a rate set or a repository. What it adds beyond arithmetic
is the refusal to treat an unknown as a zero: a component with no contribution
is carried with its reason, counted separately, and leaves `is_complete` false,
because a total that silently absorbs "we do not know" is wrong in the one
direction nobody checks.

### Classification: the one thing no CAD file can tell you

`component_classification.py` exists because costing every component as a
machined part is confidently wrong for most real assemblies. A component is
`manufactured`, `purchased`, `standard`, `excluded` or `unresolved`, and that
decides entirely where its cost comes from — the unchanged pipeline, a stated
unit cost, or nowhere.

**No CAD format states make/buy**, and this is the layer most at risk of
pretending otherwise. A purchased bearing is modelled with the same kind of
solid as a machined housing; a bolt looks like a turned shaft; a filename is a
filename. So `suggest_classification` reads exactly two things — whether the
definition is a sub-assembly, and whether the file carries geometry that could
be measured — and a structural test unparses *that function alone* and asserts
it cannot see a name, a part number, a material, a volume or a colour. The
function is unparsed on its own because the module's own prose about bolts and
bearings would otherwise match a naive grep.

What it will say is narrower than make/buy, and it says so in its own caveat:
the file carries measurable geometry, which is evidence about *what this
application can cost*, not about what the part is. The suggestion is `low`
confidence, and the engineer's answer replaces it for costing while the
suggestion itself stays on the record — `system_classification` and
`user_classification` side by side, the same rule the operation suggestions and
the setup grouping already follow.

**A suggestion is applied and labelled, not applied silently.** Without that an
assembly would cost nothing the moment classification shipped. So the suggestion
is the effective classification until someone answers, exactly as an assumed
setup grouping is used until an engineer regroups it, and every line reports
`classification_source` — in the API, in the UI and in the stored snapshot.

**Only user-owned fields cross the boundary.** `build_profiles` accepts a
decision per definition and applies just the fields a person owns; the system's
suggestion, the component's identity and its occurrence count are rebuilt from
the assembly every time. A round trip through a browser therefore cannot restate
what the system proposed or how many of a component the file contains.

**Occurrence quantity and production quantity meet exactly once.** A
definition's quantity is how many times *this assembly* uses it; how many
assemblies are being built arrives separately as `assembly_quantity` and
defaults to 1. `total_quantity = quantity x assembly_quantity` is computed in
one place, and `production_extended_cost` is derived from it rather than from a
second multiplication of an already-extended figure.

**The one legitimate zero is an exclusion**, because a person authorised it. It
contributes 0, states its reason, and does not make the total incomplete — while
a component classified as bought with no price entered, or one nobody has
classified, is `needs_costing_input` with a distinct reason and does. The
reasons are deliberately separate: telling someone "no unit cost" about a
component whose cost they *had* entered but whose currency was missing sends
them to re-type a number that is already there.

**Persistence and feedback add no table.** The decisions travel in the estimate's
request snapshot and the contributions in its result snapshot, alongside one
`ClassificationFeedback` record per component the system had an opinion about —
the exact counterpart of `OperationFeedback`, frozen, written once, and trained
on by nothing.

**The viewer draws the assembly as one geometry per definition and one
placement per occurrence.** Twelve identical bolts are one set of vertices and
twelve transforms, so a repeated part is transmitted and uploaded once, and
selection, isolation and explosion are all changes of matrix. Step 24 sent a
single baked buffer with an index range per component instead; Step 25 replaced
it, which is what made explosion possible and removed that design's caveat that
per-component selection had to be withdrawn whenever the display budget forced
decimation. Isolating still hides the edge overlay, which is built from the
whole model and cannot be narrowed the same way.

**Persistence adds no table.** The structure, the quantities, the placements and
every component's cost travel in the existing estimate snapshots, so a
historical assembly estimate stays reconstructible and a later CAD change cannot
rewrite it.

### The exploded view: a display transform that cannot become CAD truth

**Why the mesh representation changed.** Step 24 sent an assembly as one baked
vertex buffer with an index range per component. That is efficient to draw and
impossible to explode: every component's world position is welded into its
vertices, so moving one means rewriting geometry. It also transmitted a repeated
part once per placement. Step 25 replaced it with `AssemblyMesh` — one
`ComponentMeshDefinition` per definition, in its own local space, and one
`ComponentInstance` per occurrence carrying the CAD placement. A part used
twelve times is now one geometry and twelve transforms, and moving a component
is a change of matrix.

That change also removed a Step 24 caveat. When the display budget forced
decimation, renumbering the single shared index buffer invalidated every
component range, so per-component selection had to be withdrawn. With a geometry
per definition, decimation reduces each definition in place and every component
stays selectable; a floor stops any definition being reduced to nothing, because
a component that vanishes reads as a missing part rather than a coarse one.

**The algorithm is deterministic and hierarchy-aware.** `exploded_view.py`:

1. Finds the shallowest depth at which the occurrence tree actually branches.
   Chains of single children are skipped — separating an only child from its
   parent displaces the whole assembly and separates nothing. On the real export
   the top two levels are single children and the branching is at depth 3.
2. Assigns every occurrence to the group headed by its ancestor at that depth.
3. Moves each group radially away from the centre of the group centres:
   `offset = normalise(group centre - assembly centre) x distance`, with
   `distance` taken from how far apart the groups already are, so a 10 mm insert
   and a 2 m frame explode by comparable proportions.
4. Gives a group that sits *on* the centre a direction from a golden-angle
   spiral over the sphere, indexed by its position in a sorted order — so the
   fallback is deterministic and co-located groups fan out instead of piling up.

Everything below a group inherits its offset unchanged, which is what makes the
relationships inside a sub-assembly survive exactly rather than approximately.

**The split between server and browser is deliberate.** The interesting part —
grouping, directions, the fallback, the scale — is computed once in Python,
where it is unit-tested. What the browser gets is each occurrence's CAD
placement and the offset it would take at full explosion; the slider is then
`drawn = original + offset x factor`, one multiply-add per component. So the
algorithm is testable and the interaction needs no request. Measured: **0.1–2 ms**
per slider move on the pump assembly, and the whole arrangement for the real
export's 102 occurrences computes in **2.9 ms**.

**It is structurally incapable of reaching an engineering number.**
`exploded_view.py` imports nothing from this package — a test parses its
imports — and no module in the package except `main.py`, which only serves it,
so much as mentions it; a test walks the source tree to confirm that. The
arrangement is not persisted: an exploded position is a viewing preference, and
a historical estimate keeps the placements the file states. A test asserts the
stored snapshot contains no exploded position and that its transforms are the
CAD ones.

**It is not a disassembly sequence**, and both the model and the interface say
so. `ExplodedViewState.note` carries that statement, the viewer panel prints it
beneath the slider, and a test asserts the wording is present. Sequencing is a
manufacturing decision; the geometry does not imply it, and pretending otherwise
would be the most damaging thing this feature could do.

### Evidence completeness: four states, not two

Step 29's readiness report asked "is this field filled in?" — a question with
two answers, which is one too few. Measuring the real 127-estimate database in
Step 30 showed the cost: it reported 449 records as missing a component identity
when every one of them came from a single-part estimate, and 308 as missing a
paired cycle time when 302 were operations nobody ever finished. Both were
correct absences described as gaps.

So each field now lands in exactly one of four states, and the four sum to the
total:

    known           the value was recorded
    not_applicable  it cannot exist for this record
    unrecorded      it could have existed, and something persisted could
                    still recover it
    unrecoverable   it was never captured and nothing stored can recover it

**Applicability is read from the snapshot's shape, never guessed.**
`component_identity_applicable` is set only for records extracted from an
assembly's per-component feedback map, because a part estimate's flat feedback
list belongs to something with no components. `time_pairing` is derived from
whether both halves are present — both are already persisted separately, so the
pairing is a reading of them and nothing new is stored. `time_profile_version`
is not applicable when no `time_profile_id` was recorded either, because a plan
built without a profile ran under the built-in baseline and there is no
published version to have lost.

**`percent_known` divides by `applicable`**, never by the total — the same
mistake as the acceptance denominator, made in a different place.

**Nothing is backfilled that is not provable.** The only recovery Step 30
performs is one Step 28 already made: an assembly's feedback map is keyed by
component *definition*, so a row missing its own `component_id` takes the key.
That is a deterministic read of persisted structure, not an inference, and a
definition id is never confused with an occurrence id — an assembly places one
definition many times, and conflating them would attribute twelve bolts' worth
of corrections to one placement. Measured on the real database, the available
safe backfill is **zero**: every recoverable value was already recovered.

**A display name is never used as an identity.** A test unparses
`_classify_component_identity` and asserts `component_name` does not appear in
it.

**Feature confidence is never manufactured.** A test unparses
`_classify_feature_confidence` and asserts it contains no reference to a
correction count, an acceptance rate, a rule or a `was_corrected` flag. It reads
two things: the recorded confidence, and whether there was a suggestion at all.

**The one real capture hole was in the browser.** The operations table builds its
plan payload in JavaScript rather than calling `build_plan`, so a field it does
not copy never reaches the server — which is why every one of 713 stored records
lacked a feature confidence the backend was populating correctly. `plan.js` now
carries `system_source_type` and `system_feature_confidence`, and the
display-only `_source_type` duplicate was removed rather than left as a second
source of truth for the same fact.

**No migration.** All three fields live in JSON snapshot columns that already
existed; the schema is untouched and the head stays `0005`.

### The feedback analytics reads; it cannot write

`feedback_analytics.py` turns four steps' worth of recorded corrections into a
report. It is the first module in this application whose entire design is about
what it *cannot* do.

**It imports nothing from the package at all.** Not a rate set, not a repository,
not a rule, not a profile — it is handed plain snapshot dictionaries and returns
plain models. That is what makes "analytics cannot change anything" a structural
fact rather than a promise, and three tests hold it: the empty import set, that
no module but `main.py` mentions it, and that its unparsed source contains no
`session`, `Repository`, `save`, `commit` or `fit(`. A behavioural test then runs
every report and asserts every stored estimate, rate set and time profile is
byte-identical afterwards. Every endpoint is a `GET`, asserted by walking the
route table — a reporting layer with a `POST` is one somebody will write through.

**Snapshots are read as they were written.** By explicit `.get()` on dicts, never
by validating into today's models: coercing an old record into a current model
would fill absent fields with defaults that read exactly like recorded data. A
field a record does not have is reported `unknown` and counted in the readiness
section. Publishing a newer rate set or time profile therefore cannot alter a
single number, which live verification confirmed byte-for-byte.

**One source of truth, two snapshot shapes.** `extract_records` reads both the
flat `feedback` list a part estimate stores and the per-component
`component_feedback` map an assembly estimate stores since Step 28. A second
reader for the same corrections is exactly what this layer must not create.

**Feedback rows are joined to the plan beside them.** `rule_id`, `feature_type`
and `source_type` were added to the feedback record only in Step 29, so every
correction ever recorded would otherwise report them as `unknown` — measured: all
713 of them. But the plan travels in the same snapshot and carries
`system_suggestion_rule`, `feature_type` and the viewer linkage, so joining by
`operation_id` recovers all three for the whole of history. The join fills absent
fields only, never overrides a recorded one, and flags what it filled;
`source_type` falls back to the viewer linkage (a B-Rep recognizer reports faces,
a faceted one facets) and a test proves that derivation agrees with what the
recognizers actually report on both a real B-Rep and a real faceted model.

**The denominators are the whole design.** Three of them are chosen rather than
obvious:

- *Acceptance* is measured over the operations somebody actually decided about —
  accepted, changed or rejected. An operation with no committed time was never
  finished, and on this project's own history that is 302 of 713. Counting them
  made `external_cylinder_to_turning` — 100 suggestions, **one** correction, 91
  unfinished — read as 8% acceptance. It reads 89% now, with the 91 shown beside
  it. Every `CountedRate` carries `numerator`, `denominator`, `excluded` and a
  `basis` sentence saying which population it describes.
- *Operation and time correction* rates are measured only over records where the
  system actually predicted something to correct. An operation the system never
  predicted a time for was entered, not corrected.
- *Setup splitting* is counted per **estimate**, because "one assumed setup became
  several" is a property of a plan and not of an operation.

**Medians, outliers and a relative-error floor.** Medians carry every headline
figure so one extreme correction cannot define a bias; outliers are reported
separately and still counted, because an order-of-magnitude miss is the most
informative record in the set; and relative error skips predictions below
`RELATIVE_ERROR_FLOOR` (0.05 min) — dividing 0.5 by 0.0008 produces a 62,400%
error that says nothing — counting them as `near_zero_excluded` and keeping them
in every other statistic.

**Small samples are labelled, not hidden.** `SampleGuard` travels on every metric
that needs a sample: `sample_count`, `minimum_required`, `is_actionable` and the
sentence to print when it is not. The threshold is a configurable floor
(`CAD_COSTING_MIN_FEEDBACK_SAMPLE`), explicitly **not** a significance test, and
a directional bias is claimed only above it — below, the finding is "not enough
evidence for a reliable pattern"; and a median correction smaller than the
relative-error floor is `no_clear_bias` rather than a direction, because
arithmetic noise on a two-decimal minute is not a bias.

**The window is bounded.** `CAD_COSTING_MAX_FEEDBACK_ESTIMATES` caps how much
history one request reads, and the response states whether it truncated: an
answer over the most recent N estimates is useful, one that ran out of memory is
not. The browser is given aggregates and a page of records, never the history.

**Confidence is reported as an association, never as calibration.** Confidence in
this application is a three-way classification of which rule branches fired, so
`calibration_note` says a higher acceptance rate at `high` is measured and
nothing more. `LEARNING_DISCLAIMER` is on every response, because a page of
correction rates is exactly where a reader starts to assume something is
adapting.

### A plan belongs to a component, never to an assembly

`assembly_plan.py` gives each **manufactured** component definition its own
`ManufacturingPlan`. There is no assembly-level plan and no assembly-level
sequence, and both absences are load-bearing.

**Why one flat plan would be wrong, not just untidy.** Setup time is charged
once per setup. A single assembly-wide plan would put a housing's facing
operation and a shaft's turning operation in the same setup, so the two would
share a fixturing they cannot physically share and the assembly would be
under-costed by exactly the setups nobody charged for. This is the Step 21 error
one level up, and `AssemblyManufacturingPlan` exists to make it unrepresentable:
it is a mapping of definition to plan, with no operations or setups of its own.

- **Ids are namespaced at birth, not relabelled afterwards.** `build_plan` takes
  an `id_prefix`, so a component's plan is *born* with `HOUSING/OP-001` and
  `HOUSING/SETUP-001`. Rewriting ids after the fact would have to keep
  `system_setup_id`, `user_setup_id` and the setup's own membership list in step
  with each other — the kind of bookkeeping that fails silently — whereas a
  prefix means the plan's existing validator checks it like any other.
- **Recognition is per component.** Each component's own OCCT shape goes through
  the same recognizers a standalone part does, so a housing's holes are never
  attributed to a shaft, and feature ids come back prefixed by the component the
  way Step 24 established. A component whose shape the reader could not transfer
  is skipped with its reason rather than given the assembly's aggregate.
- **Only `manufactured` is planned, and `MANUFACTURED` is the explicit gate.**
  A classification the vocabulary does not recognise is refused rather than
  falling through into planning: an unrecognised label is not permission to
  machine something. `not_applicable` (bought, standard, excluded) is a
  different answer from `not_generated` (nobody has planned it yet), which is
  different again from `blocked` (unresolved, or no geometry). Collapsing the
  first two would let unplanned work be costed as no work.
- **Missing inputs are named one by one**, never counted. `_missing_inputs`
  reads `setup.setup_time_minutes` — the *committed* time — and not
  `charged_setup_time`, which is 0.0 by design when nothing is committed and
  would therefore never report a gap.
- **Estimated and committed time are separate fields.** `time_summary` reports
  what has been *committed* and is legitimately zero for a fresh plan;
  `estimated_setup_time_minutes` and `estimated_cycle_time_minutes` carry the
  system's predictions beside it. Without both, a newly generated plan reports
  0.00 minutes — true, and indistinguishable from "this component takes no time
  to make".
- **It plans; it does not cost.** Its only package imports are
  `manufacturing_plan` and `component_classification`, and a structural test
  asserts that set plus the reverse direction — no costing module may reach it.
  `manufacturing_plan` stays the sole bridge to money: each component plan emits
  its own `OperationRequirement`s, which the unchanged `calculate_cad_part_cost`
  consumes exactly as it does for a standalone part. A second test unparses the
  module and asserts it contains no `_cost`, `currency`, `rate_set` or
  `material`.

**Costing.** `/cost/assembly` takes `component_plans` and costs each manufactured
component from its own: material plus the operations in that plan carrying a
committed time, with that component's setups charged once each. `ComponentEstimate`
gained `setup_per_unit` and `run_per_unit`, split from figures the engine already
produced — `setup_cost` is its own field on every `OperationCostResult`, and the
plan hangs a setup's whole time on the first costable operation in it, so summing
gives the part's setup cost rather than one per operation. A test asserts setup +
run comes back to the operation cost, which is what makes it a split rather than
a new formula. A component whose plan commits nothing is costed for material only
and its cost line reports the plan status, so "not planned yet" is never read as
"needs no machining".

**Persistence adds no table.** The component plans travel in the existing
`plan_snapshot`, each with one feedback row per operation carrying the component
that produced it — `OperationFeedback` gained `component_id`/`component_name`,
because "the system said milling and the engineer said turning" is uninterpretable
across an assembly without knowing which component it was about.

**The browser reuses the operations table rather than copying it.** Selecting a
component's plan points `state.plan` at that component's plan object and calls
`renderPlan()`, so all twelve editing operations work unchanged and mutate the
component's plan in place. A banner names the component being edited, because
editing the wrong component's routing is otherwise completely silent. The panel's
*status* is re-read live through `plan.js`'s own `finalCycle` and `setupTime` —
the single implementation of which time counts — because the server-computed
status goes stale the moment a person edits, and a fourth copy of the three-branch
rule would drift from the other three.

### Actual costs: a recorded fact, never an input

`cost_actuals.py` holds a cost somebody outside the application states for an
estimate's part — an ERP actual, a past quotation — and the per-unit variance
between it and the estimate. It is the first place the system's figures meet a
number it did not produce, and its design keeps the two apart.

- **It imports `money` and nothing else**, and only `main.py` and `repository.py`
  import it. Both are structural tests (the second parses imports with `ast`,
  because the *table* is also called `cost_actuals` and a substring grep matches
  the ORM model and the migration). Nothing in costing can read an actual, and
  recording one changes no estimate — a test compares the stored estimate before
  and after.
- **Only the total is required.** A breakdown category left out is
  `not_supplied`, never zero. A zero would read as "this part had no material
  cost" and report the whole estimated figure as a variance.
- **The basis is never guessed.** `cost_basis` (`per_unit` / `per_batch`) has no
  default, and a per-batch cost without its batch quantity is refused rather
  than assumed to be one unit. A breakdown that adds up to more than its total is
  refused, because which figure is wrong cannot be told.
- **Variance is per unit**: `estimate - actual`, with the percentage relative to
  the actual and omitted where the actual is zero. Material is compared with the
  estimate's material cost, machining with its operation cost (machine, labour
  and setup), additional with overhead, scrap and tooling. Any part of the actual
  total that no supplied category covers is reported as unattributed rather than
  assigned to one.
- **A different batch size is warned about, not corrected.** Setup and tooling
  are spread over the batch, so per-unit cost depends on it; correcting for that
  would need the setup share of the actual, which client data does not state.
- **An assembly is compared on its total only.** Its material, operation and
  additional figures cover manufactured components alone. And an incomplete or
  mixed-currency assembly has nothing to compare against: its row stores `0.0`
  where the roll-up had no total, so `figures_from_estimate` reads the snapshot's
  `is_complete` / `is_aggregatable` / `production_total_cost` instead and reports
  the variance `unavailable` with the reason.
- **Currencies are never converted.** A mismatch is refused at entry (422).
- **Immutable.** `CostActualRepository` has `record`, `get` and `for_estimate`
  and nothing else. A correction is a new row whose `supersedes_actual_id` names
  the one it replaces; that column is unique, so a record is corrected at most
  once and the chain stays a single line. A superseded record stays listed,
  marked not current.
- **Client part number and drawing revision are plain columns on the actual**,
  stored as entered and never matched against the estimate's part id. There is no
  part-management system behind them.

Migration `0006` adds the one table and touches nothing else.

### The quotation is a presentation, and must never become a calculation

`quotation.py` turns a stored estimate into a business document. Its whole
design is one constraint: **it presents an estimate that already exists**. It
reads the snapshots the estimate persisted and arranges them; it calculates no
cost, resolves no rate, reads no CAD file and re-runs nothing.

- **It imports `estimates` and `money`, and nothing else** — a structural test
  asserts exactly that set. `estimates` is where `EstimateDetail` and
  `extract_assumptions` already live, so the provenance reader is reused rather
  than written a second time. A second structural test unparses the module and
  asserts it contains no `calculate_`, no `resolve_material(`, no rate-per-hour
  multiplication, and no `RateSet`, `ManufacturingOperation` or `Part`. There is
  no second costing path because there is nowhere for one to come from.
- **The cost lines partition the total.** They are stored figures regrouped, and
  regrouping is the one place a document could quietly disagree with the estimate
  it claims to present. So no line contains part of another, they sum, and the
  sum is checked against the stored total; a remainder is reported as
  `reconciliation` rather than absorbed — the same rule the cost-impact
  decomposition follows.
- **The stored total is the authority.** `total` is exactly what the estimate
  recorded, never the sum of the lines. Where the two differ by rounding, the
  document says so.
- **Setup and run are split from figures that already exist.** Every
  `OperationCostResult` carries `setup_cost` separately from `machine_cost` and
  `labor_cost`, and `to_operation_requirements` puts a setup's whole time on the
  first costable operation of that setup and zero on the rest — so summing
  `setup_cost` across operations gives the part's setup cost rather than one per
  operation. That is aggregation of stored figures, not a formula.
- **The time source is a label, not a third resolution.** `plan.js` and the plan
  already implement the three-branch rule for which time counts, and CLAUDE.md
  warns that a third copy would drift. So the document reads its *numbers* from
  the costed operations — the ones that actually reached the money — and derives
  the source with a two-branch test: an operation only appears there if it had a
  committed time, so if the engineer did not type it, the system's estimate was
  accepted. A test asserts every operation shown is one that reached the cost.
- **It reads no clock.** `issued_at` is a parameter, so a preview and the
  document it previews cannot differ by a timestamp, and rendering twice is
  byte-identical. A test greps the module for `datetime.now`.

**Refusal is a feature.** An assembly with components still needing costing
input, one priced in more than one currency, an estimate with no total, or
geometry that was never resolved all produce `available=False` with a blocker
naming the reason. The engineering view still renders — the person fixing it has
to see it — and `POST` refuses with 409 rather than issuing a document with a
misleading total. Nothing is ever filled in with a zero to make a total appear.

**The customer view omits the engineering section entirely** rather than hiding
it. A field a template does not print is still a field in the JSON somebody
forwards, so the internal correction records the learning architecture keeps are
absent from that payload, and a test serializes a customer quotation and greps
it for those keys.

**Persistence is metadata only.** `QuotationRow` holds the customer name, the
project, the reference, who prepared it, the notes, the validity, and the totals
as issued. The *figures* are not copied — they are re-read from the estimate's
immutable snapshots whenever the document is rendered, so there is one source
for them rather than two that could drift, and `total_at_issue` exists to be
checked against a re-render. There is no update method, for the same reason a
rate set version has none: an issued quotation is a document somebody already
has, and a correction is a new one with a new number.

**The report page is standalone.** `quotation.html` shares no globals and no
stylesheet with the application shell, because a page that gets printed and sent
should not inherit an interactive layout. Print is the primary target: repeating
table headers (`display: table-header-group`), `break-inside: avoid` on rows,
notices and setups, the controls hidden, and margins set in millimetres with no
forced paper size — forcing `size: A4` would override the reader's print dialog
and make a Letter printer scale or clip. A test greps the page's script for
arithmetic and for assignments to any money field, because a plausible figure
invented in a printable document is indistinguishable from a real one.

### IGES: a different reader, and two ways it differs

IGES went through the common abstraction wherever it could — it shares
`GeometryProperties`, the mesh path (`_brep_shape_mesh` is now used by both STEP
and IGES, so the two cannot drift into looking different), the recognizers, the
budgets and the costing pipeline unchanged. Two things could not be shared,
and both are why `cad_iges` exists rather than a rename.

**1. IGES usually carries no solid.** IGES 5.3 has a Manifold Solid B-Rep
Object (entity 186), but the common export is a bag of trimmed parametric
surfaces (entity 144) with no shell above them. OpenCASCADE transfers those as
loose faces — and `BRepGProp.VolumeProperties` will still return a number for
loose faces, because it integrates the divergence theorem over whatever it is
given. Measured on three faces of a 40×30×20 box: 10666.67 mm³, which is the
volume of nothing. So closure is established before any volume is reported —
either the file brought a solid, or the sewn shell satisfies
`BRep_Tool.IsClosed` — and a file that fails goes down the existing
`incomplete_requires_input` path with its surface area, which is real, still
reported. Sewing uses a tolerance relative to the bounding-box diagonal
(`1e-7`), deliberately tight for the same reason `cad_faceted` welds tightly: a
loose tolerance would close a shell that is not closed and then report a volume
for it. It is recorded in the existing `repair_applied` / `repair_method` /
`repair_tolerance_mm` fields, so a sewn volume is never presented as a
measurement that needed no reconstruction.

**2. A surface export loses analytic surface identity, and that cannot be
sewn back.** This is the finding that shaped the capability report. Measured on
a real 43-face part, the solid-mode export kept all 17 `Cylinder` faces and
recognition matched STEP exactly at 39 features; the surface-mode export of the
same part came back with those 17 faces as `SurfaceOfRevolution`, and
recognition found 22 planes and no holes at all. The volume was identical in
both. So closure is the wrong question for features, and asking it would have
over-promised: which kind of export a file is comes from scanning the Directory
section for entity 186, which costs a byte scan and gives the same answer
whether or not geometry was parsed.

**Units are read here, not delegated.** OCCT's IGES reader honours a declared
unit correctly — a 40-unit box declared INCH reads back as exactly 1016.0 mm —
but a file with the unit flag *and* units name removed is read as inches
anyway, silently. `detect_length_unit` parses the Global section itself
(Hollerith-counted strings and all, since a `split(",")` would corrupt any
system name containing a comma) and `ensure_readable` applies both guards in one
place, in order, so geometry extraction and feature recognition give the same
message for the same file. A structural test asserts both call it.

### Analytic and faceted STEP

A STEP file may carry exact surfaces or only flat polygons, and the difference decides which rules
can apply at all. So the file is **classified before it is loaded**, by counting entity keywords in
the raw bytes (`features/step_text.py`). That costs about two seconds on a 200 MB export, against
roughly twenty for OpenCASCADE merely to read it and a hundred to transfer it — and for a file
that cannot be used, being told quickly is the whole value.

```
                        ┌ brep     → read_step_shape → topology scan → B-Rep recognizers
STEP bytes → classify ──┤
                        └ faceted  → polygon parser  → mesh analysis  → faceted recognizers
```

Both branches emit the **same `Feature` objects**. Only `source_type` (`brep` / `faceted`), the
detection method and the evidence differ, so nothing downstream needs to know which path ran.

**The faceted path does not use OpenCASCADE at all.** It cannot: the exports that motivated it
declare `FACETED_BREP` over an `OPEN_SHELL`, which ISO 10303-42 forbids, so the reader rejects
every solid and transfers nothing. Repairing the text to `CLOSED_SHELL` does work — measured at
110 seconds on a 198 MB file, yielding 310,601 loose faces and no solids — which is both too slow
and not useful. Reading the polygons directly (`features/faceted.py`) takes 25 seconds on the same
file, needs no repair, and produces exactly what a mesh recognizer wants.

**How a hole is recovered from polygons** (`features/mesh_recognition.py`): coplanar adjacent
facets are merged into flat patches; patches meeting at under 40° are joined into a band, which
separates a tessellated bore (strips at 360/N degrees) from the face it breaks through (90°); a
circle is fitted to the band's vertices in closed form.

Two tests decide whether the band is really a cylinder, and both are stated in every feature's
evidence:

- **the fit residual** — vertices that came off a cylinder lie on the fitted circle to within
  rounding;
- **the sagitta** — how far the flat strips stand inside the circle through their corners, which
  is `1 - cos(π/N)`. Below 6% it is a tessellated circle (ten or more segments); above it is a
  designed polygon. An octagon is 7.6% and a hexagon 13.4%, so a hexagonal hole is left
  unclassified rather than reported as round.

Because a tessellator puts vertices on the true surface, the fitted radius is the **real** radius,
not the inscribed-polygon radius. Vertices are welded by position when parsing, since some writers
emit a separate `CARTESIAN_POINT` per facet and an unwelded soup has no adjacency to work from.

**Traceability** survives the difference: a faceted feature carries `source_facets` where a B-Rep
feature carries `source_faces`.

### Feature recognition

A third branch off the same parsed B-Rep, independent of both the others:

```
                    ┌→ mass → costing          (unchanged)
CAD → B-Rep (STEP) ─┼→ tessellation → viewer
                    └→ features
```

STEP parsing lives in one place. `cad_geometry.read_step_shape` is the only STEP reader; geometry
extraction, viewer tessellation and feature recognition all call it, so there is one definition of
a readable STEP file and one set of failure messages.

**One scan, then pure rules.** `features/topology.py` traverses the shape once into plain
dataclasses — surface type, axis, radius, angular and axial extent, which edges bound each face,
which faces sit across each edge, which solid each face belongs to. It is the only module in the
feature layer that imports OpenCASCADE. Every recognizer is a pure function over those
descriptors, which is why each can be tested against a topology without a running kernel and why
the model does not get traversed six times.

**The application never sees OCP.** `features/model.py` defines the vocabulary the API, the UI and
any later step consume. Nothing downstream needs to understand B-Rep topology.

**Recognizers are small and ordered.** Each claims the faces it explains, and the more specific
rule runs first: holes, then pockets and slots, then fillets and chamfers, then leftover
cylinders, then the remaining planes. A face therefore belongs to at most one feature and the
counts describe the model once instead of describing the same geometry several ways. Faces nobody
claims are counted and reported.

**How a hole is actually identified.** Cylindrical faces whose material lies outside the surface
are grouped by axis, radius and axial overlap — one bore is not always one face, and CAD systems
split full cylinders differently. A group sweeping a full turn is a bore. Each end's circular edge
is then examined: a planar face carrying that circle on its **outer** wire is a floor, so the end
is closed; carrying it on an **inner** wire means the bore breaks through, so the end is open.
Two open ends is a through hole; one closed is a blind hole. Counterbores and countersinks fall
out of the same evidence rather than needing separate scans.

**Confidence is a classification, not a probability.** High means every required condition was
checked and satisfied; medium means something was inferred rather than measured; low means the
classification stayed ambiguous. It is derived from which branches of a rule fired, so it is
reproducible.

**Traceability is preserved deliberately.** Features carry the 1-based face and edge indices from
`TopExp::MapShapes`, which are stable for a given file. That is what lets the UI say "feature
H-004 is these CAD faces", mark it on the model, and — since Step 19 — let an operation
suggestion point back at the feature that caused it.

#### Known false positives and negatives

Stated rather than hidden, because a recognizer that pretends to be complete is worse than one
with documented gaps:

- **A slot or window cut clean through a part is not detected.** The pocket rule anchors on a
  planar floor, and a through-cut has none. Verified: a through-slot with rounded ends reports its
  two end radii as concave *fillets* — a genuine false positive — and the rest as planes.
- **A stepped pocket is reported as nested recesses**, one per floor level, each with its own
  depth. Defensible rather than wrong, but it is not a single "stepped pocket" feature.
- **Chamfers are the weakest rule.** A narrow planar face at equal oblique angles to two
  neighbours is good evidence but not proof, so chamfers are capped at medium confidence. A
  narrow designed bevel that is not a broken edge will read as a chamfer.
- **A chamfer around a hole is conical**, and is reported as that hole's countersink rather than
  as a chamfer.
- **Freeform and swept surfaces are not classified.** B-spline, surface-of-extrusion and other
  non-analytic faces are typed `other` and claimed by nobody, so a hole in a moulded surface is
  not found. Observed on a real Creo bracket: 4 of its 43 faces are
  `GeomAbs_SurfaceOfExtrusion` and are reported as unclassified rather than guessed at.
- **From a faceted file only holes, shafts and flat regions are reconstructed.** Pockets, slots,
  fillets and chamfers are not attempted: on polygons a fillet is a run of narrow strips that
  cannot be told from a chamfer or a coarse curve, and a pocket floor cannot be told from any
  other flat region without the boundary structure a B-Rep has. Reported as a limitation in the
  result's warnings rather than guessed at.
- **A faceted band that does not close is not claimed at all.** A partial sweep on polygons is as
  likely to be a coarse curve or a blend as a bore.
- **Tapered and interrupted bores are not holes.** A conical bore is not a cylinder, and a bore
  broken by an intersecting feature may not sweep a full turn.
- **Every remaining flat face becomes a `plane` feature**, so a complex part yields many of them.
  That is an inventory, not a claim of manufacturing significance.
- **Assembly structure is not read.** Solids are kept separate, but product names, instances and
  hierarchy from the STEP file are ignored.

#### Performance

Recognition is one topology scan plus a bucketed grouping pass, so it grows close to linearly with
face count rather than quadratically. Measured on this machine: a 14-face bracket in ~90–140 ms; a
real 43-face Creo bracket in ~200–800 ms; a 106-face plate with 100 holes, all found, in ~700 ms.
`MAX_FACES` refuses models beyond 20,000 faces rather than producing a feature list nobody could
read. These times exclude STEP parsing, which dominates on large files — a 198 MB export takes
about 20 seconds to read before recognition even begins.

### Operation suggestions

`operation_suggestions.py` turns recognized features into *proposed* operations:

```
CAD → features → suggestions → user reviews/edits → existing costing
```

**Why this is a separate layer from recognition.** Recognition answers a geometric question — *is
this a through hole?* — which the CAD file can settle. Turning that into *drill it* is a
manufacturing decision, which the CAD file cannot: it depends on the shop, the batch size, the
fixturing and the material. Keeping them apart means a wrong suggestion is visible and correctable
without casting doubt on the geometry, and it keeps a modelling opinion out of a measurement
routine. `features/` still emits no operation vocabulary at all.

**The rules are a table, not a model.** One function per feature type, keyed to evidence the
recognizers actually produce (`subtype`, `dimensions`, `confidence`). Each returns either a
proposal — process, rule id, reason, confidence ceiling, alternatives — or a *decline* carrying the
reason. There is no ML, no training, no randomness; the same features always give the same
suggestions in the same order. The rule table is in the README.

**Confidence can only go down.** A suggestion's confidence is the weakest of three things: the
feature's own confidence, a per-rule ceiling, and a per-source ceiling. The rule ceiling encodes
that some claims are inherently weaker than others — "this planar face needs facing" is weaker than
"this through hole needs drilling" however cleanly the face was recognized, because geometry can
prove a flat exists but not that material must come off it. The source ceiling caps anything
`faceted` at medium, since a feature fitted to polygons is weaker evidence than one read from exact
surfaces. Nothing can raise a suggestion above its feature.

**Declining is a feature.** Holes whose end conditions are ambiguous or enclosed, blends confirmed
only partially, and planar faces below a relative significance threshold produce a
`DeclinedSuggestion` with the gap spelled out. An unexplained absence and an invented operation are
both worse than a stated refusal. The planar threshold is relative — a share of the largest planar
face on the same solid — so it holds for a 10 mm insert and a 2 m frame alike, and the share that
decided each case is recorded in the evidence.

**Nothing here can become money by itself.** This is enforced three ways, not just documented:

- The module imports no costing arithmetic, and no costing module imports it. Tests assert both
  directions by reading the source.
- Every suggestion reports `costable: false` and lists what is missing. It carries an operation
  *identity* and a feature link — no setup time, no cycle time, no rate.
- `to_operation_requirement` is the only bridge into costing, and it is a gate: it raises unless
  the caller supplies a cycle time, and refuses a rejected suggestion outright. Defaulting a
  missing time — to zero, to a guess, to a per-process constant — would turn a suggestion into
  money without anyone deciding to. In the UI the same rule is visible: an accepted suggestion
  lands in the operation plan with blank times and is excluded from the estimate until one is
  typed, with the plan saying so rather than the omission being silent.

**The user's decision never overwrites the system's.** An edit is recorded in `user_operation_type`
beside the untouched `operation_type`, with `user_action` one of
`pending`/`accepted`/`edited`/`rejected`/`added`. The pair *(suggested, chosen)* is the whole value
of a correction to a later learning step, and keeping only the second would discard it. Choosing the
process that was already suggested is recorded as an acceptance rather than an edit, so a
correction count stays meaningful. Nothing is persisted yet — a suggestion is not a manufacturing
decision — but the fields a feedback loop would need are in place.

`rate_set_id` is accepted only so the response can *report* whether the user's rate set prices a
suggested process. It never changes what is suggested, and no rate is read from it.

### Manufacturing family and route

`process_classification.py` sits beside the suggestions, not after costing:

```
features + suggestions → classify_process → family + route (recommended)
                                         → record_route_decision (engineer)
                                         → /cost/cad-part reads blocks_costing only
```

**It imports `features.model` and `operation_suggestions`, and only `main.py` imports it.** A
test parses both directions. It computes no time, no rate and no cost.

**It reads what recognition already established.** No second parse: the same request that
suggests operations classifies them. Three signals are derived from `Feature` objects —
features cut into a solid (pockets, slots, blind and counterbored holes); a rotational form
(every external cylinder on one axis, every planar face square to it); and a constant wall
(one opposite-plane separation holding at least 75% of the planar area, thin against it, and
agreeing with the measured volume to 10%). Each is reported as observed, not observed or not
assessable, with the constant that decided it.

**Its confidence is capped at `medium`.** Geometry cannot exclude a near-net-shape blank that
is then machined, and it does not record the material, tolerance, finish, quantity or machines
that usually decide a process. Families with no detectable evidence — casting, forging,
moulding, welding, additive, purchased — are listed as not assessed and never proposed.

**The route is a summary of the suggestions, and may disagree with one of them.** A turning
suggestion for a boss on a prismatic part stays a turning suggestion; at route level the boss
is milled, and the stage's basis says so. On a rotational part, the faces proposed for milling
are faced in the turning stage. The per-feature suggestions are unchanged by any of this.

**Recommendation and decision sit side by side**, as everywhere else: `system_*` fields are
never overwritten, `user_route`/`user_action`/`user_note` sit beside them, and choosing the
recommended route records an acceptance. Costability (`costing_status`, `blocks_costing`) is a
computed field, so a classification round-tripped through the browser cannot declare itself
costable; the server recomputes it from the stages.

**The costing boundary.** Only stages whose process type is one the time estimation prices by
the hour (milling, drilling, turning) report `cost_model_available`. Since Iteration 3 every
other stage is priced from the rate set's stage rates (`stage_costing.py`, next section) once
the route is *decided*; `costing_status` is then `stage_rates_required`. Which stages those are
is read from `STAGES` by stage id, not from the payload. A route the engineer marked unknown,
or an unreviewed recommendation that would need stage rates, is still refused before anything
is priced. An *unreviewed* `unknown` does not block — the engineer's own operations are priced
as before and the snapshot says the route was not established.

### One pricing path, and two previews that store nothing

`main.price_cad_part(session, ...)` computes everything `/cost/cad-part` needs — geometry,
the parsed plan and route, the stock, the stage inputs, the resolved rate set and the
result — and raises the endpoint's own `HTTPException`s. The endpoint stores what it
returns. **Route comparison** (`POST /cost/route-comparison`) calls it once per route —
the recommended route and the candidate families' catalog routes, each decided for the
preview — and lists priced and refused routes in a fixed order, never by cost, with
nothing chosen or stored. **Bulk preview** (`POST /cad/bulk/preview`) calls it once per CSV
row with the system's estimated times accepted on an in-memory plan copy, labels every
figure an unreviewed system estimate, and stores nothing. Because both go through the
same function, neither can price a part differently from the estimate the same inputs
would produce; tests assert equal totals.

### DFM flags are advice

`dfm.py` checks recognized features against a `DfmProfile` — versioned, immutable, seeded
(`dfm-general-guidelines` 1.0.0, illustrative), stored in `dfm_profiles` (migration
`0007`) exactly as time profiles are. Each threshold names its source or says none was
found. A rule only exists where a recognizer measures its input (hole depth and diameter,
pocket depth and width, fillet radius); there is no wall-thickness or corner-radius rule.
`POST /cad/dfm` is a separate endpoint from the suggestions, so a flag cannot reach a
plan, a time or a cost, and flags carry no severity or confidence.

### Route stages are priced from rates someone published

`stage_costing.py` prices a decided route's non-machining stages — casting, forging, sheet
cutting, bending, welding, finishing, moulding, additive, purchase — the way a supplier quotes
them. `RateSet.stage_rates` holds, per stage id, a `StageRate`: a unit rate `per_kg` or
`per_piece` and/or a one-off `batch_charge` (a pattern, a die), with a note on where it came
from. Rates live in the existing JSON column, so no migration: a version published before them
reads as having none.

    unit_cost_per_part = unit_rate x mass_kg (per_kg) | unit_rate (per_piece)
    total_cost         = unit_cost_per_part x quantity + batch_charge

- **Every stage or none.** A stage with no rate in the selected rate set, or a per-kg stage with
  no mass, returns a problem; `/cost/cad-part` refuses with `422 route_stages_not_priceable`
  naming each stage and stores nothing.
- **The mass is the engineer's, per stage, per part** (`stage_inputs`, recorded on the
  estimate), **or else the finished part's CAD mass**, labelled `cad_finished_mass` with a note.
  That is exact for a part made as formed; for one machined afterwards the note says the blank
  is heavier by material the CAD does not record — no allowance is invented. An entered blank
  lighter than the finished part is refused.
- **Material is not charged twice.** A route whose first stage is casting, forging, moulding,
  an additive build or a purchase gets its material from that stage's rate:
  `material_charged = false`, `total_material_cost = 0`, and `material_note` names the stage.
  The material record stays in the result for reference. A confirmed bar on such a route is
  refused as a contradiction.
- **Stage costs are direct cost.** `direct_cost_base = material + operations + stages`, so
  overhead and scrap apply to them exactly as to material. Without stages the result reads as
  it always did: `stage_costs = []`, `total_stage_cost = 0`, `material_charged = true`.
- **Boundaries.** `stage_costing` imports `money` and `rate_sets` only; it never sees a route.
  `main.py` turns the decided route's stages into `StageInput`s and checks published
  stage-rate keys against `STAGES` (`rate_sets` may not import the classifier).
- **Downstream.** The quotation adds a `route_stages` line only when there are stages, so its
  lines still partition the stored total; `estimates.decompose` reads a material that was not
  charged at a zero rate and reports stage changes as `route_stage` impacts.

**A decided route regroups the plan; it does not edit it.** `plan_processes` is computed on
the classification — `None` until the engineer decides, then the process types of the route's
machining stages (empty for a route with none, or one marked unknown). It becomes
`ManufacturingPlan.route_processes`, and `in_route` is the one test: an operation outside the
route stays in the plan with every field untouched, but `to_operation_requirements` skips it,
`uncosted_operations` does not count it as missing a time, and `out_of_route_operations`
names it. Setup time is still hung on the first *costable, in-route* operation of each setup.
`/cost/cad-part` sets `route_processes` from the classification it was sent, never from the
browser's copy, so the operations priced and the route recorded cannot disagree. The default
`None` is the pre-route behaviour exactly, which is why assembly component plans are unaffected.
Non-machining stages never become `PlannedOperation`s — they have no hourly rate — and are shown
in the route strip and as display rows of the operations table in route order
(`plan.js`'s `routeStageRows`), priced from stage rates and outside every time total.

**Timed welding.** Welding is the one stage with a time model, so the engineer may price it
by time instead (`stage_inputs.welding.pricing = "timed"`; `stage_costing.TIMEABLE_STAGES`).
`/cost/cad-part` then leaves the stage out of `price_stages` and adds `welding` to the plan's
`route_processes`, so the welds the engineer adds — operations with a typed `WeldSpec`
(fillet/butt, MIG/TIG, length, size) — are costed at the rate set's hourly `welding` rate,
and the stage rate is not charged as well. Weld time is `ceil(size / mm_per_pass)` passes ×
length / travel speed, divided by an arc-on factor, from `welding_*` time assumptions that
default to None: the seeded profile times no weld, and a profile that publishes them does. A
timed stage with no committed weld time is refused (`422 timed_stage_not_costed`) rather than
priced at zero. A weld line records `system_time_operation_type = "welding"` and no
`system_suggested_operation`, because the system estimated its time but never proposed it.

### Processing time, the plan, and the feedback record

**What reaches the cost is one test.** `ManufacturingPlan.counts_toward_cost` — active, in the
chosen route, not left as formed, and carrying a committed time — is read by
`to_operation_requirements`, the time summary and `operation_feedback` alike, so the feedback
record can no longer call an operation costed that the route or the blank left out. On a
decided cast- or forged-then-machined route, `as_formed_feature_types` (from the
classification) leaves the blank's blends as formed by default, and `PlannedOperation.
user_machined` overrides it per row; on any other route nothing is as formed.

**Turned parts.** `features/rotational.py` is the one rotational test (shared by the
suggestions, the classifier and the bar proposal) and measures the largest diameter and the
length along the axis. On a rotational part the plane rule proposes facing (turning), timed by
`_facing`. `raw_stock.py` imports only the feature models: it proposes a `BarStock`, records the
engineer's answer beside it, and computes a volume — never a mass or a price. `/cost/cad-part`
passes a *confirmed* bar's volume to `calculate_cad_part_cost(material_stock_volume_mm3=…)`,
which runs the unchanged mass calculation on it and costs material from that, reporting
`stock_mass` beside the untouched `mass`. Turning passes use `stock_diameter_mm` when the
estimate request carries one.

**Stepped drilling.** `operation_time.drilling_steps` turns a hole diameter into the drills
used, smallest first, from three `TimeAssumptions` parameters (threshold, pilot, max step
ratio); a stepped hole's time is one full-depth pass per drill. The estimate carries every
offered sequence in `drilling_options`, each with its own time, so an engineer's choice
(`PlannedOperation.user_drilling_steps`, beside the frozen `system_drilling_steps`) is costed
with a time the system computed — `system_cycle_for_choice` — or, for a sequence it never
offered, with none. `system_suggested_cycle_time` keeps meaning "the suggested sequence's
time", which is why feedback, quotation and assembly code read it unchanged. `plan.js`
mirrors `system_cycle_for_choice`; change one, change both.

Step 20 added the first module that produces a *time*, which is a much stronger claim than an
operation and is therefore hedged much harder.

**Times are separate from money, and from rates.** `operation_time.py` imports no costing
arithmetic. `TimeAssumptions` — feeds, speeds, approach allowances, depths of cut — is deliberately
a different model from `RateSet`: assumptions decide duration, rates decide cost, and letting one
model carry both would mean editing a feed rate silently changed a price. Every constant lives in
that model and every one that affected a number is echoed back in the estimate's `assumptions`;
there are no unexplained numbers in the code.

**A time is never invented.** Missing dimensions, an operation with no formula, or units that were
never established all yield `needs_user_input` with the gap named, and the times come back `None`
rather than `0` so nothing downstream can add a missing time into a total. The units case matters
in practice: a faceted export's dimensions are "as written in the file", and guessing between
10 mm and 0.01 m is a factor-of-1000 error in a price.

**A time is never confident.** Estimates are capped at `medium` regardless of how cleanly the
geometry was measured, because the parameters describe a plausible shop rather than the user's.
They fall to the confidence of the operation suggestion when that is lower, and can never exceed
it.

**Feature type and subtype are part of the operation, not looked up.** Dimensions alone do not
decide which formula applies: a drilling time needs the subtype to know whether the drill breaks
through, and a milling time needs the type to tell a blend (followed along its length) from a face
(swept over its area). `PlannedOperation` carries both so it can be re-estimated on its own.

**The plan is where a proposal becomes a commitment — and only there.**
`manufacturing_plan.py` resolves what gets costed in one direction: the engineer's typed value,
else the system's value *if `system_time_accepted`*, else nothing. Generating an estimate does not
set that flag; a person does. Changing an operation's process clears it, because a time estimated
for milling says nothing about turning. `to_operation_requirements` omits anything without a
committed cycle time and `uncosted_operations` names them, so the omission is visible rather than
silent. The same three-branch resolution is implemented in `plan.js`, so what the table shows and
what the backend costs cannot disagree.

**The system's proposal is never overwritten**, in the plan or in storage. Edits go to the `user_*`
fields beside the untouched `system_suggested_*` ones. `operation_feedback.py` projects the pair
into one record per operation, persisted in the estimate's `plan_snapshot`. Nothing is trained on
it and no recorded correction changes a later prediction — the estimator is a fixed set of
formulas. The data exists because a learning step is only possible if corrections were recorded
while they happened, and predictions that were silently overwritten are gone for good.

**Sequence is recorded, not computed with.** `sequence_position` is an explicit property that
affects no number: no sequence-dependent machining time, no setup consolidation, no routing
optimisation, and no enforced precedence — the system does not claim drilling must precede milling,
because a universal rule invented here would be wrong somewhere. A test costs a plan forwards and
backwards and asserts the same numbers.

**Persistence reuses the existing snapshot architecture.** `plan_snapshot` is one nullable JSON
column added by revision `0003`: a plan is written once with its estimate, never updated in place,
and always read whole, so child tables would add joins and a partial-update path for something
immutable. A later edit produces a *new* estimate with its own snapshot rather than rewriting an
old one.

### Time profiles: making a prediction explainable, and keeping it that way

Step 21 moved the cutting parameters out of hard-coded defaults into a versioned catalog.
`time_profiles.py` is deliberately a mirror image of `rate_sets.py` — same identity shape, same
immutability, same validate-then-publish flow — because the two answer structurally identical
questions about different things:

```
Rate set      = money       what an hour costs
Time profile  = duration    how long the work takes
```

**They are never merged.** A test asserts the two models share only the fields every versioned
thing has (`name`, `version`, `description`, `is_illustrative`), and that a time profile has no
currency at all. Merging them would mean editing a feed rate silently changed a price, and would
collapse two questions a shop answers on different schedules into one.

**Where the parameters live.** `TimeAssumptions` stays in `operation_time.py`, beside the formulas
that read it; `TimeProfile` is that model plus an identity, a version and a name. Same relationship
`RateSet` has to `Material` and `ManufacturingOperation`. The dependency runs
`time_profiles → operation_time` and never back, so the estimator cannot reach the catalog and a
cycle is impossible. That is why `estimate_operation_time` takes its `profile` argument untyped and
reads it with `getattr`.

**Provenance is per operation, contents are per estimate.** Each predicted time carries the profile
id, version, name and the parameter values that actually entered *that* calculation — enough to
explain one line on its own, small enough not to bloat a 22-row plan. The profile's full contents
are snapshotted once on the estimate in `time_profile_snapshot`, taken from the plan's own
references rather than a separate field that could disagree with it. Publishing a later version
therefore cannot change what a stored estimate says it assumed.

**Explicit assumptions clear the attribution.** An API caller may send parameters they have not
published; the estimate then records *no* profile. Attributing a prediction to parameters it was
not made with would be worse than attributing it to nothing, so `estimate_operation_time` drops the
profile whenever explicit assumptions are given — and `build_plan` passes the profile *instead of*
its assumptions rather than alongside them, so a profile is never mistaken for an override of
itself.

**Validation refuses, and separately advises.** Out-of-range values are a `422` from
`TimeAssumptions`: the upper bounds are typo guards (a feed of 800 mm/rev is a slipped decimal
point), and a cross-field check refuses a spindle range that would pin every operation to one
speed. Values that are legal but unusual come back as advisory notes from `advisory_notes` and
still publish — the parameters belong to the person who knows the shop. The one place the model
still clamps is spindle rpm, because that clamp is part of the deterministic model rather than
input handling: `1000·Vc/(π·D)` running away as D shrinks is physics meeting a real machine limit.

**The feedback record carries the version.** Without it a correction is uninterpretable — "the
system said 20 and the engineer said 32" means nothing unless you know which feeds and speeds the
20 came from, and a later training step would be fitting to a moving target.

### Setups: setup time belongs to a fixturing, not to an operation

Until this step every operation carried its own setup time into costing, so six drilled holes
charged six setups. On a real part that multiplied the setup cost by an order of magnitude — 280
minutes instead of 10 on `model1_1.stp`. The fix is a model, not arithmetic.

```
ManufacturingPlan
 └── setups
      ├── Setup 1 ── Facing, Drill H1, Drill H2, Drill H3
      └── Setup 2 ── Drill H4
```

**A setup is a physical fixturing context**, and setup time is charged once per setup while cycle
time is charged per operation. Membership is an explicit relationship (`system_setup_id` /
`user_setup_id`) rather than something inferred from a name or a process type — a shared process
is not evidence of shared fixturing.

**The grouping is offered, never asserted.** Geometry cannot prove that two operations can be done
in one fixturing, so the default is the conservative one (everything in a single setup), marked
`system_assumed` at **low** confidence with its assumptions written out. The engineer has final
authority and their regrouping is recorded beside the system's, so *(grouped, regrouped)* survives
into `operation_feedback` — the same discipline every other decision in the plan follows.

**The setup's estimated time is the largest per-process baseline among its operations**, never their
sum and never an average: one fixturing event takes as long as its most demanding process needs, and
summing is precisely the mistake being removed. Like every other estimate it is a proposal until
someone accepts or replaces it, and an uncommitted setup charges zero and is listed as needing input.

**Where the arithmetic actually lands.** The unchanged `OperationRequirement` has no setup concept —
it carries a per-operation `setup_time_minutes` that `calculate_operation_cost` charges once per
requirement — so `to_operation_requirements` attaches each setup's whole time to the *first costable
operation in that setup* and zero to the rest. That is the one place this could look like a fudge, so
it is worth being precise: it is not a division, not an average and not an inference. The setup is a
real object with real membership and its own time; the bridge only chooses where to hang that time so
the existing formulas total it correctly. `time_summary()` and the preview endpoint report setup and
cycle totals separately, which is how the distinction stays visible rather than buried in the bridge.

**Two levels of sequence**, both explicit and both persisted: setups in their own order, operations
in theirs within a setup. Neither changes a number today — a test costs a plan with its operations
and its setups reordered and asserts the same total. Setup *membership* does change the number, which
is the point, and the data model is now shaped so a later step could make sequence matter too.

**Persistence needed no migration.** The plan already went into `plan_snapshot` whole, and setups
travel with it. A plan stored before setups existed is read back as a single system-assumed setup
holding everything, which is exactly what it already meant.

### Explaining a change: cost impact analysis

An estimate that cannot say *why* it differs from the last one is not trustworthy, however correct
its arithmetic. Step 22 answers that from the snapshots each estimate already stored, and answers it
without inventing a cause.

**The whole layer is pure and read-only.** `estimates.py` gained the analysis and still imports no
calculator and no repository — a test asserts that by reading its source. Snapshots reach it as an
`EstimateDetail` carrier that `main.py` builds, which keeps the dependency running
`repository → estimates` and never back. Nothing is recalculated, so publishing a newer rate set or
time profile cannot change a past comparison.

**The decomposition is an identity, not a model.** Most cost components are `rate × driver`, and a
two-factor variance split accounts for a change exactly:

```
rate effect   = (r_a − r_b)·d_b
driver effect = r_b·(d_a − d_b)
interaction   = (r_a − r_b)·(d_a − d_b)
```

The three sum to the delta with no remainder. Two disciplines around it matter more than the
algebra:

- The interaction is reported as its own `combined` impact rather than folded into either side.
  Folding it in would credit one input with an effect that needed both.
- The parts are checked against the whole and any remainder is *reported* — `rounding` within a
  small allowance (every stored amount is rounded to two decimals, and a dozen of them can drift
  by pennies), `unexplained` beyond it. This guard earned its place during development: it caught
  a units bug immediately, where a per-hour rate had been multiplied by minutes and every time
  effect came out sixty times too large. A silent decomposition would have shipped a plausible
  wrong answer.

**Rate and driver must genuinely multiply to the cost**, so a per-hour rate is paired with *hours*.
`driver_display` carries the minutes a person would rather read without letting them into the
arithmetic.

**Attribution counts inputs, not fields.** Each comparable field declares the input it belongs to,
because one change usually moves several fields: adding a setup moves the count *and* the minutes;
changing the costing profile moves the material price, the machine rates *and* the overhead
percentages, since it supplies all of them. Counting fields would report "multiple inputs changed"
for a single deliberate edit — technically true, practically misleading. Only when genuinely
separate inputs move does the result say `multiple_inputs`, and then it names them and shows every
before/after rather than dividing the total into causal percentages nobody could justify.

**Some differences are deliberately priced at zero.** A sequence change is reported as a change
with a zero cost impact and a note saying the current model has no sequence-dependent cost. That is
the limitation made visible rather than hidden, which is more useful than silence and keeps the
door open for a later step that does price ordering.

**Different currencies are never subtracted.** The comparison flags the mismatch, omits the money
impacts and keeps the input differences, which remain meaningful.

**The browser computes nothing.** `impact.js` formats `/estimates/compare` and
`/estimates/{id}/assumptions`; a test greps it to make sure it derives no delta of its own. This
screen is where an invented number would be most convincing and therefore most damaging.

### The 3D viewer

Two branches start from the same CAD upload and never meet again:

```
CAD file -> cad_geometry -> cad_mass -> deterministic costing
CAD file -> cad_geometry -> cad_mesh  -> browser viewer
```

Both call the same `extract_geometry_from_bytes`, so the numbers shown beside the model are the
numbers costing uses rather than a second opinion derived from the triangles. **The mesh is only
ever used to draw pixels**, and `viewer.js` contains no costing code — a test greps it for costing
identifiers.

**Parsing stays on the server.** `cad_mesh.py` converts both formats into one common
representation — a flat `positions` array and a flat `indices` array — so the browser never has to
understand STEP or B-Rep, and the viewer is identical for both formats.

**Normals are derived in the browser, not transmitted.** That halves the payload and is correct
*because* the backend deliberately does not merge vertices across surface boundaries:

- **STEP** vertices come from each B-Rep face's own triangulation, concatenated with an offset —
  shared within a face, never between faces. Derived normals therefore come out smooth across a
  curved surface and hard at a real edge, which is how a machined part should look.
- **STL** is a triangle soup with no concept of a smooth surface, so its triangles are emitted
  unmerged and shade flat. Merging them would round off a cube's corners, misrepresenting the file.

**Tessellation settings.** OpenCASCADE is asked for a linear deflection of **0.1% of the model's
bounding-box diagonal**, so fidelity scales with the part instead of being fixed in millimetres,
plus an angular deflection of **0.5 rad (≈28.6°)** to keep small holes and fillets round. The
settings actually used are reported back in the response.

**Budgets.** All six live in `limits.py` and are separately configurable by environment variable;
see the README. Uploads are capped at `MAX_UPLOAD_BYTES` (500 MB), what the viewer will *send* at
`MAX_VIEWER_TRIANGLES` (80,000 triangles), and what it will *read* at `MAX_VIEWER_INPUT_TRIANGLES`
(2,000,000). A STEP model over the send budget is re-tessellated once at 0.5% and flagged
`coarsened` — asking OpenCASCADE for fewer triangles looks better than reducing them afterwards.
Anything still over budget is decimated for display (below). Only if *that* yields nothing usable,
or the model is past the read budget, does the endpoint return the geometry with `mesh: null` and a
`mesh_error` rather than failing. A part too dense to draw must still be a part you can cost.

**Display simplification, and why it cannot touch a number.** A 200 MB faceted Creo export runs to
hundreds of thousands of polygons — too many to hand a browser, but no obstacle at all to measuring
a volume. So the budget reduces the *picture* and nothing else:

```
full-resolution CAD mesh ──→ geometry / mass / features / costing   (never simplified)
                         └──→ mesh_simplify ──→ display mesh ──→ 3D viewer   (never measured)
```

`mesh_simplify.py` implements Rossignac & Borrel **vertex clustering**: overlay a uniform grid on
the bounding box, collapse each cell's vertices to their centroid, drop triangles that collapsed to
a line or point. It was chosen for being *deterministic* (no randomness or hash-order dependence, so
a quotation screenshot is reproducible), *linear* (one pass per grid resolution, where quadric edge
collapse would need a priority queue over every edge), and *bounded* in memory. The grid resolution
that best fits the budget is found by bracketing and bisection, and the search never returns a mesh
over budget: if none fits, it returns nothing and the caller reports the preview as unavailable.

The quarantine is structural, not a convention. `cad_mesh._finish` is the single place every source
format converges and therefore the only place decimation happens, and it runs *after* the caller has
already measured the full-resolution mesh. The costing modules do not import `mesh_simplify` at all
— a test asserts that by reading their source. Two further tests pin the behaviour directly: the
`geometry` block is identical whether or not the preview was simplified, and the same file costs the
same at any display budget. The response reports `display_mesh_simplified`,
`original_triangle_count`, `display_triangle_count`, `simplification_method` and
`simplification_ratio`, and the UI shows the original count beside the display count so a reduced
number is never read as the model's own.

Corners can be pulled inward by up to half a cell, and detail below the cell size is lost. That is
acceptable for a preview and would not be for a measurement, which is exactly why it is only ever
used for one.

**Selected features are marked, not measured.** Since Step 19 the viewer can put a dot at a
recognized feature's location and a line along its axis, so a row in the features table and a spot
on the part are visibly the same thing — and so an operation suggestion can point at the feature
that caused it. The coordinates are the backend's own reported `location` and `axis`, used verbatim
with no scaling: feature coordinates already share the mesh's coordinate space, because for an
analytic STEP both come from the same OpenCASCADE shape and for a faceted export both come from the
same polygons. Nothing is read back out of the marker, and no engineering value is derived from it.
**Face-level highlighting is not implemented**: the wire mesh concatenates every face into one
buffer with no per-face id, so lighting up exactly the faces of a feature would need a face-index
attribute the mesh does not carry. A point-and-axis marker was the honest version of this that fits
the data available.

**three.js is vendored, not loaded from a CDN** (`static/vendor/`, MIT, r169, ES-module build
imported by relative path). The rest of the project runs offline once installed, and a CDN
reference would make the viewer fail behind a corporate proxy in a way that looks like a bug.
There is still no build step. The camera controls are hand-written rather than pulling in
`OrbitControls`, because fit-to-view, reset, and keyboard support would have to be written on top
of it anyway.

**Known limitation:** there is no JS test runner in this project, so the rendering itself is not
covered by automated tests. `tests/test_cad_viewer.py` covers everything up to the browser —
including a divergence-theorem check that the mesh encloses the correct volume, which catches
inverted winding — and `tests/test_display_simplification.py` covers the decimation and its
quarantine from every engineering number. The rendering itself was verified live in a real browser
(see `PROJECT_STATUS.md`).

### The costing setup screen

`static/setup.js` turns the existing authoring endpoints into a screen a non-developer can use. It
adds no new costing concepts: a "costing profile" *is* a rate set, and a profile version *is* a
`(rate_set_id, version)` pair. The wizard collects General / Materials / Processes / Additional
costs, then publishes through `POST /rates` or `POST /rates/{id}/versions` unchanged.

**The draft lives only in the browser.** There is deliberately no server-side draft state: an
abandoned edit leaves nothing behind, and there is exactly one kind of stored rate set — a
published, immutable one. Validation happens before persistence, so a half-finished profile can
never become a published version.

**Immutability is preserved by construction.** No `PUT`, `PATCH` or `DELETE` exists on any rate
route, and the UI offers no edit path. Changing rates means "Create new version", which reads the
previous version and pre-fills the form from it so only the change has to be typed. Tests assert
both the absence of mutating routes and the absence of mutating verbs in `setup.js`.

**Validation is mirrored, not duplicated.** `setup.js` checks the same field rules next to the
field that caused the problem, but it decides nothing: `POST /rates/validate` re-checks the whole
candidate with the same `RateSet` model plus the stored catalog, and the publish endpoints re-check
again. The browser never computes rate-set logic — it asks.

**Materials and processes are edited as lists, sent as maps.** The wire format keys materials by
lowercase name and processes by lowercase process type, which makes duplicates structurally
impossible but also makes two same-named rows collapse silently. The editor therefore holds rows in
an array, where a duplicate is visible and can be reported against the row that caused it, and
folds them into the map only at publish time.

Process types are not a fixed list. The model already keys operations by an arbitrary string, so a
shop's own processes — broaching, honing, shot peening — work end to end; the datalist in the form
only suggests common ones.

### The profile is the source of material data

When a rate set is selected it supplies **density as well as price**. `calculate_cad_part_cost`
resolves the material from the rate set and injects it into `calculate_mass_from_geometry`, the
same dependency inversion the API layer already uses to hand a resolved `RateSet` to the pure
part-cost calculator. With no rate set the built-in catalog in `materials.py` is still the
fallback, so nothing about the no-profile path changed.

This closed a real gap: mass previously always resolved density through `materials.py`, so a
self-authored profile's materials could not be costed at all, and a profile that redeclared a
material's density had that density silently overruled while its price was honoured. The mass
formula itself is unchanged — only where the density comes from.

### Authoring rate sets

Rate sets are created through `POST /rates`, or `POST /rates/{rate_set_id}/versions` when the id is already known. Both routes funnel into one `_create_rate_set` helper, so validation, the duplicate rule and the read-back exist once; the second route differs only in taking the id from the path, which is why `RateSetVersionCreate` exists (a `RateSet` minus `rate_set_id`, sharing the same constraint constants, with a test asserting the two stay in sync).

No new persistence was needed. `RateSetRepository.add` already validated identity, rejected duplicates with an immutability message, and inserted — so the repository is unchanged, and the endpoint simply calls `add` then `get` to return exactly what was stored. **No schema change was required either**, so no Alembic revision was added; head remains `0002`.

Immutability is structural rather than enforced by a check in the handler:

- the unique `(rate_set_id, version)` constraint rejects a duplicate at the database level;
- `RateSetRepository` exposes no update, delete or upsert method at all;
- there is no `PUT`, `PATCH` or `DELETE` route for rate sets, so those verbs return 405.

A re-post of an existing version therefore returns **409 Conflict** — the request is well-formed, it just cannot be honoured — and the stored rates are provably untouched. Because every estimate persists its own `rate_set_version` and `rate_set_snapshot`, publishing a new version cannot retroactively change an existing estimate.

`is_illustrative` keeps its default of `true`, so an authored rate set is presumed to be a placeholder unless the author says otherwise. The flag propagates into every estimate's provenance stamp, which is what lets a reader distinguish a demo figure from a real quotation.

### Money: currency and presentation

`money.py` holds the whole policy. **Currency**: a 3-letter code on every rate set, defaulting to **INR** for this POC; an estimate is denominated in its rate set's currency, an estimate without one uses the default, and there is no conversion anywhere. **Rounding**: computed cost amounts are presented and stored at 2 decimal places, half-up (`Decimal.quantize`, since Python's `round` is half-to-even).

The mechanism matters more than the numbers. `Money` is a pydantic `PlainSerializer` annotation, not a distinct numeric type: a field annotated `Money` holds the **exact** float in memory and is rounded only when the model is serialized. Python-level arithmetic therefore always reads full precision, which makes it *structurally impossible* to round an intermediate and feed it into a later calculation. No costing formula changed to accommodate this.

Two deliberate consequences:

- Each amount is rounded from its own exact value, so displayed components can differ from a displayed total by a penny. The alternative — rounding components and summing them — would report a total that is not the rounded true total.
- Rounding covers computed cost *amounts*, not echoed input rates. A rate such as 0.125/kg would be distorted by 2-decimal rounding, so rates are reported as configured.

### Per-unit cost

```
per_unit_cost = total_part_cost / part.quantity
```

The batch total already accounts for quantity, so dividing it back out is the *only* place quantity is undone — it is never applied twice. One-time batch costs (operation setup, tooling) are amortised across the batch by this division, which is why per-unit cost falls as quantity rises.

### Additional cost formulas (overhead, scrap, tooling)

```
direct_cost_base      = total_material_cost + total_operation_cost
overhead_cost         = overhead_rate_percent / 100 * direct_cost_base
scrap_cost            = scrap_rate_percent / 100 * direct_cost_base
tooling_cost          = tooling_cost_per_batch
total_additional_cost = overhead_cost + scrap_cost + tooling_cost
```

Both percentages apply to the *same* `direct_cost_base`, which deliberately excludes overhead, scrap, and tooling. That single choice is what prevents compounding: overhead is never charged on scrap, scrap never on overhead, and neither on tooling. Each component is computed once, from the same base, and stays separately visible in the result rather than being folded into the total.

Input bounds are validated at the domain boundary:

| Input | Bound | Why |
|---|---|---|
| `overhead_rate_percent` | `[0, 1000]` | Pragmatic guard, not a business rule — the upper cap catches a fraction (`0.15`) passed where a percent (`15`) was meant. |
| `scrap_rate_percent` | `[0, 100)` | Principled — at 100% scrap no good parts are produced, so a cost per part is meaningless. |
| `tooling_cost_per_batch` | `>= 0` | A negative one-time cost is not meaningful. |

All three default to zero, so an estimate that does not ask for these components is identical to one produced before they existed.

Scrap is modelled as a **linear allowance** on direct cost, not as a yield-based uplift of `1 / (1 - scrap_rate)`. The linear form is simpler to explain and test; it understates cost at high scrap rates, which is documented rather than hidden.

### Part cost formula and quantity semantics

```
total_part_cost = total_material_cost + total_operation_cost + total_additional_cost
```

`total_material_cost` and each operation's `total_operation_cost` are *each already scaled for the full batch quantity* by their own calculations (see above). To avoid double-counting quantity at the part level, `calculate_part_cost` requires every operation's `quantity` to equal the part's `quantity` (raising an error otherwise) and then simply sums the already-scaled totals — it never multiplies by quantity again.

The additional components are batch-level for the same reason: the percentage components inherit batch scaling from `direct_cost_base`, and `tooling_cost_per_batch` is a one-time cost for the whole batch, exactly like operation setup cost. Neither is re-scaled by quantity. Every figure in a `PartCostEstimateResult` is therefore a batch total.

### Mass-from-geometry formula and unit handling

```
volume_m3 = source_volume * (meters_per_unit ** 3)
mass_kg   = volume_m3 * material.density_kg_m3
```

Supported `source_unit` values and their linear meters-per-unit (cubed for the volume conversion): `mm` → 0.001, `cm` → 0.01, `m` → 1.0, `inch` → 0.0254.

`source_unit` is always supplied by the caller and is never inferred from geometry — in particular, never assumed to be millimetres. What differs per format is whether the *file* settles the question, which `GeometryProperties.detected_unit` records:

- **STL** embeds no unit information whatsoever, so `detected_unit` is `None` and the caller's `source_unit` is authoritative.
- **STEP** embeds unit metadata, and OCCT's reader reads it and converts the geometry to millimetres while loading (its target unit `xstep.cascade.unit` defaults to `MM`). A STEP file authored in inches therefore arrives as millimetres. `detected_unit` is `"mm"`, and `calculate_mass_from_geometry` **rejects** a `source_unit` that contradicts it. That guard matters: silently accepting `inch` for already-millimetre geometry would scale the mass by 25.4³ ≈ 16 387.

The resulting `mass_kg` is for **one instance of the geometry** (per part); it is not multiplied by quantity. Quantity is applied exactly once, downstream, inside the existing `calculate_material_cost`.

## Project layout

```
costing-agent/
├── src/
│   └── cad_costing_agent/
│       ├── main.py               # FastAPI app + routes
│       ├── part.py               # Part domain model
│       ├── materials.py          # Material model + illustrative material dataset
│       ├── costing.py            # deterministic material cost calculation
│       ├── operation.py          # ManufacturingOperation domain model
│       ├── operation_costing.py  # deterministic operation cost calculation
│       ├── additional_costs.py   # deterministic overhead / scrap / tooling components
│       ├── rate_sets.py          # rate set models, seed data + resolvers (no formulas, no DB)
│       ├── db.py                 # engine, configurable URL, injected sessions, migrate/seed
│       ├── migrations/           # Alembic env.py + versioned revision scripts
│       ├── db_models.py          # SQLAlchemy ORM rows (storage only)
│       ├── money.py              # currency + 2dp rounding policy (presentation only)
│       ├── static/               # the browser UI: index.html, app.js, styles.css,
│       │                         #   viewer.js (3D viewer), setup.js (costing setup)
│       │                         #   + vendor/ (three.js, MIT)
│       ├── cad_import/           # the adapter registry: one place formats are identified
│       │   ├── model.py          #   CADDocument, capabilities, components (no library types)
│       │   └── adapters.py       #   STEP, STL, IGES, and Creo prt/asm/drw (declared, refused)
│       ├── cad_assembly.py       # STEP product structure via XCAF: definitions,
│       │                         #   occurrences, hierarchy, placements (no library types out)
│       ├── assembly_costing.py   # each component's contribution x quantity, rolled up;
│       │                         #   never counts an unknown as zero
│       ├── feedback_analytics.py# corrections read back as a report; imports nothing at
│       │                         #   all, so it cannot change a rule, a profile or an estimate
│       ├── assembly_plan.py     # one ManufacturingPlan per manufactured component; imports
│       │                         #   only the planner and the classification vocabulary
│       ├── quotation.py         # a stored estimate as a printable document; imports only
│       │                         #   `estimates` and `money`, and calculates nothing
│       ├── route_comparison.py  # route comparison preview models + selection order; imports
│       │                         #   only `money`; prices nothing itself (main → price_cad_part)
│       ├── bulk_preview.py      # bulk preview CSV parsing, row models, CSV export; imports
│       │                         #   nothing from the package; nothing it handles is stored
│       ├── dfm.py               # advisory DFM flags against a versioned, cited DfmProfile;
│       │                         #   imports only `features.model`; never reaches a cost
│       ├── cost_actuals.py      # a client's actual cost vs the estimate, per unit; imports
│       │                         #   only `money`, and no costing module reads it
│       ├── component_classification.py
│       │                         # manufactured / purchased / standard / excluded /
│       │                         #   unresolved; imports nothing at all, and never infers
│       │                         #   make/buy from a name, a shape or a material
│       ├── exploded_view.py      # deterministic display-only arrangement; imports nothing
│       │                         #   from this package, and nothing but main.py reads it
│       ├── cad_iges.py           # IGES: units, and whether its surfaces enclose a solid
│       ├── cad_mesh.py           # CAD -> renderable triangle mesh (visualization only)
│       ├── operation_suggestions.py  # features -> suggested operations (suggestions only:
│       │                         #   no time, no rate, no cost; never imported by costing)
│       ├── process_classification.py # manufacturing family + route, recommended and
│       │                         #   confirmed; says which stages need stage rates, prices nothing
│       ├── stage_costing.py      # non-machining route stages priced from the rate set's
│       │                         #   stage rates; imports only `money` and `rate_sets`
│       ├── operation_time.py     # deterministic machining-time formulas (times only,
│       │                         #   never money; needs_user_input rather than a default)
│       ├── time_profiles.py     # versioned, immutable machining assumptions (no currency)
│       ├── manufacturing_plan.py # the editable plan: system proposal vs user decision,
│       │                         #   sequence, and what carries a committed time
│       ├── operation_feedback.py # predicted-vs-chosen pairs, persisted, never trained on
│       ├── mesh_simplify.py      # deterministic display-mesh decimation (display only,
│       │                         #   never volume/mass/area/features/cost)
│       ├── limits.py             # upload / analysis / viewer budgets, separately configurable
│       ├── cad_faceted.py        # polygon parsing, welding, closure and mesh volume (no OCP)
│       ├── features/             # deterministic feature recognition (analysis only)
│       │   ├── model.py          #   the vocabulary; no OCP, no costing
│       │   ├── step_text.py      #   classify brep vs faceted without loading the file
│       │   ├── topology.py       #   the single B-Rep scan; the only OCP importer
│       │   ├── grouping.py       #   coaxial cylindrical faces -> one cylinder
│       │   ├── faceted.py        #   polygon parser for faceted STEP (no OCP)
│       │   ├── mesh_recognition.py #  features fitted back out of polygons
│       │   └── recognizers/      #   one small deterministic rule per file
│       ├── repository.py         # ORM <-> domain translation; the only DB access layer
│       ├── estimates.py          # history/comparison models + delta logic (pure, no DB)
│       ├── part_cost.py          # part-level estimate (material + operations + additional)
│       ├── cad_geometry.py       # STL + STEP geometry extraction (not connected to costing)
│       ├── cad_mass.py           # geometry volume + material density -> mass (per part, no cost)
│       └── cad_part_cost.py      # composes cad_mass + the existing part_cost (no formulas duplicated)
├── tests/                   # pytest test suite
├── pyproject.toml           # project metadata + dependencies
├── README.md
├── PROJECT_STATUS.md
└── ARCHITECTURE.md
```

## Explicitly out of scope for now

The following are intentionally not implemented yet and should be added incrementally in later steps, not all at once:

- CAD file parsing for formats other than STL and STEP (IGES, Parasolid, and Creo native `.prt`/`.asm` — a STEP reader does **not** read Creo's proprietary format)
- Automated STEP export from Creo (producing the STEP file is currently a manual step)
- Assembly-aware STEP handling (a multi-solid STEP is reduced to one aggregate shape; per-solid breakdown is not modelled)
- Feature recognition, machining-time prediction from geometry, or automatic operation/process selection
- AI/agent orchestration
- Creo integration
- Windchill integration
- Manufacturing route generation/optimization (choosing or sequencing alternative operations for a part)
- Multi-part assemblies / BOM handling
- Engineering and inspection cost (overhead, scrap allowance, and tooling now exist; these do not)
- Yield-based scrap modelling (`1 / (1 - scrap_rate)`) — scrap is currently a linear allowance
- Real/customer material or rate pricing — current rates are illustrative placeholders
- Production-grade persistence: schema changes are now migrated with Alembic, but there is still no backup/retention policy, no auth, and no concurrency story beyond SQLite's own
- Data migrations (Alembic handles schema; no revision back-fills or transforms existing rows)
- Any write API for rate sets — they are seeded from code and read-only over HTTP
- Frontend/UI
- Authentication, deployment, CI/CD

## Design principles going forward

- Keep the domain logic (costing) decoupled from the API layer and from any CAD/PLM integration, so each can be developed and tested independently.
- Prefer deterministic, explainable calculations for costing — avoid opaque logic where a simple formula or rule suffices.
- Add infrastructure (database, auth, deployment) only when a concrete feature needs it.
