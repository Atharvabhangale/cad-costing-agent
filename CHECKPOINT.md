# Project Checkpoint — CAD Costing Agent

Written 2026-09-07, at a deliberate pause after Step 30. Every figure below was
re-verified against this repository and its real database on that date; the
verification method is recorded in section 10. Nothing is committed — the
repository has no commits and every file is untracked, as it has been throughout.

Companion documents: `README.md` (what it does, 2467 lines), `ARCHITECTURE.md`
(why it is built this way, 1652 lines), `PROJECT_STATUS.md` (the step-by-step
record and the "What has NOT been done" list, 1219 lines), `CLAUDE.md` (the rules
a future session must follow, 556 lines). This file is the short way in.

---

## 1. Completed roadmap steps

Steps **1 through 30** are complete, including the sub-steps 18.5, 18.6, 18.7,
18.8, 18.9 and 21b. `PROJECT_STATUS.md` carries each one with its real test
counts and live-verification numbers.

The arc in brief: costing arithmetic and a rate catalog (1-12) → CAD geometry,
mass and the viewer (13-18.9) → feature recognition, operation suggestions and
machining-time estimation (19-21b) → format capabilities and assemblies (22-25) →
component classification and assembly roll-up (26) → quotations (27) →
per-component manufacturing plans (28) → feedback analytics (29) → evidence
completeness (30).

Step 31 has **not** been started, scoped or justified.

## 2. Current architecture

FastAPI. `main.py` is a thin HTTP layer that reads uploads, resolves a rate set
from the database and calls pure domain functions; all arithmetic lives in
modules that take values and return values. Dependencies run one way —
`main.py` → `repository.py` → domain — and the domain never reaches back.

- **34 domain modules** in `src/cad_costing_agent/`, plus three subpackages:
  `cad_import/` (3 files, format detection and capability declarations),
  `features/` (6 files, recognition), `migrations/` (Alembic).
- **44 HTTP endpoints**, of which the 8 under `/feedback/*` are all `GET`.
- **Frontend**: plain static files under `static/`, no build step. 12 classic
  scripts sharing globals in a significant load order, plus `viewer.js` as an ES
  module and a standalone `quotation.html`/`.css`/`.js` report page that shares
  nothing with the shell. The estimate page is **six sections** — Overview, CAD,
  Manufacturing, Cost, Assembly, Quote — routed by `estimateflow.js` on a
  `data-flow` attribute; the engineering-heavy views sit outside that flow under
  History, Rate sets, Time profiles and Engineering.

The central design constraint is that one CAD upload fans out into branches that
must never meet, and **structural tests enforce it by reading module source**:
`cad_geometry → cad_mass → costing` is the only path to money; the viewer, the
recognizers, the suggestions and the time estimates are separate branches;
`manufacturing_plan` is the sole bridge from a plan to a cost. Several modules
are pure by assertion — `component_classification.py`, `feedback_analytics.py`
and `exploded_view.py` import **nothing** from the package, `quotation.py`
imports only `estimates` and `money`, `assembly_plan.py` only
`manufacturing_plan` and `component_classification`. Those empty import sets are
the proof that analytics cannot write, a document cannot recalculate, and an
exploded position cannot reach a cost. `CLAUDE.md` states each boundary and
which test fails by name if it is crossed.

## 3. Database and migration state

| | |
|---|---|
| Engine | SQLite, `./cad_costing_agent.db` (7,159,808 bytes = 1748 pages x 4096) |
| Configured by | `CAD_COSTING_DATABASE_URL`, default `sqlite:///./cad_costing_agent.db` |
| Alembic head on disk | `0005_add_quotations.py` |
| `alembic_version` in the real database | **`0005`** — in step, no pending migration |
| Migrations | 5, `0001` to `0005`. **Step 30 added none** |
| Tables | 5: `estimates`, `rate_sets`, `time_profiles`, `quotations`, `alembic_version` |
| Rows | estimates **132**, rate_sets **8**, time_profiles **7**, quotations **2** |
| Integrity | `PRAGMA integrity_check` = **ok**; estimate ids 1-132 contiguous, none deleted |

Startup `run_migrations` distinguishes an empty database from an
already-Alembic one from a pre-Alembic one (which is *stamped*, not migrated), so
adopting Alembic could not lose data. `alembic.ini` deliberately does not set
`sqlalchemy.url`, so the CLI cannot drift from what the app uses.

