"use strict";

// Comparing routes side by side: a preview of each, never an estimate or a choice.
//
// A classic script loaded after actuals.js, sharing `$`, `api`, `state`, `money`,
// `escapeHtml` and `showError` with the files before it, and calling into
// process.js (`decideProcess`, `familyLabel`) and app.js (`collectManufacturingPlan`).
//
// What this file does not do, on purpose:
// - It keeps no list of routes. The server decides which routes are compared (the
//   recommendation and the other families the evidence is consistent with) and in
//   what order, and this file shows them in the order they arrive.
// - It adds nothing up and ranks nothing. Every figure is the server's, there is no
//   "best" or "cheapest" marking, and nothing is sorted. A refused route is listed
//   with the server's reason, never dropped and never shown as zero.
// - It decides nothing. "Use this route" goes through process.js's `decideProcess`,
//   which calls /cad/process/decide, exactly as choosing a route from the selector.
//
// The request carries the same inputs as the cost request. Nothing is stored by it.

state.routeComparison = null;

function clearRouteComparison() {
  state.routeComparison = null;
  const host = $("route-compare-result");
  if (host) host.innerHTML = "";
}

function routeCompareBody() {
  const body = new FormData();
  body.append("file", state.file);
  body.append("process_classification", JSON.stringify(state.processClassification));
  body.append("material", $("material").value);
  body.append("source_unit", $("source_unit").value);
  body.append("part_id", $("part_id").value.trim());
  body.append("part_name", $("part_name").value.trim());
  body.append("quantity", String(Number($("quantity").value)));
  body.append("rate_set_id", $("rate_set_id").value);
  const pinned = $("rate_set_version").value;
  if (pinned) body.append("rate_set_version", pinned);
  if (state.completion) {
    body.append("geometry_completion_method", state.completion.method);
    body.append("geometry_completion_volume_mm3", String(state.completion.volume_mm3));
  }
  const plan = collectManufacturingPlan();
  if (plan) body.append("manufacturing_plan", JSON.stringify(plan));
  if (state.rawStock) body.append("raw_stock", JSON.stringify(state.rawStock));
  if (state.stageInputs && Object.keys(state.stageInputs).length) {
    body.append("stage_inputs", JSON.stringify(state.stageInputs));
  }
  // Blank additional-cost fields are omitted, never sent as 0.
  ["overhead_rate_percent", "scrap_rate_percent", "tooling_cost_per_batch"].forEach((field) => {
    const raw = $(field).value.trim();
    if (raw !== "") body.append(field, raw);
  });
  return body;
}

function routeCompareFigure(row, value) {
  return row.status === "priced" ? money(value, row.currency) : "—";
}

function routeCompareLeftOut(row) {
  if (row.status !== "priced" || row.operations_costed === null) return "—";
  const parts = [`${row.operations_costed} costed`];
  if (row.operations_not_timed) parts.push(`${row.operations_not_timed} without a committed time`);
  if (row.operations_out_of_route) parts.push(`${row.operations_out_of_route} outside the route`);
  if (row.operations_as_formed) parts.push(`${row.operations_as_formed} left as formed`);
  return parts.join(", ");
}

function routeCompareStatus(row) {
  if (row.status === "priced") return `<span class="pill pill-ok">priced</span>`;
  const reason = row.refusal ? row.refusal.message : "Could not be priced.";
  // A stage refused only for want of the mass it is charged on can be priced
  // here: the mass is typed by the person (never taken from the CAD), kept in
  // the same `state.stageInputs` the route card and the cost request use.
  const needsMass = ((row.refusal && row.refusal.stages) || []).filter(
    (problem) => problem.code === "needs_mass"
  );
  const inputs = needsMass
    .map((problem) => {
      const entered = ((state.stageInputs || {})[problem.stage] || {}).mass_kg;
      return (
        `<label class="route-mass">${escapeHtml(problem.label)} blank, kg per part ` +
        `<input type="number" min="0" step="any" class="route-mass-input" ` +
        `data-stage="${escapeHtml(problem.stage)}" value="${entered ?? ""}" placeholder="e.g. 4.5"></label>`
      );
    })
    .join("");
  return (
    `<span class="pill pill-off">not priced</span>` +
    `<div class="hint">${escapeHtml(reason)}</div>` +
    (inputs
      ? `${inputs}<button type="button" class="secondary tiny route-recompare">Price with this mass</button>`
      : "")
  );
}

// The mass each per-kg stage was priced on, as the server reports it — from the
// CAD model unless someone entered one — shown in an editable box. Leaving it as
// it is changes nothing; typing a value records it as entered.
function routeCompareMasses(row) {
  if (row.status !== "priced" || !(row.stage_masses || []).length) return "";
  return (
    row.stage_masses
      .map(
        (mass) =>
          `<label class="route-mass">${escapeHtml(mass.label)}, kg per part ` +
          `<input type="number" min="0" step="any" class="route-mass-input" ` +
          `data-stage="${escapeHtml(mass.stage)}" value="${escapeHtml(number(mass.mass_kg, 3))}">` +
          `<span class="pill ${mass.mass_source === "entered" ? "pill-sys" : "pill-off"}">` +
          `${{ entered: "entered", blank_mass: "same as the blank" }[mass.mass_source] || "from CAD"}</span></label>` +
          `<div class="hint">${escapeHtml(mass.mass_note)}</div>`
      )
      .join("") + `<button type="button" class="secondary tiny route-recompare">Re-price</button>`
  );
}

