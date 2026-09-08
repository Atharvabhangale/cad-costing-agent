"use strict";

// Assumptions and cost-impact panels: "what was I assuming?" and "why did this
// change?".
//
// A classic script loaded after app.js, sharing `$`, `api`, `money`, `number`,
// `escapeHtml` and `state` with it.
//
// **Every number here is computed on the server.** This file formats
// `/estimates/{id}/assumptions` and `/estimates/compare`; it subtracts nothing,
// apportions nothing and infers no cause. That matters more than usual for this
// screen: a plausible-looking delta invented in the browser would be a very
// convincing wrong answer.

// Impact categories, in the order they read best, with a human label each.
const IMPACT_LABELS = {
  material_rate: "Material rate",
  material_quantity: "Material mass × quantity",
  operation_rate: "Operation rates",
  operation_time: "Operation time",
  operation_added: "Operation added",
  operation_removed: "Operation removed",
  setup_rate: "Setup rate",
  setup_time: "Setup time",
  overhead: "Overhead",
  scrap: "Scrap allowance",
  tooling: "Tooling",
  combined: "Combined effect",
  rounding: "Rounding",
  unexplained: "Unexplained",
};

// ------------------------------------------------------------- assumptions

async function renderAssumptionsFor(estimateId) {
  if (!estimateId) return;
  let assumptions;
  try {
    assumptions = await api(`/estimates/${encodeURIComponent(estimateId)}/assumptions`);
  } catch (error) {
    $("assumptions-card").classList.add("hidden");
    return;
  }
  state.assumptions = assumptions;
  $("assumptions-body").innerHTML = assumptionGroups(assumptions);
  $("assumptions-card").classList.remove("hidden");
}

function row(label, value, extra) {
  if (value === null || value === undefined || value === "") return "";
  return (
    `<tr><th>${escapeHtml(label)}</th><td>${escapeHtml(String(value))}` +
    (extra ? ` <span class="muted">${escapeHtml(extra)}</span>` : "") +
    `</td></tr>`
  );
}

function group(title, rows, badge) {
  const body = rows.filter(Boolean).join("");
  if (!body) return "";
  return (
    `<div class="assumption-group"><h3>${escapeHtml(title)}${badge || ""}</h3>` +
    `<table class="mini">${body}</table></div>`
  );
}

// A version that produced a stored number is worth being able to open, so the
// engineer can see the rates or feeds it actually carried.
function versionLink(kind, id, version, name) {
  if (!id) return "";
  const path =
    kind === "rates"
      ? `/rates/${encodeURIComponent(id)}${version ? `?version=${encodeURIComponent(version)}` : ""}`
      : `/time-profiles/${encodeURIComponent(id)}${version ? `?version=${encodeURIComponent(version)}` : ""}`;
  return (
    `<a href="${escapeHtml(path)}" target="_blank" rel="noopener">` +
    `${escapeHtml(name || id)} v${escapeHtml(version || "—")}</a>`
  );
}

function illustrativeBadge(flag) {
  if (flag === true) return ` <span class="badge badge-warn">Illustrative</span>`;
  if (flag === false) return ` <span class="badge badge-ok">Real</span>`;
  return "";
}

function assumptionGroups(a) {
  const cad = group("CAD", [
    row("File", a.source_file),
    row("Format", a.source_format),
    row("Geometry source", a.geometry_source),
    row("Volume", a.volume_mm3 == null ? null : `${number(a.volume_mm3, 3)} mm³`,
      a.volume_method && a.volume_method !== "brep" ? `(${a.volume_method})` : ""),
    row("Mass", a.mass_kg == null ? null : `${number(a.mass_kg, 4)} kg per part`),
  ]);

  const material = group("Material", [
    row("Material", a.material),
    row("Density", a.density_kg_m3 == null ? null : `${number(a.density_kg_m3, 0)} kg/m³`),
    row("Cost per kg", a.material_cost_per_kg == null ? null
      : money(a.material_cost_per_kg, a.currency)),
  ], illustrativeBadge(a.material_is_illustrative));

  const rates = group("Costing profile — money", [
    a.rate_set_id
      ? `<tr><th>Profile</th><td>${versionLink("rates", a.rate_set_id, a.rate_set_version, a.rate_set_name)}</td></tr>`
      : "",
    row("Currency", a.currency),
  ], illustrativeBadge(a.rate_set_is_illustrative));

  const time = group("Time profile — duration", [
    a.time_profile_id
      ? `<tr><th>Profile</th><td>${versionLink("time", a.time_profile_id, a.time_profile_version, a.time_profile_name)}</td></tr>`
      : `<tr><th>Profile</th><td class="muted">none — times were entered or estimated under the built-in baseline</td></tr>`,
  ], illustrativeBadge(a.time_profile_is_illustrative));

  const setupList = a.setups.length
    ? `<tr><th>Setups</th><td>${a.setups
        .map(
          (s) =>
            `${escapeHtml(s.name)} <span class="muted">(${s.operation_count} op(s), ` +
            `${s.setup_time_minutes == null ? "no setup time" : `${number(s.setup_time_minutes, 2)} min`})</span>`
        )
        .join("<br>")}</td></tr>`
    : "";

  const plan = group("Manufacturing plan", [
    row("Quantity", a.quantity),
    row("Setups", a.setup_count),
    row("Costed operations", a.operation_count),
    setupList,
    row("Setup time", `${number(a.total_setup_time_minutes, 2)} min`, "charged once per setup"),
    row("Cycle time", `${number(a.total_cycle_time_minutes, 2)} min`, "per part"),
  ]);

  const additional = group("Additional costs", [
    row("Overhead", a.overhead_rate_percent == null ? null : `${number(a.overhead_rate_percent, 2)} %`),
    row("Scrap", a.scrap_rate_percent == null ? null : `${number(a.scrap_rate_percent, 2)} %`),
    row("Tooling", a.tooling_cost_per_batch == null ? null
      : money(a.tooling_cost_per_batch, a.currency), "per batch"),
  ]);

  return cad + material + rates + time + plan + additional;
}

