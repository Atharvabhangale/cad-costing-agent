"use strict";

// Costing Setup: define the rates an estimate is calculated from, without
// touching the API by hand.
//
// This is a classic script loaded after app.js, so it shares `$`, `api`,
// `money`, `number`, `escapeHtml`, `compareVersions`, `showTab`, `state` and
// `loadRateSets` with it rather than redefining them.
//
// It contains no costing arithmetic. It collects rates, asks the backend
// whether they would publish, and posts them to the existing authoring
// endpoints. Every published version is immutable — there is deliberately no
// edit or delete path anywhere in this file.

const WIZARD_STEPS = ["general", "materials", "operations", "additional", "review"];

// A draft lives only in this object until it is published. There is no
// server-side draft state, so an abandoned edit leaves nothing behind.
const setup = {
  mode: "create", // "create" a new profile, or add a "version" to an existing one
  step: "general",
  profiles: new Map(), // rate_set_id -> summary rows, oldest version first
  draft: null,
  validation: null,
};

// ------------------------------------------------------------------ helpers

function currencySymbol(code) {
  return code === "INR" ? "₹" : code;
}

function blankDraft() {
  return {
    rate_set_id: "",
    name: "",
    version: "1.0.0",
    description: "",
    currency: "INR",
    is_illustrative: false,
    materials: [],
    operations: [],
    additional_costs: {
      overhead_rate_percent: 0,
      scrap_rate_percent: 0,
      tooling_cost_per_batch: 0,
    },
  };
}

