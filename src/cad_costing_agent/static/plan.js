"use strict";

// The manufacturing plan: the one table that decides what gets costed.
//
// A classic script loaded after app.js, features.js and suggestions.js, sharing
// `$`, `api`, `number`, `escapeHtml`, `state` and `selectFeature` with them.
//
// Every row holds two parallel sets of values that never mix:
//
//   system_suggested_*   what the system proposed, and what it estimated the
//                        time to be. Written once, when the plan is built.
//   user_*               what the engineer chose instead.
//
// **The system's proposal is never overwritten.** Editing a process or a time
// writes to the user side and leaves the prediction intact, so the estimate is
// persisted carrying both — which is the only reason a later learning step will
// have anything to learn from.
//
// The other rule this file exists to hold: an estimated time is a *proposal*.
// Generating one does not commit it. A row is costed only when the engineer
// accepts the estimate (`system_time_accepted`) or types a value, and a row with
// neither is left out of the request entirely rather than defaulted to zero.
//
// Nothing here computes a time or a cost. Times come from
// `/operations/estimate-time`, money comes from the backend's costing.

// Mirrors TimeSource in manufacturing_plan.py.
const TIME_SOURCE = { SYSTEM: "system", USER: "user", NEEDS_INPUT: "needs_input" };
const ACT = {
  PENDING: "pending",
  ACCEPTED: "accepted",
  EDITED: "edited",
  REJECTED: "rejected",
  ADDED: "added",
};

let manualCounter = 0;

// ------------------------------------------------------------------ building

function resetPlan() {
  state.plan = null;
  // Whatever this table was editing, it is not editing it any more. Leaving a
  // component id behind would let a later edit be attributed to a component
  // from a previous file.
  state.planComponentId = null;
  manualCounter = 0;
  renderPlan();
}

/**
 * Build the plan from Step 19 suggestions, then ask the backend to estimate a
 * time for each. Estimating is not accepting: every row starts needing input.
 */
