"use strict";

// Detected features panel.
//
// A classic script loaded after app.js, sharing `$`, `api`, `number`,
// `escapeHtml` and `state` with it.
//
// It measures nothing and classifies nothing: every value shown here — the
// dimensions, the confidence, the evidence, the CAD face numbers — comes from
// `/cad/features`. Recognition is STEP-only, so the button is disabled for STL
// with an explanation rather than sending a request that would be refused.
//
// This panel does not touch costing. Analysing a model changes no estimate.


// Only the dimension keys worth showing inline, in the order they read best.
// Anything else the backend reports still appears in the expanded evidence.
const DIMENSION_ORDER = [
  ["diameter_mm", "Ø", "mm"],
  ["counterbore_diameter_mm", "c'bore Ø", "mm"],
  ["counterbore_depth_mm", "c'bore depth", "mm"],
  ["countersink_angle_deg", "c'sink", "°"],
  ["radius_mm", "R", "mm"],
  ["length_mm", "length", "mm"],
  ["width_mm", "width", "mm"],
  ["depth_mm", "depth", "mm"],
  ["angle_deg", "angle", "°"],
  ["area_mm2", "area", "mm²"],
  ["floor_area_mm2", "floor", "mm²"],
];

// Whether this file's features can be analysed is the backend's answer, read
// from the capability report for the loaded file. It used to be an extension
// list kept here, which is exactly the drift the capability model removes: the
// list said STEP only, so a perfectly analysable IGES file had the button
// disabled and was told it was a triangle mesh.
function featureCapability() {
  const document_ = state.document;
  if (!document_ || !document_.capabilities) return null;
  return document_.capabilities.find((report) => report.capability === "features") || null;
}

function featuresEnabledFor() {
  const report = featureCapability();
  return !!report && (report.support === "supported" || report.support === "partially_supported");
}

// Called by app.js whenever a file is selected, so the panel always reflects
// the file currently loaded rather than a previous analysis.
function resetFeaturesPanel() {
  const file = state.file;
  $("features-summary").classList.add("hidden");
  $("features-table").classList.add("hidden");
  $("features-note").classList.add("hidden");
  $("features-body").innerHTML = "";
  state.features = null;
  $("selected-feature").classList.add("hidden");
  $("selected-feature").innerHTML = "";
  // Optional call: viewer.js is a module and loads after this classic script,
  // and a stale cached copy may predate the marker API. A missing marker must
  // not take down the whole file-selection path.
  if (window.CadViewer) window.CadViewer.clearMarker?.();

  if (!file) {
    $("analyse-features").disabled = true;
    $("features-status").textContent =
      "Upload a CAD file, then analyse it to see the holes, pockets, cylinders and faces it contains.";
    return;
  }
  const report = featureCapability();
  if (!report) {
    // The file has been selected but not identified yet.
    $("analyse-features").disabled = true;
    $("features-status").textContent = "Reading the file…";
    return;
  }

  const enabled = featuresEnabledFor();
  $("analyse-features").disabled = !enabled;
  // The reason comes from the backend, so it is specific to this file rather
  // than a general statement about formats.
  $("features-status").textContent =
    report.support === "supported" ? "Ready to analyse." : report.detail;
}

function formatDimensions(dimensions) {
  const shown = DIMENSION_ORDER.filter(([key]) => key in dimensions).map(([key, label, unit]) => {
    const value = number(dimensions[key], 3);
    return label === "Ø" || label === "R" ? `${label}${value} ${unit}` : `${label} ${value} ${unit}`;
  });
  return shown.join(" · ") || "—";
}

function formatAxis(axis) {
  if (!axis) return "—";
  const named = { "1,0,0": "X", "-1,0,0": "−X", "0,1,0": "Y", "0,-1,0": "−Y", "0,0,1": "Z", "0,0,-1": "−Z" };
  const key = `${axis.x},${axis.y},${axis.z}`;
  return named[key] || `${number(axis.x, 3)}, ${number(axis.y, 3)}, ${number(axis.z, 3)}`;
}