## 4. Test count and baseline

| | |
|---|---|
| Collected | **1724** |
| Result | **1723 passed, 1 skipped** |
| Baseline before Step 30 | 1667 passed |
| Baseline before the UI restructuring | 1716 passed |
| Baseline before the Manufacturing polish | 1721 passed |
| Test files | 48 |
| Full run | **427 s** measured 2026-09-07 (varies with machine load) |

Step 30 added 49 tests in `tests/test_evidence_completeness.py` and updated two
Step 29 tests the new semantics made stale.

Beyond behaviour the suite contains **structural tests** that read source to
assert import boundaries and forbidden idioms, and **determinism tests** that run
the same input twice and compare, excluding only `elapsed_ms`.

## 5. Important implemented capabilities

- **CAD input**: STL, STEP (analytic B-Rep *and* faceted) and IGES (solid and
  surface mode). `cad_import` is the only place a file extension is read, and it
  declares eight capabilities at four levels; a capability below `supported`
  cannot be declared without a stated reason — the validator refuses it.
- **Geometry and mass** from one entry point,
  `cad_geometry.extract_geometry_from_bytes`, so the numbers beside the 3D model
  are the numbers costing uses.
- **Assemblies**: STEP product structure via `STEPCAFControl_Reader`, with the
  definition/occurrence distinction kept throughout; per-component measurement,
  a bill of materials, an exploded *display* view and an assembly mesh sent as
  definitions plus placements.
- **Feature recognition** on analytic B-Rep and, by fitting, on faceted
  geometry — refused for STL by design.
- **Operation suggestions**, **machining-time estimation** from versioned
  immutable time profiles, **setup-grouped manufacturing plans** per component,
  and costing through the single `manufacturing_plan` bridge.
- **Component classification** (manufactured / purchased / standard / excluded /
  unresolved) driving an assembly roll-up that refuses to fake a total.
- **Quotations**: a printable customer or engineering document that presents a
  stored estimate and cannot calculate one.
- **Rate sets and time profiles**: two versioned, immutable catalogs, seeded at
  startup, both flagging illustrative defaults.
- **Feedback and correction analytics**: eight read-only endpoints over the
  recorded prediction/decision pairs.

## 6. Important limitations

- **Native Creo `.prt`/`.asm`/`.drw` are recognized and refused with an
  explanation, not supported.** The formats are proprietary and the installed
  OpenCASCADE has no reader. Parasolid, ACIS, JT and NX native are in the same
  position. Do not add a parser.
- **No drawing information of any kind** — no views, dimensions, tolerances or
  PMI are read from any format, STEP included.
- **No learning anywhere.** No model is fitted, no rule updated, no time
  assumption adjusted, no confidence recalibrated. A recorded correction never
  changes a later prediction.
- **No assembly manufacturing.** Components are separated, measured, classified,
  planned individually and rolled up, but there is no assembly labour, no
  fastening or joining time and no assembly-level routing or sequence.
- **All rates and time parameters are illustrative placeholders**, labelled as
  such. No supplier lookup, no market rates, no parts database.
- **No currency conversion.** More than one currency in an assembly is reported
  as not aggregatable.
- **Not production-grade**: no auth, no CI/CD, no deployment, no backup or
  retention policy, no concurrency handling beyond SQLite's own.
- INR is the default currency; **USD must not appear as a default anywhere**.

`PROJECT_STATUS.md`'s "What has NOT been done" list is the exhaustive version and
runs to about sixty entries.

## 7. Step 29 — feedback analytics state

Complete. `feedback_analytics.py` normalises the stored prediction/decision pairs
and reports them through eight `GET` endpoints (`/feedback/summary`,
`operations`, `times`, `setups`, `classifications`, `confidence`, `records`,
`evidence`), with a seven-sub-view UI panel, filters, pagination and drill-down.

Its design is about what it cannot do:

- It **imports nothing from the package**, and three tests hold that — the empty
  import set, that no module but `main.py` mentions it, and that its source
  contains no `session`, `Repository`, `save`, `commit` or `fit(`.
- Snapshots are read with `.get()` on plain dicts, never validated into today's
  models, because a default that fills an absent field reads exactly like
  recorded data.
- **An acceptance denominator is the operations someone decided about.** An
  operation nobody committed a time for was never finished, and is excluded and
  counted separately. Verified today: 382 accepted of **418** resolved, **486**
  excluded, and 418 + 486 = 904 exactly. Counting the unfinished ones had made a
  rule with one correction read as 8% acceptance instead of 89%.
