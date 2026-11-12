"use strict";

// Matrix preview: the loaded part under several rate-set versions at several
// quantities. A preview of each cell, never an estimate and never a choice.
//
// A classic script loaded after routecompare.js, sharing `$`, `api`, `state`,
// `money`, `escapeHtml`, `clearError` and `showError` with the files before it,
// and calling app.js's `collectManufacturingPlan`.
//
// What this file does not do, on purpose:
// - It keeps no list of rate sets. The picker is drawn from `state.rateSets`,
//   the list app.js loaded from /rates, in the order that list is in.
// - It adds nothing up, converts nothing and ranks nothing. Every figure is the
//   server's, rows and columns are shown in the order the server returns them,
//   and no cell is marked. A cell that could not be priced shows the server's
//   reason, never a zero.
// - It accepts no time. The plan is sent as it stands, exactly as the cost
//   request sends it.
//
// The request carries the same inputs as the cost request, with a list of rate
// sets and a list of quantities in place of one of each. Nothing is stored by it.

state.matrixPreview = null;
state.matrixPicked = [];

function matrixKey(rateSet) {
  return `${rateSet.rate_set_id}@${rateSet.version}`;
}

function clearMatrixPreview() {
  state.matrixPreview = null;
  const host = $("matrix-result");
  if (host) host.innerHTML = "";
}

// One checkbox per stored rate-set version. Redrawn whenever the panel is
// opened, so a version published since is offered; ticks are kept.
function renderMatrixPicker() {
  const host = $("matrix-rate-sets");
  const rateSets = state.rateSets || [];
  if (!rateSets.length) {
    host.innerHTML = `<p class="hint">No rate sets are loaded.</p>`;
    return;
  }
  host.innerHTML =
    `<p class="hint">Rate-set versions to price under:</p>` +
    rateSets
      .map((rateSet) => {
        const key = matrixKey(rateSet);
        return (
          `<label class="matrix-pick"><input type="checkbox" class="matrix-pick-box" ` +
          `data-rate-set="${escapeHtml(rateSet.rate_set_id)}" data-version="${escapeHtml(rateSet.version)}"` +
          `${state.matrixPicked.includes(key) ? " checked" : ""}> ` +
          `<span>${escapeHtml(rateSet.name)} <span class="mono">${escapeHtml(rateSet.rate_set_id)} ` +
          `v${escapeHtml(rateSet.version)}</span></span> ` +
          `<span class="badge currency">${escapeHtml(rateSet.currency)}</span>` +
          (rateSet.is_illustrative ? ` <span class="badge illustrative">Illustrative</span>` : "") +
          `</label>`
        );
      })
      .join("");
  host.querySelectorAll("input.matrix-pick-box").forEach((box) =>
    box.addEventListener("change", () => {
      state.matrixPicked = matrixPickedBoxes().map((picked) =>
        matrixKey({ rate_set_id: picked.dataset.rateSet, version: picked.dataset.version })
      );
    })
  );
}

function matrixPickedBoxes() {
  return [...$("matrix-rate-sets").querySelectorAll("input.matrix-pick-box")].filter(
    (box) => box.checked
  );
}

// The quantities as typed. A token that is not a number is sent as it parses
// and refused by the server in its own words; nothing is corrected here.
function matrixQuantities() {
  return $("matrix-quantities")
    .value.split(/[\s,;]+/)
    .filter((token) => token !== "")
    .map(Number);
}

function matrixBody() {
  const body = new FormData();
  body.append("file", state.file);
  body.append(
    "rate_sets",
    JSON.stringify(
      matrixPickedBoxes().map((box) => ({
        rate_set_id: box.dataset.rateSet,
        version: box.dataset.version,
      }))
    )
  );
  body.append("quantities", JSON.stringify(matrixQuantities()));
  body.append("material", $("material").value);
  body.append("source_unit", $("source_unit").value);
  body.append("part_id", $("part_id").value.trim());
  body.append("part_name", $("part_name").value.trim());
  if (state.completion) {
    body.append("geometry_completion_method", state.completion.method);
    body.append("geometry_completion_volume_mm3", String(state.completion.volume_mm3));
  }
  const plan = collectManufacturingPlan();
  if (plan) body.append("manufacturing_plan", JSON.stringify(plan));
  if (state.processClassification) {
    body.append("process_classification", JSON.stringify(state.processClassification));
  }
  if (state.rawStock) body.append("raw_stock", JSON.stringify(state.rawStock));
  if (state.stageInputs && Object.keys(state.stageInputs).length) {
    body.append("stage_inputs", JSON.stringify(state.stageInputs));
  }
  // Blank additional-cost fields are omitted, never sent as 0.
  ["overhead_rate_percent", "scrap_rate_percent", "tooling_cost_per_batch"].forEach((field) => {
    const raw = $(field).value.trim();
    if (raw !== "") body.append(field, raw);
  });
  return body;
}

