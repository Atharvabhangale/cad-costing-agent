"use strict";

// The manufacturing family and route: the system's recommendation, and the
// engineer's answer beside it.
//
// A classic script loaded after plan.js, sharing `$`, `api`, `state`,
// `escapeHtml` and `showError` with the files before it.
//
// The recommendation arrives with `/cad/operations/suggest` and is kept in
// `state.processClassification`. Every decision goes through
// `/cad/process/decide`, which returns the whole classification with its
// costing status recomputed — this file never works out whether a route can be
// costed, and never edits the system's fields. `app.js` sends the object as it
// stands with the cost request, where a route the costing cannot price is
// refused.
//
// The route list comes from `/process-routes`, never from a list kept here.

let processRoutes = null;

const DECISION_LABELS = {
  awaiting_review: "Awaiting review",
  insufficient_evidence: "No recommendation — needs information",
  confirmed: "Confirmed by engineer",
  overridden: "Overridden by engineer",
  marked_unknown: "Marked unknown by engineer",
};

const SIGNAL_MARK = { true: "observed", false: "not observed", null: "not assessable" };

function resetProcessPanel() {
  state.processClassification = null;
  state.stageInputs = {};
  $("process-card").classList.add("hidden");
  $("process-summary").innerHTML = "";
  $("process-costing").innerHTML = "";
  $("process-evidence").innerHTML = "";
  $("process-note").value = "";
  if (typeof clearRouteComparison === "function") clearRouteComparison();
}

async function loadProcessRoutes() {
  if (processRoutes) return processRoutes;
  processRoutes = await api("/process-routes");
  const select = $("process-route");
  select.innerHTML = processRoutes.families
    .map((family) => {
      const routes = processRoutes.routes.filter((route) => route.family === family.family);
      if (!routes.length) return "";
      return (
        `<optgroup label="${escapeHtml(family.label)}">` +
        routes
          .map((route) => `<option value="${escapeHtml(route.route_id)}">${escapeHtml(route.label)}</option>`)
          .join("") +
        "</optgroup>"
      );
    })
    .join("");
  return processRoutes;
}

function familyLabel(family) {
  const match = processRoutes && processRoutes.families.find((entry) => entry.family === family);
  return match ? match.label : family;
}

function stageList(route) {
  if (!route || !route.stages.length) return `<p class="hint">No stages.</p>`;
  return (
    `<ol class="sequence-list">` +
    route.stages
      .map(
        (stage) =>
          `<li>${escapeHtml(stage.label)} ` +
          (stage.cost_model_available
            ? `<span class="pill pill-ok">machining — hourly rates</span>`
            : `<span class="pill pill-off">priced from stage rates</span>`) +
          (stage.basis.length
            ? `<div class="hint">${stage.basis.map(escapeHtml).join("<br>")}</div>`
            : "") +
          "</li>"
      )
      .join("") +
    "</ol>"
  );
}

function listOf(items) {
  return items.length
    ? `<ul class="declined-list">${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
    : `<p class="hint">None.</p>`;
}

async function renderProcessClassification(classification) {
  state.processClassification = classification || null;
  if (!classification) {
    resetProcessPanel();
    return;
  }
  try {
    await loadProcessRoutes();
  } catch (error) {
    showError(error.message);
  }
  const c = classification;
  const system = c.system_route;
  const overridden = c.user_action === "edited";

  $("process-summary").innerHTML =
    `<p><strong>Recommended:</strong> ${escapeHtml(familyLabel(c.system_family))}` +
    (system ? ` — ${escapeHtml(system.label)}` : "") +
    ` <span class="conf conf-${escapeHtml(c.system_confidence)}">${escapeHtml(c.system_confidence)}</span>` +
    ` <span class="pill pill-sys">${escapeHtml(DECISION_LABELS[c.decision_status] || c.decision_status)}</span></p>` +
    `<p class="hint">${escapeHtml(c.system_reason)}.</p>` +
    (system ? `<p class="hint"><strong>Recommended stages</strong></p>${stageList(system)}` : "") +
    (overridden
      ? `<p><strong>Engineer's route:</strong> ${escapeHtml(familyLabel(c.family))}` +
        (c.route ? ` — ${escapeHtml(c.route.label)}` : "") +
        `</p>${c.route && c.route.stages.length ? stageList(c.route) : ""}`
      : "") +
    (c.user_note ? `<p class="hint"><strong>Note:</strong> ${escapeHtml(c.user_note)}</p>` : "");

  $("process-costing").innerHTML =
    `<p class="${c.blocks_costing ? "warning-inline" : "hint"}">${escapeHtml(c.costing_note)}</p>` +
    `<div id="process-stage-inputs"></div>`;
  renderStageInputs();

  $("process-evidence").innerHTML =
    `<table class="ops"><thead><tr><th>Signal</th><th>Result</th><th>Detail</th></tr></thead><tbody>` +
    c.signals
      .map(
        (signal) =>
          `<tr><td>${escapeHtml(signal.label)}</td>` +
          `<td>${escapeHtml(SIGNAL_MARK[String(signal.observed)])}</td>` +
          `<td>${escapeHtml(signal.detail)}</td></tr>`
      )
      .join("") +
    `</tbody></table>` +
    `<p class="hint"><strong>Evidence and limits</strong></p>${listOf(c.evidence)}` +
    `<p class="hint"><strong>Also consistent with</strong></p>` +
    listOf(c.candidates.map((candidate) => `${familyLabel(candidate.family)}: ${candidate.reason}`)) +
    `<p class="hint"><strong>Information needed to settle it</strong></p>${listOf(c.needs_information)}` +
    `<p class="hint"><strong>Not assessed from geometry</strong></p>${listOf(c.not_assessed)}`;

  $("process-accept").disabled = !system || c.user_action === "accepted";
  $("process-reset").disabled = c.user_action === "pending";
  if (c.route) $("process-route").value = c.route.route_id;
  $("process-card").classList.remove("hidden");

  // The decided route regroups the part's plan: operations outside it move to
  // "Not in this route", unchanged. The processes come from the backend's
  // `plan_processes`; null until someone decides. Never applied to a component
  // plan opened from an assembly.
  if (state.plan && !state.planComponentId) {
    state.plan.route_processes = Array.isArray(c.plan_processes) ? [...c.plan_processes] : null;
    // On a cast or forged route, the blank's blends are left as formed by
    // default (the backend's `as_formed_feature_types`); null on any other.
    state.plan.as_formed_feature_types = Array.isArray(c.as_formed_feature_types)
      ? [...c.as_formed_feature_types]
      : null;
  }
  if (typeof renderPlan === "function") renderPlan();
}

