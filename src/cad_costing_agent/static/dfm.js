// Advisory design-for-manufacturing flags.
//
// Renders what `POST /cad/dfm` returns and nothing more: every measured value,
// threshold, source and sentence comes from the server. This file computes no
// ratio and decides nothing. A flag changes no cost, time or suggestion, and the
// panel says so.
//
// A flag can be pointed at on the 3D model. Which features a flag names
// (`feature_ids`) and where each one is (`flagged_features`, the recognizer's
// own objects) both come from the same response, so marking needs no second
// request and does not depend on the features panel having been analysed. The
// marking itself is the existing feature selection (`selectFeatures`) and the
// viewer's marker: a point and an axis line, not the feature's faces.

(function () {
  // `marked` is what this panel last drew on the model: a flag's index, "all",
  // or null when the model carries no flag marker of ours.
  const state_ = { report: null, marked: null };

  const ALL = "all";

  // The server's feature object for each id a flag names. An id with no object,
  // or an object with no location, cannot be drawn — and the row says so.
  function dfmFeaturesOf(flag) {
    const returned = state_.report ? state_.report.flagged_features || [] : [];
    const drawable = [];
    const undrawable = [];
    flag.feature_ids.forEach((id) => {
      const feature = returned.find((candidate) => candidate.feature_id === id);
      if (feature && feature.location) drawable.push(feature);
      else undrawable.push(id);
    });
    return { drawable, undrawable };
  }

  function dfmFlagMarkup(flag, index) {
    const faceted = flag.evidence_type === "faceted";
    const source = flag.source_url
      ? `<a href="${escapeHtml(flag.source_url)}" target="_blank" rel="noopener">${escapeHtml(flag.source_title || flag.source_url)}</a>`
      : `<span class="muted">No published source found — illustrative threshold</span>`;
    const comparison = flag.comparison === "less_than" ? "below" : "above";
    const { drawable, undrawable } = dfmFeaturesOf(flag);
    const button = drawable.length
      ? `<button type="button" class="secondary tiny dfm-show" aria-pressed="false">Show on model</button>`
      : "";
    const scope = drawable.length ? "that part of this flag" : "this flag";
    const missing = undrawable.length
      ? `<div class="warning-inline dfm-undrawable">No location was recorded for ${escapeHtml(undrawable.join(", "))}, so ${scope} cannot be marked on the model. The flag itself stands.</div>`
      : "";
    return `
      <li class="dfm-flag${drawable.length ? " is-markable" : ""}" data-index="${index}">
        <div><strong>${escapeHtml(flag.rule_id)}</strong>
          <span class="pill">${escapeHtml(flag.evidence_type)}</span></div>
        <div>Measured ${number(flag.measured_value, 2)} ${escapeHtml(flag.measured_unit)},
          ${comparison} the profile threshold of ${number(flag.threshold, 2)}.</div>
        <div>${escapeHtml(flag.explanation)}</div>
        ${faceted ? `<div class="warning-inline">${escapeHtml(flag.evidence_note || "")}</div>` : ""}
        <div class="muted">Profile ${escapeHtml(flag.dfm_profile_id)} v${escapeHtml(flag.dfm_profile_version)} · ${source}</div>
        <div class="dfm-flag-actions">${button}
          <span class="muted">Raised on <span class="mono">${escapeHtml(flag.feature_ids.join(", "))}</span></span></div>
        ${missing}
      </li>`;
  }

  // Rows, buttons and the clear control say in words what is marked; the tint
  // on a marked row is never the only signal.
  function dfmRefreshMarked() {
    $("dfm-flags").querySelectorAll("li.dfm-flag").forEach((row) => {
      const marked =
        row.classList.contains("is-markable") &&
        (state_.marked === ALL || String(state_.marked) === row.dataset.index);
      row.classList.toggle("is-marked", marked);
      const button = row.querySelector(".dfm-show");
      if (button) {
        button.setAttribute("aria-pressed", marked ? "true" : "false");
        button.textContent = marked ? "Marked on model" : "Show on model";
      }
    });
    $("dfm-clear-marks").disabled = state_.marked === null;
  }

  function dfmUnmark(message) {
    state_.marked = null;
    $("dfm-marked").textContent = message;
    dfmRefreshMarked();
  }

  // Mark the given features through the existing selection, then report what
  // the viewer actually drew rather than assuming it drew anything.
  function dfmMark(features, label, key) {
    if (!features.length) {
      dfmUnmark(
        `${label}: no flagged feature has a recorded location, so there is nothing to mark on the model.`
      );
      return;
    }
    if (typeof selectFeatures === "function") {
      selectFeatures(
        features.map((feature) => feature.feature_id),
        { features, flagged: true, label }
      );
    }
    const viewer = window.CadViewer;
    const drawn = viewer && viewer.markFlaggedFeatures ? viewer.markFlaggedFeatures(features) : 0;
    if (!drawn) {
      dfmUnmark(
        "Nothing could be marked: the 3D preview is not showing this model (it may still be " +
          "loading, have failed, or be unavailable in this browser). The flags below are unaffected."
      );
      return;
    }
    state_.marked = key;
    const names = features.map((feature) => feature.feature_id).join(", ");
    const shown = viewer.componentState ? viewer.componentState() : null;
    const partial =
      drawn < features.length
        ? ` ${features.length} were asked for; the rest could not be drawn.`
        : "";
    const exploded =
      shown && shown.explodeFactor
        ? " The assembly is drawn exploded and markers stay where the file places each feature, " +
          "so set the explode slider back to its start to see them on their components."
        : "";
    $("dfm-marked").textContent =
      `${label}: ${drawn} feature(s) marked with a magenta diamond — ${names}.` + partial + exploded;
    dfmRefreshMarked();
  }

  function dfmRender(report) {
    state_.report = report;
    state_.marked = null;
    const list = $("dfm-flags");
    $("dfm-profile-line").textContent =
      `Thresholds: ${report.dfm_profile_id} v${report.dfm_profile_version}` +
      (report.profile_is_illustrative ? " (illustrative)" : "");
    $("dfm-disclaimer").textContent = report.disclaimer;
    list.innerHTML = report.flags.map(dfmFlagMarkup).join("");
    list.classList.toggle("hidden", !report.flags.length);
    $("dfm-marking").classList.toggle("hidden", !report.flags.length);
    $("dfm-marked").textContent = report.flags.length
      ? "Select a flag, or show all, to point at its feature(s) on the 3D model."
      : "";
    dfmRefreshMarked();
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
    state_.marked = null;
    $("dfm-flags").innerHTML = "";
    $("dfm-marked").textContent = "";
    $("dfm-marking").classList.add("hidden");
    $("dfm-clear-marks").disabled = true;
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

  // The whole row is a mouse target; the button inside it is the keyboard one.
  // A click on the source link follows the link and marks nothing.
  $("dfm-flags").addEventListener("click", (event) => {
    if (event.target.closest("a")) return;
    const row = event.target.closest("li.dfm-flag.is-markable");
    if (!row || !state_.report) return;
    const index = Number(row.dataset.index);
    const flag = state_.report.flags[index];
    if (!flag) return;
    dfmMark(dfmFeaturesOf(flag).drawable, flag.rule_id, index);
  });

  $("dfm-show-all").addEventListener("click", () => {
    if (!state_.report) return;
    const drawable = (state_.report.flagged_features || []).filter((feature) => feature.location);
    dfmMark(drawable, "All flags", ALL);
  });

  // Restores the ordinary view: no marker, no selected row, no selection box.
  $("dfm-clear-marks").addEventListener("click", () => {
    if (window.CadViewer && window.CadViewer.clearMarker) window.CadViewer.clearMarker();
    $("selected-feature").classList.add("hidden");
    $("features-body")
      .querySelectorAll("tr.feature-row.is-selected")
      .forEach((row) => row.classList.remove("is-selected"));
    dfmUnmark("Markers cleared.");
  });

  // Something else replaced or removed the marker (a feature row, an operation,
  // a new model): stop claiming a flag is marked.
  document.addEventListener("cadviewer:marker", (event) => {
    if (event.detail && event.detail.flagged) return;
    if (state_.marked !== null) dfmUnmark("");
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
