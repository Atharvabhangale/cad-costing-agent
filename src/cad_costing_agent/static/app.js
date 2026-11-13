"use strict";

// The backend is the source of truth for every number shown here. This file
// collects inputs, calls the existing API, and formats what comes back — it
// contains no costing arithmetic of its own.

const $ = (id) => document.getElementById(id);
// Which formats exist, what each can do, and where each gets its units, all
// come from /cad/formats. Nothing about a format is hardcoded here: a list in
// the browser that disagreed with the backend would advertise support that
// does not exist, which is exactly what the capability model is for.

const state = {
  file: null,
  formats: [],       // /cad/formats — the capability matrix, per format
  document: null,    // /cad/import for the selected file: what it is and what it can do
  assembly: null,    // /cad/assembly once decomposed, owned by assembly.js
  assemblyCost: null,// the component roll-up, owned by assembly.js
  assemblyCostStale: false,// a roll-up was discarded because its inputs moved
  explodedView: null,// display-only exploded arrangement; never sent to the backend
  componentCosting: null,// per-component classification decisions, owned by assembly.js
  assemblyPlan: null,   // one manufacturing plan per manufactured component
  planComponentId: null,// which component's plan the operations table is editing
  geometry: null,
  gap: null,         // the backend's report of geometry the file does not contain
  completion: null,  // the user's stated assumption, once applied
  mass: null,        // kg per part, as derived by the backend from an estimate
  autoPartName: null,// the part name this app filled in, so a typed one is kept
  features: null,    // the last /cad/features result, shared with the suggestions panel
  suggestions: null, // the last /cad/operations/suggest result
  rateSets: [],      // flat list of {rate_set_id, version, ...} from /rates
  rateSet: null,     // the full selected rate set from /rates/{id}?version=
  timeProfiles: [],  // flat list from /time-profiles
  timeProfile: null, // the full selected time profile — machining assumptions, no money
  plan: null,        // the manufacturing plan, owned by plan.js
  processClassification: null,// recommended family and route + the engineer's answer, owned by process.js
  rawStock: null,    // proposed bar for a turned part + the engineer's answer, owned by stock.js
  result: null,
  assumptions: null,        // what the current estimate rested on
  previousEstimateId: null, // so a new estimate can be explained against it
  selectedForCompare: new Set(),
};

// ---------------------------------------------------------------- formatting

const NUMBER = new Intl.NumberFormat("en-IN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

// Currencies are never converted. INR gets the rupee sign; anything else is
// shown with its own code so a non-INR figure can never be mistaken for rupees.
function money(amount, currency) {
  if (amount === null || amount === undefined) return "—";
  const formatted = NUMBER.format(amount);
  return currency === "INR" ? `₹${formatted}` : `${currency} ${formatted}`;
}

function number(value, digits = 2) {
  if (value === null || value === undefined) return "—";
  return Number(value).toLocaleString("en-IN", { maximumFractionDigits: digits });
}

function extensionOf(name) {
  return (name.split(".").pop() || "").toLowerCase();
}

// Profile names, material names, part names and descriptions are all typed by
// the user and several of them are rendered through innerHTML. A name
// containing "&" or "<" would otherwise corrupt the markup around it.
function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]
  );
}

// ------------------------------------------------------------------ requests

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail;
    try {
      const body = await response.json();
      detail = body.detail;
    } catch {
      detail = null;
    }
    throw new Error(readableError(detail, response.status));
  }
  return response.json();
}

// FastAPI validation errors arrive as a list of field objects. Turn those into
// something a person can act on rather than showing raw API internals.
function readableError(detail, status) {
  if (typeof detail === "string" && detail) return detail;
  // A structured refusal (needs_geometry_input, process_route_not_costable)
  // carries its own sentence.
  if (detail && typeof detail.message === "string" && detail.message) return detail.message;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        const field = (item.loc || []).filter((p) => p !== "body").join(" → ");
        return field ? `${field}: ${item.msg}` : item.msg;
      })
      .join("\n");
  }
  if (status === 404) return "Not found.";
  return `The request failed (HTTP ${status}).`;
}

function showError(message) {
  $("error").textContent = message;
  $("error").classList.remove("hidden");
}

function clearError() {
  $("error").classList.add("hidden");
}

// ------------------------------------------------------------------ tabs

const TABS = ["estimate", "setup", "timesetup", "history", "feedback", "bulk"];

function showTab(name) {
  document.querySelectorAll(".tab").forEach((tab) =>
    tab.classList.toggle("active", tab.dataset.tab === name)
  );
  TABS.forEach((tab) => $(`tab-${tab}`).classList.toggle("hidden", tab !== name));
  if (name === "history") loadHistory();
  if (name === "setup") loadProfiles();
  if (name === "timesetup") loadTimeProfiles();
  // Guarded forward: feedback.js is loaded after this file, and the history is
  // read only when the tab is actually opened rather than on every page load.
  if (name === "feedback" && typeof openFeedback === "function") openFeedback();
  // bulk.js is loaded after this file, so it is guarded forward as well.
  if (name === "bulk" && typeof openBulk === "function") openBulk();
}

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => showTab(tab.dataset.tab))
);

// ------------------------------------------------------------------ formats

const SUPPORT_LABELS = {
  supported: "yes",
  partially_supported: "limited",
  requires_user_input: "needs input",
  unsupported: "no",
};

// The capabilities worth naming on the upload panel. The full matrix is shown
// per file once one is selected.
const HEADLINE_CAPABILITIES = ["geometry", "viewer", "features", "costing"];