- Medians carry every headline; outliers are listed apart and still counted;
  relative error skips predictions below a floor. `SampleGuard` sits on every
  metric needing a sample, with `CAD_COSTING_MIN_FEEDBACK_SAMPLE` (currently
  **8**) as an explicitly non-statistical floor — below it the response prints a
  sentence, not a number.
- Confidence is reported as an association, never as calibration, and a
  `LEARNING_DISCLAIMER` is on every response.

## 8. Step 30 — evidence-completeness state

Complete. Step 29's readiness report counted every empty field as a gap. Step 30
established that this is one question too few, and each evidence field is now
classified into exactly one of four states, which are exhaustive and sum to the
total (asserted on the real data):

    known           the value was recorded
    not_applicable  it cannot exist for this record
    unrecorded      it could have existed, and something persisted could recover it
    unrecoverable   it was never captured and nothing stored can recover it

Measuring the real database showed **two of Step 29's three headline gaps were
not gaps**. Every record "missing" a component identity came from a single-part
estimate, where a component identity does not exist. All but one of the
"missing" paired cycle times were operations nobody committed a time for — which
Step 29 itself requires to stay unpaired. A fourth field, `time_profile_version`,
had been reported as 198 permanently unknown; all such records had no
`time_profile_id` either, meaning the plan ran under the built-in baseline, which
has no published version to have lost.

**The one real gap was in the browser.** `plan.js` builds the plan payload in
JavaScript rather than calling `build_plan`, so a field it does not copy never
reaches the server — which is why every stored record lacked a feature confidence
the backend was populating correctly. `plan.js` now carries
`system_source_type` and `system_feature_confidence` (verified present at
`static/plan.js:83-84`), and the display-only `_source_type` duplicate was
removed rather than left as a second source of truth.

Applicability is read from the snapshot's shape, never guessed:
`component_identity_applicable` is set only for records from an assembly's
per-component feedback map, and `percent_known` divides by `applicable`, never by
the total. No migration was needed — all three fields live in JSON snapshot
columns that already existed.

## 9. Exact remaining evidence limitations

Recomputed from the real database on 2026-09-07 (132 estimates → 904 operation
records, 258 classification records):

| Evidence field | Known | Applicable | Known % | Not applicable | Unrecorded | Unrecoverable |
|---|---|---|---|---|---|---|
| `feature_confidence` | 191 | 903 | **21.2%** | 1 | 0 | **712** |
| `component_identity` | 448 | 448 | **100%** | 456 | 0 | 0 |
| `paired_cycle_time` | 412 | 412 | **100%** | 492 | 0 | 0 |
| `rule_id` | 903 | 903 | **100%** | 1 | 0 | 0 |
| `source_type` | 903 | 903 | **100%** | 1 | 0 | 0 |
| `time_profile_version` | 698 | 698 | **100%** | 206 | 0 | 0 |

`feature_confidence` at 21.2% is the Step 30 fix working: the 712 unrecoverable
records are all pre-fix, and every operation written since carries the value —
191 known, up from 7 when Step 30 was reported. It rises as new estimates are
made and can never rise for the old ones.

- **Permanently unknown: 712.** These are historical feature confidences. The CAD
  file is not stored and no snapshot holds the recognizer's output, so nothing
  can re-derive them. This is a limitation, not a backlog, and it must never be
  filled with an estimate.
- **Safe backfill available: 0.** Every deterministically recoverable value was
  already recovered by Step 29's plan join. The only such recovery — taking a
  component definition id from the feedback map's own key — is already in place.
- **One record** is a hand-added operation with no feature identity and no system
  operation. That is correct: somebody typed it in.
- Time pairing across all 904 records: **412 paired, 491 unfinished, 1 not
  predicted**.
- The dataset is complete where a value can exist, but **small**. Every
  correction it holds, counted today: **382 accepted**, 20 `cycle_time_changed`,
  14 `setup_time_changed`, 10 `setup_changed`, **4 `operation_changed`**, 1
  `added`, and of 258 classification pairs **0 changed**. Better evidence quality
  is not more evidence, and the sample guards exist to keep saying so.

## 10. Real-database verification status