async function buildPlanFromSuggestions(result) {
  const operations = result.suggestions.map((suggestion, index) => ({
    operation_id: `OP-${String(index + 1).padStart(3, "0")}`,
    sequence_position: index + 1,
    feature_ids: suggestion.feature_id ? [suggestion.feature_id] : [],
    feature_label: suggestion.feature_label,
    // Needed to re-estimate: the subtype decides whether a drill breaks
    // through, and the type tells a blend from a face.
    feature_type: suggestion.feature_type,
    feature_subtype: suggestion.feature_subtype,
    operation_name: suggestion.operation_name,
    origin: "system",
    user_action: ACT.PENDING,
    system_suggested_operation: suggestion.operation_type,
    system_suggestion_confidence: suggestion.confidence,
    system_suggestion_rule: suggestion.rule_id,
    // The evidence behind the suggestion, carried into the persisted plan.
    //
    // This table builds the plan payload in the browser rather than calling
    // `build_plan`, so any field it does not copy never reaches the server and
    // is gone for good. Step 29 added both of these to the plan model and the
    // feedback record, and measuring the real database in Step 30 found all 713
    // stored records missing `feature_confidence` — the backend populated it,
    // and this builder dropped it on the way past.
    //
    // `source_type` the analytics layer can recover afterwards from the viewer
    // linkage; `feature_confidence` it cannot recover from anything, because the
    // CAD file is not stored. So losing it here loses it permanently.
    system_source_type: suggestion.source_type,
    system_feature_confidence: suggestion.feature_confidence,
    system_time_status: "needs_user_input",
    system_time_reason: "",
    system_time_evidence: [],
    system_time_assumptions: [],
    system_time_missing: [],
    system_setup_id: "SETUP-001",
    user_setup_id: null,
    system_time_profile_id: null,
    system_time_profile_version: null,
    system_time_profile_name: null,
    system_time_parameters: {},
    user_operation_type: null,
    user_setup_time: null,
    user_cycle_time: null,
    system_time_accepted: false,
    source_faces: suggestion.source_faces || [],
    source_facets: suggestion.source_facets || [],
    dimensions: suggestion.dimensions || {},
    system_drilling_steps: [],
    system_drilling_options: [],
    user_drilling_steps: null,
    user_machined: null,
    // Kept for the expandable "why" row; not part of the persisted plan model.
    // `_source_type` used to hold this too and has been removed: with the real
    // field carried above, a display-only duplicate is a second source of truth
    // for the same fact.
    _reason: suggestion.reason,
  }));

  // One setup holding everything: the same conservative default the backend
  // applies, and the opposite of the per-operation setup that made costs wrong.
  state.plan = {
    setups: [{
      setup_id: "SETUP-001",
      sequence_position: 1,
      name: "Setup 1",
      description: "",
      process_context: null,
      origin: "system",
      system_operation_ids: operations.map((o) => o.operation_id),
      system_grouping: "system_assumed",
      system_grouping_reason:
        "Every operation was placed in one setup, because nothing in the CAD proves a second " +
        "fixturing is needed.",
      system_grouping_confidence: "low",
      system_grouping_assumptions: [],
      system_setup_time_minutes: null,
      system_setup_time_source: null,
      user_action: "pending",
      user_setup_time_minutes: null,
      system_setup_time_accepted: false,
    }],
    operations,
    units: result.units,
    source_filename: result.filename,
    route_processes: decidedRouteProcesses(), as_formed_feature_types: decidedAsFormedTypes(),
  };
  renderPlan();

  if (!operations.length) return;

  $("ops-note").textContent = "Estimating times…";
  // Times are estimated under the selected published profile, so every
  // prediction can name the assumptions that produced it.
  const params = new URLSearchParams();
  if ($("time_profile_id").value) params.set("time_profile_id", $("time_profile_id").value);
  if ($("time_profile_version").value) {
    params.set("time_profile_version", $("time_profile_version").value);
  }
  const query = params.toString() ? `?${params}` : "";

  let times;
  try {
    times = await api(`/operations/estimate-time${query}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        units: result.units,
        // A turned part's passes are worked out from the bar it is proposed to be
        // cut from (the system's proposal, so the estimate stays the system's).
        stock_diameter_mm: state.rawStock ? state.rawStock.system_diameter_mm : null,
        operations: operations.map((operation) => ({
          operation_id: operation.operation_id,
          operation_type: operation.system_suggested_operation,
          feature_id: operation.feature_ids[0] || null,
          feature_type: operation.feature_type,
          feature_subtype: operation.feature_subtype,
          dimensions: operation.dimensions,
          confidence: operation.system_suggestion_confidence,
        })),
      }),
    });
  } catch (error) {
    // A failed estimate leaves every row needing input, which is the safe
    // state — it must not leave rows looking costable.
    $("ops-note").textContent = "";
    showError(error.message);
    return;
  }

  const byId = new Map(times.estimates.map((entry) => [entry.operation_id, entry.estimate]));
  operations.forEach((operation) => {
    const estimate = byId.get(operation.operation_id);
    if (!estimate) return;
    operation.system_suggested_setup_time = estimate.setup_time_minutes;
    operation.system_suggested_cycle_time = estimate.cycle_time_minutes;
    operation.system_time_method = estimate.method;
    operation.system_time_confidence = estimate.confidence;
    operation.system_time_status = estimate.time_status;
    operation.system_time_reason = estimate.reason;
    operation.system_time_evidence = estimate.evidence || [];
    operation.system_time_assumptions = estimate.assumptions || [];
    operation.system_time_missing = estimate.missing || [];
    operation.system_time_profile_id = estimate.time_profile_id;
    operation.system_time_profile_version = estimate.time_profile_version;
    operation.system_time_profile_name = estimate.time_profile_name;
    operation.system_time_parameters = estimate.parameters || {};
    // How the hole is drilled, and the alternatives with their own times.
    // Copied explicitly: a field this builder drops never reaches the server.
    operation.system_drilling_steps = estimate.drilling_steps || [];
    operation.system_drilling_options = estimate.drilling_options || [];
  });

  const baselines = operations
    .map((o) => o.system_suggested_setup_time)
    .filter((v) => v !== null && v !== undefined);
  state.plan.setups[0].system_setup_time_minutes = baselines.length ? Math.max(...baselines) : null;
  state.plan.setups[0].system_setup_time_source = "largest_process_baseline";

  state.plan.time_profile_id = times.time_profile_id;
  state.plan.time_profile_version = times.time_profile_version;
  state.plan.time_profile_name = times.time_profile_name;

  state.planWarnings = times.warnings || [];
  renderPlan();
}

// ------------------------------------------------------------------ resolving
// The same three-branch resolution the backend applies, so what the table shows
// and what gets costed cannot disagree.

function systemTimeApplies(operation) {
  return (
    operation.system_time_status === "estimated" &&
    operation.system_suggested_operation != null &&
    effectiveType(operation) === operation.system_suggested_operation &&
    systemCycleForChoice(operation) != null
  );
}

// ------------------------------------------------------------------ drilling
// Mirrors PlannedOperation.system_cycle_for_choice: the suggested drilling
// sequence's time is system_suggested_cycle_time, never overwritten; another
// sequence the system offered has its own system-computed time; one it did not
// offer has none.

function sameSteps(a, b) {
  return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => v === b[i]);
}

function drillingSteps(operation) {
  return operation.user_drilling_steps || operation.system_drilling_steps || [];
}

function systemCycleForChoice(operation) {
  const chosen = operation.user_drilling_steps;
  if (chosen == null || sameSteps(chosen, operation.system_drilling_steps)) {
    return operation.system_suggested_cycle_time;
  }
  const option = (operation.system_drilling_options || []).find((o) => sameSteps(o.steps, chosen));
  return option ? option.cycle_time_minutes : null;
}

function effectiveType(operation) {
  return operation.user_operation_type || operation.system_suggested_operation;
}

function timeSource(operation) {
  if (operation.user_cycle_time != null) return TIME_SOURCE.USER;
  if (operation.system_time_accepted && systemTimeApplies(operation)) return TIME_SOURCE.SYSTEM;
  return TIME_SOURCE.NEEDS_INPUT;
}

function finalCycle(operation) {
  if (operation.user_cycle_time != null) return operation.user_cycle_time;
  if (operation.system_time_accepted && systemTimeApplies(operation))
    return systemCycleForChoice(operation);
  return null;
}

function finalSetup(operation) {
  if (operation.user_setup_time != null) return operation.user_setup_time;
  if (operation.system_time_accepted && systemTimeApplies(operation))
    return operation.system_suggested_setup_time;
  return null;
}

function isCostable(operation) {
  return (
    operation.user_action !== ACT.REJECTED &&
    Boolean(effectiveType(operation)) &&
    finalCycle(operation) > 0 &&
    inRoute(operation) &&
    !isAsFormed(operation)
  );
}

// ------------------------------------------------------------------ as formed
// Mirrors ManufacturingPlan.is_as_formed: on a decided route that casts or
// forges a blank and then machines it, the backend's `as_formed_feature_types`
// (its blends) are left as formed by default, and a row's own `user_machined`
// overrides that either way. On any other route nothing is left as formed.

function decidedAsFormedTypes() {
  const classification = state.processClassification;
  if (state.planComponentId || !classification || !Array.isArray(classification.as_formed_feature_types)) {
    return null;
  }
  return [...classification.as_formed_feature_types];
}

function asFormedTypes() {
  return state.plan && Array.isArray(state.plan.as_formed_feature_types)
    ? state.plan.as_formed_feature_types
    : null;
}

function isAsFormed(operation) {
  const types = asFormedTypes();
  if (types === null) return false;
  if (operation.user_machined === true) return false;
  if (operation.user_machined === false) return true;
  return types.includes(operation.feature_type || "");
}

function asFormedOperations() {
  if (!state.plan) return [];
  return state.plan.operations.filter(
    (operation) => operation.user_action !== ACT.REJECTED && inRoute(operation) && isAsFormed(operation)
  );
}

// What a cast or forged blank's unmachined features are called: "As cast" or
// "As forged", as the backend named it.
function asFormedLabel() {
  return (state.processClassification && state.processClassification.as_formed_label) || "As formed";
}

// ------------------------------------------------------------------ route
// The processes the engineer's chosen manufacturing route includes, as the
// backend's `plan_processes` reported them; null while no route is decided.
// Mirrors `ManufacturingPlan.in_route`: an operation outside the route is kept
// exactly as it is, but it is not costed and not "missing a time".

// What a freshly created part plan should start with: the route the engineer
// has decided, if any. A component plan opened from an assembly never takes the
// single part's route.
function decidedRouteProcesses() {
  const classification = state.processClassification;
  if (state.planComponentId || !classification || !Array.isArray(classification.plan_processes)) {
    return null;
  }
  return [...classification.plan_processes];
}

function routeProcesses() {
  return state.plan && Array.isArray(state.plan.route_processes) ? state.plan.route_processes : null;
}

function inRoute(operation) {
  const processes = routeProcesses();
  return processes === null || processes.includes(effectiveType(operation) || "");
}

function outOfRouteOperations() {
  if (!state.plan) return [];
  return state.plan.operations.filter(
    (operation) => operation.user_action !== ACT.REJECTED && !inRoute(operation)
  );
}

// ------------------------------------------------------------------ setups
// The same three-branch resolution the backend uses for a setup's time, so the
// table and the cost can never disagree about what is committed.

function setupsOf() {
  if (!state.plan) return [];
  return [...(state.plan.setups || [])].sort((a, b) => a.sequence_position - b.sequence_position);
}

function setupById(id) {
  return (state.plan ? state.plan.setups || [] : []).find((s) => s.setup_id === id) || null;
}

function setupOf(operation) {
  return operation.user_setup_id || operation.system_setup_id;
}

function setupTime(setup) {
  if (setup.user_setup_time_minutes != null) return setup.user_setup_time_minutes;
  if (setup.system_setup_time_accepted && setup.system_setup_time_minutes != null)
    return setup.system_setup_time_minutes;
  return null;
}

function setupTimeSource(setup) {
  if (setup.user_setup_time_minutes != null) return TIME_SOURCE.USER;
  if (setup.system_setup_time_accepted && setup.system_setup_time_minutes != null)
    return TIME_SOURCE.SYSTEM;
  return TIME_SOURCE.NEEDS_INPUT;
}

function operationsInSetup(setupId) {
  if (!state.plan) return [];
  return state.plan.operations
    .filter((o) => o.user_action !== ACT.REJECTED && setupOf(o) === setupId)
    .sort((a, b) => a.sequence_position - b.sequence_position);
}

function activeOperations() {
  if (!state.plan) return [];
  const order = new Map(setupsOf().map((s, index) => [s.setup_id, index]));
  return state.plan.operations
    .filter(
      (operation) =>
        operation.user_action !== ACT.REJECTED && inRoute(operation) && !isAsFormed(operation)
    )
    .sort(
      (a, b) =>
        (order.get(setupOf(a)) ?? 1e6) - (order.get(setupOf(b)) ?? 1e6) ||
        a.sequence_position - b.sequence_position
    );
}

// ------------------------------------------------------------------ rendering

// Which plan this table is editing. Blank for a standalone part; named when an
// assembly component's plan has been opened here, because editing the wrong
// component's routing is otherwise completely silent.
function renderPlanScope() {
  const banner = $("plan-scope");
  if (!banner) return;
  const componentId = state.planComponentId;
  if (!componentId || !state.assemblyPlan) {
    banner.classList.add("hidden");
    banner.innerHTML = "";
    return;
  }
  const entry = (state.assemblyPlan.component_plans || []).find(
    (item) => item.definition_id === componentId
  );
  banner.classList.remove("hidden");
  banner.innerHTML = `Editing the manufacturing plan for
    <strong>${escapeHtml(entry ? entry.component_name : componentId)}</strong>, a component of
    ${escapeHtml(state.assemblyPlan.assembly_name || "this assembly")}. Its setups are its own —
    no other component shares them.
    <button type="button" id="close-component-plan" class="secondary tiny">Close</button>`;
  const close = $("close-component-plan");
  if (close) {
    close.addEventListener("click", () => {
      state.plan = null;
      state.planComponentId = null;
      renderPlan();
    });
  }
}

function renderPlan() {
  const body = $("ops-body");
  const operations = activeOperations();
  renderPlanScope();
  const stageRows = routeStageRows();

  if (!operations.length && !setupsOf().length && !(stageRows && (stageRows.before.length || stageRows.after.length))) {
    body.innerHTML = "";
    $("ops-note").textContent =
      "No operations yet. Suggest them from the recognized features, or add one by hand — " +
      "costing works either way.";
    $("ops-uncosted").classList.add("hidden");
    $("plan-impact").classList.add("hidden");
    $("use-all-estimates").classList.add("hidden");
    renderSequence([]);
    return;
  }

  // Rows are grouped under their setup, because that grouping is what decides
  // how often setup time is charged. A flat list would hide the one thing this
  // table now exists to show.
  const rows = [];
  if (stageRows) rows.push(...stageRows.before);
  const machinesNothing = Array.isArray(routeProcesses()) && routeProcesses().length === 0;
  setupsOf().forEach((setup) => {
    const members = operationsInSetup(setup.setup_id).filter((o) => inRoute(o) && !isAsFormed(o));
    // A route with no machining stage holds no setup's work; an empty setup band
    // there only suggests fixturing nobody does. It stays in the plan, unshown.
    if (!members.length && machinesNothing) return;
    rows.push(setupHeaderRow(setup));
    if (!members.length) {
      rows.push(
        `<tr class="setup-empty"><td colspan="8">No operations in this setup yet — move one in,
          or remove the setup.</td></tr>`
      );
    }
    members.forEach((operation) => rows.push(planRow(operation)));
  });
  if (stageRows) rows.push(...stageRows.after);

  // Operations the chosen route leaves out, kept below rather than deleted, so
  // switching the route back — or changing a row's process — restores them as
  // they were, times and edits included.
  const outside = outOfRouteOperations();
  if (outside.length) {
    // Folded away by default: they are not part of the chosen route, and shown
    // in full they made the plan look as if choosing a route had changed nothing.
    // Still in the page and one click from view, never removed.
    rows.push(
      `<tr class="setup-empty parked-head"><td colspan="8"><strong>Not in this route</strong> — ` +
        `${outside.length} operation${outside.length === 1 ? "" : "s"} kept but not costed, ` +
        `because the chosen route does not include their process. Switch the route back, or ` +
        `change a row's process to one the route includes, to bring it back. ` +
        `<button type="button" class="secondary tiny" id="toggle-parked">` +
        `${state.showParked ? "Hide" : "Show"} ${outside.length}</button></td></tr>`
    );
    outside.forEach((operation) =>
      rows.push(planRow(operation).replace(/<tr\b/g, '<tr data-parked="1"'))
    );
  }

  // On a cast or forged route, what the blank leaves as formed: kept below and
  // not costed, each one a click from being machined after all.
  const formed = asFormedOperations();
  if (formed.length) {
    rows.push(
      `<tr class="setup-empty"><td colspan="8"><strong>${escapeHtml(asFormedLabel())} — not ` +
        `machined</strong> — ${formed.length} operation${formed.length === 1 ? "" : "s"} on ` +
        `features the blank is formed with (its fillets and chamfers by default), kept but not ` +
        `costed. Use <em>Machine it</em> on any that are cut after all, or <em>` +
        `${escapeHtml(asFormedLabel())}</em> on a row above to leave it unmachined.</td></tr>`
    );
    formed.forEach((operation) => rows.push(planRow(operation)));
  }

  body.innerHTML = rows.join("");
  body.classList.toggle("parked-collapsed", !state.showParked);
  const toggle = $("toggle-parked");
  if (toggle) {
    toggle.addEventListener("click", () => {
      state.showParked = !state.showParked;
      renderPlan();
    });
  }
  renderPlanRoute();
  wirePlanRows();
  wireSetupRows();
  renderSequence(operations);

  const estimable = operations.filter(
    (operation) => systemTimeApplies(operation) && !operation.system_time_accepted
  );
  $("use-all-estimates").classList.toggle("hidden", estimable.length === 0);

  const processes = Object.keys((state.rateSet && state.rateSet.operations) || {});
  $("ops-note").textContent = processes.length
    ? `Priced by this rate set: ${processes.join(", ")}. Click a row to see why the operation ` +
      "was suggested and how its time was worked out."
    : "This rate set prices no operations, so operations cannot be costed.";

  refreshUncostedNote();
  refreshPlanImpact();

  // An edit here changes the component's own plan, so its summary has to be
  // re-read. Guarded forward, the way app.js guards its calls into impact.js:
  // assembly.js is loaded after this file.
  if (state.planComponentId && typeof renderComponentPlanDetail === "function") {
    // Both, not one: the status column and the detail panel read the same plan,
    // and refreshing only the panel left the table contradicting it.
    renderComponentPlans();
    renderComponentPlanDetail(state.planComponentId);
  }
}

