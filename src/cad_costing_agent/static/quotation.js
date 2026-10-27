// The quotation report page.
//
// Standalone: it shares no globals with the application shell, because it is a
// document rather than a panel. It fetches one object from
// GET /estimates/{id}/quotation and lays it out.
//
// **It computes no cost.** Not a total, not a subtotal, not an extended price,
// not a per-unit figure — every amount printed here is a number the backend
// read out of the estimate's own snapshot. A plausible figure invented in
// JavaScript is indistinguishable from a real one, and this page is the one
// that gets printed and sent, so a test greps this file for arithmetic and for
// assignments to any money field.
//
// It also never rephrases the volume statement. The backend decides whether a
// volume was measured or assumed and writes the sentence; the page prints it
// verbatim, so a template edit cannot turn an assumption into a measurement.

const params = new URLSearchParams(location.search);
const estimateId = params.get("estimate");
const quoteNumber = params.get("quote");

const metadata = {
  customer_name: params.get("customer_name") || "",
  project_name: params.get("project_name") || "",
  quote_reference: params.get("quote_reference") || "",
  prepared_by: params.get("prepared_by") || "",
  notes: params.get("notes") || "",
  validity: params.get("validity") || "",
};

let currentView = params.get("view") === "engineering" ? "engineering" : "customer";

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]
  );
}

// Presentation only. The backend has already rounded every amount to the
// project's two-decimal money policy; this adds separators and a symbol.
function money(amount, currency) {
  if (amount === null || amount === undefined) return "—";
  const formatted = Number(amount).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return currency === "INR" ? `₹${formatted}` : `${currency || ""} ${formatted}`.trim();
}