The section 3 and section 9 figures were recomputed on 2026-09-07 by reading the
real database through a **`sqlite3` `mode=ro` URI** — no SQLAlchemy engine, no
session, no startup hook, so no migration, seed or write could run — and feeding
the decoded snapshots into `feedback_analytics.extract_records`, which is
duck-typed. Results: 132 estimates, 904 operation records, the six-field table
above, four states summing to the total on every field, `permanently_unknown` =
712, `safe_backfill_available` = 0, and the acceptance identity 418 + 486 = 904.
`PRAGMA integrity_check` reports **ok** and estimate ids 1-132 are contiguous, so
nothing has been lost.

**One unexplained observation, recorded rather than smoothed over.** The first
read in this checkpoint pass saw **128** estimates and a 6,033,408-byte file; a
later read of the same path in the same pass saw **132** and 7,159,808 bytes, and
the file's mtime had moved to 15:31:16. The four extra rows (ids 129-132) carry
`created_at` values of 09:49-10:01 that morning, rate set `poc-machine-shop
v96.0.0` and real part and assembly names, so they are **live-verification
records from earlier in the day, not test output** — the suite runs against
in-memory databases and could not produce `DIFFERENTIALASSEMBLY_2_ASM`. The most
plausible reading is a leftover application process holding uncommitted work from
the morning that flushed later; **two stray `python.exe` processes were still
running at the end of this pass** (nothing listening on port 8710). Nothing was
deleted and the database verifies clean, but the next session should check for and
stop stray processes before trusting a row count, and should treat any figure
measured against a live-served database as a moving target.

The last full application verification in a running server and a real browser was
performed during Step 30 and is recorded in `PROJECT_STATUS.md`: the evidence
report served in 0.33 s, the summary agreed with all 720 paged records on every
correction kind, three confusion cells drilled down to matching counts, six
filter combinations behaved, Reset cleared all eight controls, and publishing both
a new rate-set version and a new time-profile version left the evidence report,
the summary and the records **byte-identical**. **No server was started during
this checkpoint pass**, and no application behaviour was changed, so that remains
the current live status.

## 11. What must NOT be changed casually

- **Never delete or recreate the database.** `./cad_costing_agent.db` holds the
  128-estimate history every figure above is measured from. Any schema change
  needs a migration that works against both a fresh and an existing database.
- **The import boundaries.** `component_classification.py`,
  `feedback_analytics.py` and `exploded_view.py` import nothing from the package;
  `quotation.py` imports only `estimates` and `money`. Those empty sets are the
  enforcement, not a convention.
- **`manufacturing_plan` as the sole bridge to money.** No costing module may
  import `assembly_plan`, `operation_time`, `operation_suggestions`,
  `mesh_simplify` or `operation_feedback`.
- **Rate-set and time-profile versions are immutable.** Publish a new version;
  there is no update or delete path anywhere, by design.
- **The three-branch time resolution** (engineer's typed value → the system's
  value *if accepted* → nothing). It is implemented in `manufacturing_plan` and
  deliberately mirrored in `plan.js`; change one, change both. An operation with
  no committed cycle time is *omitted* from a cost, never zeroed or averaged.
- **Setup time belongs to a setup, charged once**, however many operations it
  holds. Charging it per operation was a real costing error (280 min vs 10).
- **A volume is never invented**, an assumption is never printed as a
  measurement, and a missing input produces `needs_user_input` rather than a
  default.
- **The system's suggestion is never overwritten** — edits go to `user_*` fields
  beside untouched `system_*` ones.
- **`plan.js` is a persistence path.** When you add a field to
  `PlannedOperation`, add it to `plan.js` too, or it will never reach the server.
- **The estimate's six sections.** Every card belongs to exactly one
  `.flow-pane`; do not reintroduce a page that shows everything at once. Handlers
  address elements by id, so moving a card must preserve its ids —
  `estimateflow.js` arranges the page and is tested for containing no arithmetic,
  no format list and no second copy of the time rule.
- **A structural grep must strip prose first.** This has bitten seven times
  across three languages, because the prose here explains at length what the code
  deliberately does not do. Strip comments and docstrings, prefer a precise
  token, and write the pattern out literally rather than interpolating it — three
  assertions once carried a literal backspace byte where a word-boundary escape
  was meant and could never fail.
- **Documentation discipline**: `README.md`, `ARCHITECTURE.md` and
  `PROJECT_STATUS.md` describe only what has actually been built. No aspirational
  claims, and this proof-of-concept is never described as production-grade.

