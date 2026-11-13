"use strict";

// CO2e (illustrative): kilograms of CO2e for one stored estimate.
//
// A classic script loaded after drivers.js and before estimateflow.js, sharing
// `$`, `api`, `escapeHtml` and `NUMBER`. app.js calls it through a `typeof`
// guard for the Cost section's card and the History detail.
//
// It renders /estimates/{id}/co2e and nothing else: every figure, every status
// and every reason is the server's. It adds nothing up and multiplies nothing —
// a line the server could not estimate is printed with its reason, never as 0 —
// and it never shows a CO2e figure as money or beside a currency.

let co2eRegionsLoaded = false;

async function co2eRegionOptions() {
  if (co2eRegionsLoaded || !$("co2e-region")) return;
  try {
    const catalogs = await api("/emission-factors");
    // In the server's order (each catalog lists its regions sorted).
    const regions = [...new Set(catalogs.flatMap((catalog) => catalog.regions || []))];
    $("co2e-region").innerHTML =
      `<option value="">The rate set's factory region</option>` +
      regions.map((r) => `<option value="${escapeHtml(r)}">${escapeHtml(r)}</option>`).join("");
    co2eRegionsLoaded = true;
  } catch (error) {
    // The card still works with the rate set's region; the list is a convenience.
  }
}

function co2eKg(value) {
  return value === null || value === undefined ? "—" : `${NUMBER.format(value)} kg CO2e`;
}

function co2eSource(title, url, basis) {
  if (basis === "no_published_source_found_illustrative") {
    return `<span class="muted">no published source found — illustrative</span>`;
  }
  if (!title) return "";
  return url
    ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(title)}</a>`
    : escapeHtml(title);
}

function co2eLineRow(line) {
  const basis = [
    line.mass_kg !== null && line.mass_kg !== undefined
      ? `${NUMBER.format(line.mass_kg)} kg × ${line.quantity} (${escapeHtml(line.mass_source || "")})`
      : "",
    line.run_hours !== null && line.run_hours !== undefined
      ? `${NUMBER.format(line.run_hours)} run h` + (line.kwh !== null && line.kwh !== undefined ? ` → ${NUMBER.format(line.kwh)} kWh` : "")
      : "",
  ]
    .filter(Boolean)
    .join("; ");
  const factors = [
    line.factor !== null && line.factor !== undefined
      ? `${line.factor} ${escapeHtml(line.factor_unit || "")} — ${co2eSource(line.factor_source_title, line.factor_source_url, line.factor_basis)}`
      : "",
    line.grid_factor_kg_co2e_per_kwh !== null && line.grid_factor_kg_co2e_per_kwh !== undefined
      ? `grid ${line.grid_factor_kg_co2e_per_kwh} kg CO2e/kWh — ${co2eSource(line.grid_source_title, line.grid_source_url, line.grid_basis)}`
      : "",
  ]
    .filter(Boolean)
    .join("<br>");
  const figure =
    line.status === "estimated"
      ? co2eKg(line.kg_co2e)
      : `<span class="badge illustrative">Not estimated</span><div class="hint">${escapeHtml(line.reason || "")}</div>`;
  return `<tr class="co2e-line co2e-${escapeHtml(line.status)}">
    <td>${escapeHtml(line.label)}</td><td>${basis || "—"}</td><td>${factors || "—"}</td><td class="num">${figure}</td></tr>`;
}

function co2eMarkup(result) {
  const status = {
    complete: "Every line estimated",
    partial: "Partial — some lines could not be estimated and are not in the total",
    not_estimated: "Nothing could be estimated",
  }[result.status] || result.status;
  const region = result.region
    ? `region ${escapeHtml(result.region)} (${result.region_source === "rate_set_factory" ? "the rate set's factory" : "chosen here"})`
    : "no region stated";
  return `
    <p class="co2e-summary"><strong>${co2eKg(result.total_kg_co2e)}</strong> for the batch of
      ${result.quantity ?? "—"} · ${co2eKg(result.per_unit_kg_co2e)} per part
      <span class="badge ${result.status === "complete" ? "real" : "illustrative"}">${escapeHtml(status)}</span></p>
    <p class="hint">Catalog ${escapeHtml(result.emission_catalog_id)} v${escapeHtml(result.emission_catalog_version)}${
      result.catalog_is_illustrative ? " (illustrative)" : ""} · ${region}</p>
    <table class="co2e-table"><thead><tr><th>Line</th><th>Basis (as stored)</th><th>Factor and source</th><th class="num">CO2e</th></tr></thead>
      <tbody>${(result.lines || []).map(co2eLineRow).join("")}</tbody></table>
    ${(result.notes || []).map((note) => `<p class="hint">${escapeHtml(note)}</p>`).join("")}
    <p class="hint">${escapeHtml(result.method)}</p>
    <p class="hint"><strong>${escapeHtml(result.disclaimer)}</strong></p>`;
}

async function renderCo2eInto(hostId, estimateId, region) {
  const host = $(hostId);
  if (!host || !estimateId) return;
  host.innerHTML = "<p class='muted'>Loading CO2e…</p>";
  const query = region ? `?region=${encodeURIComponent(region)}` : "";
  try {
    const result = await api(`/estimates/${encodeURIComponent(estimateId)}/co2e${query}`);
    host.innerHTML = co2eMarkup(result);
  } catch (error) {
    host.innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
  }
}

let co2eEstimateId = null;

// The Cost section's card, for the estimate the result panel is showing.
async function renderCo2eFor(estimateId) {
  const card = $("co2e-card");
  if (!card) return;
  co2eEstimateId = estimateId || null;
  card.classList.toggle("hidden", !estimateId);
  await co2eRegionOptions();
  await renderCo2eInto("co2e-body", estimateId, $("co2e-region").value);
}

if ($("co2e-region")) {
  $("co2e-region").addEventListener("change", () => {
    if (co2eEstimateId) renderCo2eInto("co2e-body", co2eEstimateId, $("co2e-region").value);
  });
}