// The rate a non-machining stage will be priced at, read from the selected rate
// set as published, and the mass it is charged on. Nothing is worked out here.
function stageRateText(stage) {
  const rateSet = state.rateSet;
  const rate = rateSet && rateSet.stage_rates ? rateSet.stage_rates[stage.stage] : null;
  const currency = rateSet ? rateSet.currency : "INR";
  if (!rate) {
    return `<span class="warning-inline">no rate in ${
      rateSet ? `${escapeHtml(rateSet.name)} v${escapeHtml(rateSet.version)}` : "the selected rate set"
    } — this route cannot be costed until one is published</span>`;
  }
  const parts = [];
  if (rate.unit_rate !== null && rate.unit_rate !== undefined) {
    parts.push(`${money(rate.unit_rate, currency)} ${rate.unit_basis === "per_kg" ? "per kg" : "per piece"}`);
  }
  if (rate.batch_charge) parts.push(`${money(rate.batch_charge, currency)} per batch`);
  const entered = ((state.stageInputs || {})[stage.stage] || {}).mass_kg;
  const mass =
    rate.unit_basis === "per_kg"
      ? entered !== undefined
        ? ` · on ${number(entered, 3)} kg per part (entered)`
        : " · on a mass you enter on the route card, or else the casting/forging blank's or the CAD model's — the estimate says which"
      : "";
  return `<span class="muted">${parts.join(" + ")}${mass}</span>`;
}

