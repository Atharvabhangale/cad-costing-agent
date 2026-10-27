// The assembly panel: the component tree, the bill of materials, and the
// component-cost roll-up.
//
// Every figure here comes from the backend. The browser adds nothing up — not
// the quantities, not the extended costs, not the total — because a plausible
// number invented in JavaScript beside a real one is indistinguishable from it.
// A test greps this file to keep it that way.

const GEOMETRY_STATUS_LABELS = {
  measured: "measured",
  no_geometry_in_file: "no geometry in file",
  not_measured: "not measured",
  failed: "measurement failed",
};

function resetAssemblyPanel() {
  state.assembly = null;
  state.assemblyCost = null;
  state.assemblyCostStale = false;
  state.explodedView = null;
  state.componentCosting = null;
  state.assemblyPlan = null;
  state.planComponentId = null;
  $("component-costing-card").classList.add("hidden");
  $("component-costing-body").innerHTML = "";
  $("component-detail").innerHTML = "";
  $("component-plan-card").classList.add("hidden");
  $("component-plan-body").innerHTML = "";
  $("component-plan-detail").innerHTML = "";
  $("component-plan-status").textContent = "";
  $("assembly-card").classList.add("hidden");
  $("assembly-tree").innerHTML = "";
  hideExplodeControls();
  $("assembly-body").innerHTML = "";
  $("assembly-cost-body").innerHTML = "";
  $("assembly-cost-card").classList.add("hidden");
  $("decompose").disabled = true;
  $("assembly-status").textContent = "";
}

// Called by app.js once a file has been identified. Whether a file is an
// assembly is the backend's answer, read from the imported document — the point
// of stating it at upload is that nobody discovers afterwards that a 43-part
// assembly was costed as one lump.
function refreshAssemblyPanel() {
  const document_ = state.document;
  $("assembly-card").classList.remove("hidden");
  $("assembly-cost-card").classList.add("hidden");
  $("component-costing-card").classList.add("hidden");
  $("component-costing-body").innerHTML = "";
  $("component-detail").innerHTML = "";
  $("component-plan-card").classList.add("hidden");
  $("component-plan-body").innerHTML = "";
  $("component-plan-detail").innerHTML = "";
  state.componentCosting = null;
  state.assemblyPlan = null;
  state.planComponentId = null;
  $("assembly-tree").innerHTML = "";
  hideExplodeControls();
  $("assembly-body").innerHTML = "";

  if (!document_) {
    $("decompose").disabled = true;
    $("assembly-status").textContent = "Upload a CAD file to see whether it is an assembly.";
    return;
  }

  if (document_.structure_state !== "structure_available") {
    $("decompose").disabled = true;
    const report = document_.capabilities.find((c) => c.capability === "assembly");
    $("assembly-status").textContent =
      report && report.detail
        ? `Single part. ${report.detail}`
        : "This file is a single part, not an assembly.";
    return;
  }

  $("decompose").disabled = false;
  $("assembly-status").textContent = document_.assembly_note || "This file is an assembly.";
}

$("decompose").addEventListener("click", async () => {
  if (!state.file) return;
  $("decompose").disabled = true;
  $("assembly-status").textContent =
    "Reading the component structure… on a very large assembly this takes several seconds.";

  const body = new FormData();
  body.append("file", state.file);
  try {
    const result = await api("/cad/assembly?measure=true&with_mesh=true", {
      method: "POST",
      body,
    });
    state.assembly = result.document.assembly;
    state.explodedView = result.exploded_view || null;
    // Suggestions, not decisions. Every one says so, and the user's answers are
    // written into `user_*` fields beside them rather than over them.
    state.componentCosting = result.component_costing || [];
    renderAssembly(result);
  } catch (error) {
    $("assembly-status").textContent = error.message;
    return;
  } finally {
    $("decompose").disabled = false;
  }

  // The structure is on screen; the plans follow it without being asked for.
  // Deliberately not awaited above: the tree, the bill of materials and the
  // classifications are usable immediately, and feature recognition across
  // every manufactured component takes seconds on top of the read.
  generateComponentPlans({ automatic: true });
});