## 12. Recommended next step, and why

**Quotation revisions as a chain**, with the operations-table layout fix
alongside it.

A quotation currently names its previous estimate and the change, but a revised
quotation of the same subject is just another number — there is no chain a
customer could read as Rev A against Rev B. The comparison layer already computes
the difference and `estimates.compare_estimates` already takes plain carriers, so
this is the largest remaining piece of customer-facing work with the least new
machinery behind it. `quotation.py`'s existing refusal-and-immutability rules
give it a clear shape: an issued quotation cannot change, so a revision is a new
document that points at its predecessor.

Alongside it, the operations table is 600 px wide inside a 487 px card even for a
single part, so the page scrolls sideways by about 105 px. It has been that way
since Step 21 and is the most visible rough edge left in the interface; Steps 26,
28 and 29 each established the fix (wide content on its own full-width sub-row).

**Learning remains explicitly out of scope.** Step 30 improved the evidence's
quality, not its quantity.

## 13. Alternatives considered and deliberately deferred

- **Training anything on the feedback data.** Deferred indefinitely and
  deliberately. The pairs are persisted and the analytics reports them, but with
  4 operation corrections and 19 time corrections the dataset cannot support a
  model, and the architecture is built so that this module *cannot* learn. If a
  future step trains something, it does not live in `feedback_analytics.py`.
- **Assembly-level manufacturing** — assembly labour, fastening time, joining,
  routing and a disassembly sequence. Deferred because none of it is in a STEP
  file; it would all be entered by a person, and the roll-up says so today.
- **A PDF library.** Deferred: the quotation prints through the browser, and a
  PDF renderer would be a second rendering path to keep in agreement with the
  first.
- **Per-component materials, supplier price lookup, a standards catalogue.**
  Deferred for the same reason: nothing in the file says what a component is made
  of or what it costs, and inferring either is prohibited.
- **Higher-quality display decimation** (quadric edge collapse). Deferred;
  vertex clustering is coarser but deterministic, and it is never used for a
  measurement.
- **Face-level highlighting in the viewer.** Deferred; it needs a per-face id the
  wire mesh does not carry.
- **A parametric CAD provider** — see section 15. Considered as a *direction*,
  deliberately not scheduled, and explicitly not a dependency.

## 14. Figure 3 is faceted geometry, not native parametric CAD

This distinction matters and is easy to lose. `cadmodels/Figure 3.stp` — the 198
MB file most of the hard live verification in this project was done against — is
a **faceted STEP export**, the kind a Creo Illustrate-style pipeline produces. It
is **not** native parametric CAD and carries none of the information one would.

Measured, and recorded in `PROJECT_STATUS.md`:

- AP214 `AUTOMOTIVE_DESIGN`, **272,113 polygons in 6,473 `FACETED_BREP` bodies**,
  and **zero** analytic surfaces (no `ADVANCED_FACE`).
- OpenCASCADE reads it without error but transfers **no faces at all**, because
  the file writes `FACETED_BREP` on open shells, which ISO 10303-42 does not
  allow — a `FACETED_BREP`'s outer boundary must be a `CLOSED_SHELL`.
- Its **structure survives when its geometry does not**: XCAF recovers all **43
  definitions, 102 occurrences and 4 levels**, so components can be named,
  counted and placed while being unmeasurable. All **36 part definitions** report
  `geometry_status = no_geometry_in_file`, and every one of their manufacturing
  plans is `blocked` with the reason. Not one is given a fabricated plan.
- It is costable only by the user supplying a volume as an **explicit
  assumption**, recorded as `volume_confidence: user_supplied` and never
  presented as a measurement.

So: no parametric features, no feature tree, no design intent, no dimensions,
tolerances or PMI, and no analytic surfaces to recognize holes or fillets from.
Anything reconstructed from its polygons is weaker evidence than something read
from an exact surface, and the code says so everywhere — a faceted feature caps
its operation suggestion at `medium` confidence. `Figure 4.stp` was the same
export twice and is no longer present on this machine.

Never describe this project as having been validated against native parametric
CAD on the strength of Figure 3.

## 15. A future possibility: provider-neutral parametric CAD

Recorded as a **direction under consideration, not a plan, not scheduled, and
not started**. Nothing in the repository implements or depends on any of it
today.

The gap section 14 describes is real: everything this system knows about a part
is recovered from a neutral file *after* the design intent has been discarded.
A parametric source would carry features, dimensions and design intent directly.