async function loadFormats() {
  try {
    state.formats = await api("/cad/formats");
  } catch (error) {
    state.formats = [];
    $("format-support").innerHTML =
      "<p class='muted'>Could not load the list of supported formats.</p>";
    return;
  }

  // Only formats that can actually do something are offered to the file
  // picker. The others are still listed, so a user with a .prt file learns why
  // it is not accepted rather than finding it silently ignored.
  const usable = state.formats.filter((format) => format.any_support);
  $("file").accept = usable
    .flatMap((format) => format.extensions.map((extension) => `.${extension}`))
    .join(",");

  $("format-support").innerHTML = state.formats
    .map((format) => {
      const extensions = format.extensions.map((e) => `<code>.${e}</code>`).join(" ");
      const capabilities = HEADLINE_CAPABILITIES.map((name) => {
        const report = format.capabilities.find((c) => c.capability === name);
        return `<span class="cap ${report.support}" title="${escapeHtml(
          report.detail || "Fully supported."
        )}">${name.replace("_", " ")}: ${SUPPORT_LABELS[report.support]}</span>`;
      }).join("");
      return `
        <div class="format ${format.any_support ? "" : "format-unavailable"}">
          <div class="format-head"><strong>${escapeHtml(format.label)}</strong> ${extensions}</div>
          <div class="format-headline">${escapeHtml(format.headline)}</div>
          <div class="caps">${capabilities}</div>
        </div>`;
    })
    .join("");
}

function formatFor(extension) {
  return state.formats.find((format) => format.extensions.includes(extension)) || null;
}

function acceptedExtensions() {
  return state.formats
    .filter((format) => format.any_support)
    .flatMap((format) => format.extensions);
}

// ------------------------------------------------------------------ file

const drop = $("drop");
$("browse").addEventListener("click", () => $("file").click());
$("file").addEventListener("change", (event) => acceptFile(event.target.files[0]));

["dragenter", "dragover"].forEach((type) =>
  drop.addEventListener(type, (event) => {
    event.preventDefault();
    drop.classList.add("over");
  })
);
["dragleave", "drop"].forEach((type) =>
  drop.addEventListener(type, (event) => {
    event.preventDefault();
    drop.classList.remove("over");
  })
);
drop.addEventListener("drop", (event) => acceptFile(event.dataTransfer.files[0]));

// The viewer lives in viewer.js, which is an ES module and so cannot be called
// directly from this classic script. It publishes itself on window instead.
const viewer = () => window.CadViewer;

async function acceptFile(file) {
  if (!file) return;
  clearError();
  const extension = extensionOf(file.name);
  const known = formatFor(extension);

  // A recognized format with no support is refused with that format's own
  // explanation, not a generic message: a .drw file is a drawing, and saying
  // so is more use than "unsupported".
  if (known && !known.any_support) {
    const reason = known.capabilities.find((c) => c.capability === "geometry");
    showError(`${known.label}: ${reason.detail}`);
    return;
  }
  if (!known) {
    const accepted = acceptedExtensions()
      .map((value) => `.${value}`)
      .join(", ");
    showError(
      `"${file.name}" is not a CAD file this application reads. Accepted formats are ${accepted}.`
    );
    return;
  }

  state.file = file;
  state.document = null;
  state.geometry = null;
  state.gap = null;
  state.completion = null;
  state.mass = null;
  resetGeometryGap();
  $("file-info").classList.remove("hidden");
  $("file-name").textContent = file.name;
  // This file is not the last one, so it does not keep the last one's name.
  applyDefaultPartName(file.name);
  $("file-size").textContent = `(${(file.size / 1024).toFixed(0)} KB)`;
  $("geo-status").textContent = "reading…";
  $("model-info").classList.add("hidden");
  // The last estimate was for a different file. It stays in `state.result` as
  // the baseline a new estimate is explained against, but it is no longer a
  // figure describing what is on screen, so it stops being displayed.
  $("result").classList.add("hidden");
  if (viewer()) viewer().loading("Reading and tessellating the model…");

  applyUnitRulesFor(known);
  resetImportReport();
  refreshCalculateState();
  // The features panel belongs to whichever file is loaded now, so any
  // previous analysis is cleared rather than left looking current.
  if (typeof resetFeaturesPanel === "function") resetFeaturesPanel();
  if (typeof resetSuggestionsPanel === "function") resetSuggestionsPanel();
  if (typeof resetAssemblyPanel === "function") resetAssemblyPanel();

  // Read the model straight away so the file is visibly *processed*, not merely
  // attached. One upload returns both the geometry metadata and the mesh the
  // viewer draws; neither needs any other input, so this can run on selection.
  const body = new FormData();
  body.append("file", file);
  try {
    // Identification first, without parsing geometry — a byte scan and a header
    // read, so it costs milliseconds even on a very large export. It is what
    // says whether this particular file can do what its format promises.
    const identify = new FormData();
    identify.append("file", file);
    state.document = await api("/cad/import?with_geometry=false", {
      method: "POST",
      body: identify,
    });
    renderImportReport(state.document);
    applyUnitRulesFor(known, state.document);
    // The features and suggestions panels gate on this file's capabilities, so
    // they are refreshed now that those are known.
    if (typeof resetFeaturesPanel === "function") resetFeaturesPanel();
    if (typeof resetSuggestionsPanel === "function") resetSuggestionsPanel();
    // Whether this file is a part or an assembly is said now, at upload.
    if (typeof refreshAssemblyPanel === "function") refreshAssemblyPanel();
    // Identification is quick even on a very large export, so the context bar
    // names the file now rather than after the geometry parse.
    if (typeof refreshFlowChrome === "function") refreshFlowChrome();

    const model = await api("/cad/mesh", { method: "POST", body });
    state.geometry = model.geometry;
    renderModelInfo(model);
    // Some files simply do not contain enough geometry to measure a volume.
    // That is a question for the user, not a dead end.
    showGeometryGap(model.geometry);
    $("geo-status").textContent = "";
    // After the gap, not before: the overview's warning and its recommended
    // next step both turn on whether a volume could be measured.
    if (typeof refreshFlowChrome === "function") refreshFlowChrome();

    // A model can be readable but too dense to preview. Geometry and costing
    // are unaffected, so only the viewer degrades.
    if (model.mesh) viewer().show(model.mesh);
    else viewer().error(model.mesh_error || "This model could not be previewed.");
  } catch (error) {
    $("geo-status").textContent = "could not be read";
    if (viewer()) viewer().error("This file could not be read as CAD geometry.");
    showError(error.message);
  }
}