function matrixRowHeading(rateSet) {
  if (!rateSet.resolved) {
    return (
      `<strong>${escapeHtml(rateSet.requested_rate_set_id)}</strong>` +
      (rateSet.requested_version ? ` <span class="mono">v${escapeHtml(rateSet.requested_version)}</span>` : "") +
      `<div class="hint">${escapeHtml(rateSet.reason || "Not found.")}</div>`
    );
  }
  return (
    `<strong>${escapeHtml(rateSet.name)}</strong>` +
    `<div class="mono">${escapeHtml(rateSet.rate_set_id)} v${escapeHtml(rateSet.version)}` +
    `${rateSet.requested_version ? "" : " (latest)"}</div>` +
    `<span class="badge currency">${escapeHtml(rateSet.currency)}</span>` +
    (rateSet.is_illustrative ? ` <span class="badge illustrative">Illustrative rates</span>` : "")
  );
}

function matrixSplitLine(label, cell, value) {
  return `<tr><th>${escapeHtml(label)}</th><td>${escapeHtml(money(value, cell.currency))}</td></tr>`;
}

function matrixCellMarkup(cell) {
  if (cell.status !== "priced") {
    const reason = cell.refusal ? cell.refusal.message : "Could not be priced.";
    return (
      `<td class="matrix-cell matrix-refused"><span class="pill pill-off">not priced</span>` +
      `<div class="hint">${escapeHtml(reason)}</div></td>`
    );
  }
  return (
    `<td class="matrix-cell">` +
    `<div class="matrix-unit">${escapeHtml(money(cell.per_unit_cost, cell.currency))} <span class="muted">per unit</span></div>` +
    `<div class="matrix-total">${escapeHtml(money(cell.total_part_cost, cell.currency))} <span class="muted">total</span></div>` +
    `<details class="matrix-split"><summary>Cost split</summary><table class="mini">` +
    matrixSplitLine(cell.material_charged ? "Material" : "Material (in a route stage)", cell, cell.total_material_cost) +
    matrixSplitLine("Operations", cell, cell.total_operation_cost) +
    matrixSplitLine("Route stages", cell, cell.total_stage_cost) +
    matrixSplitLine("Direct cost", cell, cell.direct_cost_base) +
    matrixSplitLine("Overhead", cell, cell.overhead_cost) +
    matrixSplitLine("Scrap allowance", cell, cell.scrap_cost) +
    matrixSplitLine("Tooling", cell, cell.tooling_cost) +
    matrixSplitLine("Total", cell, cell.total_part_cost) +
    `</table></details></td>`
  );
}

// A priced cell whose plan has operations without a committed time, if any: the
// server's count, shown where the figures are.
function matrixUntimedCell(preview) {
  return preview.rows
    .flatMap((row) => row.cells)
    .find((cell) => cell.status === "priced" && cell.operations_not_timed);
}

function renderMatrixPreview() {
  const host = $("matrix-result");
  const preview = state.matrixPreview;
  if (!preview) {
    host.innerHTML = "";
    return;
  }
  const head = preview.quantities
    .map((quantity) => `<th>Quantity ${escapeHtml(number(quantity, 0))}</th>`)
    .join("");
  const rows = preview.rows
    .map(
      (row) =>
        `<tr><th class="matrix-rate-set">${matrixRowHeading(row.rate_set)}</th>` +
        row.cells.map(matrixCellMarkup).join("") +
        `</tr>`
    )
    .join("");
  const untimed = matrixUntimedCell(preview);
  host.innerHTML =
    `<p class="warning-inline">${escapeHtml(preview.note)}</p>` +
    (preview.currency_note ? `<p class="warning">${escapeHtml(preview.currency_note)}</p>` : "") +
    (untimed
      ? `<p class="warning">${escapeHtml(String(untimed.operations_not_timed))} operation(s) in the plan have no ` +
        `committed time, so their machining is in none of these figures. Enter or accept their ` +
        `times in the operations table, then preview again.</p>`
      : "") +
    `<div class="matrix-scroll"><table class="matrix-grid"><thead><tr><th>Rate set</th>${head}</tr></thead>` +
    `<tbody>${rows}</tbody></table></div>` +
    `<p class="hint">Priced for ${escapeHtml(state.matrixPreviewFor || "the loaded file")} with the inputs as ` +
    `they stood when you pressed Preview. Nothing was stored.</p>`;
}

async function runMatrixPreview() {
  const status = $("matrix-status");
  if (!state.file) {
    status.textContent = "Load a CAD file first.";
    return;
  }
  if (!matrixPickedBoxes().length) {
    status.textContent = "Tick at least one rate-set version.";
    return;
  }
  clearError();
  const button = $("matrix-run");
  button.disabled = true;
  status.textContent = "Pricing each cell…";
  try {
    state.matrixPreview = await api("/cost/matrix-preview", { method: "POST", body: matrixBody() });
    state.matrixPreviewFor = state.file.name;
    renderMatrixPreview();
    status.textContent = "";
  } catch (error) {
    clearMatrixPreview();
    status.textContent = "";
    showError(error.message);
  } finally {
    button.disabled = false;
  }
}

$("matrix-details").addEventListener("toggle", () => {
  if ($("matrix-details").open) renderMatrixPicker();
});
$("matrix-run").addEventListener("click", runMatrixPreview);
// A grid describes the file it was priced for; a new file takes it down.
$("file").addEventListener("change", clearMatrixPreview);
$("drop").addEventListener("drop", clearMatrixPreview);
