"use strict";

// Bulk preview. Sends a CSV and the CAD files it names to /cad/bulk/preview and
// shows what comes back. Nothing is stored, and this file adds nothing up: every
// figure, count and label is the server's. The CSV that can be downloaded is the
// one the server wrote.

let bulkLast = null;

async function openBulk() {
  try {
    const rateSets = await api("/rates");
    const ids = [...new Set(rateSets.map((entry) => entry.rate_set_id))];
    $("bulk-rate-set").innerHTML =
      `<option value="">(none — each row names its own)</option>` +
      ids.map((id) => `<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`).join("");
    const profiles = await api("/time-profiles");
    const profileIds = [...new Set(profiles.map((entry) => entry.time_profile_id))];
    $("bulk-profile").innerHTML =
      `<option value="">built-in baseline</option>` +
      profileIds.map((id) => `<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`).join("");
  } catch (error) {
    showBulkError(error.message);
  }
}

function showBulkError(message) {
  $("bulk-error").textContent = message;
  $("bulk-error").classList.remove("hidden");
}

function bulkStatusPill(status) {
  const cls = status === "priced" ? "pill-ok" : "pill-warn";
  return `<span class="pill ${cls}">${escapeHtml(status)}</span>`;
}

function renderBulkResult(body) {
  const summary = body.summary;
  const currencies = summary.by_currency
    .map((entry) => `${money(entry.total_cost, entry.currency)} (${entry.priced_rows} priced)`)
    .join(" · ");
  const rows = body.rows
    .map((row) => {
      const totals = row.totals;
      const omitted = (row.omitted_operations || [])
        .map((op) => `${escapeHtml(op.operation_id)} ${escapeHtml(op.operation_name)}: ${escapeHtml(op.reason)}`)
        .join("<br>");
      const detail = row.reason
        ? escapeHtml(row.reason)
        : [row.route ? `Route: ${escapeHtml(row.route)}` : "", ...(row.notes || []).map(escapeHtml)]
            .filter(Boolean)
            .join("<br>");
      return `<tr>
        <td>${row.line}</td>
        <td>${escapeHtml(row.part_id)}<br><span class="muted">${escapeHtml(row.part_name)}</span></td>
        <td>${escapeHtml(row.file)}</td>
        <td>${escapeHtml(row.material)}</td>
        <td class="num">${row.quantity ?? "—"}</td>
        <td>${bulkStatusPill(row.status)}</td>
        <td class="num">${totals ? money(totals.total_cost, row.currency) : "—"}</td>
        <td class="num">${totals ? money(totals.per_unit_cost, row.currency) : "—"}</td>
        <td class="num">${totals ? money(totals.material_cost, row.currency) : "—"}</td>
        <td class="num">${totals ? money(totals.operations_cost, row.currency) : "—"}</td>
        <td class="num">${totals ? money(totals.stages_cost, row.currency) : "—"}</td>
        <td class="num">${row.operations_costed ?? "—"}</td>
        <td class="num">${row.operations_omitted ?? "—"}</td>
        <td>${detail}${omitted ? `<details><summary>omitted operations</summary>${omitted}</details>` : ""}</td>
      </tr>`;
    })
    .join("");
  $("bulk-result").classList.remove("hidden");
  $("bulk-result").innerHTML = `
    <p class="warning-inline"><strong>${escapeHtml(body.preview_label)}</strong> ${escapeHtml(body.notice)}</p>
    <p><strong>${summary.rows}</strong> rows — ${summary.priced} priced, ${summary.refused} refused,
      ${summary.error} error. Time profile: ${escapeHtml(body.time_profile || "")}.
      ${summary.operations_omitted} operation(s) left out for want of a time.</p>
    <p>${currencies ? `Preview sum by currency: ${currencies}` : "No row was priced."}
      <span class="muted">${escapeHtml(summary.currency_note)}</span></p>
    ${summary.unreferenced_files.length
      ? `<p class="muted">Uploaded but named by no row: ${summary.unreferenced_files.map(escapeHtml).join(", ")}</p>`
      : ""}
    <div class="table-scroll"><table class="history">
      <thead><tr><th>Line</th><th>Part</th><th>File</th><th>Material</th><th class="num">Qty</th>
        <th>Status</th><th class="num">Total</th><th class="num">Per unit</th><th class="num">Material</th>
        <th class="num">Operations</th><th class="num">Stages</th><th class="num">Costed</th>
        <th class="num">Omitted</th><th>Detail</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
}

async function runBulkPreview() {
  $("bulk-error").classList.add("hidden");
  const csvFile = $("bulk-csv").files[0];
  const cadFiles = [...$("bulk-files").files];
  if (!csvFile) return showBulkError("Choose the batch CSV first.");
  if (!cadFiles.length) return showBulkError("Choose the CAD files the CSV names.");

  const form = new FormData();
  form.append("batch_csv", csvFile);
  cadFiles.forEach((file) => form.append("files", file));
  if ($("bulk-rate-set").value) form.append("rate_set_id", $("bulk-rate-set").value);
  if ($("bulk-profile").value) form.append("time_profile_id", $("bulk-profile").value);

  $("bulk-run").disabled = true;
  $("bulk-export").disabled = true;
  $("bulk-status").textContent = "Pricing… every row runs the full pipeline, so this can take a while.";
  try {
    bulkLast = await api("/cad/bulk/preview", { method: "POST", body: form });
    renderBulkResult(bulkLast);
    $("bulk-export").disabled = false;
    $("bulk-status").textContent = "Preview only — nothing was saved.";
  } catch (error) {
    bulkLast = null;
    $("bulk-result").classList.add("hidden");
    $("bulk-status").textContent = "";
    showBulkError(error.message);
  } finally {
    $("bulk-run").disabled = false;
  }
}

function exportBulkCsv() {
  if (!bulkLast) return;
  // The CSV text is the server's; the BOM only lets spreadsheets read UTF-8.
  const blob = new Blob(["﻿" + bulkLast.csv], { type: "text/csv;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "bulk-preview-UNREVIEWED.csv";
  link.click();
  URL.revokeObjectURL(link.href);
}

$("bulk-run").addEventListener("click", runBulkPreview);
$("bulk-export").addEventListener("click", exportBulkCsv);