If that is ever pursued, the shape it should take is **provider-neutral**: a
narrow internal interface for "a parametric model source", with concrete
providers behind it, in exactly the way `cad_import` already puts format
detection and capability declarations behind one boundary. **Onshape, with
FeatureScript, would be one optional provider — never a dependency.** The
requirements that would have to hold:

- The system must remain fully functional with **no provider configured at all**.
  STL, STEP and IGES stay first-class; nothing may become reachable only through
  a provider.
- No provider's vocabulary may leak into the domain. A provider produces the same
  `GeometryProperties` and `Feature` objects every other path produces, through
  the same single entry point, so the numbers cannot become a second opinion.
- The existing honesty rules apply unchanged: a capability below `supported`
  states its reason, an assumption is never printed as a measurement, and
  evidence from a parametric source is *stronger* than a faceted
  reconstruction — which the `source_type` and confidence machinery already has
  a place to record.
- Nothing about it may require network access at import time for the paths that
  work offline today. three.js is vendored rather than loaded from a CDN
  precisely so the app works behind a corporate proxy.

Treat this as background for scoping a future step, not as a commitment. It is
**not** part of Step 31 unless a later session explicitly justifies it.

---

## NEXT SESSION

**Do this first, in order.**

**1. Read, before touching anything:**

- `CHECKPOINT.md` (this file) — the whole thing.
- `CLAUDE.md` (556 lines) — the operating rules, the module boundaries and which
  test fails by name if one is crossed. Non-optional.
- `PROJECT_STATUS.md` — the `## Current stage` block at the top, the `## Next
  step` block at the bottom, and the `## What has NOT been done` list.
- `ARCHITECTURE.md` — only the sections for the area you are about to work in.
- Then, and only then, the source for that area.

**2. Current test command:**

```bash
.venv/Scripts/activate                 # source .venv/bin/activate elsewhere
pytest                                 # 1724 collected: 1723 pass, 1 skip, ~6 min
pytest tests/test_evidence_completeness.py          # one file
pytest -k "evidence and not viewer"                 # by expression
```

Record the baseline **1723 passing** before making any change, so the step's
delta is real.

**3. Database precautions:**

- The real database is `./cad_costing_agent.db` (7.2 MB, **132 estimates**,
  Alembic head **`0005`**, integrity `ok`). **Never delete, recreate or reseed
  it.**
- **First, check for stray processes.** Two `python.exe` processes were still
  running when this checkpoint was written, and the database file was written to
  by something other than this session (see section 10). Run `tasklist //FI
  "IMAGENAME eq python.exe"`, account for anything you find, and stop leftover
  servers before measuring anything.
- For anything that runs the app, point it at a throwaway file:

  ```bash
  CAD_COSTING_DATABASE_URL=sqlite:///./scratch.db uvicorn cad_costing_agent.main:app --port 8710
  ```

  Startup migrates and seeds whatever it is pointed at, and the default target is
  the real file.
- To *inspect* the real database, open it read-only —
  `sqlite3.connect("file:cad_costing_agent.db?mode=ro", uri=True)` — which is how
  every figure in sections 3 and 9 was verified.
- Any schema change needs an Alembic migration that works against both a fresh
  and an existing database. Verify `alembic current` reports `0005` before
  starting, and confirm what it reports afterwards.

**4. What Step 31 is expected to investigate:**

Quotation revisions as a chain — whether an issued quotation can point at the
predecessor it revises, so a customer can read Rev A against Rev B, given that an
issued quotation is immutable and `quotation.py` may not calculate anything. The
investigation should establish: where a revision relationship would be persisted
(and whether migration `0006` is actually necessary); whether
`estimates.compare_estimates` already produces everything a revision document
needs; how a revision chain behaves when a quotation is unavailable or refused;
and what the customer view must still omit. The operations-table layout fix
(600 px content in a 487 px card) is a candidate to run alongside it.

**5. Do not start implementing.**

Step 31 has **no approved scope**. Inspect first, write a short implementation
plan naming the source of truth, the persistence location, whether a migration is
genuinely necessary and which tests must change — and get that scope justified
and agreed before writing code. The user builds this project one numbered step
per message with an explicit "do NOT implement in this step" list; assume the
same discipline applies and wait for it.

Do not begin any later roadmap item, do not introduce machine learning, and do
not commit anything.