// The decided route's non-machining stages, as rows of the table in route
// order: those before the first machining stage go above the setups, the rest
// below. They are not operations — a stage rate prices each one, so a row has
// no setup, no cycle time and no feature, and nothing in it is costed here or
// counted by the setup and cycle totals. Null when no route is decided.
function routeStageRows() {
  const classification = state.processClassification;
  if (state.planComponentId || routeProcesses() === null || !classification || !classification.route) {
    return null;
  }
  const stages = classification.route.stages;
  const firstMachining = stages.findIndex((stage) => stage.process_type);
  const row = (stage) => {
    const position = stages.indexOf(stage) + 1;
    return (
      `<tr class="setup-empty route-stage-row" data-stage="${escapeHtml(stage.stage)}"><td colspan="8">` +
      `<div class="setup-band"><span class="seq-cell">Step ${position} of ${stages.length}</span>` +
      `<strong>${escapeHtml(stage.label)}</strong>` +
      `<span class="pill pill-off">route stage — stage rate, no time</span>` +
      `${stageRateText(stage)}</div></td></tr>`
    );
  };
  const split = firstMachining === -1 ? stages.length : firstMachining;
  return {
    before: stages.slice(0, split).filter((stage) => !stage.process_type).map(row),
    after: stages.slice(split).filter((stage) => !stage.process_type).map(row),
  };
}

// The stages of the engineer's chosen route, in order, above the table. A
// machining stage says how many operations of its process the plan holds; any
// other stage says plainly that nothing here can price it. Read from the
// classification the backend returned — this decides nothing.
function renderPlanRoute() {
  const strip = $("plan-route");
  if (!strip) return;
  const classification = state.processClassification;
  if (state.planComponentId || routeProcesses() === null || !classification) {
    strip.classList.add("hidden");
    strip.innerHTML = "";
    return;
  }
  const route = classification.route;
  const stages = route ? route.stages : [];
  const counted = activeOperations();
  const items = stages.map((stage) => {
    if (!stage.process_type) {
      const formed = asFormedTypes() !== null && stage === stages[0] ? asFormedOperations().length : 0;
      return (
        `<li>${escapeHtml(stage.label)} <span class="pill pill-off">priced from stage rates</span> ` +
        stageRateText(stage) +
        (formed ? ` <span class="muted">· leaves ${formed} feature${formed === 1 ? "" : "s"} unmachined below</span>` : "") +
        `</li>`
      );
    }
    const count = counted.filter((operation) => effectiveType(operation) === stage.process_type).length;
    return (
      `<li>${escapeHtml(stage.label)} <span class="pill pill-ok">machining — hourly rates</span> ` +
      (count
        ? `<span class="muted">${count} operation${count === 1 ? "" : "s"} below</span>`
        : `<span class="muted">no operations yet — add one with + Add operation</span>`) +
      "</li>"
    );
  });
  strip.innerHTML =
    `<strong>Route:</strong> ${escapeHtml(route ? route.label : "unknown")}` +
    (items.length
      ? `<ol class="sequence-list">${items.join("")}</ol>`
      : `<p class="muted">No manufacturing stages in this route, so no operation below is costed.</p>`);
  strip.classList.remove("hidden");
}

function setupHeaderRow(setup) {
  const id = escapeHtml(setup.setup_id);
  const members = operationsInSetup(setup.setup_id).filter((o) => inRoute(o) && !isAsFormed(o));
  const costable = members.filter(isCostable).length;
  const source = setupTimeSource(setup);
  const committed = setupTime(setup);

  const grouping =
    setup.origin === "user"
      ? `<span class="pill pill-ok">User grouping</span>`
      : `<span class="pill pill-off" title="${escapeHtml(setup.system_grouping_reason || "")}">
           System grouping · ${escapeHtml(setup.system_grouping_confidence || "low")} confidence</span>`;

  // The estimate is shown as a placeholder, never as the field's value — the
  // same rule the operation times follow.
  const placeholder =
    setup.system_setup_time_minutes != null ? `${setup.system_setup_time_minutes} est.` : "enter";
  const value = setup.user_setup_time_minutes != null ? String(setup.user_setup_time_minutes) : "";
  const needsInput = source === TIME_SOURCE.NEEDS_INPUT;

  const useEstimate =
    needsInput && setup.system_setup_time_minutes != null
      ? `<button type="button" class="secondary tiny setup-use-est" data-id="${id}">Use estimate</button>`
      : "";

  const badge =
    source === TIME_SOURCE.USER
      ? `<span class="pill pill-ok">User value</span>`
      : source === TIME_SOURCE.SYSTEM
        ? `<span class="pill pill-sys">System estimate</span>`
        : `<span class="pill pill-off">Needs input</span>`;

  // One cell across the table: a setup heads the operations under it rather
  // than being one of them, and its controls need widths of their own.
  return `<tr class="setup-row" data-setup="${id}">
      <td colspan="8">
        <div class="setup-band">
          <span class="num seq-cell">${setup.sequence_position}
            <span class="seq-buttons">
              <button type="button" class="secondary tiny setup-up" data-id="${id}" title="Move setup earlier">↑</button>
              <button type="button" class="secondary tiny setup-down" data-id="${id}" title="Move setup later">↓</button>
            </span>
          </span>
          <span class="setup-identity">
            <input class="setup-name" data-id="${id}" value="${escapeHtml(setup.name)}">
            <span class="muted setup-meta">${members.length} operation(s), ${costable} costable
              ${setup.process_context ? `· ${escapeHtml(setup.process_context)}` : ""}</span>
          </span>
          <span class="setup-timing">
            <label class="setup-time-label">setup
              <input class="setup-time ${needsInput ? "needs-input" : ""}" data-id="${id}"
                type="number" min="0" step="any" value="${escapeHtml(value)}"
                placeholder="${escapeHtml(placeholder)}">
            </label>
            ${badge}
            ${useEstimate}
          </span>
          <span class="setup-grouping">${grouping}</span>
          <button type="button" class="secondary tiny setup-remove" data-id="${id}"
            ${members.length ? "disabled title=\"Move its operations out first\"" : ""}>Remove</button>
        </div>
      </td>
    </tr>`;
}

// Moving work between fixturings is a real manufacturing decision with a real
// cost, so it is an explicit choice on the row rather than something inferred.
function setupChoiceCell(operation) {
  const current = setupOf(operation);
  const options = setupsOf()
    .map(
      (setup) =>
        `<option value="${escapeHtml(setup.setup_id)}"${setup.setup_id === current ? " selected" : ""}>` +
        `${escapeHtml(setup.name)}</option>`
    )
    .join("");
  const moved = operation.user_setup_id && operation.user_setup_id !== operation.system_setup_id;
  const wasIn = moved ? setupById(operation.system_setup_id) : null;
  return (
    `<select class="op-setup-choice" data-id="${escapeHtml(operation.operation_id)}">${options}</select>` +
    (wasIn ? ` <span class="was">was ${escapeHtml(wasIn.name)}</span>` : "")
  );
}