// -------------------------------------------------------------- cost impact

/**
 * Show "why did this change?" against the previous estimate, if there is one.
 * Called after a new estimate is calculated.
 */
async function renderImpactAgainstPrevious(estimateId) {
  const previous = state.previousEstimateId;
  if (!previous || previous === estimateId) {
    $("impact-card").classList.add("hidden");
    return;
  }
  let comparison;
  try {
    comparison = await api(
      `/estimates/compare?baseline=${encodeURIComponent(previous)}` +
        `&comparison=${encodeURIComponent(estimateId)}`
    );
  } catch (error) {
    $("impact-card").classList.add("hidden");
    return;
  }
  $("impact-body").innerHTML = impactMarkup(comparison, {
    heading: `Compared with the previous estimate <span class="mono">${escapeHtml(previous)}</span>`,
  });
  $("impact-card").classList.remove("hidden");
}

function impactMarkup(comparison, options) {
  const { heading = "" } = options || {};
  const total = comparison.total_part_cost;
  const currency = comparison.comparison.currency;
  const sign = total.delta > 0 ? "+" : "";

  const headline =
    `<div class="impact-headline">` +
    `<span class="muted">${money(total.baseline, comparison.baseline.currency)}</span> → ` +
    `<strong>${money(total.comparison, currency)}</strong> ` +
    `<span class="impact-change ${total.delta >= 0 ? "up" : "down"}">` +
    `${sign}${money(total.delta, currency)}` +
    (total.percent_change == null ? "" : ` (${sign}${number(total.percent_change, 1)}%)`) +
    `</span></div>`;

  if (!comparison.currencies_match) {
    return (
      (heading ? `<p class="hint">${heading}</p>` : "") +
      `<div class="impact-warning">${escapeHtml(comparison.attribution_note)}</div>` +
      changesMarkup(comparison)
    );
  }

  const attribution =
    `<p class="attribution attribution-${escapeHtml(comparison.attribution)}">` +
    `${escapeHtml(comparison.attribution_note)}</p>`;

  const drivers = comparison.impacts.length
    ? `<table class="mini impacts"><tbody>` +
      comparison.impacts
        .map(
          (impact) =>
            `<tr class="impact-${escapeHtml(impact.category)}">` +
            `<td class="num ${impact.delta >= 0 ? "up" : "down"}">` +
            `${impact.delta > 0 ? "+" : ""}${money(impact.delta, impact.currency || currency)}</td>` +
            `<th>${escapeHtml(IMPACT_LABELS[impact.category] || impact.category)}</th>` +
            `<td class="muted">${escapeHtml(impact.description)}</td></tr>`
        )
        .join("") +
      `</tbody></table>`
    : `<p class="hint">No cost difference to account for.</p>`;

  return (
    (heading ? `<p class="hint">${heading}</p>` : "") +
    headline +
    attribution +
    `<h3>Cost drivers</h3>` +
    drivers +
    changesMarkup(comparison) +
    sequenceMarkup(comparison) +
    operationsMarkup(comparison)
  );
}

function changesMarkup(comparison) {
  if (!comparison.changes.length) return "";
  return (
    `<h3>What changed</h3><table class="mini"><tbody>` +
    comparison.changes
      .map(
        (change) =>
          `<tr><th>${escapeHtml(change.label)}</th>` +
          `<td><span class="muted">${escapeHtml(change.before ?? "—")}</span> → ` +
          `<strong>${escapeHtml(change.after ?? "—")}</strong>` +
          (change.affects_cost
            ? ""
            : ` <span class="badge">not priced by the current model</span>`) +
          `</td></tr>`
      )
      .join("") +
    `</tbody></table>`
  );
}