// ------------------------------------------------------ what this file can do

function resetImportReport() {
  $("import-report").classList.add("hidden");
  $("import-report").innerHTML = "";
}

// Every line here is a backend statement about the uploaded file. Nothing is
// derived in the browser, and a capability short of full support always shows
// the backend's own reason for it.
function renderImportReport(document_) {
  const panel = $("import-report");
  const rows = document_.capabilities
    .filter((report) => report.support !== "supported")
    .map(
      (report) => `
        <li><span class="cap ${report.support}">${report.capability.replace("_", " ")}: ${
        SUPPORT_LABELS[report.support]
      }</span> ${escapeHtml(report.detail)}</li>`
    )
    .join("");

  const warnings = (document_.warnings || [])
    .map((warning) => `<li class="warn">${escapeHtml(warning.message)}</li>`)
    .join("");

  const unit =
    document_.unit_source === "not_carried_by_format"
      ? "this format records no unit — set it in step 2"
      : document_.units
      ? `${document_.units} (declared by the file)`
      : "not declared by the file";

  panel.innerHTML = `
    <div class="import-head">
      <strong>${escapeHtml(document_.format_label)}</strong>
      <span class="muted">${escapeHtml(document_.representation.replace("_", " "))}</span>
      <span class="muted">units: ${escapeHtml(unit)}</span>
      ${
        document_.metadata && document_.metadata.originating_system
          ? `<span class="muted">from ${escapeHtml(document_.metadata.originating_system)}</span>`
          : ""
      }
    </div>
    ${warnings ? `<ul class="import-warnings">${warnings}</ul>` : ""}
    ${
      rows
        ? `<p class="hint">What this file cannot do, and why:</p><ul class="import-caveats">${rows}</ul>`
        : "<p class='muted'>Every capability is fully supported for this file.</p>"
    }`;
  panel.classList.remove("hidden");
}

// ---------------------------------------------------------- geometry gap

function resetGeometryGap() {
  $("geometry-gap").classList.add("hidden");
  $("gap-applied").classList.add("hidden");
  $("gap-status").textContent = "";
  $("gap-volume").value = "";
}

// A file whose polygons do not enclose a solid has no volume to measure. The
// backend says what it knows and what is missing; this asks for the rest.
function showGeometryGap(geometry) {
  if (!geometry || geometry.geometry_status !== "incomplete_requires_input") return;
  const report = geometry.incomplete || {};
  state.gap = report;

  $("gap-summary").innerHTML =
    `<strong>${report.open_component_count} of ${report.component_count} components could not be ` +
    `measured.</strong> The watertight part comes to ` +
    `<strong>${number(report.known_volume_mm3, 3)} mm³</strong>, which is ` +
    `${number((report.known_polygon_share || 0) * 100, 0)}% of the polygons — not the part's volume.`;
  $("gap-explanation").textContent = report.explanation || "";
  $("geometry-gap").classList.remove("hidden");
  refreshCalculateState();
}

function selectedGapMethod() {
  const chosen = document.querySelector("input[name='gap-method']:checked");
  return chosen ? chosen.value : "total_volume";
}

document.querySelectorAll("input[name='gap-method']").forEach((radio) =>
  radio.addEventListener("change", () => {
    const uploading = selectedGapMethod() === "upload_corrected_cad";
    $("gap-entry").classList.toggle("hidden", uploading);
    $("gap-upload-note").classList.toggle("hidden", !uploading);
  })
);

$("apply-gap").addEventListener("click", async () => {
  clearError();
  const method = selectedGapMethod();
  if (method === "upload_corrected_cad") return;

  const raw = $("gap-volume").value.trim();
  const supplied = Number(raw);
  if (raw === "" || !Number.isFinite(supplied) || supplied <= 0) {
    showError("Enter a volume in mm³ greater than zero.");
    return;
  }

  // The backend applies the assumption and derives the mass, so the figures
  // shown here are its arithmetic and not the browser's.
  $("apply-gap").disabled = true;
  $("gap-status").textContent = "Applying…";
  const body = new FormData();
  body.append("file", state.file);
  body.append("material", $("material").value);
  body.append("source_unit", $("source_unit").value);
  body.append("geometry_completion_method", method);
  body.append("geometry_completion_volume_mm3", String(supplied));

  let mass;
  try {
    mass = await api("/cad/mass", { method: "POST", body });
  } catch (error) {
    $("gap-status").textContent = "";
    $("apply-gap").disabled = false;
    showError(error.message);
    return;
  }

  state.completion = { method, volume_mm3: supplied };
  state.mass = mass.mass_kg;
  $("gap-status").textContent = "";
  $("apply-gap").disabled = false;
  $("gap-applied").innerHTML =
    `<strong>✓ Geometry completed using your input.</strong><br>` +
    `${method === "total_volume" ? "Total volume supplied" : "Missing volume supplied"}: ` +
    `${number(supplied, 3)} mm³<br>` +
    `Volume used: <strong>${number(mass.volume_m3 * 1e9, 3)} mm³</strong> · ` +
    `Mass: <strong>${number(mass.mass_kg, 4)} kg per part</strong>`;
  $("gap-applied").classList.remove("hidden");
  if (typeof refreshFlowChrome === "function") refreshFlowChrome();

  $("mi-mass").textContent = `${number(mass.mass_kg, 4)} kg per part (from your assumption)`;
  refreshCalculateState();
});