// The non-machining stages of a decided route, each with the rate the selected
// rate set holds for it. A per-kg stage needs the mass it is charged on, typed
// here per part and sent with the cost request as `stage_inputs`; nothing fills
// it from the CAD mass. Whether the route can actually be priced is decided by
// the server when it is costed — this only shows what it will need.
function renderStageInputs() {
  const host = $("process-stage-inputs");
  const c = state.processClassification;
  if (!host) return;
  state.stageInputs = state.stageInputs || {};
  if (!c || c.costing_status !== "stage_rates_required" || c.blocks_costing || !c.route) {
    host.innerHTML = "";
    return;
  }
  const rateSet = state.rateSet;
  const currency = rateSet ? rateSet.currency : "INR";
  const rows = c.route.stages
    .filter((stage) => !stage.cost_model_available)
    .map((stage) => {
      const rate = rateSet && rateSet.stage_rates ? rateSet.stage_rates[stage.stage] : null;
      if (!rate) {
        return `<tr><th>${escapeHtml(stage.label)}</th><td colspan="2" class="warning-inline">
          No rate in ${rateSet ? `${escapeHtml(rateSet.name)} v${escapeHtml(rateSet.version)}` : "the selected rate set"}.
          Publish a version with one under Rate sets.</td></tr>`;
      }
      const parts = [];
      if (rate.unit_rate !== null && rate.unit_rate !== undefined) {
        parts.push(`${money(rate.unit_rate, currency)} ${rate.unit_basis === "per_kg" ? "per kg" : "per piece"}`);
      }
      if (rate.batch_charge) parts.push(`${money(rate.batch_charge, currency)} per batch`);
      const entered = (state.stageInputs[stage.stage] || {}).mass_kg;
      const input =
        rate.unit_basis === "per_kg"
          ? `<label>Mass charged on, kg per part
               <input type="number" step="any" min="0" class="stage-mass" data-stage="${escapeHtml(stage.stage)}"
                 value="${entered ?? ""}" placeholder="required"></label>`
          : "";
      return `<tr><th>${escapeHtml(stage.label)}</th>
        <td>${parts.join(" + ")}${rate.note ? `<div class="hint">${escapeHtml(rate.note)}</div>` : ""}</td>
        <td>${input}</td></tr>`;
    })
    .join("");
  host.innerHTML =
    `<table class="mini stage-rates"><tbody>${rows}</tbody></table>` +
    `<p class="hint">For a casting or forging, the mass is the blank's weight; the finished part's ` +
    `CAD mass is never used in its place.</p>`;
  host.querySelectorAll("input.stage-mass").forEach((input) =>
    input.addEventListener("input", () => {
      const raw = input.value.trim();
      // Blank is "not entered", never 0.
      if (raw === "") delete state.stageInputs[input.dataset.stage];
      else state.stageInputs[input.dataset.stage] = { mass_kg: Number(raw) };
    })
  );
}

async function decideProcess(action, routeId) {
  if (!state.processClassification) return;
  const note = $("process-note").value.trim();
  try {
    const updated = await api("/cad/process/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        classification: state.processClassification,
        action,
        route_id: routeId || null,
        note: note || null,
      }),
    });
    await renderProcessClassification(updated);
  } catch (error) {
    showError(error.message);
  }
}

$("process-accept").addEventListener("click", () => decideProcess("accepted"));
$("process-override").addEventListener("click", () =>
  decideProcess("edited", $("process-route").value)
);
$("process-unknown").addEventListener("click", () => decideProcess("edited", "unknown"));
$("process-reset").addEventListener("click", () => decideProcess("pending"));
