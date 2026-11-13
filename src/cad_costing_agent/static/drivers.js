"use strict";

// Cost drivers: what makes up one stored estimate's cost, each figure with its
// share of the stored total.
//
// A classic script loaded after dfm.js and before estimateflow.js, sharing `$`,
// `api`, `money`, `NUMBER` and `escapeHtml` with the files before it. app.js
// (the current estimate), the History detail and assembly.js (the roll-up) call
// it through a `typeof` guard, because they load before it.
//
// What this file does not do, on purpose:
// - It adds nothing up and works nothing out. Every amount and every share is
//   the server's (/estimates/{id}/cost-drivers). A share bar's width is the
//   server's percentage passed straight into CSS; the percentage is also
//   printed, so nothing is told by colour or length alone.
// - It ranks nothing. Rows are shown in the server's order, which is the order
//   the cost is built up, and no row is marked.
// - A figure the estimate did not record is shown as not recorded, never as 0.

async function renderCostDriversInto(hostId, estimateId) {
  const host = $(hostId);
  if (!host || !estimateId) return;
  host.innerHTML = "<p class='muted'>Loading cost drivers…</p>";
  try {
    const breakdown = await api(`/estimates/${encodeURIComponent(estimateId)}/cost-drivers`);
    host.innerHTML = costDriversMarkup(breakdown);
  } catch (error) {
    host.innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
  }
}

// The Cost section's card, for the estimate the result panel is showing.
async function renderCostDriversFor(estimateId) {
  const card = $("drivers-card");
  if (!card) return;
  card.classList.toggle("hidden", !estimateId);
  await renderCostDriversInto("drivers-body", estimateId);
}

function costDriverShare(driver) {
  if (driver.share_percent === null || driver.share_percent === undefined) {
    return `<td class="num">—</td><td class="driver-bar-cell"></td>`;
  }
  return (
    `<td class="num">${NUMBER.format(driver.share_percent)}%</td>` +
    `<td class="driver-bar-cell"><span class="driver-track">` +
    `<span class="driver-bar${driver.category === "reconciliation" ? " driver-bar-recon" : ""}" ` +
    `style="width: ${driver.share_percent}%"></span></span></td>`
  );
}

function costDriverRows(drivers, currency) {
  return drivers
    .map((driver) => {
      const parts = (driver.of_which || [])
        .map(
          (part) =>
            `${escapeHtml(part.label)} ${
              part.amount === null ? "not recorded" : money(part.amount, currency)
            }`
        )
        .join(" · ");
      const notes = [driver.note, parts ? `of which ${parts}` : ""].filter(Boolean);
      return `
        <tr class="${driver.recorded ? "" : "driver-unrecorded"} driver-${escapeHtml(driver.category)}">
          <td>${escapeHtml(driver.label)}${
            notes.length ? `<div class="hint small">${notes.map(escapeHtml).join("<br>")}</div>` : ""
          }</td>
          <td class="num">${money(driver.amount, currency)}</td>
          ${costDriverShare(driver)}
        </tr>`;
    })
    .join("");
}

function costDriverTable(drivers, currency, caption) {
  return `
    <table class="mini drivers-table">
      <thead><tr><th>${escapeHtml(caption)}</th><th class="num">Amount</th>
        <th class="num">Share of total</th><th class="driver-bar-cell"><span class="sr-only">Share bar</span></th></tr></thead>
      <tbody>${costDriverRows(drivers, currency)}</tbody>
    </table>`;
}

function costDriversMarkup(breakdown) {
  const currency = breakdown.currency;
  const head =
    `<p class="hint">Estimate <span class="mono">${escapeHtml(breakdown.estimate_id)}</span> — ` +
    `basis: ${escapeHtml(breakdown.basis)}. Total ${money(breakdown.total, currency)}. ` +
    `${escapeHtml(breakdown.order_note)} ${escapeHtml(breakdown.note)}</p>`;
  if (breakdown.status === "not_available") {
    return `${head}<p class="warning">Not available for this estimate: ${escapeHtml(breakdown.reason)}</p>`;
  }
  const partial =
    breakdown.status === "partial"
      ? `<p class="warning">Partial roll-up: ${escapeHtml(breakdown.reason)}</p>`
      : "";
  const components = (breakdown.component_drivers || []).length
    ? `<h3>By component</h3>${costDriverTable(breakdown.component_drivers, currency, "Component")}`
    : "";
  return `${head}${partial}
    ${costDriverTable(breakdown.drivers, currency, "Driver")}
    ${breakdown.rounding_note ? `<p class="hint small">${escapeHtml(breakdown.rounding_note)}</p>` : ""}
    ${components}`;
}