// Every value here comes from the backend response. Nothing about the geometry
// is measured or recomputed in the browser.
function renderModelInfo(model) {
  const geometry = model.geometry;
  const unit = unitLabel();
  $("mi-file").textContent = model.filename;
  $("mi-format").textContent = geometry.source_format.toUpperCase();
  // A zero here would read as "this part has no volume" rather than "this file
  // does not say".
  $("mi-volume").textContent =
    geometry.geometry_status === "incomplete_requires_input"
      ? "not measurable from this file — see below"
      : `${number(geometry.volume, 3)} ${unit}³`;
  $("mi-area").textContent = `${number(geometry.surface_area, 3)} ${unit}²`;
  const box = geometry.bounding_box;
  $("mi-bbox").textContent =
    `${number(box.x, 2)} × ${number(box.y, 2)} × ${number(box.z, 2)} ${unit}`;
  $("mi-unit").textContent = geometry.detected_unit
    ? `${geometry.detected_unit} (declared by the file)`
    : "not stated by the file — set it in step 2";
  $("mi-mass").textContent = state.mass
    ? `${number(state.mass, 4)} kg per part`
    : "— (calculate an estimate to derive it)";
  // A reduced count must never read as the model's own polygon count, so the
  // original is shown beside it whenever the preview was simplified.
  $("mi-mesh").textContent = !model.mesh
    ? "unavailable"
    : model.mesh.display_mesh_simplified
      ? `${number(model.mesh.display_triangle_count, 0)} triangles ` +
        `(original: ${number(model.mesh.original_triangle_count, 0)}) — ` +
        `Simplified for display`
      : `${number(model.mesh.triangle_count, 0)} triangles`;
  // The faceted note already makes the display-only point; don't say it twice.
  const tessellation = model.mesh ? model.mesh.tessellation.note : "";
  const displayOnly = tessellation.includes("display only")
    ? tessellation
    : `${tessellation} The mesh is for display only — volume, surface area and mass come ` +
      `from the exact geometry, not from these triangles.`;
  $("mi-note").textContent = !model.mesh
    ? model.mesh_error || ""
    : model.mesh.display_mesh_simplified
      ? `Preview simplified for browser performance. Engineering geometry and costing use ` +
        `the full CAD data. ${displayOnly}`
      : displayOnly;
  $("model-info").classList.remove("hidden");
}

function unitLabel() {
  return $("source_unit").value;
}

// A format that declares its own unit has it normalised to mm by the reader,
// and the backend rejects any other source_unit — so the control is locked
// rather than letting the user pick something that will fail. Which formats
// those are is a backend fact read from /cad/formats, not an extension list
// kept here: IGES declares its unit exactly as STEP does, and a hardcoded list
// would have missed it.
function applyUnitRulesFor(format, document_) {
  const unit = $("source_unit");
  const declares = format && format.unit_source === "file_declared";

  if (declares) {
    unit.value = "mm";
    unit.disabled = true;
    // Once the file itself has been read, say what *it* declared rather than
    // what the format generally does.
    $("unit-note").textContent =
      document_ && document_.unit_note ? document_.unit_note : format.unit_note;
  } else {
    unit.disabled = false;
    $("unit-note").textContent =
      (format && format.unit_note) ||
      "This format does not record units. Choose the unit this model was drawn in.";
  }
}

$("source_unit").addEventListener("change", () => {
  if (state.geometry) acceptFile(state.file);
});

// ------------------------------------------------------------------ rate sets

async function loadRateSets() {
  try {
    state.rateSets = await api("/rates");
  } catch (error) {
    showError(`Could not load rate sets. ${error.message}`);
    return;
  }
  // Labelled by profile name rather than by slug — the id is developer-facing.
  const previous = $("rate_set_id").value;
  const families = [...new Set(state.rateSets.map((r) => r.rate_set_id))].sort();
  $("rate_set_id").innerHTML = families
    .map((id) => `<option value="${id}">${escapeHtml(profileName(id))}</option>`)
    .join("");
  if (families.includes(previous)) $("rate_set_id").value = previous;
  await onFamilyChange();
}

// The display name of a profile's newest version — a renamed profile should
// show its current name, not the one its first version happened to carry.
function profileName(rateSetId) {
  const versions = state.rateSets
    .filter((r) => r.rate_set_id === rateSetId)
    .sort((a, b) => compareVersions(a.version, b.version));
  const newest = versions[versions.length - 1];
  return newest ? newest.name : rateSetId;
}