function timeCell(operation, which) {
  const source = timeSource(operation);
  const systemValue =
    which === "setup" ? operation.system_suggested_setup_time : systemCycleForChoice(operation);
  const userValue = which === "setup" ? operation.user_setup_time : operation.user_cycle_time;
  const applies = systemTimeApplies(operation);

  // The placeholder shows the system's estimate without adopting it, so an
  // empty box can never be mistaken for a committed value.
  const placeholder = applies && systemValue != null ? `${systemValue} est.` : "enter";
  const value = userValue != null ? String(userValue) : "";
  const cls = source === TIME_SOURCE.NEEDS_INPUT ? "needs-input" : "";
  return (
    `<input class="op-${which} ${cls}" type="number" min="0" step="any" ` +
    `value="${escapeHtml(value)}" placeholder="${escapeHtml(placeholder)}" ` +
    `data-id="${escapeHtml(operation.operation_id)}">`
  );
}

function sourceCell(operation) {
  const source = timeSource(operation);
  if (source === TIME_SOURCE.USER) {
    return `<span class="pill pill-ok">User value</span>`;
  }
  if (source === TIME_SOURCE.SYSTEM) {
    return `<span class="pill pill-sys">System estimate</span>`;
  }
  if (systemTimeApplies(operation)) {
    return (
      `<span class="pill pill-off">Needs input</span> ` +
      `<button type="button" class="secondary tiny use-est" data-id="${escapeHtml(
        operation.operation_id
      )}">Use estimate</button>`
    );
  }
  return `<span class="pill pill-off">Needs input</span>`;
}

// How the hole is opened up, smallest drill first. The options and their times
// are the backend's (`system_drilling_options`); the suggested one is marked.
// Shown only for a drilling row that has them.
function drillingChoice(operation) {
  const options = operation.system_drilling_options || [];
  if (effectiveType(operation) !== "drilling" || !options.length) return "";
  const chosen = drillingSteps(operation);
  const current = options.find((option) => sameSteps(option.steps, chosen));
  // Compact on the row (the routing row is one line); the full description is
  // the tooltip and the option's own title.
  const chain = (steps) => steps.map((d) => `Ø${d}`).join("→");
  return (
    `<select class="op-drill" data-id="${escapeHtml(operation.operation_id)}" ` +
    `style="flex: 0 0 auto; max-width: 135px" ` +
    `title="${escapeHtml(
      `How this hole is drilled, smallest drill first${current ? `: ${current.label}` : ""}`
    )}">` +
    options
      .map(
        (option, index) =>
          `<option value="${index}" title="${escapeHtml(option.label)}"` +
          `${sameSteps(option.steps, chosen) ? " selected" : ""}>` +
          `${escapeHtml(chain(option.steps))} · ${escapeHtml(String(option.cycle_time_minutes))} min` +
          `${option.suggested ? " (suggested)" : ""}</option>`
      )
      .join("") +
    // No "was …" note beside it: the row is one line, and the suggested option
    // is already marked "(suggested)" in the list and named in Details.
    `</select>`
  );
}

// On a cast or forged route only: move a row between machined and as formed.
function formedToggle(operation) {
  if (asFormedTypes() === null || !inRoute(operation)) return "";
  const id = escapeHtml(operation.operation_id);
  return isAsFormed(operation)
    ? `<button type="button" class="secondary tiny op-formed" data-id="${id}" data-machined="true" ` +
        `title="This feature is cut after all: cost its machining">Machine it</button>`
    : `<button type="button" class="secondary tiny op-formed" data-id="${id}" data-machined="false" ` +
        `title="The blank is formed with this: do not cost machining it">${escapeHtml(asFormedLabel())}</button>`;
}

function planRow(operation) {
  const id = escapeHtml(operation.operation_id);
  const chosen = effectiveType(operation);
  const edited = operation.user_operation_type != null;
  const processes = planProcessChoices(operation);

  // Process and name on one line. The full name is also the input's tooltip,
  // because a narrower field shows less of a long description.
  const operationCell =
    `<div class="op-cell">` +
    `<select class="op-process" data-id="${id}">` +
    processes
      .map(
        (option) =>
          `<option value="${escapeHtml(option)}"${option === chosen ? " selected" : ""}>` +
          `${escapeHtml(option)}</option>`
      )
      .join("") +
    `</select>` +
    `<input class="op-name" data-id="${id}" value="${escapeHtml(operation.operation_name)}"` +
    ` title="${escapeHtml(operation.operation_name)}">` +
    (edited
      ? `<span class="was">was ${escapeHtml(operation.system_suggested_operation)}</span>`
      : "") +
    drillingChoice(operation) +
    `</div>`;

  // Time confidence, not operation confidence: this column sits beside the
  // times, and a confident operation can still have a weak time.
  const confidence = systemTimeApplies(operation)
    ? `<span class="conf conf-${escapeHtml(operation.system_time_confidence)}">` +
      `${escapeHtml(operation.system_time_confidence)}</span>`
    : `<span class="muted">—</span>`;

  const featureCell = operation.feature_ids.length
    ? `<span class="mono feature-link" data-feature="${escapeHtml(operation.feature_ids[0])}">` +
      `${escapeHtml(operation.feature_ids[0])}</span>`
    : `<span class="muted">—</span>`;

  return `<tr class="plan-row" data-id="${id}">
      <td class="num seq-cell">${operation.sequence_position}
        <div class="seq-buttons">
          <button type="button" class="secondary tiny move-up" data-id="${id}" title="Move up">↑</button>
          <button type="button" class="secondary tiny move-down" data-id="${id}" title="Move down">↓</button>
        </div>
      </td>
      <td>${operationCell}</td>
      <td>${featureCell}</td>
      <td class="num">${timeCell(operation, "cycle")}</td>
      <td>${setupChoiceCell(operation)}</td>
      <td>${sourceCell(operation)}</td>
      <td>${confidence}</td>
      <td class="row-actions">
        <button type="button" class="secondary tiny op-details" data-id="${id}"
          aria-expanded="false" title="Why this operation, its rule, its parameters and its provenance">Details</button>
        ${formedToggle(operation)}
        <button type="button" class="secondary tiny remove-op" data-id="${id}">Remove</button>
      </td>
    </tr>
    <tr class="evidence-row hidden"><td colspan="8">${planExplanation(operation)}</td></tr>`;
}

