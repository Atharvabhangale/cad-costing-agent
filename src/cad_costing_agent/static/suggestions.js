"use strict";

// Operation-suggestions panel: the explanation, not the plan.
//
// A classic script loaded after app.js and features.js, sharing `$`, `api`,
// `number`, `escapeHtml` and `state` with them.
//
// It suggests nothing of its own. Everything shown comes from
// `/cad/operations/suggest`, which applies deterministic feature-to-operation
// rules to the features the recognizer found.
//
// Since Step 20 the operations themselves live in the **manufacturing plan**,
// which is where they get times, an order and a cost. What stays here is what
// is not a plan row: the grouped summary, the features that deliberately
// produced no suggestion, and the caveats. Keeping the operations in two tables
// would leave it ambiguous which one the estimate actually came from.


// Suggestions come from features, so they are available exactly when feature
// recognition is — which is the backend's answer for this file, not an
// extension list kept here.
function suggestionsEnabledFor() {
  return typeof featuresEnabledFor === "function" && featuresEnabledFor();
}

function resetSuggestionsPanel() {
  state.suggestions = null;
  $("suggestions-summary").classList.add("hidden");
  $("suggestions-note").classList.add("hidden");
  $("declined-wrap").classList.add("hidden");
  $("declined-list").innerHTML = "";
  if (typeof resetPlan === "function") resetPlan();

  const file = state.file;
  if (!file) {
    $("suggest-operations").disabled = true;
    $("suggestions-status").textContent =
      "Analyse a .step or .stp file, then suggest operations from the features it contains. " +
      "Suggestions and their estimated times are proposals only — nothing is costed until you " +
      "accept it in the manufacturing plan.";
    return;
  }
  const supported = suggestionsEnabledFor();
  const report = typeof featureCapability === "function" ? featureCapability() : null;
  $("suggest-operations").disabled = !supported;
  $("suggestions-status").textContent = supported
    ? "Ready to suggest operations from this model's features."
    : (report ? report.detail + " " : "") +
      "Add operations by hand in the manufacturing plan instead — costing works exactly as " +
      "before.";
}

// ------------------------------------------------------------------ fetching

$("suggest-operations").addEventListener("click", async () => {
  if (!state.file) return;
  clearError();
  $("suggest-operations").disabled = true;
  $("suggestions-status").textContent = "Applying feature-to-operation rules…";

  const body = new FormData();
  body.append("file", state.file);
  // Sent only so the response can say whether these processes are priced.
  // It never changes what is suggested.
  if ($("rate_set_id").value) body.append("rate_set_id", $("rate_set_id").value);
  if ($("rate_set_version").value) body.append("rate_set_version", $("rate_set_version").value);

  let result;
  try {
    result = await api("/cad/operations/suggest", { method: "POST", body });
  } catch (error) {
    $("suggestions-status").textContent = "Could not suggest operations for this model.";
    $("suggest-operations").disabled = false;
    showError(error.message);
    return;
  }

  state.suggestions = result;
  renderSuggestions(result);
  // The plan is what gets costed; suggestions only explain where it came from.
  await buildPlanFromSuggestions(result);
  $("suggest-operations").disabled = false;
  // A disclosure with nothing behind it is worse than no disclosure.
  const declined = $("declined-wrap-toggle");
  if (declined) declined.classList.toggle("hidden", result.declined.length === 0);
  if (typeof refreshFlowChrome === "function") refreshFlowChrome();
});

// ----------------------------------------------------------------- rendering
//
// This panel shows only what is *not* a plan row: the grouped summary, the
// features that produced no suggestion, and the caveats. The operations
// themselves live in the manufacturing plan, where they can be timed, ordered
// and costed — keeping them in two tables would leave it ambiguous which one
// the estimate came from.

function renderSuggestions(result) {
  const total = result.suggestions.length;
  $("suggestions-status").textContent =
    `${total} suggestion${total === 1 ? "" : "s"} from ${result.feature_count} recognized ` +
    `feature${result.feature_count === 1 ? "" : "s"}` +
    (result.declined.length ? `, ${result.declined.length} feature(s) not suggested` : "") +
    ` — ${number(result.elapsed_ms, 0)} ms. They are now in the manufacturing plan, where ` +
    "nothing is costed until you accept or enter a time.";

  $("suggestions-summary").innerHTML = result.summary
    .map((line) => `<li>${escapeHtml(line)}</li>`)
    .join("");
  $("suggestions-summary").classList.toggle("hidden", result.summary.length === 0);

  $("declined-list").innerHTML = result.declined
    .map(
      (item) =>
        `<li><span class="mono">${escapeHtml(item.feature_id)}</span> ` +
        `${escapeHtml(item.feature_label)} — ${escapeHtml(item.reason)}</li>`
    )
    .join("");
  $("declined-wrap").classList.toggle("hidden", result.declined.length === 0);

  const notes = [
    "Every suggestion names the feature that caused it, so it can be traced back to the CAD. " +
      "Click a plan row to see why the operation was proposed and how its time was worked out.",
  ];
  if (result.warnings.length) notes.push(...result.warnings);
  $("suggestions-note").textContent = notes.join(" ");
  $("suggestions-note").classList.remove("hidden");
}