function featureLabel(feature) {
  return feature.subtype
    ? `${feature.feature_type} (${feature.subtype.replace(/_/g, " ")})`
    : feature.feature_type;
}

$("analyse-features").addEventListener("click", async () => {
  if (!state.file) return;
  clearError();
  $("analyse-features").disabled = true;
  $("features-status").textContent = "Analysing the B-Rep…";

  const body = new FormData();
  body.append("file", state.file);

  let result;
  try {
    result = await api("/cad/features", { method: "POST", body });
  } catch (error) {
    $("features-status").textContent = "Could not analyse this model.";
    $("analyse-features").disabled = false;
    showError(error.message);
    return;
  }

  renderFeatures(result);
  $("analyse-features").disabled = false;
  if (typeof refreshFeatureDetailCount === "function") refreshFeatureDetailCount();
  if (typeof refreshFlowChrome === "function") refreshFlowChrome();
});

function renderFeatures(result) {
  state.features = result;
  const total = result.features.length;
  $("features-status").textContent =
    `${total} feature${total === 1 ? "" : "s"} across ${result.solid_count} solid${
      result.solid_count === 1 ? "" : "s"
    }, from ${result.face_count} faces and ${result.edge_count} edges — analysed in ${number(
      result.elapsed_ms,
      0
    )} ms.`;

  $("features-summary").innerHTML = result.summary
    .map((line) => `<li>${escapeHtml(line)}</li>`)
    .join("");
  $("features-summary").classList.toggle("hidden", result.summary.length === 0);

  const multipleSolids = result.solid_count > 1;
  const rows = [];
  result.solids.forEach((solid) => {
    if (multipleSolids && solid.features.length) {
      rows.push(
        `<tr class="solid-row"><td colspan="6">Solid ${solid.solid_index + 1} — ${
          solid.face_count
        } faces, ${number(solid.volume_mm3, 1)} mm³</td></tr>`
      );
    }
    solid.features.forEach((feature) => {
      rows.push(`<tr class="feature-row" data-id="${escapeHtml(feature.feature_id)}">
        <td class="mono">${escapeHtml(feature.feature_id)}</td>
        <td>${escapeHtml(featureLabel(feature))}</td>
        <td>${escapeHtml(formatDimensions(feature.dimensions))}</td>
        <td>${escapeHtml(formatAxis(feature.axis))}</td>
        <td><span class="conf conf-${escapeHtml(feature.confidence)}">${escapeHtml(
        feature.confidence
      )}</span></td>
        <td class="mono">${feature.source_faces.join(", ")}</td>
      </tr>
      <tr class="evidence-row hidden"><td colspan="6">
        <strong>How this was detected:</strong> <code>${escapeHtml(feature.detection_method)}</code>
        <ul>${feature.evidence.map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul>
      </td></tr>`);
    });
  });

  $("features-body").innerHTML = rows.join("");
  $("features-table").classList.toggle("hidden", total === 0);

  // Every claim is inspectable: clicking a feature reveals the conditions the
  // recognizer actually checked, and marks the feature on the 3D model so the
  // row and the geometry it describes are visibly the same thing.
  $("features-body").querySelectorAll("tr.feature-row").forEach((row) =>
    row.addEventListener("click", () => {
      row.nextElementSibling.classList.toggle("hidden");
      const feature = featureById(row.dataset.id);
      if (feature) selectFeature(feature);
    })
  );

  const notes = [
    "Click any feature to see the geometric conditions that identified it.",
    "Face numbers refer to the STEP model's own faces, so a feature can be traced back to the CAD.",
  ];
  if (result.unclassified_face_count) {
    notes.push(
      `${result.unclassified_face_count} face(s) matched no recognizer and are reported as unclassified rather than guessed at.`
    );
  }
  if (result.warnings.length) notes.push(...result.warnings);
  notes.push("Features are geometry only — they do not change the cost estimate.");
  $("features-note").textContent = notes.join(" ");
  $("features-note").classList.remove("hidden");
}

// ------------------------------------------------------- feature selection