// How many of the plan's operations have no committed time, read from the
// server's rows. Machining is left out of every total until someone accepts or
// enters those times; this says so where the totals are, with the one action
// that fixes it.
function routeCompareUntimed(comparison) {
  return comparison.routes.find((row) => row.status === "priced" && row.operations_not_timed);
}

function routeCompareIsChosen(row) {
  const c = state.processClassification;
  return Boolean(c && c.user_action !== "pending" && c.route && c.route.route_id === row.route_id);
}

function renderRouteComparison() {
  const host = $("route-compare-result");
  const comparison = state.routeComparison;
  if (!host) return;
  if (!comparison) {
    host.innerHTML = "";
    return;
  }
  if (!comparison.routes.length) {
    host.innerHTML = `<p class="hint">${escapeHtml(comparison.not_compared || "Nothing to compare.")}</p>`;
    return;
  }
  const rows = comparison.routes
    .map((row) => {
      const stages = row.stages
        .map((stage) => escapeHtml(stage.label) + (stage.pricing === "stage_rate" ? " (stage rate)" : ""))
        .join(" → ");
      const chosen = routeCompareIsChosen(row);
      return (
        `<tr data-route="${escapeHtml(row.route_id)}">` +
        `<td><strong>${escapeHtml(row.route_label)}</strong>` +
        `<div class="hint">${escapeHtml(familyLabel(row.family))} — ${stages}</div></td>` +
        `<td>${row.role === "recommended" ? "Recommended by the system" : "Other family the evidence allows"}</td>` +
        `<td>${routeCompareStatus(row)}</td>` +
        `<td>${routeCompareFigure(row, row.total_part_cost)}</td>` +
        `<td>${routeCompareFigure(row, row.per_unit_cost)}</td>` +
        `<td>${routeCompareFigure(row, row.total_material_cost)}</td>` +
        `<td>${routeCompareFigure(row, row.total_operation_cost)}</td>` +
        `<td>${routeCompareFigure(row, row.total_stage_cost)}${routeCompareMasses(row)}</td>` +
        `<td>${escapeHtml(routeCompareLeftOut(row))}</td>` +
        `<td>${
          chosen
            ? `<span class="pill pill-sys">Your chosen route</span>`
            : `<button type="button" class="secondary tiny route-use" data-route="${escapeHtml(row.route_id)}">Use this route</button>`
        }</td>` +
        `</tr>`
      );
    })
    .join("");
  const untimed = routeCompareUntimed(comparison);
  const canAccept = !$("use-all-estimates").classList.contains("hidden");
  host.innerHTML =
    `<p class="warning-inline">${escapeHtml(comparison.note)}</p>` +
    (untimed
      ? `<p class="warning">Machining is not in these totals: ${untimed.operations_not_timed} operation(s) ` +
        `have no committed time, so "Operations" reads ₹0.00. ` +
        (canAccept
          ? `<button type="button" class="tiny route-accept-times">Use all estimated times and compare again</button> ` +
            `— the same as "Use all estimated times" in the operations table; you can change any time afterwards.`
          : `Enter or accept their times in the operations table, then compare again.`) +
        `</p>`
      : "") +
    `<table class="ops route-compare"><thead><tr>` +
    `<th>Route</th><th>Why listed</th><th>Status</th><th>Total</th><th>Per unit</th>` +
    `<th>Material</th><th>Operations</th><th>Stage rates</th><th>Plan coverage</th><th></th>` +
    `</tr></thead><tbody>${rows}</tbody></table>` +
    `<p class="hint">Rate set: ${escapeHtml(comparison.rate_set || "not resolved")}. ` +
    `Nothing was stored.</p>`;
  host.querySelectorAll("button.route-use").forEach((button) =>
    button.addEventListener("click", async () => {
      await decideProcess("edited", button.dataset.route);
      renderRouteComparison();
    })
  );
  // Blank is "not entered", never 0. Every row showing the same stage shares one
  // value, as the route card does.
  host.querySelectorAll("input.route-mass-input").forEach((input) =>
    input.addEventListener("input", () => {
      state.stageInputs = state.stageInputs || {};
      const raw = input.value.trim();
      if (raw === "") delete state.stageInputs[input.dataset.stage];
      else state.stageInputs[input.dataset.stage] = { mass_kg: Number(raw) };
      host.querySelectorAll(`input.route-mass-input[data-stage="${input.dataset.stage}"]`).forEach((other) => {
        if (other !== input) other.value = raw;
      });
      if (typeof renderStageInputs === "function") renderStageInputs();
    })
  );
  host.querySelectorAll("button.route-recompare").forEach((button) =>
    button.addEventListener("click", compareRoutes)
  );
  host.querySelectorAll("button.route-accept-times").forEach((button) =>
    button.addEventListener("click", () => {
      $("use-all-estimates").click();
      compareRoutes();
    })
  );
}

async function compareRoutes() {
  if (!state.file || !state.processClassification) return;
  const button = $("route-compare");
  button.disabled = true;
  $("route-compare-status").textContent = "Pricing each route…";
  try {
    state.routeComparison = await api("/cost/route-comparison", {
      method: "POST",
      body: routeCompareBody(),
    });
    renderRouteComparison();
  } catch (error) {
    clearRouteComparison();
    showError(error.message);
  } finally {
    $("route-compare-status").textContent = "";
    button.disabled = false;
  }
}

$("route-compare").addEventListener("click", compareRoutes);