function planExplanation(operation) {
  const parts = [];
  if (operation._reason) {
    parts.push(
      `<strong>Why this operation:</strong> ${escapeHtml(operation._reason)}` +
        (operation.system_suggestion_rule
          ? ` (<code>${escapeHtml(operation.system_suggestion_rule)}</code>)`
          : "")
    );
  }
  if (operation.system_time_status === "estimated") {
    parts.push(
      `<strong>Estimated cycle:</strong> ${operation.system_suggested_cycle_time} min` +
        (operation.system_drilling_steps && operation.system_drilling_steps.length > 1
          ? ` for ${escapeHtml(operation.system_drilling_steps.map((d) => `Ø${d}`).join(" → "))}`
          : "") +
        (operation.user_drilling_steps != null
          ? ` · <strong>you chose</strong> ${escapeHtml(
              operation.user_drilling_steps.map((d) => `Ø${d}`).join(" → ")
            )}`
          : "") +
        ` · ` +
        `<strong>setup:</strong> ${operation.system_suggested_setup_time} min · ` +
        `<strong>method:</strong> <code>${escapeHtml(operation.system_time_method)}</code>`
    );
    if (operation.system_time_profile_id) {
      parts.push(
        `<strong>Assumptions from:</strong> ${escapeHtml(operation.system_time_profile_name)} ` +
          `<span class="mono">${escapeHtml(operation.system_time_profile_id)}</span> ` +
          `v${escapeHtml(operation.system_time_profile_version)} — a published version, so this ` +
          `prediction stays explainable even after the profile is revised.`
      );
    }
    if (operation.system_time_evidence.length) {
      parts.push(
        `<strong>Evidence:</strong><ul>` +
          operation.system_time_evidence.map((line) => `<li>${escapeHtml(line)}</li>`).join("") +
          `</ul>`
      );
    }
    if (operation.system_time_assumptions.length) {
      parts.push(
        `<strong>Assumptions:</strong><ul>` +
          operation.system_time_assumptions.map((line) => `<li>${escapeHtml(line)}</li>`).join("") +
          `</ul>`
      );
    }
    if (!systemTimeApplies(operation)) {
      parts.push(
        `<p class="provenance">This estimate was for ` +
          `${escapeHtml(operation.system_suggested_operation)}. You changed the operation to ` +
          `${escapeHtml(effectiveType(operation))}, so it no longer applies — enter a time for ` +
          `the process you chose. The original estimate is still recorded.</p>`
      );
    }
  } else {
    parts.push(
      `<strong>No time estimated.</strong> ${escapeHtml(operation.system_time_reason)}`
    );
  }
  return parts.join(" ");
}

function planProcessChoices(operation) {
  const priced = Object.keys((state.rateSet && state.rateSet.operations) || {});
  const merged = [...priced];
  [operation.system_suggested_operation, operation.user_operation_type].forEach((option) => {
    if (option && !merged.includes(option)) merged.push(option);
  });
  return merged;
}

// Two levels: the setups in their order, and each setup's operations in theirs.
function renderSequence(operations) {
  const list = $("sequence-list");
  if (!operations.length) {
    list.innerHTML = `<li class="muted">No operations yet.</li>`;
    return;
  }
  list.innerHTML = setupsOf()
    .map((setup) => {
      const members = operationsInSetup(setup.setup_id).filter((o) => inRoute(o) && !isAsFormed(o));
      const time = setupTime(setup);
      const inner = members.length
        ? `<ol class="sequence-ops">` +
          members
            .map(
              (operation) =>
                `<li>${escapeHtml(effectiveType(operation) || "—")}` +
                (operation.feature_ids.length
                  ? ` <span class="muted mono">${escapeHtml(operation.feature_ids[0])}</span>`
                  : ` <span class="muted">(added by hand)</span>`) +
                `</li>`
            )
            .join("") +
          `</ol>`
        : `<div class="muted">empty</div>`;
      return (
        `<li><strong>${escapeHtml(setup.name)}</strong> ` +
        `<span class="muted">setup ${time == null ? "not set" : `${number(time, 2)} min`}, ` +
        `charged once</span>${inner}</li>`
      );
    })
    .join("");
}

// ------------------------------------------------------------------ editing

function operationById(id) {
  return state.plan ? state.plan.operations.find((o) => o.operation_id === id) : null;
}

// Positions are per setup: an operation's number is its place in its own
// fixturing, not in the plan as a whole.
function renumber() {
  setupsOf().forEach((setup) => {
    operationsInSetup(setup.setup_id).forEach((operation, index) => {
      operation.sequence_position = index + 1;
    });
  });
}

/** Show or hide one operation's explanation, and say which state it is in. */
function toggleEvidence(row) {
  if (!row || !row.nextElementSibling) return;
  const shown = row.nextElementSibling.classList.toggle("hidden") === false;
  const button = row.querySelector(".op-details");
  if (button) {
    button.setAttribute("aria-expanded", String(shown));
    button.textContent = shown ? "Hide" : "Details";
  }
}

function wirePlanRows() {
  const body = $("ops-body");

  body.querySelectorAll("tr.plan-row").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("button, select, input")) return;
      toggleEvidence(row);
    });
  });

  // The row click has always revealed the explanation; nothing said so. The
  // button is the same action with a label on it.
  body.querySelectorAll(".op-details").forEach((button) =>
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleEvidence(button.closest("tr.plan-row"));
    })
  );

  // Selecting an operation identifies the feature that caused it, on the model.
  body.querySelectorAll(".feature-link").forEach((link) =>
    link.addEventListener("click", (event) => {
      event.stopPropagation();
      const feature = (state.features ? state.features.features : []).find(
        (item) => item.feature_id === link.dataset.feature
      );
      if (feature && typeof selectFeature === "function") selectFeature(feature, { scroll: true });
    })
  );

  body.querySelectorAll(".op-process").forEach((select) =>
    select.addEventListener("change", () => {
      const operation = operationById(select.dataset.id);
      const chosen = select.value;
      if (chosen === operation.system_suggested_operation) {
        // Picking what was suggested is an acceptance, not a correction.
        operation.user_operation_type = null;
        operation.user_action = ACT.ACCEPTED;
      } else {
        operation.user_operation_type = chosen;
        operation.user_action = ACT.EDITED;
        // The estimate was for the old process and must not carry over.
        operation.system_time_accepted = false;
      }
      renderPlan();
    })
  );

  // A different drilling sequence is a different amount of work: the earlier
  // acceptance does not carry over, exactly as with a change of process. The
  // suggestion stays in system_drilling_steps; picking it again records no choice.
  body.querySelectorAll(".op-drill").forEach((select) =>
    select.addEventListener("change", () => {
      const operation = operationById(select.dataset.id);
      const option = (operation.system_drilling_options || [])[Number(select.value)];
      if (!option) return;
      if (sameSteps(option.steps, operation.system_drilling_steps)) {
        operation.user_drilling_steps = null;
        if (operation.user_operation_type == null && operation.user_action === ACT.EDITED) {
          operation.user_action = ACT.ACCEPTED;
        }
      } else {
        operation.user_drilling_steps = [...option.steps];
        if (operation.user_action === ACT.PENDING || operation.user_action === ACT.ACCEPTED) {
          operation.user_action = ACT.EDITED;
        }
      }
      operation.system_time_accepted = false;
      renderPlan();
    })
  );

  // The engineer's answer to "is this cut, or left as formed?", stored beside the
  // default. Choosing what the default already says records no choice.
  body.querySelectorAll(".op-formed").forEach((button) =>
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const operation = operationById(button.dataset.id);
      const machined = button.dataset.machined === "true";
      const byDefault = (asFormedTypes() || []).includes(operation.feature_type || "");
      operation.user_machined = machined === !byDefault ? null : machined;
      renderPlan();
    })
  );

  body.querySelectorAll(".op-name").forEach((input) =>
    input.addEventListener("input", () => {
      operationById(input.dataset.id).operation_name = input.value;
    })
  );

  ["setup", "cycle"].forEach((which) =>
    body.querySelectorAll(`.op-${which}`).forEach((input) =>
      input.addEventListener("input", () => {
        const operation = operationById(input.dataset.id);
        const raw = input.value.trim();
        const field = which === "setup" ? "user_setup_time" : "user_cycle_time";
        operation[field] = raw === "" ? null : Number(raw);
        if (operation.user_action === ACT.PENDING || operation.user_action === ACT.ACCEPTED) {
          operation.user_action = ACT.EDITED;
        }
        refreshUncostedNote();
        input.classList.toggle("needs-input", timeSource(operation) === TIME_SOURCE.NEEDS_INPUT);
      })
    )
  );

  body.querySelectorAll(".use-est").forEach((button) =>
    button.addEventListener("click", () => {
      const operation = operationById(button.dataset.id);
      operation.system_time_accepted = true;
      operation.user_action = ACT.ACCEPTED;
      renderPlan();
    })
  );

  body.querySelectorAll(".remove-op").forEach((button) =>
    button.addEventListener("click", () => {
      const operation = operationById(button.dataset.id);
      if (operation.origin === "user") {
        // Nothing was predicted for a hand-added line, so there is nothing to keep.
        state.plan.operations = state.plan.operations.filter(
          (o) => o.operation_id !== operation.operation_id
        );
      } else {
        // Keep it, marked rejected: "suggested, and the engineer removed it" is
        // a correction worth recording.
        operation.user_action = ACT.REJECTED;
      }
      renumber();
      renderPlan();
    })
  );

  body.querySelectorAll(".move-up").forEach((button) =>
    button.addEventListener("click", () => move(button.dataset.id, -1))
  );
  body.querySelectorAll(".move-down").forEach((button) =>
    button.addEventListener("click", () => move(button.dataset.id, 1))
  );
}