function sequenceMarkup(comparison) {
  const sequence = comparison.sequence;
  if (!sequence || !sequence.changed) return "";
  // Stated rather than hidden: making the limitation visible is the point.
  return (
    `<h3>Sequence</h3>` +
    `<table class="mini"><tbody>` +
    `<tr><th>Before</th><td class="mono small">${escapeHtml(sequence.before.join(" → "))}</td></tr>` +
    `<tr><th>After</th><td class="mono small">${escapeHtml(sequence.after.join(" → "))}</td></tr>` +
    `<tr><th>Cost impact</th><td>${money(sequence.cost_impact, comparison.comparison.currency)}</td></tr>` +
    `</tbody></table>` +
    `<p class="provenance">${escapeHtml(sequence.note)}</p>`
  );
}

function operationsMarkup(comparison) {
  const moved = comparison.operations.filter((o) => o.status !== "unchanged");
  if (!moved.length) return "";
  const currency = comparison.comparison.currency;
  return (
    `<h3>Operations</h3><table class="mini impacts"><thead><tr>` +
    `<th>Operation</th><th>Status</th><th class="num">Cycle</th><th class="num">Setup</th>` +
    `<th class="num">Cost</th><th>What changed</th></tr></thead><tbody>` +
    moved
      .map(
        (o) =>
          `<tr><td class="mono">${escapeHtml(o.operation_id)}<br>` +
          `<span class="muted small">${escapeHtml(o.operation_name || "")}</span></td>` +
          `<td><span class="pill pill-${o.status === "removed" ? "off" : "ok"}">` +
          `${escapeHtml(o.status)}</span></td>` +
          `<td class="num">${fromTo(o.cycle_before, o.cycle_after, "min")}</td>` +
          `<td class="num">${fromTo(o.setup_before, o.setup_after, "min")}</td>` +
          `<td class="num">${o.cost_delta == null ? "—" : (o.cost_delta > 0 ? "+" : "") + money(o.cost_delta, currency)}</td>` +
          `<td class="muted small">${escapeHtml(o.changes.join("; "))}</td></tr>`
      )
      .join("") +
    `</tbody></table>`
  );
}

function fromTo(before, after, unit) {
  if (before == null && after == null) return "—";
  if (before == null) return `<strong>${number(after, 2)}</strong> ${escapeHtml(unit)}`;
  if (after == null) return `<span class="muted">${number(before, 2)} ${escapeHtml(unit)}</span>`;
  if (before === after) return `${number(after, 2)} ${escapeHtml(unit)}`;
  return (
    `<span class="muted">${number(before, 2)}</span> → ` +
    `<strong>${number(after, 2)}</strong> ${escapeHtml(unit)}`
  );
}

// ------------------------------------------------- side-by-side assumptions

/**
 * The two estimates' assumptions in one table, differences marked. Used by the
 * history comparison, where seeing them beside each other is the point.
 */
function sideBySideAssumptions(comparison) {
  const a = comparison.baseline_assumptions;
  const b = comparison.comparison_assumptions;
  if (!a || !b) return "";

  const changed = new Set(comparison.changes.map((change) => change.field));
  const section = (title, fields) => {
    const rows = fields
      .map(([field, label, format]) => {
        const before = a[field];
        const after = b[field];
        if (before == null && after == null) return "";
        const render = format || ((v) => (v == null ? "—" : String(v)));
        return (
          `<tr class="${changed.has(field) ? "row-changed" : ""}">` +
          `<th>${escapeHtml(label)}</th>` +
          `<td>${escapeHtml(render(before))}</td>` +
          `<td>${escapeHtml(render(after))}</td></tr>`
        );
      })
      .filter(Boolean)
      .join("");
    return rows
      ? `<h3>${escapeHtml(title)}</h3><table class="mini compare-cols">` +
          `<thead><tr><th></th><th>A</th><th>B</th></tr></thead><tbody>${rows}</tbody></table>`
      : "";
  };

  const num = (digits) => (v) => (v == null ? "—" : number(v, digits));

  return (
    section("Geometry", [
      ["source_file", "CAD file"],
      ["geometry_source", "Geometry source"],
      ["volume_mm3", "Volume (mm³)", num(3)],
      ["mass_kg", "Mass (kg)", num(4)],
    ]) +
    section("Material", [
      ["material", "Material"],
      ["density_kg_m3", "Density (kg/m³)", num(0)],
      ["material_cost_per_kg", "Cost per kg", num(4)],
    ]) +
    section("Costing profile", [
      ["rate_set_id", "Profile"],
      ["rate_set_version", "Version"],
      ["currency", "Currency"],
    ]) +
    section("Time profile", [
      ["time_profile_id", "Profile"],
      ["time_profile_version", "Version"],
    ]) +
    section("Manufacturing plan", [
      ["quantity", "Quantity"],
      ["setup_count", "Setups"],
      ["operation_count", "Costed operations"],
      ["total_setup_time_minutes", "Setup minutes", num(2)],
      ["total_cycle_time_minutes", "Cycle minutes per part", num(2)],
    ]) +
    section("Additional costs", [
      ["overhead_rate_percent", "Overhead %", num(2)],
      ["scrap_rate_percent", "Scrap %", num(2)],
      ["tooling_cost_per_batch", "Tooling per batch", num(2)],
    ])
  );
}
