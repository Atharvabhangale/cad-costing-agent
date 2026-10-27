// Advisory design-for-manufacturing flags.
//
// Renders what `POST /cad/dfm` returns and nothing more: every measured value,
// threshold, source and sentence comes from the server. This file computes no
// ratio and decides nothing. A flag changes no cost, time or suggestion, and the
// panel says so.

(function () {
  const state_ = { report: null };

  function dfmFlagMarkup(flag, index) {
    const faceted = flag.evidence_type === "faceted";
    const source = flag.source_url
      ? `<a href="${escapeHtml(flag.source_url)}" target="_blank" rel="noopener">${escapeHtml(flag.source_title || flag.source_url)}</a>`
      : `<span class="muted">No published source found — illustrative threshold</span>`;
    const comparison = flag.comparison === "less_than" ? "below" : "above";
    const buttons = flag.feature_ids.length <= 3
      ? flag.feature_ids
          .map(
            (id) =>
              `<button type="button" class="secondary tiny dfm-show" data-id="${escapeHtml(id)}">Show ${escapeHtml(id)}</button>`
          )
          .join(" ")
      : `<span class="muted">${flag.feature_ids.length} holes: ${escapeHtml(flag.feature_ids.join(", "))}</span>`;
    return `
      <li class="dfm-flag" data-index="${index}">
        <div><strong>${escapeHtml(flag.rule_id)}</strong>
          <span class="pill">${escapeHtml(flag.evidence_type)}</span></div>
        <div>Measured ${number(flag.measured_value, 2)} ${escapeHtml(flag.measured_unit)},
          ${comparison} the profile threshold of ${number(flag.threshold, 2)}.</div>
        <div>${escapeHtml(flag.explanation)}</div>
        ${faceted ? `<div class="warning-inline">${escapeHtml(flag.evidence_note || "")}</div>` : ""}
        <div class="muted">Profile ${escapeHtml(flag.dfm_profile_id)} v${escapeHtml(flag.dfm_profile_version)} · ${source}</div>
        <div>${buttons}</div>
      </li>`;
  }

  function dfmRender(report) {
    state_.report = report;
    const list = $("dfm-flags");
    $("dfm-profile-line").textContent =
      `Thresholds: ${report.dfm_profile_id} v${report.dfm_profile_version}` +
      (report.profile_is_illustrative ? " (illustrative)" : "");
    $("dfm-disclaimer").textContent = report.disclaimer;
    list.innerHTML = report.flags.map(dfmFlagMarkup).join("");
    list.classList.toggle("hidden", !report.flags.length);
    // The sentence and every count come from the server; nothing is added up here.
    $("dfm-status").innerHTML =
      `${escapeHtml(report.summary)}<ul class="dfm-rules">` +
      report.rules
        .map((rule) =>
          rule.evaluated
            ? `<li>${escapeHtml(rule.label)}: ${rule.features_checked} checked, ${rule.flags} flagged</li>`
            : `<li>${escapeHtml(rule.label)}: not checked — ${escapeHtml(rule.skipped_reason || "not in this profile")}</li>`
        )
        .join("") +
      "</ul>";
    $("dfm-body").classList.remove("hidden");
  }

  function dfmClear() {
    state_.report = null;
    $("dfm-flags").innerHTML = "";
    $("dfm-body").classList.add("hidden");
    $("dfm-status").textContent =
      "Check recognized features against general machining guidelines. Advisory only.";
    const enabled = typeof featuresEnabledFor === "function" && !!state.file && featuresEnabledFor();
    $("dfm-run").disabled = !enabled;
  }

  const previousReset = typeof resetFeaturesPanel === "function" ? resetFeaturesPanel : null;
  // Called by app.js on every file selection, so a previous file's flags never
  // sit beside the model now loaded.
  resetFeaturesPanel = function () {
    if (previousReset) previousReset();
    dfmClear();
  };

  $("dfm-flags").addEventListener("click", (event) => {
    const button = event.target.closest(".dfm-show");
    if (!button) return;
    const feature = typeof featureById === "function" ? featureById(button.dataset.id) : null;
    if (feature) selectFeature(feature, { scroll: false });
    else $("dfm-status").textContent = "Analyse features first to highlight a feature on the model.";
  });

  $("dfm-run").addEventListener("click", async () => {
    if (!state.file) return;
    clearError();
    $("dfm-run").disabled = true;
    $("dfm-status").textContent = "Checking features against the thresholds…";
    const body = new FormData();
    body.append("file", state.file);
    let report;
    try {
      report = await api("/cad/dfm", { method: "POST", body });
    } catch (error) {
      $("dfm-status").textContent = "Could not check this model.";
      $("dfm-run").disabled = false;
      showError(error.message);
      return;
    }
    dfmRender(report);
    $("dfm-run").disabled = false;
  });

  dfmClear();
})();