// ------------------------------------------------------------------ setups

function wireSetupRows() {
  const body = $("ops-body");

  body.querySelectorAll(".setup-name").forEach((input) =>
    input.addEventListener("input", () => {
      const setup = setupById(input.dataset.id);
      setup.name = input.value;
      if (setup.user_action === ACT.PENDING) setup.user_action = ACT.EDITED;
      renderSequence(activeOperations());
      refreshPlanImpact();
    })
  );

  body.querySelectorAll(".setup-time").forEach((input) =>
    input.addEventListener("input", () => {
      const setup = setupById(input.dataset.id);
      const raw = input.value.trim();
      // The system's estimate is never overwritten; the entry sits beside it.
      setup.user_setup_time_minutes = raw === "" ? null : Number(raw);
      if (setup.user_action === ACT.PENDING || setup.user_action === ACT.ACCEPTED) {
        setup.user_action =
          setup.user_setup_time_minutes === setup.system_setup_time_minutes
            ? ACT.ACCEPTED
            : ACT.EDITED;
      }
      input.classList.toggle("needs-input", setupTimeSource(setup) === TIME_SOURCE.NEEDS_INPUT);
      refreshUncostedNote();
      refreshPlanImpact();
    })
  );

  body.querySelectorAll(".setup-use-est").forEach((button) =>
    button.addEventListener("click", () => {
      const setup = setupById(button.dataset.id);
      setup.system_setup_time_accepted = true;
      if (setup.user_action === ACT.PENDING) setup.user_action = ACT.ACCEPTED;
      renderPlan();
    })
  );

  body.querySelectorAll(".setup-remove").forEach((button) =>
    button.addEventListener("click", () => {
      if (operationsInSetup(button.dataset.id).length) return;
      if (state.plan.setups.length <= 1) {
        showError("A plan needs at least one setup.");
        return;
      }
      state.plan.setups = state.plan.setups.filter((s) => s.setup_id !== button.dataset.id);
      renumberSetups();
      renderPlan();
    })
  );

  // Selecting a setup points at every feature it touches, so "what does this
  // fixturing actually reach?" has a visible answer on the model.
  body.querySelectorAll("tr.setup-row").forEach((row) =>
    row.addEventListener("click", (event) => {
      if (event.target.closest("button, select, input")) return;
      const members = operationsInSetup(row.dataset.setup);
      const ids = members.flatMap((operation) => operation.feature_ids || []);
      if (typeof selectFeatures === "function" && ids.length) {
        selectFeatures(ids, { label: setupById(row.dataset.setup).name });
      }
    })
  );

  body.querySelectorAll(".setup-up").forEach((button) =>
    button.addEventListener("click", () => moveSetup(button.dataset.id, -1))
  );
  body.querySelectorAll(".setup-down").forEach((button) =>
    button.addEventListener("click", () => moveSetup(button.dataset.id, 1))
  );

  body.querySelectorAll(".op-setup-choice").forEach((select) =>
    select.addEventListener("change", () => {
      const operation = operationById(select.dataset.id);
      const target = select.value;
      // Choosing the setup the system picked is not a correction any more.
      operation.user_setup_id = target === operation.system_setup_id ? null : target;
      if (operation.user_setup_id && operation.user_action === ACT.PENDING) {
        operation.user_action = ACT.EDITED;
      }
      renumber();
      renderPlan();
    })
  );
}

function renumberSetups() {
  setupsOf().forEach((setup, index) => {
    setup.sequence_position = index + 1;
  });
}

function moveSetup(setupId, offset) {
  const order = setupsOf();
  const index = order.findIndex((s) => s.setup_id === setupId);
  const target = index + offset;
  if (index < 0 || target < 0 || target >= order.length) return;
  order.splice(target, 0, order.splice(index, 1)[0]);
  order.forEach((setup, position) => {
    setup.sequence_position = position + 1;
  });
  renderPlan();
}

$("add-setup").addEventListener("click", () => {
  if (!state.plan) {
    state.plan = {
      setups: [], operations: [], units: "mm", source_filename: "", route_processes: decidedRouteProcesses(), as_formed_feature_types: decidedAsFormedTypes(),
    };
  }
  const used = new Set((state.plan.setups || []).map((s) => s.setup_id));
  let index = used.size + 1;
  while (used.has(`SETUP-${String(index).padStart(3, "0")}`)) index += 1;
  state.plan.setups.push({
    setup_id: `SETUP-${String(index).padStart(3, "0")}`,
    sequence_position: state.plan.setups.length + 1,
    name: `Setup ${state.plan.setups.length + 1}`,
    description: "",
    process_context: null,
    // Added by hand, so the system proposed nothing here — recording a grouping
    // it never made would put a false claim into the feedback data.
    origin: "user",
    system_operation_ids: [],
    system_grouping: "user_defined",
    system_grouping_reason: "Added by the engineer; the system proposed no such setup.",
    system_grouping_confidence: "low",
    system_grouping_assumptions: [],
    system_setup_time_minutes: null,
    system_setup_time_source: null,
    user_action: ACT.ADDED,
    user_setup_time_minutes: null,
    system_setup_time_accepted: false,
  });
  renumberSetups();
  renderPlan();
});

// ---------------------------------------------------------- the cost impact
//
// The browser does no costing arithmetic: it sends the plan to the backend,
// which prices it through the same unmodified calculators an estimate uses. So
// the preview and the estimate cannot disagree, and moving an operation between
// setups shows its real effect before anyone commits to it.

let lastImpact = null;
let impactPending = null;

