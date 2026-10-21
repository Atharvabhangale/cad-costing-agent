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
  $("process-card").classList.add("hidden");
  $("process-summary").innerHTML = "";
  $("process-costing").innerHTML = "";
  $("process-evidence").innerHTML = "";
  $("process-note").value = "";
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
            ? `<span class="pill pill-ok">cost model available</span>`
            : `<span class="pill pill-off">no cost model</span>`) +
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
    `<p class="${c.blocks_costing ? "warning-inline" : "hint"}">${escapeHtml(c.costing_note)}</p>`;

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