function versionsFor(familyId) {
  return state.rateSets
    .filter((r) => r.rate_set_id === familyId)
    .map((r) => r.version)
    .sort(compareVersions)
    .reverse(); // newest first
}

// Numeric comparison, matching the backend: 10.0.0 outranks 2.0.0.
function compareVersions(a, b) {
  const pa = a.split(".").map(Number);
  const pb = b.split(".").map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i += 1) {
    const diff = (pa[i] || 0) - (pb[i] || 0);
    if (diff) return diff;
  }
  return 0;
}

async function onFamilyChange() {
  const familyId = $("rate_set_id").value;
  const versions = versionsFor(familyId);
  // "Latest" is the default, but an exact version can always be pinned.
  $("rate_set_version").innerHTML =
    `<option value="">Latest (${versions[0] || "—"})</option>` +
    versions.map((v) => `<option value="${v}">${v}</option>`).join("");
  await loadSelectedRateSet();
}

async function loadSelectedRateSet() {
  const familyId = $("rate_set_id").value;
  const pinned = $("rate_set_version").value;
  if (!familyId) return;
  const query = pinned ? `?version=${encodeURIComponent(pinned)}` : "";
  try {
    state.rateSet = await api(`/rates/${encodeURIComponent(familyId)}${query}`);
  } catch (error) {
    showError(error.message);
    return;
  }
  renderRateSetDetail();
  populateMaterials();
  populateProcessOptions();
  refreshCalculateState();
  // Guarded forward: process.js shows which stage rates this rate set holds.
  if (typeof renderStageInputs === "function") renderStageInputs();
}

function renderRateSetDetail() {
  const rateSet = state.rateSet;
  const badges = [
    `<span class="badge">v${rateSet.version}</span>`,
    `<span class="badge currency">Currency: ${rateSet.currency}</span>`,
    rateSet.is_illustrative
      ? `<span class="badge illustrative">Illustrative POC rates</span>`
      : `<span class="badge real">Real rates</span>`,
  ];
  $("rate-badges").innerHTML = badges.join("");
  $("rate-desc").textContent = rateSet.description || "";
}

// Only materials the rate set actually prices, so a request can never fail with
// "this rate set has no rate for that material".
function populateMaterials() {
  const previous = $("material").value;
  const names = Object.values(state.rateSet.materials || {}).map((m) => m.name);
  $("material").innerHTML = names.length
    ? names.map((n) => `<option value="${n}">${n}</option>`).join("")
    : `<option value="">No materials priced by this rate set</option>`;
  if (names.includes(previous)) $("material").value = previous;
}

function populateProcessOptions() {
  const processes = Object.keys(state.rateSet.operations || {});
  $("add-op").disabled = processes.length === 0;
  // The plan renders its own process dropdowns, so re-render rather than
  // rewriting them here — that keeps one definition of what a plan row looks
  // like, and preserves each row's current selection.
  if (typeof renderPlan === "function") renderPlan();
}

$("rate_set_id").addEventListener("change", onFamilyChange);
$("rate_set_version").addEventListener("change", loadSelectedRateSet);

// ------------------------------------------------------------------ operations
//
// The operations table is the **manufacturing plan**, owned by plan.js: it holds
// what the system suggested, what the engineer chose, the sequence, and which
// times are committed. `+ Add operation` and the row controls are wired there.
//
// What lives here is only the rate-set-driven part: which processes this profile
// can price at all.

function collectManufacturingPlan() {
  // plan.js strips its UI-only fields and returns the shape the backend's
  // ManufacturingPlan expects. Null when there is nothing to cost.
  return typeof planPayload === "function" ? planPayload() : null;
}

// ------------------------------------------------------------------ calculate

// The value the markup ships with. Read once, before anything can change it,
// so the first upload can tell "nobody has named this" from "the user did".
const INITIAL_PART_NAME = $("part_name").value.trim();

["part_id", "part_name", "quantity", "material"].forEach((id) =>
  $(id).addEventListener("input", refreshCalculateState)
);

/** The default name for a freshly loaded file: its own name, less the suffix.
 *
 * Deliberately not tidied any further. "model1_1" is what the file is called,
 * and inventing "Model 1" would be this layer naming a part it knows nothing
 * about.
 */
function defaultPartNameFor(filename) {
  const stem = String(filename || "").replace(/\.[^.]+$/, "").trim();
  return stem || String(filename || "").trim();
}

/** Apply that default, unless a person has typed a name of their own.
 *
 * The markup ships a starting value and this app fills one in on every upload;
 * both are the system's suggestion. Anything else in the field came from the
 * user and is left exactly where it is.
 */
function applyDefaultPartName(filename) {
  const field = $("part_name");
  const current = field.value.trim();
  const isSystemsOwn =
    current === "" ||
    current === (state.autoPartName || "") ||
    current === INITIAL_PART_NAME;
  if (!isSystemsOwn) return;

  const suggested = defaultPartNameFor(filename);
  if (!suggested) return;
  field.value = suggested;
  state.autoPartName = suggested;
}

function refreshCalculateState() {
  // A model whose geometry could not be measured cannot be costed until the
  // user has said what the missing volume is.
  const geometryResolved = !state.gap || Boolean(state.completion);
  const ready =
    Boolean(state.file) &&
    geometryResolved &&
    Boolean($("part_id").value.trim()) &&
    Boolean($("part_name").value.trim()) &&
    Boolean($("material").value) &&
    Number($("quantity").value) >= 1;
  $("calculate").disabled = !ready;
  $("calculate").title = geometryResolved
    ? ""
    : "Supply the missing geometry above before calculating.";
}

