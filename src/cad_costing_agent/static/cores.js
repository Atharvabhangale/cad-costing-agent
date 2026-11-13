// Proposed cores and side actions (inside the CAD section's pull-direction card).
//
// Renders what `POST /cad/cores/propose` returns and nothing more: the undercut
// regions against the pull axis, each a proposed core, with the count, method and
// limits from the server. This file counts nothing and decides nothing.
//
// The count is a proposal. It reaches a time only when the engineer puts a count
// on a timed moulding or die-casting stage (process.js's "Cores / side actions,
// confirmed" input). "Use this count" fills that input for them — a click by the
// engineer, never done automatically — and they may type a different number.
//
// An IIFE, so it declares no global function another classic script could
// silently replace. It reads the same pull-axis inputs draw.js does.

(function () {
  const cores_ = { result: null };
  const SHOT_STAGES = ["die_casting", "moulding"];

  function coresAxisValue() {
    const values = ["draw-axis-x", "draw-axis-y", "draw-axis-z"].map((id) => $(id).value.trim());
    if (values.every((value) => value === "")) return null;
    return JSON.stringify(values.map((value) => (value === "" ? null : Number(value))));
  }

  // The decided route's timed shot stage, if any: where a confirmed count goes.
  function coreStage() {
    const classification = state.processClassification;
    const stages = classification && classification.route ? classification.route.stages : [];
    return stages.find(
      (stage) => SHOT_STAGES.includes(stage.stage) && stagePricing(stage.stage) === "timed"
    ) || null;
  }

  function coresRender(result) {
    cores_.result = result;
    if (!result.measured) {
      $("cores-status").textContent = `No proposal. ${result.not_measured_reason || ""}`;
      $("cores-body").classList.add("hidden");
      return;
    }
    $("cores-status").textContent =
      `Against pull axis ${result.pull_axis_label} (${result.axis_source === "engineer_entered" ? "the axis you entered" : "the recommended axis"}).`;
    $("cores-count").innerHTML =
      `<strong>${escapeHtml(String(result.proposed_core_count))} proposed core(s)</strong> from ` +
      `${escapeHtml(String(result.undercut_face_count))} undercut face(s). A proposal: nothing is ` +
      "charged for a core until a count is confirmed on the forming stage.";
    $("cores-regions").innerHTML = (result.regions || [])
      .map(
        (region) =>
          `<li>Region ${escapeHtml(String(region.region))}: face(s) ${escapeHtml(region.face_ids.join(", "))}, ` +
          `${number(region.area_mm2, 1)} mm²</li>`
      )
      .join("");
    $("cores-method").textContent = result.method_description;
    $("cores-limits").innerHTML = (result.limitations || []).map((line) => `<li>${escapeHtml(line)}</li>`).join("");
    const stage = coreStage();
    $("cores-use").disabled = !stage;
    $("cores-confirm").textContent = stage
      ? `The count is yours to confirm on ${stage.label}; "Use this count" fills it in, and you can change it there.`
      : "To time cores, decide a moulding or die-casting route priced by time; its stage takes the confirmed count.";
    $("cores-body").classList.remove("hidden");
  }

  function coresClear() {
    cores_.result = null;
    $("cores-body").classList.add("hidden");
    $("cores-status").textContent =
      "Group the undercut faces into proposed cores or side actions. Analysis only; nothing is applied.";
    const enabled = typeof featuresEnabledFor === "function" && !!state.file && featuresEnabledFor();
    $("cores-run").disabled = !enabled;
  }

  const previousReset = typeof resetFeaturesPanel === "function" ? resetFeaturesPanel : null;
  resetFeaturesPanel = function () {
    if (previousReset) previousReset();
    coresClear();
  };

  $("cores-run").addEventListener("click", async () => {
    if (!state.file) return;
    clearError();
    $("cores-run").disabled = true;
    $("cores-status").textContent = "Measuring undercuts and grouping them…";
    const body = new FormData();
    body.append("file", state.file);
    const axis = coresAxisValue();
    if (axis !== null) body.append("pull_axis", axis);
    try {
      coresRender(await api("/cad/cores/propose", { method: "POST", body }));
    } catch (error) {
      $("cores-status").textContent = "Could not propose cores for this model.";
      showError(error.message);
    }
    $("cores-run").disabled = false;
  });

  // The engineer's click: the server's count goes into the stage input as the
  // engineer's confirmation. Never called on its own.
  $("cores-use").addEventListener("click", () => {
    const stage = coreStage();
    if (!stage || !cores_.result) return;
    setStageInput(stage.stage, "cores", cores_.result.proposed_core_count);
    if (typeof renderStageInputs === "function") renderStageInputs();
    $("cores-confirm").textContent =
      `${cores_.result.proposed_core_count} core(s) confirmed on ${stage.label}. Generate its line ` +
      "again so its time includes them.";
  });

  coresClear();
})();
