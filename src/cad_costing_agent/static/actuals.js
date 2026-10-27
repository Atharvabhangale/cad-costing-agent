// Actual costs recorded against a stored estimate, shown in the History detail.
//
// The browser works nothing out here. Every per-unit figure, variance and
// percentage is read from /estimates/{id}/actuals; this file only lays them out
// and sends what a person typed. A blank breakdown field is sent as null, never
// as 0 — the server reports it as "not supplied", and a zero would be a claim
// that the part had no such cost.

const ACTUAL_STATE_LABELS = {
  compared: "",
  not_supplied: "Not supplied",
  not_comparable: "Not comparable",
};

async function renderActualsPanel(estimate) {
  const host = $("history-actuals");
  if (!host) return;
  host.innerHTML = "<p class='muted'>Loading actual costs…</p>";
  let data;
  try {
    data = await api(`/estimates/${encodeURIComponent(estimate.estimate_id)}/actuals`);
  } catch (error) {
    host.innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
    return;
  }
  host.innerHTML = `
    <h3>Actual cost (client data)</h3>
    <p class="muted">Costs stated outside this application — an ERP actual, a past quotation —
    compared with this estimate per unit. Recording one changes nothing in the estimate.
    ${data.estimate_kind === "assembly"
      ? "An assembly is compared on its total only."
      : ""}</p>
    <div id="actuals-list">${
      data.entries.length
        ? data.entries.map((entry) => actualEntryMarkup(entry)).join("")
        : "<p class='muted'>No actual cost recorded yet.</p>"
    }</div>
    ${actualFormMarkup(data)}`;

  host.querySelectorAll("button.actual-correct").forEach((button) =>
    button.addEventListener("click", () => {
      const entry = data.entries.find((item) => item.record.actual_id === button.dataset.id);
      fillActualForm(entry.record);
    })
  );
  $("actual-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitActual(estimate);
  });
  $("actual-cancel-correction").addEventListener("click", () => fillActualForm(null));
}

function signed(value, suffix = "") {
  if (value === null || value === undefined) return "—";
  return `${value > 0 ? "+" : ""}${NUMBER.format(value)}${suffix}`;
}