function number(value, digits = 2) {
  if (value === null || value === undefined) return "—";
  return Number(value).toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function minutes(value) {
  if (value === null || value === undefined) return "—";
  return `${number(value, 2)} min`;
}

function date(value) {
  if (!value) return "—";
  return new Date(value).toLocaleDateString("en-IN", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function field(label, value, plain) {
  return `<div class="field"><span class="label">${escapeHtml(label)}</span>
    <span class="value${plain ? " plain" : ""}">${value === null || value === undefined || value === "" ? "—" : escapeHtml(value)}</span></div>`;
}

async function api(path) {
  const response = await fetch(path);
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || response.statusText);
  return body;
}

// ------------------------------------------------------------------ rendering

function renderHead(quote) {
  const issued = quote.issued_at ? date(quote.issued_at) : date(quote.estimate_created_at);
  return `
    <div class="head">
      <div>
        <h1>Quotation / Costing Report</h1>
        <div class="subtitle">${
          quote.view === "engineering"
            ? "Engineering detail — internal"
            : "Prepared for the customer named below"
        }</div>
      </div>
      <div class="ref">
        <div class="number">${escapeHtml(quote.quote_reference)}</div>
        <div>${quote.issued_at ? "Issued" : "Prepared"} ${escapeHtml(issued)}</div>
        <div class="muted small">Estimate ${escapeHtml(quote.estimate_id)}</div>
      </div>
    </div>`;
}

function renderParties(quote) {
  const meta = quote.metadata;
  const subject = quote.subject;
  return `
    <div class="fields">
      ${field("Customer", meta.customer_name)}
      ${field(subject.kind === "assembly" ? "Assembly" : "Part", subject.name)}
      ${field("Project", meta.project_name)}
      ${field("Model reference", subject.model_reference, true)}
      ${field("Prepared by", meta.prepared_by)}
      ${field("Quantity", `${subject.quantity} ${quote.totals ? quote.totals.quantity_label : ""}`)}
      ${field("Validity", meta.validity)}
      ${field("Currency", quote.totals ? quote.totals.currency : "—")}
    </div>
    ${meta.notes ? `<p class="provenance-note">${escapeHtml(meta.notes)}</p>` : ""}`;
}

function renderBlocked(quote) {
  return `
    ${renderHead(quote)}
    <div class="unavailable">
      <h2>Quotation unavailable</h2>
      ${quote.blockers
        .map(
          // The heading above already says "Quotation unavailable"; every
          // blocker carries the same title for API consumers, so printing it
          // per reason said it three times on one page.
          (blocker) =>
            `<div class="notice stop" style="text-align:left">${escapeHtml(
              blocker.detail
            )}</div>`
        )
        .join("")}
      <p class="muted small" style="margin-top:14px">
        No customer-facing quotation is produced for this estimate. Resolve the
        items above and the document becomes available; nothing here is filled
        in with a zero to make a total appear.
      </p>
    </div>
    <div class="foot"><span>${escapeHtml(quote.basis_note)}</span></div>`;
}

function renderWarnings(quote) {
  if (!quote.warnings.length) return "";
  return `
    <h2>Please note</h2>
    ${quote.warnings
      .map(
        (warning) => `
        <div class="notice ${warning.severity === "advisory" ? "advisory" : ""}">
          <strong>${escapeHtml(warning.title)}</strong>${escapeHtml(warning.detail)}
        </div>`
      )
      .join("")}`;
}

function renderSummary(quote) {
  const totals = quote.totals;
  if (!totals) return "";
  const rows = totals.lines
    .map(
      (line) => `
      <tr>
        <th>${escapeHtml(line.label)}</th>
        <td class="num">${money(line.amount, totals.currency)}</td>
      </tr>
      ${
        line.detail
          ? `<tr class="reason"><td colspan="2" class="muted small">${escapeHtml(
              line.detail
            )}</td></tr>`
          : ""
      }`
    )
    .join("");

  const perUnitLabel =
    quote.subject.kind === "assembly" ? "Per assembly" : "Per unit";

  return `
    <h2>Cost breakdown</h2>
    <table class="summary">
      <tbody>
        ${rows}
        <tr class="total">
          <th>Total${
            totals.quantity > 1 && quote.subject.kind === "assembly"
              ? ` — ${totals.quantity} ${escapeHtml(totals.quantity_label)}`
              : ""
          }</th>
          <td class="num">${money(totals.total, totals.currency)}</td>
        </tr>
        <tr class="subtle">
          <th>${perUnitLabel}</th>
          <td class="num">${money(totals.per_unit, totals.currency)}</td>
        </tr>
      </tbody>
    </table>
    <p class="basis">
      The lines above cover ${escapeHtml(totals.lines_basis)}. All figures are in
      ${escapeHtml(totals.currency)}; no currency conversion is applied anywhere.
    </p>
    ${
      totals.quantity_note
        ? `<p class="basis">${escapeHtml(totals.quantity_note)}</p>`
        : ""
    }
    ${
      totals.reconciliation
        ? `<div class="notice advisory">${escapeHtml(totals.reconciliation)}</div>`
        : ""
    }`;
}

function renderComponents(quote) {
  if (!quote.components.length) return "";
  const currency = quote.totals ? quote.totals.currency : "";
  const rows = quote.components
    .map(
      (component) => `
      <tr class="${component.excluded ? "excluded" : component.costed ? "" : "uncosted"}">
        <td>${escapeHtml(component.name)}${
        component.part_number && component.part_number !== component.name
          ? `<div class="muted small">${escapeHtml(component.part_number)}</div>`
          : ""
      }</td>
        <td><span class="chip ${escapeHtml(component.classification || "")}">${escapeHtml(
        component.classification_label
      )}</span></td>
        <td class="num">${component.quantity}</td>
        <td class="num">${money(component.unit_cost, component.currency || currency)}</td>
        <td class="num">${
          component.extended_cost === null
            ? "not costed"
            : money(component.extended_cost, component.currency || currency)
        }</td>
      </tr>
      ${
        component.reason
          ? `<tr class="reason"><td colspan="5">${escapeHtml(component.reason)}</td></tr>`
          : ""
      }`
    )
    .join("");

  const assembly = quote.assembly;
  return `
    <h2>Components</h2>
    <table>
      <thead>
        <tr><th>Component</th><th>Classification</th><th class="num">Qty</th>
            <th class="num">Unit cost</th><th class="num">Extended</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    ${
      assembly
        ? `<p class="basis">
             ${assembly.definition_count} definitions ·
             ${assembly.part_definition_count} parts ·
             ${assembly.occurrence_count} placements ·
             ${assembly.costed_definition_count} costed${
            assembly.uncosted_definition_count
              ? ` · ${assembly.uncosted_definition_count} needing input`
              : ""
          }${
            assembly.excluded_definition_count
              ? ` · ${assembly.excluded_definition_count} excluded by choice`
              : ""
          }.
             ${escapeHtml(assembly.sub_assembly_note)}
           </p>`
        : ""
    }`;
}

function renderManufacturing(quote) {
  const manufacturing = quote.manufacturing;
  if (!manufacturing || !manufacturing.operation_count) return "";
  return `
    <h2>Manufacturing summary</h2>
    <dl class="pairs">
      <dt>Setups</dt><dd>${manufacturing.setup_count}</dd>
      <dt>Operations</dt><dd>${manufacturing.operation_count}${
    manufacturing.processes.length
      ? ` — ${escapeHtml(manufacturing.processes.join(", "))}`
      : ""
  }</dd>
      <dt>Setup time</dt><dd>${minutes(manufacturing.total_setup_time_minutes)}</dd>
      <dt>Cycle time</dt><dd>${minutes(manufacturing.total_cycle_time_minutes)} per part</dd>
    </dl>
    <p class="basis">${escapeHtml(manufacturing.time_basis)}</p>
    ${
      manufacturing.uncosted_note
        ? `<div class="notice">${escapeHtml(manufacturing.uncosted_note)}</div>`
        : ""
    }`;
}

function renderProvenance(quote) {
  const provenance = quote.provenance;
  const assumed = provenance.volume_basis === "assumed";
  return `
    <h2>Basis of this quotation</h2>
    <dl class="pairs">
      <dt>CAD source</dt><dd>${escapeHtml(provenance.source_file || "—")}${
    provenance.source_format ? ` (${escapeHtml(provenance.source_format)})` : ""
  }</dd>
      ${
        provenance.geometry_source || provenance.geometry_status
          ? `<dt>Geometry</dt><dd>${escapeHtml(
              provenance.geometry_source || "—"
            )} · ${escapeHtml(provenance.geometry_status || "—")}</dd>`
          : `<dt>Geometry</dt><dd class="muted">measured per component — see the component
              table above</dd>`
      }
      <dt>Material</dt><dd>${escapeHtml(provenance.material || "—")}${
    provenance.density_kg_m3 ? ` · ${number(provenance.density_kg_m3, 0)} kg/m³` : ""
  }${provenance.mass_kg ? ` · ${number(provenance.mass_kg, 4)} kg per part` : ""}${
    provenance.material_basis === "bar_stock" && provenance.stock_mass_kg
      ? ` · costed from ${escapeHtml(provenance.stock_description || "the stock it is cut from")}, ` +
        `${number(provenance.stock_mass_kg, 4)} kg per part`
      : ""
  }</dd>
      <dt>Rate set</dt><dd>${escapeHtml(provenance.rate_set_name || provenance.rate_set_id || "—")}
        ${provenance.rate_set_version ? `v${escapeHtml(provenance.rate_set_version)}` : ""}</dd>
      <dt>Time profile</dt><dd>${escapeHtml(
        provenance.time_profile_name || provenance.time_profile_id || "—"
      )} ${provenance.time_profile_version ? `v${escapeHtml(provenance.time_profile_version)}` : ""}</dd>
      <dt>Plan</dt><dd>${escapeHtml(provenance.plan_state || "—")}</dd>
    </dl>
    ${
      provenance.volume_statement
        ? assumed
          ? `<div class="assumption">
               <span class="headline">${escapeHtml(
                 provenance.volume_statement.split(" User-supplied")[0]
               )}</span>
               <div>User-supplied assumption: ${number(
                 provenance.user_supplied_volume_mm3,
                 1
               )} mm³</div>
               <div>Volume used: ${number(provenance.volume_used_mm3, 1)} mm³</div>
             </div>`
          : `<p class="basis">${escapeHtml(provenance.volume_statement)}</p>`
        : ""
    }`;
}

function renderRevision(quote) {
  const revision = quote.revision;
  if (!revision) return "";
  const currency = revision.currency;
  return `
    <h2>Revision</h2>
    <dl class="pairs">
      <dt>Previous estimate</dt><dd>${escapeHtml(revision.previous_estimate_id)} —
        ${money(revision.previous_total, currency)}</dd>
      <dt>This estimate</dt><dd>${money(revision.current_total, currency)}</dd>
      <dt>Change</dt><dd>${
        revision.change === null
          ? "not comparable"
          : `${revision.change > 0 ? "+" : ""}${money(revision.change, currency)}`
      }</dd>
    </dl>
    <p class="basis">${escapeHtml(revision.note)}</p>`;
}

// Provenance as sentences rather than raw JSON: this appears on paper, and a
// null in a printed object reads as a defect rather than as "no profile was
// used", which is what it means for hand-entered operations.
function provenancePairs(label, provenance) {
  const stated = Object.entries(provenance || {}).filter(
    ([, value]) => value !== null && value !== undefined && value !== ""
  );
  if (!stated.length) {
    return `<dt>${escapeHtml(label)}</dt><dd class="muted">none recorded — no profile was
      used for this estimate</dd>`;
  }
  return `<dt>${escapeHtml(label)}</dt><dd>${stated
    .map(([key, value]) => `${escapeHtml(key.replace(/_/g, " "))}: ${escapeHtml(value)}`)
    .join(" · ")}</dd>`;
}

function renderEngineering(quote) {
  const engineering = quote.engineering;
  if (!engineering) return "";

  const setups = engineering.setups.length
    ? engineering.setups
        .map(
          (setup) => `
        <div class="setup">
          <div class="setup-head">Setup ${setup.sequence_position} — ${escapeHtml(
            setup.name || setup.setup_id
          )}</div>
          <div class="setup-meta">Setup time: ${minutes(
            setup.setup_time_minutes
          )} (${escapeHtml(setup.setup_time_source)}) · charged once for this setup</div>
          <ul>
            ${setup.operations
              .map(
                (operation) =>
                  `<li>${escapeHtml(
                    operation.operation_name || operation.operation_id
                  )} — ${minutes(operation.cycle_time_minutes)}${
                    operation.time_source_label
                      ? ` (${escapeHtml(operation.time_source_label)})`
                      : ""
                  }${
                    operation.feature_ids.length
                      ? ` · ${escapeHtml(operation.feature_ids.join(", "))}`
                      : ""
                  }</li>`
              )
              .join("")}
          </ul>
        </div>`
        )
        .join("")
    : "";

  const operations = engineering.operations
    .map(
      (operation) => `
      <tr>
        <td>${escapeHtml(operation.operation_id)}</td>
        <td>${escapeHtml(operation.operation_name)}</td>
        <td>${escapeHtml(operation.process_type)}</td>
        <td class="num">${minutes(operation.setup_time_minutes)}</td>
        <td class="num">${minutes(operation.cycle_time_minutes)}</td>
        <td class="num">${money(
          operation.setup_cost,
          quote.totals ? quote.totals.currency : ""
        )}</td>
        <td class="num">${money(
          operation.run_cost,
          quote.totals ? quote.totals.currency : ""
        )}</td>
        <td class="num">${money(
          operation.total_cost,
          quote.totals ? quote.totals.currency : ""
        )}</td>
      </tr>`
    )
    .join("");

  const decisions = engineering.component_decisions.length
    ? `<h2>Component decisions</h2>
       <table>
         <thead><tr><th>Component</th><th class="num">Qty</th><th>System</th><th>Engineer</th>
                    <th>Material</th><th>Supplier</th></tr></thead>
         <tbody>${engineering.component_decisions
           .map(
             (decision) => `
             <tr>
               <td>${escapeHtml(decision.name)}</td>
               <td class="num">${decision.quantity}</td>
               <td>${escapeHtml(decision.system_classification || "—")}</td>
               <td>${escapeHtml(decision.user_classification || "not stated")}</td>
               <td>${escapeHtml(decision.material || "—")}${
               decision.material_source
                 ? ` <span class="muted small">(${escapeHtml(decision.material_source)})</span>`
                 : ""
             }</td>
               <td>${escapeHtml(decision.supplier || "—")}</td>
             </tr>`
           )
           .join("")}</tbody>
       </table>`
    : "";

  return `
    <div class="page-break"></div>
    <h2>Engineering detail — appendix</h2>
    <p class="basis">
      Read from the estimate's own snapshots. This section is not part of the
      customer document.
    </p>
    ${setups ? `<h2>Setups and operations</h2>${setups}` : ""}
    ${
      operations
        ? `<h2>Costed operations</h2>
           <table>
             <thead><tr><th>Id</th><th>Operation</th><th>Process</th>
                        <th class="num">Setup</th><th class="num">Cycle</th>
                        <th class="num">Setup cost</th><th class="num">Run cost</th>
                        <th class="num">Total</th></tr></thead>
             <tbody>${operations}</tbody>
           </table>`
        : ""
    }
    ${
      engineering.features.length
        ? `<h2>Features referenced</h2><p class="small">${escapeHtml(
            engineering.features.join(", ")
          )}</p>`
        : ""
    }
    ${decisions}
    ${
      engineering.corrections.length
        ? `<h2>System proposals and engineering decisions</h2>
           <ul class="small">${engineering.corrections
             .map((sentence) => `<li>${escapeHtml(sentence)}</li>`)
             .join("")}</ul>`
        : ""
    }
    <h2>Profile provenance</h2>
    <dl class="pairs">
      ${provenancePairs("Rate set", engineering.rate_set_provenance)}
      ${provenancePairs("Time profile", engineering.time_profile_provenance)}
    </dl>`;
}

function render(quote) {
  document.title = `${quote.quote_reference} — ${quote.subject.name || "Quotation"}`;

  if (!quote.available) {
    $("report").innerHTML = renderBlocked(quote);
    return;
  }

  $("report").innerHTML = `
    ${renderHead(quote)}
    ${renderParties(quote)}
    ${renderWarnings(quote)}
    ${renderSummary(quote)}
    ${renderComponents(quote)}
    ${renderManufacturing(quote)}
    ${renderProvenance(quote)}
    ${renderRevision(quote)}
    ${(quote.scope_notes || [])
      .map((note) => `<p class="provenance-note">${escapeHtml(note)}</p>`)
      .join("")}
    <div class="foot">
      <span>${escapeHtml(quote.basis_note)}</span>
      <span>${escapeHtml(quote.quote_reference)}</span>
    </div>
    ${renderEngineering(quote)}`;
}

// ------------------------------------------------------------------ loading

function query() {
  const search = new URLSearchParams({ view: currentView });
  Object.entries(metadata).forEach(([key, value]) => {
    if (value) search.set(key, value);
  });
  return search.toString();
}

async function load() {
  try {
    if (quoteNumber) {
      const body = await api(`/quotations/${encodeURIComponent(quoteNumber)}?view=${currentView}`);
      render(body.quotation);
      $("status").textContent = `Issued ${date(body.issued.issued_at)}`;
      $("issue").hidden = true;
      $("edit-details").hidden = true;
      return;
    }
    if (!estimateId) {
      $("report").innerHTML =
        "<p class='muted'>No estimate given. Open this page from an estimate in the history table.</p>";
      return;
    }
    const quote = await api(
      `/estimates/${encodeURIComponent(estimateId)}/quotation?${query()}`
    );
    render(quote);
    $("issue").hidden = !quote.available;
    $("status").textContent = quote.available ? "Preview — not yet issued" : "";
  } catch (error) {
    $("report").innerHTML = `<p class="notice stop">${escapeHtml(error.message)}</p>`;
  }
}

$("print").addEventListener("click", () => window.print());

$("view").value = currentView;
$("view").addEventListener("change", (event) => {
  currentView = event.target.value;
  load();
});

$("edit-details").addEventListener("click", () => {
  $("details").hidden = !$("details").hidden;
  $("m-customer").value = metadata.customer_name;
  $("m-project").value = metadata.project_name;
  $("m-reference").value = metadata.quote_reference;
  $("m-prepared").value = metadata.prepared_by;
  $("m-validity").value = metadata.validity;
  $("m-notes").value = metadata.notes;
});

$("apply-details").addEventListener("click", () => {
  metadata.customer_name = $("m-customer").value;
  metadata.project_name = $("m-project").value;
  metadata.quote_reference = $("m-reference").value;
  metadata.prepared_by = $("m-prepared").value;
  metadata.validity = $("m-validity").value;
  metadata.notes = $("m-notes").value;
  $("details").hidden = true;
  load();
});

$("issue").addEventListener("click", async () => {
  $("issue").disabled = true;
  try {
    const response = await fetch(
      `/estimates/${encodeURIComponent(estimateId)}/quotation?view=${currentView}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          customer_name: metadata.customer_name,
          project_name: metadata.project_name,
          quote_number: metadata.quote_reference || null,
          prepared_by: metadata.prepared_by,
          notes: metadata.notes,
          validity: metadata.validity,
        }),
      }
    );
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || response.statusText);
    // Reopened by its quote number, so what is on screen from here on is the
    // issued document read back rather than a preview that looks like one.
    location.search = `?quote=${encodeURIComponent(body.issued.quote_number)}&view=${currentView}`;
  } catch (error) {
    $("status").textContent = error.message;
    $("issue").disabled = false;
  }
});

load();