$("calculate").addEventListener("click", async () => {
  clearError();
  $("calculate").disabled = true;
  $("calc-status").textContent = "Calculating…";

  const body = new FormData();
  body.append("file", state.file);
  body.append("material", $("material").value);
  body.append("source_unit", $("source_unit").value);
  body.append("part_id", $("part_id").value.trim());
  body.append("part_name", $("part_name").value.trim());
  body.append("quantity", String(Number($("quantity").value)));
  body.append("rate_set_id", $("rate_set_id").value);

  // Only pin a version when one was explicitly chosen; otherwise let the
  // backend resolve the latest.
  const pinned = $("rate_set_version").value;
  if (pinned) body.append("rate_set_version", pinned);

  // Only sent when the file's own geometry was not measurable; supplying it
  // for a measurable model is refused by the API, and rightly.
  if (state.completion) {
    body.append("geometry_completion_method", state.completion.method);
    body.append("geometry_completion_volume_mm3", String(state.completion.volume_mm3));
  }

  // The plan is sent whole: the backend derives the operations to cost from it,
  // taking only those with a committed time, and persists the rest of it as the
  // audit record of what was suggested versus what was chosen.
  const plan = collectManufacturingPlan();
  if (plan) body.append("manufacturing_plan", JSON.stringify(plan));

  // The recommended route and any decision on it, sent as they stand. The
  // backend recomputes whether the route can be priced and refuses it if not;
  // nothing about that is decided here.
  if (state.processClassification) {
    body.append("process_classification", JSON.stringify(state.processClassification));
  }
  // The proposed bar and any decision on it. The backend costs material from
  // it only if it was confirmed; nothing about that is decided here.
  if (state.rawStock) {
    body.append("raw_stock", JSON.stringify(state.rawStock));
  }
  // Masses typed for per-kg route stages, only those entered. The backend
  // decides whether the route can be priced with them.
  if (state.stageInputs && Object.keys(state.stageInputs).length) {
    body.append("stage_inputs", JSON.stringify(state.stageInputs));
  }

  // Blank additional-cost fields are OMITTED, never sent as 0. Sending a zero
  // would override the rate set's own overhead/scrap/tooling rather than
  // deferring to it.
  ["overhead_rate_percent", "scrap_rate_percent", "tooling_cost_per_batch"].forEach((field) => {
    const raw = $(field).value.trim();
    if (raw !== "") body.append(field, raw);
  });

  try {
    const previous = state.result ? state.result.estimate_id : null;
    state.result = await api("/cost/cad-part", { method: "POST", body });
    state.previousEstimateId = previous;
    renderResult(state.result);
    // Both panels read entirely from the server; nothing is computed here.
    if (typeof renderAssumptionsFor === "function") {
      await renderAssumptionsFor(state.result.estimate_id);
    }
    if (typeof renderCostDriversFor === "function") {
      await renderCostDriversFor(state.result.estimate_id);
    }
    if (typeof renderImpactAgainstPrevious === "function") {
      await renderImpactAgainstPrevious(state.result.estimate_id);
    }
  } catch (error) {
    $("result").classList.add("hidden");
    showError(error.message);
  } finally {
    $("calc-status").textContent = "";
    refreshCalculateState();
  }
});