function refreshPlanImpact() {
  const payload = planPayload();
  const box = $("plan-impact");
  if (!payload || !payload.operations.length) {
    box.classList.add("hidden");
    return;
  }
  clearTimeout(impactPending);
  impactPending = setTimeout(async () => {
    const params = new URLSearchParams({ quantity: String(Number($("quantity").value) || 1) });
    if ($("rate_set_id").value) params.set("rate_set_id", $("rate_set_id").value);
    if ($("rate_set_version").value) params.set("rate_set_version", $("rate_set_version").value);
    let preview;
    try {
      preview = await api(`/operations/plan-cost-preview?${params}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch (error) {
      box.classList.add("hidden");
      return;
    }
    renderPlanImpact(preview);
  }, 250);
}

function renderPlanImpact(preview) {
  const box = $("plan-impact");
  const time = preview.time;
  const delta = lastImpact
    ? {
        setup: round2(time.total_setup_time_minutes - lastImpact.time.total_setup_time_minutes),
        cycle: round2(time.total_cycle_time_minutes - lastImpact.time.total_cycle_time_minutes),
        cost: round2(preview.total_operation_cost - lastImpact.total_operation_cost),
        setups: time.setup_count - lastImpact.time.setup_count,
      }
    : null;

  const signed = (value, unit) => `${value > 0 ? "+" : ""}${number(value, 2)}${unit}`;

  const changes = [];
  if (delta && delta.setups) changes.push(signed(delta.setups, " setup"));
  if (delta && delta.setup) changes.push(signed(delta.setup, " min setup time"));
  if (delta && delta.cycle) changes.push(signed(delta.cycle, " min cycle time"));
  if (delta && delta.cost) changes.push(`${signed(delta.cost, "")} ${escapeHtml(preview.currency)}`);

  box.innerHTML =
    `<div class="impact-figures">
       <span><strong>${time.setup_count}</strong> setup${time.setup_count === 1 ? "" : "s"}</span>
       <span>Setup time <strong>${number(time.total_setup_time_minutes, 2)} min</strong>
         <span class="muted">(charged once each)</span></span>
       <span>Cycle time <strong>${number(time.total_cycle_time_minutes, 2)} min</strong>
         <span class="muted">(over ${time.quantity} part(s))</span></span>
       <span>Operation cost <strong>${money(preview.total_operation_cost, preview.currency)}</strong>
         <span class="muted">= ${money(preview.setup_cost, preview.currency)} setup +
         ${money(preview.run_cost, preview.currency)} run</span></span>
     </div>` +
    (changes.length
      ? `<div class="impact-delta">Since your last change: ${changes.join(" · ")}</div>`
      : "") +
    (time.setups_needing_time.length
      ? `<div class="impact-warning">${time.setups_needing_time.length} setup(s) have no committed
         setup time, so no setup cost is charged for them. Nothing is assumed.</div>`
      : "") +
    (preview.unpriced_processes.length
      ? `<div class="impact-warning">This rate set prices no
         ${escapeHtml(preview.unpriced_processes.join(", "))}, so those operations are not costed.</div>`
      : "");
  box.classList.remove("hidden");
  lastImpact = preview;
}

function round2(value) {
  return Math.round(value * 100) / 100;
}

// Reordering never crosses a setup boundary: moving work to another fixturing is
// a different decision with a different cost, and has its own control.
function move(operationId, offset) {
  const operation = operationById(operationId);
  const order = operationsInSetup(setupOf(operation));
  const index = order.findIndex((o) => o.operation_id === operationId);
  const target = index + offset;
  if (index < 0 || target < 0 || target >= order.length) return;
  order.splice(target, 0, order.splice(index, 1)[0]);
  order.forEach((o, position) => {
    o.sequence_position = position + 1;
  });
  renderPlan();
}

$("use-all-estimates").addEventListener("click", () => {
  activeOperations().forEach((operation) => {
    if (systemTimeApplies(operation)) {
      operation.system_time_accepted = true;
      if (operation.user_action === ACT.PENDING) operation.user_action = ACT.ACCEPTED;
    }
  });
  renderPlan();
});

$("add-op").addEventListener("click", () => {
  if (!state.plan) {
    state.plan = {
      setups: [], operations: [], units: "mm", source_filename: "", route_processes: decidedRouteProcesses(), as_formed_feature_types: decidedAsFormedTypes(),
    };
  }
  manualCounter += 1;
  if (!state.plan.setups || !state.plan.setups.length) $("add-setup").click();
  // A line added under a chosen route starts as one of the route's own
  // processes, so it lands in the route's stage rather than outside it.
  const processes = [
    ...(routeProcesses() || []),
    ...Object.keys((state.rateSet && state.rateSet.operations) || {}),
  ];
  const target = setupsOf()[setupsOf().length - 1];
  state.plan.operations.push({
    operation_id: `OP-USER-${manualCounter}`,
    sequence_position: operationsInSetup(target.setup_id).length + 1,
    system_setup_id: target.setup_id,
    user_setup_id: null,
    feature_ids: [],
    feature_label: "",
    operation_name: `Operation ${manualCounter}`,
    origin: "user",
    user_action: ACT.ADDED,
    system_suggested_operation: null,
    system_suggestion_confidence: null,
    system_suggestion_rule: null,
    system_time_status: "needs_user_input",
    system_time_reason: "Added by the engineer; the system proposed nothing for this line.",
    system_time_evidence: [],
    system_time_assumptions: [],
    system_time_missing: [],
    user_operation_type: processes[0] || "milling",
    user_setup_time: null,
    user_cycle_time: null,
    system_time_accepted: false,
    source_faces: [],
    source_facets: [],
    dimensions: {},
  });
  renumber();
  renderPlan();
});

// The visible half of the rule the backend enforces: a row with no committed
// cycle time is not costed, and is said so rather than quietly dropped.
function refreshUncostedNote() {
  const uncosted = activeOperations().filter((operation) => !isCostable(operation));
  const note = $("ops-uncosted");
  setTimeout(notifyPlanChanged, 0);
  if (!uncosted.length) {
    note.classList.add("hidden");
    note.textContent = "";
    return;
  }
  note.textContent =
    `${uncosted.length} operation${uncosted.length === 1 ? "" : "s"} ` +
    `${uncosted.length === 1 ? "has" : "have"} no committed time, so ` +
    `${uncosted.length === 1 ? "it is" : "they are"} not costed. ` +
    "Accept the estimate or enter a cycle time to include " +
    `${uncosted.length === 1 ? "it" : "them"}. Nothing is assumed.`;
  note.classList.remove("hidden");
}

// Whatever the plan now says about committed time, the overview says the same:
// this is the one count, read through `isCostable`, never a second opinion.
function notifyPlanChanged() {
  if (typeof refreshFlowChrome === "function") refreshFlowChrome();
}

/**
 * The plan as the backend's `ManufacturingPlan` expects it. The private `_`
 * fields used only for the UI's explanation row are stripped.
 */
function planPayload() {
  if (!state.plan || !state.plan.operations.length) return null;
  return {
    setups: state.plan.setups || [],
    units: state.plan.units || "mm",
    source_filename: state.plan.source_filename || "",
    // Copied explicitly: this builder is a persistence path, and a field it
    // does not copy never reaches the server.
    route_processes: routeProcesses(),
    as_formed_feature_types: asFormedTypes(),
    time_profile_id: state.plan.time_profile_id || null,
    time_profile_version: state.plan.time_profile_version || null,
    time_profile_name: state.plan.time_profile_name || null,
    operations: state.plan.operations.map((operation) => {
      const copy = { ...operation };
      Object.keys(copy)
        .filter((key) => key.startsWith("_"))
        .forEach((key) => delete copy[key]);
      return copy;
    }),
  };
}