// Shared with the suggestions panel, which calls this to answer "why did you
// suggest that?" by pointing at the feature responsible.
//
// Everything shown or drawn here comes from `/cad/features`. The 3D marker is
// placed at the backend's own reported location and axis — the same numbers
// this table prints — and nothing is measured from the marker or from the
// browser's geometry.

function featureById(id) {
  return (state.features ? state.features.features : []).find(
    (feature) => feature.feature_id === id
  );
}

function selectFeature(feature, options) {
  // `flagged` is set only by the DFM panel, for a feature the server's flags
  // name; it changes the marker drawn and the sentence below, nothing else.
  const { scroll = false, flagged = false } = options || {};

  // Point at it on the model.
  if (window.CadViewer) window.CadViewer.markFeature?.(feature, { flagged });

  // Light up its row, and only its row.
  const body = $("features-body");
  body.querySelectorAll("tr.feature-row").forEach((row) =>
    row.classList.toggle("is-selected", row.dataset.id === feature.feature_id)
  );

  if (scroll) {
    const row = body.querySelector(`tr.feature-row[data-id="${CSS.escape(feature.feature_id)}"]`);
    if (row) {
      row.scrollIntoView({ block: "nearest" });
      // The evidence is the point of following the link, so open it.
      if (row.nextElementSibling) row.nextElementSibling.classList.remove("hidden");
    }
  }

  const where = feature.source_faces.length
    ? `CAD face(s) <span class="mono">${feature.source_faces.join(", ")}</span>`
    : feature.source_facets.length
      ? `${feature.source_facets.length} polygon(s) of the faceted body`
      : "no source geometry recorded";

  const at = feature.location
    ? `at (${number(feature.location.x, 2)}, ${number(feature.location.y, 2)}, ${number(
        feature.location.z,
        2
      )}) mm`
    : "no representative point";

  $("selected-feature").innerHTML =
    `<strong>${escapeHtml(feature.feature_id)}</strong> ` +
    `${escapeHtml(featureLabel(feature))} — ${escapeHtml(formatDimensions(feature.dimensions))}<br>` +
    `${where} · ${at} · axis ${escapeHtml(formatAxis(feature.axis))} · ` +
    `confidence ${escapeHtml(feature.confidence)} · source ${escapeHtml(feature.source_type)}` +
    `<br><span class="muted">${
      flagged ? "Marked on the 3D model as a DFM-flagged feature." : "Marked on the 3D model."
    } The marker is a pointer, not a ` +
    `measurement — every dimension above comes from the CAD file.</span>`;
  $("selected-feature").classList.remove("hidden");
}


/**
 * Mark several features at once — what a *setup* touches, rather than one
 * operation. Used by the manufacturing plan when a setup row is selected.
 *
 * The viewer marks one feature at a time, so this marks the first and reports
 * the rest by name. That is honest about the viewer's current capability rather
 * than silently showing only part of the answer.
 */
function selectFeatures(featureIds, options) {
  // `features` lets a caller that already holds the server's feature objects
  // (the DFM report carries the ones it flagged) select them without the
  // features panel having been analysed first.
  const { label = "", features = null, flagged = false } = options || {};
  const found = features || featureIds
    .map((id) => featureById(id))
    .filter(Boolean);

  if (!found.length) {
    $("selected-feature").classList.add("hidden");
    return;
  }

  selectFeature(found[0], { scroll: !flagged, flagged });

  if (found.length > 1 && flagged) {
    const names = found.map((feature) => feature.feature_id);
    $("selected-feature").innerHTML +=
      `<br><strong>${escapeHtml(label || "This flag")}</strong> covers ` +
      `${found.length} features: <span class="mono">${escapeHtml(names.join(", "))}</span>. ` +
      `<span class="muted">The details above are the first; the DFM panel marks each one ` +
      `that has a location.</span>`;
  } else if (found.length > 1) {
    const names = found.map((feature) => feature.feature_id);
    $("selected-feature").innerHTML +=
      `<br><strong>${escapeHtml(label || "This setup")}</strong> covers ` +
      `${found.length} features: <span class="mono">${escapeHtml(names.join(", "))}</span>. ` +
      `<span class="muted">The marker shows the first; the viewer marks one feature at a time.</span>`;
  }
}