function renderResult(response) {
  const cost = response.part_cost;
  const currency = cost.currency;
  const rateSet = cost.rate_set;

  $("r-total").textContent = money(cost.total_part_cost, currency);
  $("r-unit").textContent = money(cost.per_unit_cost, currency);

  $("r-material").textContent =
    money(cost.total_material_cost, currency) +
    (cost.material_charged === false ? " — included in the stage rate" : "");
  $("r-ops").textContent = money(cost.total_operation_cost, currency);
  // Non-machining route stages, as the server priced them. Hidden for an
  // estimate without any, so it reads exactly as before.
  const stages = cost.stage_costs || [];
  $("r-stages-row").classList.toggle("hidden", !stages.length);
  $("r-stage-detail-row").classList.toggle("hidden", !stages.length && !cost.material_note);
  $("r-stages").textContent = stages.length ? money(cost.total_stage_cost, currency) : "";
  $("r-stage-detail").innerHTML = [
    ...stages.map(
        (line) =>
          `${escapeHtml(line.label)}: ${money(line.total_cost, currency)}` +
          (line.unit_basis === "per_kg"
            ? ` (${money(line.unit_rate, currency)}/kg × ${number(line.mass_kg, 3)} kg × ${line.quantity}` +
              `${line.batch_charge ? ` + ${money(line.batch_charge, currency)} per batch` : ""})` +
              (line.mass_note ? ` — ${escapeHtml(line.mass_note)}` : "")
            : line.unit_basis === "per_piece"
              ? ` (${money(line.unit_rate, currency)} × ${line.quantity}` +
                `${line.batch_charge ? ` + ${money(line.batch_charge, currency)} per batch` : ""})`
              : ` (${money(line.batch_charge, currency)} per batch)`)
    ),
    ...(cost.material_note ? [escapeHtml(cost.material_note)] : []),
  ].join("<br>");
  $("r-direct").textContent = money(cost.direct_cost_base, currency);
  $("r-overhead").textContent = money(cost.additional_costs.overhead_cost, currency);
  $("r-scrap").textContent = money(cost.additional_costs.scrap_cost, currency);
  $("r-tooling").textContent = money(cost.additional_costs.tooling_cost, currency);
  $("r-total2").textContent = money(cost.total_part_cost, currency);

  $("r-qty").textContent = cost.material.quantity;
  $("r-currency").textContent = currency;
  $("r-rateset").textContent = rateSet
    ? `${rateSet.rate_set_id} v${rateSet.version} — ${rateSet.name}`
    : "none (rates supplied per request)";
  // The finished part's mass is always shown; when material was costed from
  // the bar it is cut from, that mass is named beside it, never in its place.
  $("r-mass").textContent =
    `${number(response.mass.mass_kg, 4)} kg per part` +
    (response.material_basis === "bar_stock" && response.stock_mass
      ? ` · material costed from the bar: ${number(response.stock_mass.mass_kg, 4)} kg per part`
      : "");
  $("r-id").textContent = cost.estimate_id || "—";

  // Mass needs a material and a unit, so it only exists once an estimate has
  // been produced. Mirror the backend's figure into the viewer's info panel.
  state.mass = response.mass.mass_kg;
  $("mi-mass").textContent = `${number(state.mass, 4)} kg per part`;

  // How the volume was arrived at travels with the result, so a user-supplied
  // figure is never mistaken for a measurement of the CAD.
  const geometry = response.geometry || {};
  const supplied =
    geometry.user_supplied_total_volume_mm3 ?? geometry.user_supplied_missing_volume_mm3;
  if (supplied !== null && supplied !== undefined) {
    $("r-geometry").innerHTML =
      `<strong>Volume assumed, not measured.</strong> ` +
      `${geometry.user_supplied_total_volume_mm3 !== null &&
        geometry.user_supplied_total_volume_mm3 !== undefined
        ? "Total volume"
        : "Missing volume"} of ${number(supplied, 3)} mm³ was supplied by you; ` +
      `the volume used was ${number(geometry.volume, 3)} mm³.`;
  } else {
    $("r-geometry").textContent =
      `${number(geometry.volume, 3)} mm³ measured from ` +
      `${geometry.geometry_source === "brep" ? "exact CAD surfaces" : "the polygon mesh"}` +
      `${geometry.repair_applied ? `, after restoring ${geometry.repaired_facet_count} missing facet(s)` : ""}.`;
  }

  $("r-provenance").textContent = rateSet
    ? `This estimate used rate set "${rateSet.rate_set_id}" version ${rateSet.version}, ` +
      `denominated in ${currency}. That version is immutable, so this estimate will not change ` +
      `if newer rates are published later.`
    : `This estimate was produced without a rate set.`;

  const warning = $("illustrative-warning");
  if (rateSet && rateSet.is_illustrative) {
    warning.textContent =
      "⚠ These figures come from ILLUSTRATIVE PLACEHOLDER RATES, not real pricing. " +
      "Do not quote them. Author a rate set with your own rates to get a usable number.";
    warning.classList.remove("hidden");
  } else {
    warning.classList.add("hidden");
  }

  $("result").classList.remove("hidden");
  $("result").scrollIntoView({ behavior: "smooth", block: "start" });

  if (typeof refreshFlowChrome === "function") refreshFlowChrome();
}

// ------------------------------------------------------------------ history

async function loadHistory() {
  $("history-status").textContent = "Loading…";
  let page;
  try {
    page = await api("/estimates?limit=50");
  } catch (error) {
    $("history-status").textContent = "";
    showError(error.message);
    return;
  }
  $("history-status").textContent = `${page.total} estimate(s)`;
  state.selectedForCompare.clear();
  updateCompareButton();

  $("history-body").innerHTML = page.items
    .map(
      (row) => `<tr class="row" data-id="${row.estimate_id}">
        <td><input type="checkbox" class="pick" data-id="${row.estimate_id}"></td>
        <td>${new Date(row.created_at + "Z").toLocaleString()}</td>
        <td>${escapeHtml(row.part_id)} — ${escapeHtml(row.part_name)}</td>
        <td>${row.quantity ?? ""}</td>
        <td>${row.rate_set_id ? `${escapeHtml(profileName(row.rate_set_id))} v${row.rate_set_version}` : "—"}</td>
        <td class="num">${money(row.total_part_cost, row.currency)}</td>
      </tr>`
    )
    .join("");

  $("history-body").querySelectorAll("input.pick").forEach((box) =>
    box.addEventListener("change", (event) => {
      event.stopPropagation();
      const id = box.dataset.id;
      if (box.checked) state.selectedForCompare.add(id);
      else state.selectedForCompare.delete(id);
      updateCompareButton();
    })
  );
  $("history-body").querySelectorAll("tr.row").forEach((row) =>
    row.addEventListener("click", (event) => {
      if (event.target.classList.contains("pick")) return;
      showEstimateDetail(row.dataset.id);
    })
  );
}

function updateCompareButton() {
  $("compare-btn").disabled = state.selectedForCompare.size !== 2;
}

