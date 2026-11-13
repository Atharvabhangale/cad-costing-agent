// Pull direction, draft and undercuts (the CAD section's pull-direction card).
//
// Renders what `POST /cad/draw-direction` returns and nothing more: every axis,
// angle, count and area, the recommendation and its basis come from the server.
// This file compares nothing, ranks nothing and adds nothing up. It states no
// manufacturing process and changes no route, time or cost.
//
// The engineer may enter a pull axis. It is sent as typed; the server evaluates
// it like every candidate and reports it beside the recommendation. The same
// three inputs are read by dfm.js, so the draft and undercut DFM rules check
// against the axis the engineer entered.
//
// Undercut faces are pointed at with the viewer's existing flag marker: a point
// the server sampled on each face, with a line along the pull axis. It is a
// pointer, not an outline of the face — the display mesh does not record which
// triangles belong to which CAD face.
//
// An IIFE, so it declares no global function another classic script could
// silently replace.

(function () {
  const draw_ = { result: null, marked: false };

  function drawAxisValue() {
    const values = ["draw-axis-x", "draw-axis-y", "draw-axis-z"].map((id) => $(id).value.trim());
    if (values.every((value) => value === "")) return null;
    return JSON.stringify(values.map((value) => (value === "" ? null : Number(value))));
  }

  function drawCandidateRows(result) {
    return (result.candidates || [])
      .map(
        (candidate) => `
        <tr${candidate.recommended ? ' class="is-recommended"' : ""}>
          <td>${escapeHtml(String(candidate.order + 1))}</td>
          <td class="mono">${escapeHtml(candidate.label)}</td>
          <td>${escapeHtml(String(candidate.undercut_faces))}</td>
          <td>${number(candidate.undercut_area_mm2, 1)}</td>
          <td>${escapeHtml(String(candidate.zero_draft_faces))}</td>
          <td>${number(candidate.zero_draft_area_mm2, 1)}</td>
          <td>${candidate.recommended ? "Recommended" : ""}${candidate.engineer_entered ? " Entered" : ""}</td>
          <td class="muted">${escapeHtml(candidate.sources.join("; "))}</td>
        </tr>`
      )
      .join("");
  }

  function drawFaceRows(result) {
    return (result.faces || [])
      .map(
        (face) => `
        <tr>
          <td>${escapeHtml(String(face.face_id))}</td>
          <td>${escapeHtml(face.surface_type)}</td>
          <td>${face.draft_deg === null ? "—" : number(face.draft_deg, 3)}</td>
          <td class="mono">${escapeHtml(face.draft_method)}</td>
          <td>${escapeHtml(face.release)}</td>
          <td>${escapeHtml(String(face.samples_undercut))} of ${escapeHtml(String(face.samples))}</td>
        </tr>`
      )
      .join("");
  }

  function drawRender(result) {
    draw_.result = result;
    draw_.marked = false;
    if (!result.measured) {
      $("draw-status").textContent = `Not measured. ${result.not_measured_reason || ""}`;
      $("draw-body").classList.add("hidden");
      return;
    }
    const source =
      result.axis_source === "engineer_entered"
        ? "the axis you entered"
        : "the recommended axis";
    $("draw-status").textContent =
      `Faces reported against pull axis ${result.pull_axis_label} (${source}). ` +
      `Recommended: ${result.recommended_axis_label}.`;
    $("draw-basis").textContent = result.recommendation_basis;
    $("draw-counts").innerHTML = `
      <strong>${escapeHtml(String(result.undercut_face_count))} undercut face(s)</strong>,
      ${number(result.undercut_area_mm2, 1)} mm² ·
      <strong>${escapeHtml(String(result.zero_draft_face_count))} zero-draft face(s)</strong>,
      ${number(result.zero_draft_area_mm2, 1)} mm² ·
      least draft on a face that releases:
      ${result.minimum_draft_deg === null ? "—" : `${number(result.minimum_draft_deg, 3)}° (face ${escapeHtml(String(result.minimum_draft_face))})`}`;
    $("draw-candidates").innerHTML = drawCandidateRows(result);
    $("draw-faces").innerHTML = drawFaceRows(result);
    $("draw-parting").textContent = result.parting_note;
    $("draw-method").textContent = result.method_description;
    $("draw-limits").innerHTML = (result.limitations || [])
      .map((line) => `<li>${escapeHtml(line)}</li>`)
      .join("");
    const notes = [result.budget_note].concat(result.warnings || []).filter(Boolean);
    $("draw-warnings").innerHTML = notes.map((line) => `<div class="warning-inline">${escapeHtml(line)}</div>`).join("");
    $("draw-show-undercuts").disabled = !result.undercut_face_count;
    $("draw-marked").textContent = result.undercut_face_count
      ? "Show the undercut faces to point at each one on the 3D model."
      : "No face is an undercut along this axis, so there is nothing to mark.";
    $("draw-body").classList.remove("hidden");
  }

  function drawClear() {
    draw_.result = null;
    draw_.marked = false;
    $("draw-body").classList.add("hidden");
    $("draw-status").textContent =
      "Measure the pull direction, draft and undercuts for a cast, moulded or forged part. Analysis only.";
    const enabled = typeof featuresEnabledFor === "function" && !!state.file && featuresEnabledFor();
    $("draw-run").disabled = !enabled;
  }

  const previousReset = typeof resetFeaturesPanel === "function" ? resetFeaturesPanel : null;
  // Called by app.js on every file selection, so a previous file's result never
  // sits beside the model now loaded.
  resetFeaturesPanel = function () {
    if (previousReset) previousReset();
    drawClear();
  };

  $("draw-run").addEventListener("click", async () => {
    if (!state.file) return;
    clearError();
    $("draw-run").disabled = true;
    $("draw-status").textContent = "Casting rays along each candidate pull axis…";
    const body = new FormData();
    body.append("file", state.file);
    const axis = drawAxisValue();
    if (axis !== null) body.append("pull_axis", axis);
    let result;
    try {
      result = await api("/cad/draw-direction", { method: "POST", body });
    } catch (error) {
      $("draw-status").textContent = "Could not measure the pull direction of this model.";
      $("draw-run").disabled = false;
      showError(error.message);
      return;
    }
    drawRender(result);
    $("draw-run").disabled = false;
  });

  // Each undercut face's own sampled point, with the pull axis as its line.
  $("draw-show-undercuts").addEventListener("click", () => {
    const result = draw_.result;
    if (!result) return;
    const markers = result.faces
      .filter((face) => face.release === "undercut" && face.marker)
      .map((face) => ({ location: face.marker, axis: result.pull_axis, label: `Undercut face ${face.face_id}` }));
    const viewer = window.CadViewer;
    const drawn = viewer && viewer.markFlaggedFeatures ? viewer.markFlaggedFeatures(markers) : 0;
    if (!drawn) {
      $("draw-marked").textContent =
        "Nothing could be marked: the 3D preview is not showing this model. The counts above are unaffected.";
      return;
    }
    draw_.marked = true;
    $("draw-marked").textContent =
      `${drawn} marker(s) drawn with a magenta diamond, one at a sampled point on each undercut face ` +
      `(${markers.map((marker) => marker.label).join(", ")}), with a line along the pull axis. ` +
      "Each points at a face; it does not outline it.";
  });

  document.addEventListener("cadviewer:marker", (event) => {
    if (event.detail && event.detail.flagged) return;
    if (draw_.marked) {
      draw_.marked = false;
      $("draw-marked").textContent = "";
    }
  });

  drawClear();
})();