// "Pune Machine Shop" -> "pune-machine-shop", matching the id pattern the
// backend enforces so the user never has to think about slugs.
function slugify(text) {
  return String(text)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function bumpMinor(version) {
  const [major, minor] = version.split(".").map(Number);
  return `${major}.${minor + 1}.0`;
}

function latestVersionOf(rateSetId) {
  const versions = setup.profiles.get(rateSetId) || [];
  return versions[versions.length - 1];
}

// ------------------------------------------------------------------ listing

async function loadProfiles() {
  $("setup-status").textContent = "Loading…";
  let rows;
  try {
    rows = await api("/rates");
  } catch (error) {
    $("setup-status").textContent = "";
    $("profile-cards").innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
    return;
  }

  setup.profiles = new Map();
  rows.forEach((row) => {
    if (!setup.profiles.has(row.rate_set_id)) setup.profiles.set(row.rate_set_id, []);
    setup.profiles.get(row.rate_set_id).push(row);
  });
  setup.profiles.forEach((versions) =>
    versions.sort((a, b) => compareVersions(a.version, b.version))
  );

  $("setup-status").textContent = `${setup.profiles.size} profile(s), ${rows.length} published version(s).`;
  renderProfileCards();
}

function renderProfileCards() {
  const cards = [...setup.profiles.entries()].map(([rateSetId, versions]) => {
    const latest = versions[versions.length - 1];
    // Only the newest version is what "Latest" resolves to; the rest stay
    // readable because historical estimates still point at them.
    const history = versions
      .slice()
      .reverse()
      .map(
        (row, index) => `<li>
          <span class="ver">v${row.version}</span>
          <span class="ver-tag ${index === 0 ? "current" : "superseded"}">${
            index === 0 ? "Current" : "Superseded"
          }</span>
          <span class="muted">${row.material_count} materials · ${row.process_count} processes</span>
        </li>`
      )
      .join("");

    return `<article class="profile-card">
      <header>
        <h3>${escapeHtml(latest.name)}</h3>
        <div class="badges">
          <span class="badge currency">${escapeHtml(latest.currency)}</span>
          <span class="badge ${latest.is_illustrative ? "illustrative" : "real"}">${
            latest.is_illustrative ? "Illustrative rates" : "Real rates"
          }</span>
        </div>
      </header>
      <p class="profile-figures">
        <strong>v${latest.version}</strong> ·
        ${latest.material_count} material${latest.material_count === 1 ? "" : "s"} ·
        ${latest.process_count} process${latest.process_count === 1 ? "" : "es"}
      </p>
      <p class="hint">${escapeHtml(latest.description || "No description.")}</p>
      <ul class="version-list">${history}</ul>
      <div class="card-actions">
        <button type="button" data-use="${escapeHtml(rateSetId)}">Use for estimate</button>
        <button type="button" class="secondary" data-newversion="${escapeHtml(rateSetId)}">Create new version</button>
      </div>
    </article>`;
  });

  $("profile-cards").innerHTML =
    cards.join("") || `<p class="muted">No costing profiles yet. Create one to get started.</p>`;

  $("profile-cards")
    .querySelectorAll("[data-use]")
    .forEach((button) =>
      button.addEventListener("click", () => useProfileForEstimate(button.dataset.use))
    );
  $("profile-cards")
    .querySelectorAll("[data-newversion]")
    .forEach((button) =>
      button.addEventListener("click", () => startNewVersion(button.dataset.newversion))
    );
}

// Jump to the estimate page with this profile selected and its newest version
// pinned, so what was just reviewed is exactly what gets costed.
async function useProfileForEstimate(rateSetId) {
  showTab("estimate");
  await loadRateSets();
  $("rate_set_id").value = rateSetId;
  await onFamilyChange();
  $("rate_set_id").scrollIntoView({ behavior: "smooth", block: "center" });
}

// ------------------------------------------------------------------ opening the wizard

$("new-profile").addEventListener("click", () => {
  setup.mode = "create";
  setup.draft = blankDraft();
  $("editor-title").textContent = "New costing profile";
  $("editor-subtitle").textContent =
    "Nothing is saved until you publish. Version 1.0.0 will be the first published version.";
  $("p-id").disabled = false;
  delete $("p-id").dataset.touched; // a fresh profile derives its id from the name again
  openEditor();
});

// "Create new version" copies the previous configuration so only what changed
// has to be edited. The profile's identity is fixed at this point.
async function startNewVersion(rateSetId) {
  const latest = latestVersionOf(rateSetId);
  let previous;
  try {
    previous = await api(`/rates/${encodeURIComponent(rateSetId)}?version=${encodeURIComponent(latest.version)}`);
  } catch (error) {
    showError(error.message);
    return;
  }

  setup.mode = "version";
  setup.draft = {
    rate_set_id: previous.rate_set_id,
    name: previous.name,
    version: bumpMinor(previous.version),
    description: previous.description || "",
    currency: previous.currency,
    is_illustrative: previous.is_illustrative,
    // Flattened to arrays so rows can be edited, reordered and removed, and so
    // two rows sharing a name are visible rather than silently collapsing.
    materials: Object.values(previous.materials || {}).map((entry) => ({
      name: entry.name,
      density_kg_m3: entry.density_kg_m3,
      cost_per_kg: entry.cost_per_kg,
    })),
    operations: Object.entries(previous.operations || {}).map(([process, rate]) => ({
      process_type: process,
      machine_rate_per_hour: rate.machine_rate_per_hour,
      labor_rate_per_hour: rate.labor_rate_per_hour,
    })),
    additional_costs: { ...previous.additional_costs },
  };

  $("editor-title").textContent = `New version of ${previous.name}`;
  $("editor-subtitle").textContent =
    `Copied from v${previous.version}. Change what you need and publish — v${previous.version} ` +
    `stays exactly as it is, so estimates already made with it never change.`;
  $("p-id").disabled = true;
  openEditor();
}

function openEditor() {
  $("setup-list").classList.add("hidden");
  $("setup-editor").classList.remove("hidden");
  setup.step = "general";
  writeDraftToForm();
  renderMaterials();
  renderOperations();
  showStep("general");
}

$("cancel-edit").addEventListener("click", () => {
  setup.draft = null;
  $("setup-editor").classList.add("hidden");
  $("setup-list").classList.remove("hidden");
  clearWizardErrors();
  loadProfiles();
});

// ------------------------------------------------------------------ steps

function showStep(step) {
  setup.step = step;
  WIZARD_STEPS.forEach((name) => {
    $(`wpane-${name}`).classList.toggle("hidden", name !== step);
  });
  document
    .querySelectorAll(".wstep")
    .forEach((button) => button.classList.toggle("active", button.dataset.step === step));

  const index = WIZARD_STEPS.indexOf(step);
  $("wizard-back").disabled = index === 0;
  $("wizard-next").classList.toggle("hidden", step === "review");
  $("publish-version").classList.toggle("hidden", step !== "review");
  if (step === "review") renderReview();
}

document.querySelectorAll(".wstep").forEach((button) =>
  button.addEventListener("click", () => {
    readFormIntoDraft();
    showStep(button.dataset.step);
  })
);

$("wizard-next").addEventListener("click", () => {
  readFormIntoDraft();
  const problems = validateStep(setup.step);
  if (problems.length) {
    showWizardErrors(problems);
    return;
  }
  clearWizardErrors();
  showStep(WIZARD_STEPS[WIZARD_STEPS.indexOf(setup.step) + 1]);
});

$("wizard-back").addEventListener("click", () => {
  readFormIntoDraft();
  clearWizardErrors();
  showStep(WIZARD_STEPS[WIZARD_STEPS.indexOf(setup.step) - 1]);
});

// ------------------------------------------------------------------ general pane

function writeDraftToForm() {
  const draft = setup.draft;
  $("p-name").value = draft.name;
  $("p-id").value = draft.rate_set_id;
  $("p-version").value = draft.version;
  $("p-description").value = draft.description;
  $("p-currency").value = draft.currency;
  $("p-illustrative").value = String(draft.is_illustrative);
  $("p-overhead").value = draft.additional_costs.overhead_rate_percent;
  $("p-scrap").value = draft.additional_costs.scrap_rate_percent;
  $("p-tooling").value = draft.additional_costs.tooling_cost_per_batch;
  updateCurrencySymbols();
}

function readFormIntoDraft() {
  const draft = setup.draft;
  if (!draft) return;
  draft.name = $("p-name").value.trim();
  if (!$("p-id").disabled) draft.rate_set_id = $("p-id").value.trim();
  draft.version = $("p-version").value.trim();
  draft.description = $("p-description").value.trim();
  draft.currency = $("p-currency").value;
  draft.is_illustrative = $("p-illustrative").value === "true";
  draft.additional_costs = {
    overhead_rate_percent: numberOrNaN($("p-overhead").value),
    scrap_rate_percent: numberOrNaN($("p-scrap").value),
    tooling_cost_per_batch: numberOrNaN($("p-tooling").value),
  };
  readMaterialRows();
  readOperationRows();
}

// Blank must not silently become 0 — it is a missing answer, not a zero rate.
function numberOrNaN(raw) {
  const text = String(raw).trim();
  return text === "" ? NaN : Number(text);
}

// Derive the id from the name until the user overrides it, so a non-developer
// never has to invent a slug.
$("p-name").addEventListener("input", () => {
  if (setup.mode !== "create" || $("p-id").dataset.touched === "yes") return;
  $("p-id").value = slugify($("p-name").value);
});
$("p-id").addEventListener("input", () => {
  $("p-id").dataset.touched = "yes";
});
$("p-currency").addEventListener("change", () => {
  setup.draft.currency = $("p-currency").value;
  updateCurrencySymbols();
});

function updateCurrencySymbols() {
  const symbol = currencySymbol($("p-currency").value);
  document.querySelectorAll("#setup-editor .cur-symbol").forEach((element) => {
    element.textContent = symbol;
  });
}

// ------------------------------------------------------------------ materials

function renderMaterials() {
  $("materials-body").innerHTML = setup.draft.materials
    .map(
      (material, index) => `<tr>
        <td><input class="m-name" value="${escapeHtml(material.name)}" placeholder="e.g. EN8 steel"></td>
        <td><input class="m-density" type="number" step="any" value="${material.density_kg_m3 ?? ""}"></td>
        <td><input class="m-cost" type="number" step="any" value="${material.cost_per_kg ?? ""}"></td>
        <td><button type="button" class="secondary tiny" data-remove-material="${index}">Remove</button></td>
      </tr>`
    )
    .join("");
  bindRemoval("materials-body", (index) => {
    setup.draft.materials.splice(index, 1);
    renderMaterials();
  });
}

function readMaterialRows() {
  const rows = [...$("materials-body").querySelectorAll("tr")];
  setup.draft.materials = rows.map((row) => ({
    name: row.querySelector(".m-name").value.trim(),
    density_kg_m3: numberOrNaN(row.querySelector(".m-density").value),
    cost_per_kg: numberOrNaN(row.querySelector(".m-cost").value),
  }));
}

$("add-material").addEventListener("click", () => {
  readMaterialRows();
  setup.draft.materials.push({ name: "", density_kg_m3: "", cost_per_kg: "" });
  renderMaterials();
});

// ------------------------------------------------------------------ operations

function renderOperations() {
  $("operations-body").innerHTML = setup.draft.operations
    .map(
      (operation, index) => `<tr>
        <td><input class="o-process" list="process-suggestions" value="${escapeHtml(operation.process_type)}" placeholder="e.g. Milling"></td>
        <td><input class="o-machine" type="number" step="any" value="${operation.machine_rate_per_hour ?? ""}"></td>
        <td><input class="o-labor" type="number" step="any" value="${operation.labor_rate_per_hour ?? ""}"></td>
        <td><button type="button" class="secondary tiny" data-remove-operation="${index}">Remove</button></td>
      </tr>`
    )
    .join("");
  bindRemoval("operations-body", (index) => {
    setup.draft.operations.splice(index, 1);
    renderOperations();
  });
}

function readOperationRows() {
  const rows = [...$("operations-body").querySelectorAll("tr")];
  setup.draft.operations = rows.map((row) => ({
    process_type: row.querySelector(".o-process").value.trim(),
    machine_rate_per_hour: numberOrNaN(row.querySelector(".o-machine").value),
    labor_rate_per_hour: numberOrNaN(row.querySelector(".o-labor").value),
  }));
}

$("add-process").addEventListener("click", () => {
  readOperationRows();
  setup.draft.operations.push({ process_type: "", machine_rate_per_hour: "", labor_rate_per_hour: "" });
  renderOperations();
});

// Reading the rows before splicing keeps edits the user has made to *other*
// rows from being lost when one row is removed.
function bindRemoval(bodyId, remove) {
  $(bodyId)
    .querySelectorAll("button[data-remove-material], button[data-remove-operation]")
    .forEach((button) =>
      button.addEventListener("click", () => {
        if (bodyId === "materials-body") readMaterialRows();
        else readOperationRows();
        remove(Number(button.dataset.removeMaterial ?? button.dataset.removeOperation));
      })
    );
}

// ------------------------------------------------------------------ validation

const VERSION_PATTERN = /^\d+\.\d+\.\d+$/;
const ID_PATTERN = /^[a-z0-9]+(-[a-z0-9]+)*$/;

// Mirrors the backend's field rules so problems are caught next to the field
// that caused them. The backend re-checks everything and remains the authority:
// nothing here decides whether a version may be published.
function validateStep(step) {
  const draft = setup.draft;
  const problems = [];

  if (step === "general") {
    if (!draft.name) problems.push("Profile name is required.");
    if (!draft.rate_set_id) problems.push("Profile ID is required.");
    else if (!ID_PATTERN.test(draft.rate_set_id))
      problems.push(
        `Profile ID "${draft.rate_set_id}" must be lowercase letters, digits and single dashes, e.g. "pune-machine-shop".`
      );
    if (!VERSION_PATTERN.test(draft.version))
      problems.push(`Version "${draft.version}" must look like 1.0.0 (MAJOR.MINOR.PATCH).`);
    else if (setup.mode === "version") {
      const latest = latestVersionOf(draft.rate_set_id);
      if (latest && compareVersions(draft.version, latest.version) <= 0) {
        problems.push(
          `Version ${draft.version} is not newer than the current v${latest.version}. ` +
            "Published versions can never be changed, so a new version must be higher."
        );
      }
    } else if (setup.profiles.has(draft.rate_set_id)) {
      problems.push(
        `A profile with ID "${draft.rate_set_id}" already exists. Use "Create new version" on ` +
          "that profile, or choose a different ID."
      );
    }
  }

  if (step === "materials") {
    if (!draft.materials.length) problems.push("Add at least one material.");
    const seen = new Map();
    draft.materials.forEach((material, index) => {
      const row = `Material row ${index + 1}`;
      if (!material.name) problems.push(`${row}: name is required.`);
      else {
        const key = material.name.toLowerCase();
        if (seen.has(key))
          problems.push(
            `${row}: "${material.name}" duplicates row ${seen.get(key) + 1}. Each material must appear once.`
          );
        else seen.set(key, index);
      }
      if (!(material.density_kg_m3 > 0))
        problems.push(`${row}: density must be greater than zero (kg/m³).`);
      if (!(material.cost_per_kg > 0))
        problems.push(`${row}: cost per kg must be greater than zero.`);
    });
  }

  if (step === "operations") {
    const seen = new Map();
    draft.operations.forEach((operation, index) => {
      const row = `Process row ${index + 1}`;
      if (!operation.process_type) problems.push(`${row}: process name is required.`);
      else {
        const key = operation.process_type.toLowerCase();
        if (seen.has(key))
          problems.push(
            `${row}: "${operation.process_type}" duplicates row ${seen.get(key) + 1}. Each process must appear once.`
          );
        else seen.set(key, index);
      }
      if (!(operation.machine_rate_per_hour > 0))
        problems.push(`${row}: machine rate per hour must be greater than zero.`);
      if (!(operation.labor_rate_per_hour > 0))
        problems.push(`${row}: labour rate per hour must be greater than zero.`);
    });
  }

  if (step === "additional") {
    const { overhead_rate_percent: overhead, scrap_rate_percent: scrap, tooling_cost_per_batch: tooling } =
      draft.additional_costs;
    if (!(overhead >= 0 && overhead <= 1000))
      problems.push("Overhead % must be between 0 and 1000. Enter a percentage (15), not a fraction (0.15).");
    if (!(scrap >= 0 && scrap < 100))
      problems.push("Scrap % must be at least 0 and below 100. At 100% no good parts are produced.");
    if (!(tooling >= 0)) problems.push("Tooling per batch cannot be negative.");
  }

  return problems;
}

function showWizardErrors(problems) {
  $("wizard-errors").innerHTML = problems.map((line) => `• ${escapeHtml(line)}`).join("<br>");
  $("wizard-errors").classList.remove("hidden");
  $("wizard-errors").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function clearWizardErrors() {
  $("wizard-errors").classList.add("hidden");
  $("wizard-errors").innerHTML = "";
}

// ------------------------------------------------------------------ review

// The wire format the backend expects: materials keyed by lowercase name,
// operations keyed by lowercase process type.
function draftToRateSet() {
  const draft = setup.draft;
  const materials = {};
  draft.materials.forEach((material) => {
    materials[material.name.trim().toLowerCase()] = {
      name: material.name.trim(),
      density_kg_m3: material.density_kg_m3,
      cost_per_kg: material.cost_per_kg,
    };
  });
  const operations = {};
  draft.operations.forEach((operation) => {
    operations[operation.process_type.trim().toLowerCase()] = {
      machine_rate_per_hour: operation.machine_rate_per_hour,
      labor_rate_per_hour: operation.labor_rate_per_hour,
    };
  });
  return {
    rate_set_id: draft.rate_set_id,
    name: draft.name,
    version: draft.version,
    description: draft.description,
    currency: draft.currency,
    is_illustrative: draft.is_illustrative,
    materials,
    operations,
    additional_costs: draft.additional_costs,
  };
}

async function renderReview() {
  const draft = setup.draft;
  // Stays disabled until something says otherwise, so it can never be clicked
  // during the round trip to the validator.
  $("publish-version").disabled = true;
  // Use the app's own money formatting so a rate reads the same here as it
  // does on an estimate — grouped digits, two decimals, never converted.
  const money2 = (value) => money(Number(value), draft.currency);

  // Every step's rules, so the review reflects the whole profile rather than
  // only the pane that happens to be open.
  const problems = WIZARD_STEPS.flatMap((step) => (step === "review" ? [] : validateStep(step)));

  $("review-body").innerHTML = `
    <div class="review-summary">
      <div><span class="label">Profile</span><strong>${escapeHtml(draft.name || "—")}</strong></div>
      <div><span class="label">Version</span><strong>v${escapeHtml(draft.version)}</strong></div>
      <div><span class="label">Currency</span><strong>${escapeHtml(draft.currency)}</strong></div>
      <div><span class="label">Rate status</span><strong>${
        draft.is_illustrative ? "Illustrative" : "Real rates"
      }</strong></div>
      <div><span class="label">Materials</span><strong>${draft.materials.length}</strong></div>
      <div><span class="label">Processes</span><strong>${draft.operations.length}</strong></div>
    </div>

    <table class="editor-table review-table">
      <thead><tr><th>Material</th><th>Density (kg/m³)</th><th>Cost per kg</th></tr></thead>
      <tbody>${
        draft.materials
          .map(
            (m) => `<tr><td>${escapeHtml(m.name)}</td><td>${number(m.density_kg_m3, 1)}</td>
                    <td>${money2(m.cost_per_kg)}</td></tr>`
          )
          .join("") || `<tr><td colspan="3" class="muted">No materials.</td></tr>`
      }</tbody>
    </table>

    <table class="editor-table review-table">
      <thead><tr><th>Process</th><th>Machine / hour</th><th>Labour / hour</th></tr></thead>
      <tbody>${
        draft.operations
          .map(
            (o) => `<tr><td>${escapeHtml(o.process_type)}</td>
                    <td>${money2(o.machine_rate_per_hour)}</td>
                    <td>${money2(o.labor_rate_per_hour)}</td></tr>`
          )
          .join("") || `<tr><td colspan="3" class="muted">No processes.</td></tr>`
      }</tbody>
    </table>

    <table class="editor-table review-table">
      <thead><tr><th>Overhead</th><th>Scrap allowance</th><th>Tooling per batch</th></tr></thead>
      <tbody><tr>
        <td>${number(draft.additional_costs.overhead_rate_percent, 2)}%</td>
        <td>${number(draft.additional_costs.scrap_rate_percent, 2)}%</td>
        <td>${money2(draft.additional_costs.tooling_cost_per_batch)}</td>
      </tr></tbody>
    </table>

    <div id="review-verdict" class="muted">Checking with the server…</div>`;

  if (problems.length) {
    setup.validation = null;
    $("publish-version").disabled = true;
    $("review-verdict").className = "warning";
    $("review-verdict").innerHTML =
      `<strong>Not ready to publish.</strong><br>` +
      problems.map((line) => `• ${escapeHtml(line)}`).join("<br>");
    return;
  }

  // The backend decides whether this would publish; the browser only asks.
  try {
    setup.validation = await api("/rates/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(draftToRateSet()),
    });
  } catch (error) {
    setup.validation = null;
    $("publish-version").disabled = true;
    $("review-verdict").className = "warning";
    $("review-verdict").innerHTML = `<strong>The server rejected this configuration.</strong><br>${escapeHtml(
      error.message
    )}`;
    return;
  }

  const notes = setup.validation.messages.map((line) => `• ${escapeHtml(line)}`).join("<br>");
  $("publish-version").disabled = !setup.validation.valid;
  $("review-verdict").className = setup.validation.valid ? "verdict-ok" : "warning";
  $("review-verdict").innerHTML = setup.validation.valid
    ? `<strong>Ready to publish.</strong> ${
        setup.validation.is_new_profile
          ? "This will create a new profile."
          : "This will be added as a new version; earlier versions are unchanged."
      }${notes ? `<br>${notes}` : ""}`
    : `<strong>Cannot publish.</strong><br>${notes}`;
}

// ------------------------------------------------------------------ publish

$("publish-version").addEventListener("click", async () => {
  readFormIntoDraft();
  $("publish-version").disabled = true;
  $("publish-status").textContent = "Publishing…";
  clearWizardErrors();

  const payload = draftToRateSet();
  try {
    if (setup.mode === "version") {
      // The id comes from the path for an existing family.
      const { rate_set_id: rateSetId, ...body } = payload;
      await api(`/rates/${encodeURIComponent(rateSetId)}/versions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } else {
      await api("/rates", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    }
  } catch (error) {
    $("publish-status").textContent = "";
    $("publish-version").disabled = false;
    showWizardErrors([error.message]);
    return;
  }

  const published = { id: payload.rate_set_id, version: payload.version, name: payload.name };
  setup.draft = null;
  $("publish-status").textContent = "";
  $("setup-editor").classList.add("hidden");
  $("setup-list").classList.remove("hidden");
  await loadProfiles();
  // Make it immediately selectable on the estimate page.
  await loadRateSets();
  $("setup-status").textContent =
    `Published ${published.name} v${published.version}. It is now available on the estimate page.`;
});

// ------------------------------------------------------------------ start

setup.draft = blankDraft();