function renderAssembly(result) {
  const assembly = result.document.assembly;
  const definitions = assembly.definitions;
  const parts = definitions.filter((d) => !d.is_assembly);
  const measured = definitions.filter((d) => d.geometry_status === "measured");

  // The viewer draws the assembly component by component when it can — one
  // geometry per definition, one mesh per occurrence. When it cannot, the tree
  // is still shown: a structure is worth having without a picture.
  if (result.mesh) {
    viewer().showAssembly(result.mesh, result.exploded_view);
    showExplodeControls(result.exploded_view);
  } else {
    hideExplodeControls();
  }

  renderAssemblyTree(assembly, Boolean(result.mesh));

  const rows = definitions
    .map((definition) => {
      const geometry = definition.geometry;
      const occurrences = assembly.occurrences.filter(
        (o) => o.definition_id === definition.definition_id
      );
      const first = occurrences[0];
      // Only a drawn component can be selected. A sub-assembly is never drawn
      // — its children are — and a component the file carries no geometry for
      // has nothing to show, so neither is offered as a link that would do
      // nothing.
      const drawn = Boolean(
        result.mesh &&
          result.mesh.instances.some((instance) => instance.definition_id === definition.definition_id)
      );
      const selectable = drawn && first;
      return `
        <tr class="${definition.is_assembly ? "row-subassembly" : ""}">
          <td>${"&nbsp;".repeat((first ? first.depth : 0) * 3)}${
        selectable
          ? `<button type="button" class="link component-pick" data-occurrence="${escapeHtml(
              first.occurrence_id
            )}">${escapeHtml(definition.name)}</button>`
          : escapeHtml(definition.name)
      }</td>
          <td class="mono small">${escapeHtml(definition.part_number || "—")}</td>
          <td>${definition.is_assembly ? "sub-assembly" : "part"}</td>
          <td class="num">${definition.is_assembly ? "—" : "×" + definition.quantity}</td>
          <td><span class="geo-${definition.geometry_status}">${
        GEOMETRY_STATUS_LABELS[definition.geometry_status] || definition.geometry_status
      }</span></td>
          <td class="num">${geometry ? number(geometry.volume, 3) + " mm³" : "—"}</td>
        </tr>`;
    })
    .join("");

  $("assembly-body").innerHTML = `
    <table class="mini bom">
      <thead>
        <tr><th>Component</th><th>Part number</th><th>Type</th><th>Qty</th>
            <th>Geometry</th><th>Volume</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="hint">
      ${assembly.definition_count ?? definitions.length} definitions ·
      ${assembly.occurrences.length} occurrences ·
      ${assembly.max_depth} level(s) deep ·
      ${measured.length} of ${parts.length} parts measured.
      A quantity is how many times the assembly uses a component, not how many
      assemblies are being made.
    </p>
    ${
      result.mesh && result.mesh.instances.length
        ? `<div class="row">
             <button type="button" id="restore-components" class="secondary">Show all components</button>
             <span class="muted small" id="isolated-note"></span>
           </div>
           <p class="provenance">${escapeHtml(result.mesh.tessellation.note)}</p>`
        : `<p class="muted small">${escapeHtml(
            result.mesh_error ||
              "This assembly's components carry no geometry, so they cannot be drawn or highlighted in the viewer. The structure, quantities and placements above are unaffected."
          )}</p>`
    }
    ${(assembly.warnings || [])
      .map((w) => `<p class="assembly-warning">${escapeHtml(w.message)}</p>`)
      .join("")}`;

  $("assembly-status").textContent = `${assembly.name} — read in ${number(
    assembly.read_ms,
    0
  )} ms.`;

  document.querySelectorAll(".component-pick").forEach((button) => {
    button.addEventListener("click", () => selectComponent(button.dataset.occurrence));
  });
  document.querySelectorAll(".node-pick").forEach((button) => {
    button.addEventListener("click", () => selectComponent(button.dataset.occurrence));
  });
  const restore = $("restore-components");
  if (restore) {
    restore.addEventListener("click", () => {
      viewer().restoreComponents();
      $("isolated-note").textContent = "";
      document
        .querySelectorAll(".component-pick")
        .forEach((b) => b.classList.remove("picked"));
    });
  }
  $("assembly-cost-card").classList.remove("hidden");
  $("component-plan-card").classList.remove("hidden");
  renderComponentCosting();
  renderComponentPlans();
}

// ---------------------------------------------- component manufacturing plans

const PLAN_STATUS_LABELS = {
  not_generated: "Not generated",
  generated: "Generated",
  needs_user_input: "Needs user input",
  not_applicable: "No manufacturing plan",
  blocked: "Cannot be planned yet",
};

function componentPlanFor(definitionId) {
  if (!state.assemblyPlan) return null;
  return (
    (state.assemblyPlan.component_plans || []).find(
      (entry) => entry.definition_id === definitionId
    ) || null
  );
}

// A component's plan status, before anything has been generated. The classification
// decides who is even eligible, so the status shown here follows the *current*
// classification rather than whatever it was when the plans were built — a
// component just reclassified as purchased must not still read "generated".
function provisionalStatus(profile) {
  const chosen = effectiveClassification(profile);
  if (profile.is_assembly) return { status: "not_applicable", reason: "sub-assembly" };
  if (chosen === "purchased" || chosen === "standard") {
    return { status: "not_applicable", reason: "bought rather than made" };
  }
  if (chosen === "excluded") {
    return { status: "not_applicable", reason: "excluded from the cost by choice" };
  }
  if (chosen === "manufactured") return { status: "not_generated", reason: "" };
  return { status: "blocked", reason: "not classified yet" };
}

// What is still missing from a component's plan, read from the plan as it
// stands right now.
//
// `entry.status` and `entry.missing` were computed by the server when the plans
// were built, so after an edit here they are stale — the operations table would
// show a time as committed while this panel still listed it as missing. The
// resolution rule is not reimplemented: `plan.js` owns `finalCycle` and
// `setupTime`, which are the single implementation of "which time counts", and
// this file is loaded after it. Guarded forward all the same, so a load-order
// change degrades to the server's answer rather than throwing.
function livePlanGaps(plan) {
  if (typeof finalCycle !== "function" || typeof setupTime !== "function") return null;
  const gaps = [];
  const active = plan.operations.filter((operation) => operation.user_action !== "rejected");
  active.forEach((operation) => {
    if (finalCycle(operation) === null) {
      gaps.push(
        `${operation.operation_id} (${operation.operation_name}) has no committed cycle time`
      );
    }
  });
  plan.setups.forEach((setup) => {
    const members = active.filter(
      (operation) => (operation.user_setup_id || operation.system_setup_id) === setup.setup_id
    );
    if (members.length && setupTime(setup) === null) {
      gaps.push(`${setup.setup_id} (${setup.name}) has no committed setup time`);
    }
  });
  return gaps;
}

// The status as it stands, which is a reading of the plan rather than an
// opinion about it: complete when nothing is missing, needing input otherwise.
function livePlanStatus(entry) {
  if (!entry || !entry.plan) return entry ? entry.status : "not_generated";
  const gaps = livePlanGaps(entry.plan);
  if (gaps === null) return entry.status;
  return gaps.length ? "needs_user_input" : "generated";
}

// Committed time, or the estimate with a word saying it is only an estimate.
//
// A freshly generated plan has committed nothing, so its committed total is a
// genuine 0.00 — which reads exactly like "this component takes no time to
// make". Showing the estimate labelled as an estimate is the honest answer;
// showing 0.00 is the one thing this must not do.
function planMinutes(entry, stale, which) {
  if (!entry || !entry.plan || stale) return "—";
  const key = which === "setup" ? "setup_time_minutes" : "cycle_time_minutes";
  const estimateKey =
    which === "setup" ? "estimated_setup_time_minutes" : "estimated_cycle_time_minutes";

  // Once the component has actually been costed *and* every time in its plan is
  // committed, the roll-up carries the server's own figures for it. A plan with
  // nothing committed reports a genuine 0.00 — which would print as "0 min" and
  // read as "this takes no time", so the estimate is shown instead, labelled.
  const line = (state.assemblyCost ? state.assemblyCost.lines : []).find(
    (item) => item.definition_id === entry.definition_id
  );
  if (
    livePlanStatus(entry) === "generated" &&
    line &&
    line[key] !== null &&
    line[key] !== undefined
  ) {
    return `${number(line[key], 2)} min`;
  }
  const estimated = entry[estimateKey];
  if (estimated === null || estimated === undefined) return "not committed";
  return `<span class="muted">${number(estimated, 2)} min est.</span>`;
}

function renderComponentPlans() {
  const profiles = (state.componentCosting || []).filter((p) => !p.is_assembly);
  if (!profiles.length) {
    $("component-plan-card").classList.add("hidden");
    return;
  }
  $("component-plan-card").classList.remove("hidden");

  const rows = profiles
    .map((profile) => {
      const entry = componentPlanFor(profile.definition_id);
      const provisional = provisionalStatus(profile);
      // A generated plan is only shown as generated while the classification
      // that earned it still stands.
      const stale = entry && provisional.status !== "not_generated";
      const status = stale || !entry ? provisional.status : livePlanStatus(entry);
      const id = escapeHtml(profile.definition_id);
      const summary = entry && entry.time_summary;

      const counts =
        entry && entry.plan && !stale
          ? `${entry.feature_count} feature(s) · ${entry.plan.operations.length} operation(s) ·
             ${entry.plan.setups.length} setup(s) · setup ${planMinutes(
              entry,
              stale,
              "setup"
            )} · cycle ${planMinutes(entry, stale, "cycle")} per part`
          : "";

      // Three columns, with everything else on a full-width sub-row. The
      // assembly panel is a narrow column: eight columns overflowed it by
      // 111 px and pushed the whole page sideways, which is the same defect
      // the classification table hit in Step 26.
      return `
        <tr class="decision-row">
          <td>
            <button type="button" class="link plan-pick" data-definition="${id}">${escapeHtml(
        profile.name
      )}</button>
            <div class="tiny muted">×${profile.quantity} in this assembly</div>
          </td>
          <td><span class="chip class-${escapeHtml(
            effectiveClassification(profile) || "unresolved"
          )}">${escapeHtml(
        CLASSIFICATION_LABELS[effectiveClassification(profile)] || "Unresolved"
      )}</span></td>
          <td><span class="plan-status ${escapeHtml(status)}">${escapeHtml(
        PLAN_STATUS_LABELS[status] || status
      )}</span></td>
        </tr>
        ${
          counts
            ? `<tr class="reason-row"><td colspan="3" class="tiny">${counts}</td></tr>`
            : ""
        }
        ${
          !entry || stale
            ? provisional.reason
              ? `<tr class="reason-row"><td colspan="3" class="tiny muted">${escapeHtml(
                  provisional.reason
                )}</td></tr>`
              : ""
            : status === "needs_user_input"
            ? `<tr class="reason-row"><td colspan="3" class="tiny muted">${escapeHtml(
                entry.reason_detail ||
                  "This plan is not finished: some times are still uncommitted. The operations they belong to are left out of the cost rather than defaulted to zero."
              )}</td></tr>`
            : entry.reason_detail && !entry.plan
            ? `<tr class="reason-row"><td colspan="3" class="tiny muted">${escapeHtml(
                entry.reason_detail
              )}</td></tr>`
            : ""
        }`;
    })
    .join("");

  $("component-plan-body").innerHTML = `
    <table class="mini bom plans">
      <thead><tr><th>Component</th><th>Classification</th><th>Plan</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="hint small">
      Setup and cycle time are reported separately, never added together: setup
      is charged once per setup and cycle time once per part, so a combined
      figure cannot be multiplied by anything correctly. One plan per component
      definition — a component used twice is planned once and its cost
      multiplied afterwards.
    </p>
    ${
      state.assemblyPlan && state.assemblyPlan.note
        ? `<p class="provenance">${escapeHtml(state.assemblyPlan.note)}</p>`
        : ""
    }`;

  document.querySelectorAll(".plan-pick").forEach((button) => {
    button.addEventListener("click", () => {
      renderComponentPlanDetail(button.dataset.definition);
      const occurrence = (state.assembly.occurrences || []).find(
        (item) => item.definition_id === button.dataset.definition
      );
      if (occurrence) selectComponent(occurrence.occurrence_id);
    });
  });
}

// What a component looks like when it is selected. A manufactured one shows its
// plan; a purchased, standard or excluded one shows its classification and its
// cost instead of an empty plan, because an empty plan reads as "no work
// needed" and that is not what it means.
function renderComponentPlanDetail(definitionId) {
  const panel = $("component-plan-detail");
  const profile = profileFor(definitionId);
  if (!profile) {
    panel.innerHTML = "";
    return;
  }
  const entry = componentPlanFor(definitionId);
  const chosen = effectiveClassification(profile);
  const line = (state.assemblyCost ? state.assemblyCost.lines : []).find(
    (item) => item.definition_id === definitionId
  );
  const currency = state.assemblyCost && state.assemblyCost.currency;

  if (chosen !== "manufactured") {
    panel.innerHTML = `
      <h3>${escapeHtml(profile.name)}</h3>
      <p><span class="chip class-${escapeHtml(chosen || "unresolved")}">${escapeHtml(
      CLASSIFICATION_LABELS[chosen] || "Unresolved"
    )}</span>
        <span class="plan-status not_applicable">${escapeHtml(
          PLAN_STATUS_LABELS[chosen === "unresolved" || !chosen ? "blocked" : "not_applicable"]
        )}</span></p>
      <table class="mini"><tbody>
        <tr><th>Quantity</th><td>×${profile.quantity} in this assembly</td></tr>
        <tr><th>Unit cost</th><td>${
          profile.unit_cost === null || profile.unit_cost === undefined
            ? "not entered"
            : money(profile.unit_cost, profile.currency || currency)
        }</td></tr>
        <tr><th>Contribution</th><td>${
          line && line.extended_cost !== null
            ? money(line.extended_cost, line.currency || currency)
            : "not costed yet"
        }</td></tr>
      </tbody></table>
      <p class="provenance">${escapeHtml(
        entry ? entry.reason_detail : "This component has no machining plan."
      )}</p>`;
    return;
  }

  if (!entry || !entry.plan) {
    panel.innerHTML = `
      <h3>${escapeHtml(profile.name)}</h3>
      <p><span class="plan-status not_generated">${escapeHtml(
        PLAN_STATUS_LABELS.not_generated
      )}</span></p>
      <p class="hint small">${escapeHtml(
        entry && entry.reason_detail
          ? entry.reason_detail
          : "No plan has been generated for this component yet. That is not the same as needing no machining — generate the component plans above."
      )}</p>`;
    return;
  }

  const plan = entry.plan;
  // Read from the plan as it stands, not from what the server said when it was
  // built: an edit in the operations table has to be reflected here or the two
  // panels contradict each other about what is committed.
  const gaps = livePlanGaps(plan) || entry.missing;
  const status = livePlanStatus(entry);
  const setups = [...plan.setups]
    .sort((a, b) => a.sequence_position - b.sequence_position)
    .map((setup) => {
      const members = plan.operations.filter(
        (operation) =>
          (operation.user_setup_id || operation.system_setup_id) === setup.setup_id &&
          operation.user_action !== "rejected"
      );
      const committed =
        setup.user_setup_time_minutes !== null && setup.user_setup_time_minutes !== undefined
          ? `${number(setup.user_setup_time_minutes, 2)} min (you entered it)`
          : setup.system_setup_time_accepted
          ? `${number(setup.system_setup_time_minutes, 2)} min (system estimate, accepted)`
          : "no committed setup time";
      return `
        <div class="setup-block">
          <div class="setup-head">Setup ${setup.sequence_position} — ${escapeHtml(
        setup.name || setup.setup_id
      )}</div>
          <div class="tiny muted">Setup time: ${escapeHtml(
            committed
          )} · charged once for this setup</div>
          <ul class="missing-list" style="list-style:disc">
            ${members
              .map(
                (operation) =>
                  `<li style="color:inherit">${escapeHtml(
                    operation.operation_name || operation.operation_id
                  )} — ${
                    operation.user_cycle_time !== null &&
                    operation.user_cycle_time !== undefined
                      ? `${number(operation.user_cycle_time, 2)} min (you entered it)`
                      : operation.system_time_accepted
                      ? `${number(finalCycle(operation), 2)} min (system estimate, accepted)`
                      : "no committed cycle time"
                  }${
                    operation.feature_ids.length
                      ? ` · ${escapeHtml(operation.feature_ids.join(", "))}`
                      : ""
                  }</li>`
              )
              .join("")}
          </ul>
        </div>`;
    })
    .join("");

  panel.innerHTML = `
    <h3>${escapeHtml(profile.name)}</h3>
    <p><span class="chip class-manufactured">Manufactured</span>
      <span class="plan-status ${escapeHtml(status)}">${escapeHtml(
    PLAN_STATUS_LABELS[status] || status
  )}</span></p>
    <table class="mini"><tbody>
      <tr><th>Quantity</th><td>×${entry.quantity} in this assembly · one plan, reused</td></tr>
      <tr><th>Features</th><td>${entry.feature_count}${
    entry.declined_feature_count
      ? ` (${entry.declined_feature_count} recognized but not machinable)`
      : ""
  }</td></tr>
      <tr><th>Operations</th><td>${plan.operations.length}</td></tr>
      <tr><th>Setups</th><td>${plan.setups.length}</td></tr>
      <tr><th>Setup time</th><td>${planMinutes(entry, false, "setup")}${
    line && line.setup_time_minutes !== null && line.setup_time_minutes !== undefined
      ? ", charged once for the setup"
      : ""
  }</td></tr>
      <tr><th>Cycle time</th><td>${planMinutes(entry, false, "cycle")}${
    line && line.cycle_time_minutes !== null && line.cycle_time_minutes !== undefined
      ? " per part"
      : ""
  }</td></tr>
      <tr><th>Total manufacturing time</th><td>${
        line &&
        line.setup_time_minutes !== null &&
        line.setup_time_minutes !== undefined &&
        line.cycle_time_minutes !== null
          ? `${number(line.setup_time_minutes, 2)} min setup + ${number(
              line.cycle_time_minutes,
              2
            )} min cycle — reported apart, because one is charged per setup and the other per part`
          : status === "generated"
          ? "<span class='muted'>every time is committed — cost the components to see the totals</span>"
          : "<span class='muted'>not yet — some times are still uncommitted</span>"
      }</td></tr>
      <tr><th>Component cost</th><td>${
        line && line.extended_cost !== null
          ? `${money(line.per_unit_cost, line.currency || currency)} each · ${money(
              line.extended_cost,
              line.currency || currency
            )} for ${line.quantity}`
          : "not costed yet"
      }</td></tr>
    </tbody></table>
    ${
      gaps.length
        ? `<p class="assembly-warning"><strong>Needs input</strong> — these operations are left
             out of the cost rather than defaulted to zero:</p>
           <ul class="missing-list">${gaps
             .map((item) => `<li>${escapeHtml(item)}</li>`)
             .join("")}</ul>`
        : `<p class="hint small">Every operation and setup in this plan carries a committed
             time. Setup is charged once for its setup; cycle time is charged per part.</p>`
    }
    <h3 style="margin-top:12px">Manufacturing plan</h3>
    ${setups}
    <div class="row">
      <button type="button" id="edit-component-plan" class="secondary"
              data-definition="${escapeHtml(definitionId)}">
        Edit this component's plan
      </button>
      <span class="muted small">Opens it in the operations table, where every edit works
        exactly as it does for a single part.</span>
    </div>`;

  const edit = $("edit-component-plan");
  if (edit) edit.addEventListener("click", () => editComponentPlan(definitionId));
}

// Retarget the existing operations table at this component's plan.
//
// Deliberately not a second editing UI: `plan.js` already implements accepting
// and rejecting suggestions, changing a process, changing times, adding and
// removing operations, creating and removing setups, moving an operation
// between setups, changing a setup's time and reordering both — all with the
// system-versus-user semantics Steps 20 and 21 established. Pointing
// `state.plan` at the component's own plan object means every one of those
// edits works unchanged and mutates the component's plan in place, rather than
// a parallel implementation that could drift from it.
function editComponentPlan(definitionId) {
  const entry = componentPlanFor(definitionId);
  if (!entry || !entry.plan) return;
  state.plan = entry.plan;
  state.planComponentId = definitionId;
  renderPlan();

  // Go where the plan now is. The operations table lives in the Manufacturing
  // section, so retargeting it while the Assembly section is showing left the
  // table correct and invisible — `scrollIntoView` on an element inside a
  // hidden pane scrolls nowhere, so the click looked like it had done nothing.
  // `setFlow` lands at the top of the section, where `plan-scope` says which
  // component is being edited before the routing itself.
  //
  // Guarded forward: `estimateflow.js` is loaded after this file, and the rule
  // here is that a file may only call into one loaded before it. Without the
  // router the previous behaviour stands.
  if (typeof setFlow === "function") {
    setFlow("manufacturing");
  } else {
    $("ops-table").scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

// ------------------------------------------------- component plan generation
//
// A manufactured component's cost is its plan's cost, so the plans are built as
// part of decomposing rather than waiting behind a button. Before this, "Cost
// every component" on an assembly whose plans had never been generated produced
// a complete-looking total of material and tooling with nothing for the making
// — the dependency between the two buttons was real, undeclared, and invisible
// in the result.
//
// Generating is not accepting. Every operation comes back with the system's
// estimated time and `system_time_accepted` false, exactly as a single part's
// suggestions do, and nothing is committed until a person says so.

/** The components a plan could actually be built for, right now.
 *
 * The classification decides eligibility and the geometry decides possibility;
 * asking both here means a 102-component export that carries no component
 * geometry is not sent away to spend a minute discovering it can plan nothing.
 */
function plannableComponents() {
  const definitions = new Map(
    ((state.assembly && state.assembly.definitions) || []).map((d) => [d.definition_id, d])
  );
  return (state.componentCosting || []).filter((profile) => {
    if (profile.is_assembly) return false;
    if (effectiveClassification(profile) !== "manufactured") return false;
    const definition = definitions.get(profile.definition_id);
    return !!definition && definition.geometry_status === "measured";
  });
}

/** Which plannable components have no plan yet. */
function componentsWithoutPlans() {
  return plannableComponents().filter((profile) => {
    const entry = componentPlanFor(profile.definition_id);
    return !entry || !entry.plan;
  });
}

/** Build the manufactured components' plans through the existing endpoint.
 *
 * `merge` keeps any plan we already hold, because a rebuild returns freshly
 * estimated times with nothing accepted — adopting those wholesale would throw
 * away every time the engineer has committed. A component that is no longer
 * manufactured comes back without a plan and that answer is taken, which is how
 * a reclassified component loses the plan it should not have.
 */
async function generateComponentPlans({ merge = false, automatic = false } = {}) {
  if (!state.file) return null;
  if (automatic && !plannableComponents().length) return null;

  $("generate-component-plans").disabled = true;
  $("component-plan-card").classList.remove("hidden");
  $("component-plan-status").textContent = automatic
    ? "Recognizing features and planning each manufactured component…"
    : "Rebuilding every manufactured component's plan…";

  const body = new FormData();
  body.append("file", state.file);
  body.append("rate_set_id", $("rate_set_id").value);
  // Named `timeProfile`, not `profile`: everywhere else in this file a
  // `profile` is a component's costing profile, and reusing the word for a
  // DOM element makes the two indistinguishable at a glance.
  const timeProfile = $("time_profile_id");
  if (timeProfile && timeProfile.value) {
    body.append("time_profile_id", timeProfile.value);
  }
  // The same decisions the cost request sends, so a component excluded here is
  // not planned there — the endpoint refuses that contradiction rather than
  // planning it anyway.
  body.append("components", JSON.stringify(componentDecisions()));

  try {
    const result = await api("/cad/assembly/plans", { method: "POST", body });
    state.assemblyPlan = merge
      ? mergeComponentPlans(state.assemblyPlan, result.plan)
      : result.plan;
    state.planComponentId = null;
    state.plan = null;
    const planned = (state.assemblyPlan.component_plans || []).filter((entry) => entry.plan);
    $("component-plan-status").textContent =
      `${planned.length} component plan(s) ready in ${number(result.read_ms, 0)} ms. ` +
      "Every time in them is the system's estimate: nothing is committed until you accept " +
      "or type one, and an operation with no committed time is left out of the cost.";
    renderComponentPlans();
    renderPlan();
    if (typeof refreshFlowChrome === "function") refreshFlowChrome();
    return state.assemblyPlan;
  } catch (error) {
    $("component-plan-status").textContent = error.message;
    return null;
  } finally {
    $("generate-component-plans").disabled = false;
  }
}

/** Fresh plans, except where we already hold one that may carry commitments. */
function mergeComponentPlans(existing, incoming) {
  if (!existing) return incoming;
  const held = new Map(
    (existing.component_plans || []).map((entry) => [entry.definition_id, entry])
  );
  return {
    ...incoming,
    component_plans: (incoming.component_plans || []).map((entry) => {
      const ours = held.get(entry.definition_id);
      return ours && ours.plan && entry.plan ? ours : entry;
    }),
  };
}

/** Fill in plans for components that have just become manufactured.
 *
 * Called after a classification changes. It asks the server only when there is
 * a real gap: dropping a plan a component is no longer entitled to needs no
 * round trip, and rebuilding the ones that already exist would discard
 * committed times.
 */
async function reconcileComponentPlans() {
  if (!state.assemblyPlan) return;

  // A component that is no longer manufactured keeps no plan. Done here rather
  // than by asking the server, because the answer is already known.
  const eligible = new Set(plannableComponents().map((profile) => profile.definition_id));
  let dropped = false;
  state.assemblyPlan = {
    ...state.assemblyPlan,
    component_plans: (state.assemblyPlan.component_plans || []).map((entry) => {
      if (entry.plan && !eligible.has(entry.definition_id)) {
        dropped = true;
        return { ...entry, plan: null, time_summary: null };
      }
      return entry;
    }),
  };
  if (dropped) {
    if (state.planComponentId && !eligible.has(state.planComponentId)) {
      state.planComponentId = null;
      state.plan = null;
      renderPlan();
    }
    renderComponentPlans();
  }

  if (componentsWithoutPlans().length) {
    await generateComponentPlans({ merge: true, automatic: true });
  }
}

$("generate-component-plans").addEventListener("click", () => generateComponentPlans());

// ------------------------------------------------- component classification

const CLASSIFICATION_LABELS = {
  manufactured: "Manufactured",
  purchased: "Purchased",
  standard: "Standard",
  excluded: "Excluded",
  unresolved: "Unresolved",
};

const CLASSIFICATION_ORDER = [
  "manufactured",
  "purchased",
  "standard",
  "excluded",
  "unresolved",
];

// A stated unit cost is denominated in the same currency as the rate set the
// rest of the assembly is being costed against, so a bought part and a machined
// one end up in the same money. It is written into the profile the moment a
// component becomes user-priced rather than only rendered as a placeholder: a
// currency shown in a field but absent from the request is exactly how an
// entered price gets silently refused.
function assemblyCurrency() {
  return (state.rateSet && state.rateSet.currency) || "INR";
}

function profileFor(definitionId) {
  return (state.componentCosting || []).find((p) => p.definition_id === definitionId);
}

// The classification actually in force. Resolved in one direction only, and
// the same order the server resolves it in: what the engineer said, then what
// the system suggested, then nothing.
function effectiveClassification(profile) {
  return profile.user_classification || profile.system_classification || null;
}

function renderComponentCosting() {
  const profiles = (state.componentCosting || []).filter((p) => !p.is_assembly);
  if (!profiles.length) {
    $("component-costing-card").classList.add("hidden");
    return;
  }
  $("component-costing-card").classList.remove("hidden");

  const rows = profiles
    .map((profile) => {
      const chosen = effectiveClassification(profile);
      const fromUser = Boolean(profile.user_classification);
      // Compared here rather than read from the profile's own
      // `classification_changed`: that field was computed by the server when
      // the assembly was decomposed, and an answer given since then would leave
      // it stale — a row saying "you confirmed it" about a classification the
      // user had just changed.
      const changed = fromUser && profile.user_classification !== profile.system_classification;
      const id = escapeHtml(profile.definition_id);
      const options = CLASSIFICATION_ORDER.map(
        (value) =>
          `<option value="${value}"${value === chosen ? " selected" : ""}>${
            CLASSIFICATION_LABELS[value]
          }</option>`
      ).join("");

      return `
        <tr data-definition="${id}" class="decision-row">
          <td>
            <button type="button" class="link classification-pick" data-definition="${id}">${escapeHtml(
        profile.name
      )}</button>
            <div class="mono tiny muted">${escapeHtml(profile.part_number || "—")}</div>
          </td>
          <td class="num">×${profile.quantity}</td>
          <td>
            <select class="classification-select" data-definition="${id}">${options}</select>
            <div class="tiny ${changed ? "changed" : "muted"}">${
        fromUser
          ? changed
            ? `you changed it from ${CLASSIFICATION_LABELS[profile.system_classification]}`
            : "you confirmed it"
          : `system suggested · ${escapeHtml(profile.system_confidence || "low")} confidence`
      }</div>
          </td>
        </tr>
        <tr class="inputs-row"><td colspan="3" class="costing-inputs">${classificationInputs(
          profile,
          chosen
        )}</td></tr>`;
    })
    .join("");

  $("component-costing-body").innerHTML = `
    <table class="mini bom">
      <thead><tr><th>Component</th><th>Qty</th><th>Classification</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="hint small">
      A quantity here is how many times this assembly uses the component. It is
      not how many assemblies you are building — that is entered with the
      roll-up below, and the two are multiplied exactly once, on the server.
    </p>`;

  bindCostingInputs();
}

// What a component needs in order to be costed depends entirely on what kind of
// component it is, so the row asks for that and nothing else. A purchased part
// is never offered a material-and-machining path it will not take.
function classificationInputs(profile, chosen) {
  const id = escapeHtml(profile.definition_id);
  if (chosen === "purchased" || chosen === "standard") {
    return `
      <div class="inline-fields">
        <label class="tiny">Unit cost
          <input type="number" step="0.01" min="0" class="decision" data-field="unit_cost"
                 data-definition="${id}" value="${
      profile.unit_cost === null || profile.unit_cost === undefined ? "" : profile.unit_cost
    }" placeholder="none entered">
        </label>
        <label class="tiny">Currency
          <input type="text" maxlength="3" class="decision currency-input" data-field="currency"
                 data-definition="${id}" value="${escapeHtml(
      profile.currency || assemblyCurrency()
    )}">
        </label>
        ${
          chosen === "purchased"
            ? `<label class="tiny">Supplier
                 <input type="text" class="decision" data-field="supplier"
                        data-definition="${id}" value="${escapeHtml(profile.supplier || "")}">
               </label>
               <label class="tiny">Supplier part no.
                 <input type="text" class="decision" data-field="supplier_part_number"
                        data-definition="${id}" value="${escapeHtml(
                profile.supplier_part_number || ""
              )}">
               </label>`
            : `<span class="tiny muted">Standard item — a configured unit cost. There is no
                 standards database here and no price is looked up.</span>`
        }
      </div>`;
  }
  if (chosen === "excluded") {
    return `
      <div class="inline-fields">
        <span class="chip excluded-chip">Excluded from assembly total</span>
        <label class="tiny">Reason
          <input type="text" class="decision" data-field="exclusion_reason"
                 data-definition="${id}" value="${escapeHtml(
      profile.exclusion_reason || "user_selected"
    )}">
        </label>
      </div>`;
  }
  if (chosen === "manufactured") {
    return `
      <div class="inline-fields">
        <label class="tiny">Material
          <input type="text" class="decision" data-field="material" data-definition="${id}"
                 value="${escapeHtml(profile.material || "")}"
                 placeholder="assembly default">
        </label>
        <span class="tiny muted">${
          profile.material_source === "cad_file"
            ? "material as the CAD file states it"
            : "the CAD file states no material for this component"
        }</span>
      </div>`;
  }
  return `<span class="tiny warn">Needs costing input — nobody has said whether this is
    made, bought, standard or excluded, and the CAD file does not say. It is not counted
    as zero.</span>`;
}

function bindCostingInputs() {
  document.querySelectorAll(".classification-select").forEach((select) => {
    select.addEventListener("change", () => {
      const profile = profileFor(select.dataset.definition);
      if (!profile) return;
      // Written beside the system's proposal, never over it.
      profile.user_classification = select.value;
      if (select.value === "excluded" && !profile.exclusion_reason) {
        profile.exclusion_reason = "user_selected";
      }
      if ((select.value === "purchased" || select.value === "standard") && !profile.currency) {
        profile.currency = assemblyCurrency();
      }
      markRollUpStale();
      renderComponentCosting();
      renderComponentPlans();
      renderComponentDetail(profile.definition_id);
      if (typeof renderComponentPlanDetail === "function") {
        renderComponentPlanDetail(profile.definition_id);
      }
      // Who is entitled to a plan just changed. A component that lost the
      // entitlement loses its plan here; one that gained it gets a plan built,
      // and the plans already held keep whatever times were committed in them.
      reconcileComponentPlans();
    });
  });

  document.querySelectorAll(".decision").forEach((input) => {
    input.addEventListener("change", () => {
      const profile = profileFor(input.dataset.definition);
      if (!profile) return;
      const field = input.dataset.field;
      if (field === "unit_cost") {
        profile.unit_cost = input.value === "" ? null : Number(input.value);
      } else if (field === "currency") {
        profile.currency = input.value.toUpperCase() || null;
      } else {
        profile[field] = input.value || null;
      }
      markRollUpStale();
      renderComponentDetail(profile.definition_id);
    });
  });

  document.querySelectorAll(".classification-pick").forEach((button) => {
    button.addEventListener("click", () => {
      renderComponentDetail(button.dataset.definition);
      const occurrence = (state.assembly.occurrences || []).find(
        (item) => item.definition_id === button.dataset.definition
      );
      if (occurrence) selectComponent(occurrence.occurrence_id);
    });
  });
}

// Only the fields a person owns. The system's suggestion, the component's
// identity and its occurrence count are the server's, and a round trip through
// the browser must not be able to rewrite any of them. Shared by the plan
// request and the cost request so the two cannot disagree about what was
// classified as what.
function componentDecisions() {
  return (state.componentCosting || []).map((profile) => ({
    definition_id: profile.definition_id,
    user_classification: profile.user_classification,
    classification_reason: profile.classification_reason,
    material: profile.material,
    unit_cost: profile.unit_cost,
    currency: profile.currency,
    supplier: profile.supplier,
    supplier_part_number: profile.supplier_part_number,
    exclusion_reason: profile.exclusion_reason,
    notes: profile.notes,
  }));
}

function markRollUpStale() {
  if (!state.assemblyCost) return;
  state.assemblyCost = null;
  // Remembered as stale rather than merely absent. Both are "no roll-up", but
  // they call for different sentences, and Cost and Quote must refuse a stale
  // one out loud instead of quietly falling back to some other estimate.
  state.assemblyCostStale = true;
  $("assembly-cost-body").innerHTML =
    "<p class='assembly-warning'>A classification changed, so the roll-up below no longer " +
    "matches these components. Cost them again.</p>";
  if (typeof refreshFlowChrome === "function") refreshFlowChrome();
}

// The detail panel. What it shows depends on what kind of component it is,
// because the fields that mean something differ: a purchased part has a
// supplier and no operations, a manufactured one has geometry and features and
// no supplier.
function renderComponentDetail(definitionId) {
  const profile = profileFor(definitionId);
  const panel = $("component-detail");
  if (!profile) {
    panel.innerHTML = "";
    return;
  }
  const definition = (state.assembly.definitions || []).find(
    (item) => item.definition_id === definitionId
  );
  const chosen = effectiveClassification(profile);
  const line = (state.assemblyCost ? state.assemblyCost.lines : []).find(
    (item) => item.definition_id === definitionId
  );
  const currency = profile.currency || (state.assemblyCost && state.assemblyCost.currency);

  const rows = [
    ["Classification", CLASSIFICATION_LABELS[chosen] || "—"],
    [
      "Source",
      profile.user_classification
        ? "you selected it"
        : `system suggestion (${escapeHtml(profile.system_confidence || "low")} confidence)`,
    ],
    ["Quantity", `×${profile.quantity} in this assembly`],
  ];

  if (chosen === "manufactured") {
    rows.push(["Material", escapeHtml(profile.material || "assembly default")]);
    rows.push([
      "Geometry",
      definition && definition.geometry
        ? `${number(definition.geometry.volume, 3)} mm³`
        : "no geometry in file",
    ]);
    rows.push(["Path", "geometry → mass → material → features → operations → setups → cost"]);
  } else if (chosen === "purchased") {
    rows.push(["Supplier", escapeHtml(profile.supplier || "—")]);
    rows.push(["Supplier part no.", escapeHtml(profile.supplier_part_number || "—")]);
    rows.push([
      "Unit cost",
      profile.unit_cost === null || profile.unit_cost === undefined
        ? "not entered"
        : money(profile.unit_cost, currency),
    ]);
  } else if (chosen === "standard") {
    rows.push(["Standard item", "configured unit cost, no catalogue lookup"]);
    rows.push([
      "Unit cost",
      profile.unit_cost === null || profile.unit_cost === undefined
        ? "not entered"
        : money(profile.unit_cost, currency),
    ]);
  } else if (chosen === "excluded") {
    rows.push(["Excluded", "not included in the manufacturing cost"]);
    rows.push(["Reason", escapeHtml(profile.exclusion_reason || "user_selected")]);
  }

  if (line) {
    rows.push([
      "Total contribution",
      line.extended_cost === null
        ? "not costed"
        : money(line.extended_cost, line.currency || currency),
    ]);
    rows.push(["Costing status", escapeHtml(line.costing_status)]);
    if (line.reason_detail) rows.push(["Why", escapeHtml(line.reason_detail)]);
  } else {
    rows.push(["Costing status", "not costed yet — run the roll-up"]);
  }

  panel.innerHTML = `
    <h3>${escapeHtml(profile.name)}</h3>
    <table class="mini">
      <tbody>${rows
        .map(([label, value]) => `<tr><th>${label}</th><td>${value}</td></tr>`)
        .join("")}</tbody>
    </table>
    ${
      profile.system_caveat
        ? `<p class="provenance">${escapeHtml(profile.system_caveat)}</p>`
        : ""
    }`;
}

// The assembly tree. Nested by occurrence, because that is the structure the
// file states — and each row names four different things that are easy to
// confuse: whether it is a part or a sub-assembly, which *definition* it is,
// how many times that definition is used in the whole assembly, and which of
// those placements this particular row is.
function renderAssemblyTree(assembly, drawable) {
  const byParent = new Map();
  assembly.occurrences.forEach((occurrence) => {
    const key = occurrence.parent_occurrence_id || "";
    if (!byParent.has(key)) byParent.set(key, []);
    byParent.get(key).push(occurrence);
  });
  const definitions = new Map(
    assembly.definitions.map((definition) => [definition.definition_id, definition])
  );
  // Which placement of its definition each occurrence is, in document order.
  const ordinal = new Map();
  const seen = new Map();
  assembly.occurrences.forEach((occurrence) => {
    const n = (seen.get(occurrence.definition_id) || 0) + 1;
    seen.set(occurrence.definition_id, n);
    ordinal.set(occurrence.occurrence_id, n);
  });

  function node(occurrence) {
    const definition = definitions.get(occurrence.definition_id) || {};
    const children = byParent.get(occurrence.occurrence_id) || [];
    const quantity = definition.quantity || 0;
    const measurable = definition.geometry_status === "measured";
    const label = escapeHtml(definition.name || occurrence.instance_name);

    return `
      <li>
        <div class="node">
          <span class="kind ${definition.is_assembly ? "subassembly" : "part"}">${
      definition.is_assembly ? "sub-assy" : "part"
    }</span>
          ${
            drawable && measurable
              ? `<button type="button" class="link node-pick" data-occurrence="${escapeHtml(
                  occurrence.occurrence_id
                )}">${label}</button>`
              : label
          }
          ${quantity > 1 ? `<span class="qty">×${quantity}</span>` : ""}
          ${
            quantity > 1
              ? `<span class="instance-of">placement ${ordinal.get(
                  occurrence.occurrence_id
                )} of ${quantity}</span>`
              : ""
          }
          ${measurable || definition.is_assembly ? "" : '<span class="no-geometry">no geometry</span>'}
        </div>
        ${children.length ? `<ul>${children.map(node).join("")}</ul>` : ""}
      </li>`;
  }

  const roots = byParent.get("") || [];
  $("assembly-tree").innerHTML = `
    <ul class="tree">
      <li>
        <div class="node">
          <span class="kind subassembly">assembly</span>
          <strong>${escapeHtml(assembly.name)}</strong>
          <span class="qty">${assembly.definitions.length} definitions · ${
    assembly.occurrences.length
  } occurrences</span>
        </div>
        <ul>${roots.map(node).join("")}</ul>
      </li>
    </ul>`;
}

// ------------------------------------------------------------ exploded view

function hideExplodeControls() {
  $("explode-controls").classList.add("hidden");
  $("explode").value = "0";
  $("explode-value").textContent = "0%";
}

function showExplodeControls(exploded) {
  if (!exploded || !exploded.enabled) {
    hideExplodeControls();
    return;
  }
  $("explode-controls").classList.remove("hidden");
  $("explode").value = "0";
  $("explode-value").textContent = "0%";
}

// The slider only moves already-drawn components. It triggers no request, no
// re-tessellation, no feature recognition and no costing — the offsets came
// with the assembly and the arithmetic is one multiply-add per component.
$("explode").addEventListener("input", (event) => {
  const percent = Number(event.target.value);
  $("explode-value").textContent = `${percent}%`;
  if (viewer() && typeof viewer().setExplode === "function") {
    viewer().setExplode(percent / 100);
  }
});

$("explode-reset").addEventListener("click", () => {
  $("explode").value = "0";
  $("explode-value").textContent = "0%";
  if (viewer() && typeof viewer().resetExplode === "function") viewer().resetExplode();
});

// Selecting a component isolates it in the viewer, which is what makes
// component -> features -> operations something a person can follow.
function selectComponent(occurrenceId) {
  if (!viewer() || typeof viewer().isolateComponent !== "function") return;
  const shown = viewer().isolateComponent(occurrenceId);
  document
    .querySelectorAll(".component-pick, .node-pick")
    .forEach((button) => button.classList.toggle("picked", button.dataset.occurrence === occurrenceId));

  const note = $("isolated-note");
  if (!note) return;
  if (!shown) {
    note.textContent = "This component has no geometry to show.";
    return;
  }

  // Name it, say which placement of its definition it is, and — when the view
  // is exploded — say where it is being drawn against where the CAD puts it,
  // so the two are never confused.
  const occurrence = (state.assembly.occurrences || []).find(
    (item) => item.occurrence_id === occurrenceId
  );
  const definition = (state.assembly.definitions || []).find(
    (item) => occurrence && item.definition_id === occurrence.definition_id
  );
  const placement = viewer().componentPlacement(occurrenceId);
  const parts = [
    `Showing ${definition ? definition.name : occurrenceId} only`,
    definition && definition.quantity > 1 ? `used ×${definition.quantity}` : null,
  ].filter(Boolean);

  if (placement) {
    const moved = placement.displayed.some(
      (value, index) => Math.abs(value - placement.original[index]) > 1e-9
    );
    parts.push(
      moved
        ? `drawn at (${placement.displayed.map((v) => number(v, 1)).join(", ")}), CAD places it at (${placement.original
            .map((v) => number(v, 1))
            .join(", ")})`
        : `at its CAD placement (${placement.original.map((v) => number(v, 1)).join(", ")})`
    );
  }
  note.textContent = parts.join(" · ") + ".";
  if (occurrence) renderComponentDetail(occurrence.definition_id);
}

$("cost-assembly").addEventListener("click", async () => {
  if (!state.file) return;
  $("cost-assembly").disabled = true;
  $("assembly-cost-body").innerHTML = "<p class='muted'>Costing each component…</p>";

  const body = new FormData();
  body.append("file", state.file);
  body.append("material", $("material").value);
  body.append("source_unit", $("source_unit").value);
  body.append("rate_set_id", $("rate_set_id").value);
  const pinned = $("rate_set_version").value;
  if (pinned) body.append("rate_set_version", pinned);
  body.append("assembly_quantity", $("assembly_quantity").value || "1");
  body.append("components", JSON.stringify(componentDecisions()));
  // Each manufactured component's own plan, keyed by definition. One plan per
  // definition — never one per occurrence — so the server multiplies its
  // per-part cost by the occurrence count exactly once.
  const plans = {};
  ((state.assemblyPlan && state.assemblyPlan.component_plans) || []).forEach((entry) => {
    if (entry.plan) plans[entry.definition_id] = entry.plan;
  });
  if (Object.keys(plans).length) body.append("component_plans", JSON.stringify(plans));
  const timeProfile = $("time_profile_id");
  if (timeProfile && timeProfile.value) body.append("time_profile_id", timeProfile.value);

  try {
    state.assemblyCost = await api("/cost/assembly", { method: "POST", body });
    state.assemblyCostStale = false;
    renderAssemblyCost(state.assemblyCost);
    // The roll-up is this assembly's estimate, so the sections that name an
    // estimate — the context bar, Cost and Quote — are re-read here rather
    // than waiting for the next tab click.
    if (typeof refreshFlowChrome === "function") refreshFlowChrome();
  } catch (error) {
    $("assembly-cost-body").innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
  } finally {
    $("cost-assembly").disabled = false;
  }
});

function renderAssemblyCost(summary) {
  const rows = summary.lines
    .map(
      (line) => `
        <tr class="${line.costed ? "" : line.excluded ? "row-excluded" : "row-uncosted"}">
          <td>${escapeHtml(line.name)}</td>
          <td>${
            line.classification
              ? `<span class="chip class-${line.classification}">${
                  CLASSIFICATION_LABELS[line.classification] || line.classification
                }</span>`
              : "<span class='tiny muted'>sub-assembly</span>"
          }</td>
          <td class="num">${line.is_assembly ? "—" : "×" + line.quantity}</td>
          <td class="num">${
            line.per_unit_cost === null
              ? "—"
              : money(line.per_unit_cost, line.currency || summary.currency)
          }</td>
          <td class="num">${
            line.extended_cost === null
              ? "not costed"
              : money(line.extended_cost, line.currency || summary.currency)
          }</td>
        </tr>
        ${
          line.reason_detail
            ? `<tr class="reason-row"><td colspan="5" class="small muted">${escapeHtml(
                line.reason_detail
              )}</td></tr>`
            : ""
        }`
    )
    .join("");

  // Every contribution visible, including the excluded zero — shown rather
  // than omitted, so a deliberate exclusion is legible as a decision.
  const contributions = [
    ["Manufactured", summary.manufactured_cost],
    ["Purchased", summary.purchased_cost],
    ["Standard", summary.standard_cost],
    ["Excluded", summary.excluded_cost],
  ]
    .map(
      ([label, value]) =>
        `<tr><th>${label}</th><td class="num">${
          value === null || value === undefined ? "—" : money(value, summary.currency)
        }</td></tr>`
    )
    .join("");

  $("assembly-cost-body").innerHTML = `
    <table class="mini bom">
      <thead><tr><th>Component</th><th>Classification</th><th>Qty</th><th>Per unit</th>
                 <th>Extended</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <table class="mini">
      <tbody>
        ${contributions}
        <tr><th>Assembly total (one assembly)</th><td class="num"><strong>${
          summary.total_cost === null ? "no total" : money(summary.total_cost, summary.currency)
        }</strong></td></tr>
        ${
          summary.assembly_quantity > 1
            ? `<tr><th>${summary.assembly_quantity} assemblies</th><td class="num"><strong>${
                summary.production_total_cost === null
                  ? "no total"
                  : money(summary.production_total_cost, summary.currency)
              }</strong></td></tr>`
            : ""
        }
        <tr><th>Manufactured breakdown</th><td class="num">${
          summary.material_cost === null ? "—" : money(summary.material_cost, summary.currency)
        } material · ${
    summary.setup_cost === null || summary.setup_cost === undefined
      ? "—"
      : money(summary.setup_cost, summary.currency)
  } setup · ${
    summary.run_cost === null || summary.run_cost === undefined
      ? "—"
      : money(summary.run_cost, summary.currency)
  } run</td></tr>
        <tr><th>Components costed</th><td class="num">${summary.costed_definition_count} of ${
    summary.part_definition_count
  } (${summary.costed_occurrence_count} of ${summary.part_occurrence_count} part occurrences)</td></tr>
        ${
          summary.excluded_definition_count
            ? `<tr><th>Excluded</th><td class="num">${summary.excluded_definition_count} definition(s), by choice</td></tr>`
            : ""
        }
      </tbody>
    </table>
    ${
      summary.needs_input_definition_count
        ? `<p class="assembly-warning"><strong>Assembly total incomplete</strong> —
             ${summary.needs_input_definition_count} component(s) need costing input.</p>`
        : ""
    }
    <!-- The partial-manufacturing case needs no banner of its own: the
         completeness note below already opens with "Partial assembly estimate —
         manufacturing plans incomplete", and is rendered in the warning style
         whenever the roll-up is not complete. One sentence, authored once, on
         the server that knows the counts. -->
    ${
      summary.is_aggregatable
        ? ""
        : `<p class="assembly-warning">${escapeHtml(summary.currency_note)}</p>`
    }
    <p class="${summary.is_complete ? "hint" : "assembly-warning"}">${escapeHtml(
    summary.completeness_note
  )}</p>
    ${summary.notes.map((n) => `<p class="provenance">${escapeHtml(n)}</p>`).join("")}`;
  renderComponentCosting();
  renderComponentPlans();
  if (state.planComponentId) renderComponentPlanDetail(state.planComponentId);
}