function actualEntryMarkup(entry) {
  const record = entry.record;
  const variance = entry.variance;
  const currency = record.currency;
  const basis =
    record.cost_basis === "per_batch"
      ? `per batch of ${record.batch_quantity}`
      : `per unit${record.batch_quantity ? `, batch of ${record.batch_quantity}` : ""}`;
  const identity = [
    record.client_part_number ? `Client part ${escapeHtml(record.client_part_number)}` : "",
    record.drawing_revision ? `drawing rev ${escapeHtml(record.drawing_revision)}` : "",
    record.cost_date ? `dated ${escapeHtml(record.cost_date)}` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  let body;
  if (variance.status !== "available") {
    body = `<p class="warning">Variance unavailable: ${escapeHtml(variance.unavailable_detail)}</p>`;
  } else {
    const rows = variance.lines
      .map(
        (line) => `<tr>
          <th>${escapeHtml(line.label)}</th>
          <td class="num">${money(line.estimate_per_unit, currency)}</td>
          <td class="num">${
            line.state === "not_supplied"
              ? "<em>Not supplied</em>"
              : money(line.actual_per_unit, currency)
          }</td>
          <td class="num">${
            line.state === "compared" ? signed(line.variance) : ACTUAL_STATE_LABELS[line.state]
          }</td>
          <td class="num">${line.state === "compared" ? signed(line.variance_percent, "%") : ""}</td>
        </tr>`
      )
      .join("");
    // One sentence per distinct note, under the table rather than in a column,
    // so a long explanation cannot stretch every row it sits beside.
    const notes = [
      ...new Set(
        variance.lines
          .filter((line) => line.note)
          .map((line) =>
            line.state === "not_supplied" ? line.note : `${line.label}: ${line.note}`
          )
      ),
    ];
    body = `
      <table class="mini actual-variance">
        <thead><tr><th></th><th class="num">Estimate / unit</th><th class="num">Actual / unit</th>
          <th class="num">Variance</th><th class="num">%</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      ${notes.map((text) => `<p class="muted actual-note">${escapeHtml(text)}</p>`).join("")}
      ${
        variance.unattributed_actual_per_unit !== null &&
        variance.unattributed_actual_per_unit !== undefined
          ? `<p class="muted">Actual not attributed to any supplied category:
             ${money(variance.unattributed_actual_per_unit, currency)} per unit.</p>`
          : ""
      }
      ${variance.warnings.map((text) => `<p class="warning">${escapeHtml(text)}</p>`).join("")}
      <p class="provenance">${escapeHtml(variance.basis)} ${escapeHtml(variance.category_mapping)}</p>`;
  }

  return `<div class="actual-entry${entry.is_current ? "" : " superseded"}">
    <p><strong>${money(record.total_cost, currency)}</strong> ${basis} —
      ${escapeHtml(record.source)}
      ${identity ? `<br><span class="muted">${identity}</span>` : ""}
      <br><span class="muted mono">Recorded ${new Date(record.recorded_at + "Z").toLocaleString()}
      · ${record.actual_id}</span>
      ${record.supersedes_actual_id
        ? `<br><span class="muted">Corrects ${record.supersedes_actual_id}</span>`
        : ""}
      ${record.notes ? `<br><span class="muted">${escapeHtml(record.notes)}</span>` : ""}</p>
    ${
      entry.is_current
        ? `<button type="button" class="secondary actual-correct" data-id="${record.actual_id}">
             Correct this record</button>`
        : `<p class="muted">Superseded by ${entry.superseded_by}; kept as entered.</p>`
    }
    ${body}
  </div>`;
}

function actualFormMarkup(data) {
  return `
    <form id="actual-form" class="actual-form">
      <h4 id="actual-form-title">Record an actual cost</h4>
      <p id="actual-correcting" class="warning hidden"></p>
      <input type="hidden" id="actual-supersedes">
      <div class="grid3">
        <label>Source <input id="actual-source" required
          placeholder="e.g. Client ERP actual, FY2025"></label>
        <label>Cost basis
          <select id="actual-basis" required>
            <option value="">Choose…</option>
            <option value="per_unit">Per unit</option>
            <option value="per_batch">Per batch</option>
          </select></label>
        <label>Batch quantity <input id="actual-batch" type="number" min="1" step="1"
          placeholder="required per batch"></label>
        <label>Currency <input id="actual-currency" value="${escapeHtml(data.currency)}"
          maxlength="3"></label>
        <label>Total cost <input id="actual-total" type="number" min="0" step="any" required></label>
        <label>Material <input id="actual-material" type="number" min="0" step="any"
          placeholder="optional"></label>
        <label>Machining <input id="actual-machining" type="number" min="0" step="any"
          placeholder="optional"></label>
        <label>Additional <input id="actual-additional" type="number" min="0" step="any"
          placeholder="optional"></label>
        <label>Cost date <input id="actual-date" type="date"></label>
        <label>Client part number <input id="actual-part-number" maxlength="64"
          placeholder="optional"></label>
        <label>Drawing revision <input id="actual-revision" maxlength="32"
          placeholder="optional"></label>
        <label>Notes <input id="actual-notes" maxlength="2000"></label>
      </div>
      <p id="actual-error" class="error hidden"></p>
      <div class="row">
        <button type="submit">Record actual cost</button>
        <button type="button" id="actual-cancel-correction" class="secondary hidden">
          Cancel correction</button>
      </div>
    </form>`;
}

const ACTUAL_FIELDS = {
  "actual-source": "source",
  "actual-basis": "cost_basis",
  "actual-batch": "batch_quantity",
  "actual-currency": "currency",
  "actual-total": "total_cost",
  "actual-material": "material_cost",
  "actual-machining": "machining_cost",
  "actual-additional": "additional_cost",
  "actual-date": "cost_date",
  "actual-part-number": "client_part_number",
  "actual-revision": "drawing_revision",
  "actual-notes": "notes",
};

// A correction starts from the record it replaces, which stays unchanged.
function fillActualForm(record) {
  for (const [id, field] of Object.entries(ACTUAL_FIELDS)) {
    if (record) $(id).value = record[field] ?? "";
    else if (id !== "actual-currency") $(id).value = "";
  }
  $("actual-supersedes").value = record ? record.actual_id : "";
  $("actual-error").classList.add("hidden");
  $("actual-form-title").textContent = record ? "Correct an actual cost" : "Record an actual cost";
  $("actual-correcting").textContent = record
    ? `Correcting ${record.actual_id}. The original stays on the record, marked superseded.`
    : "";
  $("actual-correcting").classList.toggle("hidden", !record);
  $("actual-cancel-correction").classList.toggle("hidden", !record);
  $("actual-form").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// Blank is null. Never 0.
function optionalNumber(id) {
  const raw = $(id).value.trim();
  return raw === "" ? null : Number(raw);
}

function optionalText(id) {
  const raw = $(id).value.trim();
  return raw === "" ? null : raw;
}

async function submitActual(estimate) {
  const error = $("actual-error");
  error.classList.add("hidden");
  const payload = {
    source: $("actual-source").value,
    cost_basis: $("actual-basis").value,
    batch_quantity: optionalNumber("actual-batch"),
    currency: $("actual-currency").value.trim().toUpperCase(),
    total_cost: optionalNumber("actual-total"),
    material_cost: optionalNumber("actual-material"),
    machining_cost: optionalNumber("actual-machining"),
    additional_cost: optionalNumber("actual-additional"),
    cost_date: optionalText("actual-date"),
    client_part_number: optionalText("actual-part-number"),
    drawing_revision: optionalText("actual-revision"),
    notes: $("actual-notes").value,
    supersedes_actual_id: optionalText("actual-supersedes"),
  };
  try {
    await api(`/estimates/${encodeURIComponent(estimate.estimate_id)}/actuals`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (failure) {
    error.textContent = failure.message;
    error.classList.remove("hidden");
    return;
  }
  renderActualsPanel(estimate);
}