async function showEstimateDetail(estimateId) {
  const panel = $("history-detail");
  panel.classList.remove("hidden");
  panel.innerHTML = "<p class='muted'>Loading…</p>";
  let estimate;
  try {
    estimate = await api(`/estimates/${encodeURIComponent(estimateId)}`);
  } catch (error) {
    panel.innerHTML = `<p class="muted">${error.message}</p>`;
    return;
  }
  const currency = estimate.currency;
  // The stored snapshot's shape depends on which endpoint made the estimate:
  // /cost/part stores the part cost itself, /cost/cad-part nests it under
  // "part_cost" alongside geometry and mass. Handle both.
  const snapshot = estimate.result_snapshot || {};
  const perUnit =
    snapshot.per_unit_cost ??
    (snapshot.part_cost ? snapshot.part_cost.per_unit_cost : null) ??
    null;
  panel.innerHTML = `
    <h2>Estimate detail</h2>
    <div class="headline">
      <div class="headline-item"><span class="label">Total cost</span>
        <span class="big">${money(estimate.total_part_cost, currency)}</span></div>
      <div class="headline-item"><span class="label">Per unit</span>
        <span class="big">${money(perUnit, currency)}</span></div>
    </div>
    <table class="mini">
      <tr><th>Part</th><td>${escapeHtml(estimate.part_id)} — ${escapeHtml(estimate.part_name)}</td></tr>
      <tr><th>Material</th><td>${escapeHtml(estimate.material)}</td></tr>
      <tr><th>Quantity</th><td>${estimate.quantity}</td></tr>
      <tr><th>Material cost</th><td>${money(estimate.total_material_cost, currency)}</td></tr>
      <tr><th>Operation cost</th><td>${money(estimate.total_operation_cost, currency)}</td></tr>
      <tr><th>Additional cost</th><td>${money(estimate.total_additional_cost, currency)}</td></tr>
      <tr><th>Costing profile</th><td>${
        estimate.rate_set_id
          ? `${escapeHtml(profileName(estimate.rate_set_id))} v${estimate.rate_set_version}`
          : "—"
      }</td></tr>
      <tr><th>Currency</th><td>${currency}</td></tr>
      <tr><th>Created</th><td>${new Date(estimate.created_at + "Z").toLocaleString()}</td></tr>
      <tr><th>Estimate ID</th><td class="mono">${estimate.estimate_id}</td></tr>
    </table>
    <div class="row">
      <a class="button" id="open-quotation" target="_blank"
         href="quotation.html?estimate=${encodeURIComponent(estimate.estimate_id)}">
        Generate quotation
      </a>
      <a class="button secondary" target="_blank"
         href="quotation.html?estimate=${encodeURIComponent(
           estimate.estimate_id
         )}&view=engineering">
        Engineering detail
      </a>
    </div>
    <p class="provenance">Read back from the stored snapshot taken when this estimate was
    created, so it reflects the rates in force at that moment. The quotation is rendered from
    that same snapshot and recalculates nothing, so it keeps saying what this estimate said
    however the rates change later.</p>
    <details class="detail-toggle" id="history-drivers-toggle">
      <summary>Cost drivers</summary>
      <div id="history-drivers"></div>
    </details>
    <div id="history-actuals"></div>`;
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  // Guarded forward: actuals.js is loaded after this file.
  if (typeof renderActualsPanel === "function") renderActualsPanel(estimate);
  // Guarded forward: drivers.js is loaded after this file.
  if (typeof renderCostDriversInto === "function") {
    renderCostDriversInto("history-drivers", estimate.estimate_id);
  }
}

$("refresh-history").addEventListener("click", loadHistory);

$("compare-btn").addEventListener("click", async () => {
  const [baseline, comparison] = [...state.selectedForCompare];
  const panel = $("compare-result");
  panel.classList.remove("hidden");
  panel.innerHTML = "<p class='muted'>Comparing…</p>";
  let result;
  try {
    result = await api(
      `/estimates/compare?baseline=${encodeURIComponent(baseline)}&comparison=${encodeURIComponent(
        comparison
      )}`
    );
  } catch (error) {
    panel.innerHTML = `<p class="muted">${error.message}</p>`;
    return;
  }

  const currency = result.baseline.currency;
  const line = (label, delta) => `
    <tr><th>${label}</th>
      <td>${money(delta.baseline, result.baseline.currency)}</td>
      <td>${money(delta.comparison, result.comparison.currency)}</td>
      <td class="${delta.delta >= 0 ? "up" : "down"}">${delta.delta >= 0 ? "+" : ""}${money(
    delta.delta,
    currency
  )}</td>
      <td>${delta.percent_change === null ? "—" : `${number(delta.percent_change)}%`}</td></tr>`;

  // Side by side first, then the account of why they differ. Both come from
  // the backend; this only lays them out.
  panel.innerHTML = `
    <h2>Comparison</h2>
    <div class="compare-ids">
      <span>A <span class="mono">${escapeHtml(result.baseline.estimate_id)}</span></span>
      <span>B <span class="mono">${escapeHtml(result.comparison.estimate_id)}</span></span>
    </div>
    <h3>Overview</h3>
    <table class="breakdown">
      <thead><tr><th></th><th>A</th><th>B</th><th>Change</th><th>%</th></tr></thead>
      <tbody>
        ${line("Material", result.material_cost)}
        ${line("Operations", result.operation_cost)}
        ${line("Additional", result.additional_cost)}
        ${line("Total", result.total_part_cost)}
      </tbody>
    </table>
    ${
      typeof sideBySideAssumptions === "function" && result.detailed
        ? sideBySideAssumptions(result)
        : ""
    }
    ${typeof impactMarkup === "function" && result.detailed ? impactMarkup(result, {}) : ""}
    <p class="provenance">Older: ${escapeHtml(result.older_estimate_id)}<br>Newer: ${escapeHtml(
    result.newer_estimate_id
  )}<br>${escapeHtml(result.basis)}</p>`;
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
});

// ------------------------------------------------------------------ start

// The format panel has to exist before a file can be judged against it, and
// the unit control's initial state depends on it too.
loadFormats().then(() => applyUnitRulesFor(null));
loadRateSets();
refreshCalculateState();
