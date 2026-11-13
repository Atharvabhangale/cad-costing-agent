"use strict";

// Sheet metal: the part's thickness, bends and flat pattern, and the cut and bend
// lines they add to the plan.
//
// A classic script loaded after plan.js, process.js and stock.js, sharing `$`,
// `api`, `state`, `escapeHtml`, `number`, `showError` and plan.js's helpers.
// Wrapped in a function so it declares no top-level name another file could
// silently replace.
//
// Everything shown comes from `POST /cad/sheet-metal`: this file measures
// nothing, unfolds nothing and adds nothing up. The K-factor is the engineer's
// and is sent as typed; blank is sent as nothing, and the server then reports
// the flat size as not worked out rather than assuming one. The cut and bend
// lines come from `POST /operations/sheet-metal-lines` with their estimated
// times unaccepted — they reach a cost only once someone accepts or enters a
// time, on a route whose sheet stage is priced by time.

(function () {
  const sheet = { analysis: null };

  // The card belongs to a sheet-metal route: recommended, or chosen.
  function sheetRouteApplies() {
    const c = state.processClassification;
    if (!c) return false;
    return c.family === "sheet_metal" || c.system_family === "sheet_metal";
  }

  function sheetShowCard() {
    const show = sheetRouteApplies() && !state.planComponentId;
    $("sheet-card").classList.toggle("hidden", !show);
    $("sheet-measure").disabled = !state.file;
  }

  function sheetClear() {
    sheet.analysis = null;
    $("sheet-status").textContent =
      "Measure the part to see its thickness, bends and flat blank. Enter your K-factor first " +
      "for the flat size; none is assumed.";
    $("sheet-result").innerHTML = "";
    $("sheet-checks").innerHTML = "";
    $("sheet-add-lines").disabled = true;
    sheetStockClear();
    $("sheet-stock-block").classList.add("hidden");
  }

  // ---------------------------------------------------------------- sheet share
  //
  // The flat blank nested on the engineer's sheet. `/cad/sheet-stock/propose`
  // works out the nest, both orientations and the utilisation; this code shows
  // them as returned. The share is kept in `state.sheetStock` and sent with the
  // cost request; the backend costs material from it only once it is confirmed
  // through `/cad/sheet-stock/decide`, and re-measures the part before it does.

  function sheetStockClear() {
    state.sheetStock = null;
    $("sheet-nest").innerHTML = "";
    $("sheet-stock-confirm").disabled = true;
    $("sheet-stock-use").disabled = true;
    $("sheet-stock-withdraw").disabled = true;
  }

  function sheetStockRender(stock) {
    state.sheetStock = stock || null;
    if (!stock) {
      sheetStockClear();
      return;
    }
    const nest = stock.nest;
    const status = stock.confirmed
      ? stock.source === "user"
        ? `<span class="pill pill-ok">Your count — material costed from the sheet share</span>`
        : `<span class="pill pill-ok">Confirmed — material costed from the sheet share</span>`
      : `<span class="pill pill-off">Proposed — not used until confirmed</span>`;
    const rows = nest.orientations
      .map(
        (o) =>
          `<tr><td>${escapeHtml(o.label)}${o.orientation === nest.chosen_orientation
            ? ` <span class="muted">— the estimate's choice</span>` : ""}</td>` +
          `<td>${o.count_along} along × ${o.count_across} across</td>` +
          `<td><strong>${o.parts_per_sheet}</strong></td></tr>`
      )
      .join("");
    $("sheet-nest").innerHTML =
      `<p><strong>${escapeHtml(stock.description)}</strong> ${status}</p>` +
      `<table class="mini" style="width:auto"><thead><tr><th>Orientation</th><th>Grid</th>` +
      `<th>Parts per sheet</th></tr></thead><tbody>${rows}</tbody></table>` +
      `<p class="hint">Material per part ${number(stock.volume_mm3, 0)} mm³ — the sheet's volume ` +
      `shared by the ${stock.parts_per_sheet} parts on it` +
      (stock.source === "user"
        ? ` · the estimate was ${stock.system_parts_per_sheet} per sheet, ` +
          `${number(nest.utilisation_percent, 1)}% utilisation`
        : "") +
      `. ${escapeHtml(nest.choice_note)}</p>` +
      (stock.confirmed
        ? ""
        : `<p class="hint">Until you confirm it, material is costed from the finished part's mass,
            as before — which leaves out the spacing, the margins and the skeleton.</p>`) +
      `<ul class="declined-list">${nest.limitations.map((l) => `<li>${escapeHtml(l)}</li>`).join("")}</ul>` +
      `<details class="detail-toggle"><summary>How the nest was counted</summary>` +
      `<ul class="declined-list">${stock.basis.map((l) => `<li>${escapeHtml(l)}</li>`).join("")}</ul></details>`;
    $("sheet-parts").value = stock.parts_per_sheet;
    $("sheet-stock-confirm").disabled = stock.confirmed && stock.source === "system";
    $("sheet-stock-use").disabled = false;
    $("sheet-stock-withdraw").disabled = !stock.confirmed;
  }

  function sheetInput(id) {
    const raw = $(id).value.trim();
    return raw === "" ? null : Number(raw);
  }

  async function sheetNestRun() {
    if (!sheet.analysis || !sheet.analysis.flat_pattern) return;
    clearError();
    try {
      sheetStockRender(
        await api("/cad/sheet-stock/propose", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          // A blank field is sent as null: the server then asks for it rather
          // than assuming a sheet, a spacing or a margin.
          body: JSON.stringify({
            analysis: sheet.analysis,
            sheet_length_mm: sheetInput("sheet-length"),
            sheet_width_mm: sheetInput("sheet-width"),
            spacing_mm: sheetInput("sheet-spacing"),
            edge_margin_mm: sheetInput("sheet-margin"),
          }),
        })
      );
    } catch (error) {
      sheetStockClear();
      showError(error.message);
    }
  }

  async function sheetStockDecide(action, extra) {
    if (!state.sheetStock) return;
    clearError();
    try {
      sheetStockRender(
        await api("/cad/sheet-stock/decide", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ stock: state.sheetStock, action, ...(extra || {}) }),
        })
      );
    } catch (error) {
      showError(error.message);
    }
  }

  function sheetRender(a) {
    sheet.analysis = a;
    const heading = {
      sheet: "A constant-thickness sheet",
      not_sheet: "Not a sheet part",
      unsupported: "A sheet this measurement cannot unfold",
    }[a.status] || a.status;
    const reasons = a.reasons.length
      ? `<ul class="declined-list">${a.reasons.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}</ul>`
      : "";
    const bends = a.bends.length
      ? `<table class="mini" style="width:auto"><thead><tr><th>Bend</th><th>Angle</th><th>Inside radius</th>` +
        `<th>Length</th><th>Flanges</th><th>Bend allowance</th></tr></thead><tbody>` +
        a.bends
          .map(
            (b) =>
              `<tr><td class="mono">${escapeHtml(b.bend_id)}</td><td>${number(b.angle_deg, 2)}°</td>` +
              `<td>${number(b.inner_radius_mm, 3)} mm</td><td>${number(b.length_mm, 2)} mm</td>` +
              `<td class="mono">${escapeHtml(b.flanges.join(" – "))}</td>` +
              `<td>${b.bend_allowance_mm === null ? `<span class="muted">${escapeHtml(b.bend_allowance_formula)}</span>`
                : `${number(b.bend_allowance_mm, 3)} mm`}</td></tr>`
          )
          .join("") +
        `</tbody></table>`
      : a.status === "sheet"
      ? `<p class="hint">No bends: a flat blank.</p>`
      : "";
    const f = a.flat_pattern;
    const flat = f
      ? `<table class="mini"><tbody>` +
        `<tr><th>Flat blank (bounding rectangle)</th><td>${number(f.rectangle_length_mm, 2)} × ${number(f.rectangle_width_mm, 2)} mm ` +
        `<span class="muted">— ${escapeHtml(f.rectangle_orientation)}</span></td></tr>` +
        `<tr><th>Blank area</th><td>${number(f.net_area_mm2, 1)} mm² net of holes · ${number(f.outline_area_mm2, 1)} mm² inside the outline</td></tr>` +
        `<tr><th>Cut perimeter</th><td>${number(f.cut_perimeter_mm, 2)} mm ` +
        `<span class="muted">(outline ${number(f.outer_perimeter_mm, 2)} + inner contours ${number(f.inner_perimeter_mm, 2)})</span></td></tr>` +
        `<tr><th>Contours / pierces</th><td>${f.contour_count} / ${f.pierce_count}</td></tr>` +
        `<tr><th>K-factor</th><td>${number(f.k_factor, 3)} <span class="muted">(entered)</span></td></tr>` +
        `</tbody></table><p class="provenance">Self-check: ${escapeHtml(f.self_check)}.</p>`
      : a.status === "sheet"
      ? `<p class="warning-inline">${escapeHtml(a.flat_pattern_note || "No flat pattern.")}</p>` +
        `<p class="hint">Known without K: ${a.contour_count} contour(s), ${a.pierce_count} pierce(s), ` +
        `${number(a.flange_edge_cut_length_mm, 2)} mm of flange edges plus ${a.bend_end_count} bend end(s).</p>`
      : "";
    $("sheet-result").innerHTML =
      `<p><strong>${escapeHtml(heading)}</strong>` +
      (a.thickness_mm !== null ? ` — ${number(a.thickness_mm, 3)} mm thick` : "") +
      (a.status === "sheet" ? `, ${a.bend_count} bend${a.bend_count === 1 ? "" : "s"}` : "") +
      `</p>${reasons}${bends}${flat}`;
    $("sheet-checks").innerHTML =
      `<ul class="declined-list">${a.checks.map((c) => `<li>${escapeHtml(c)}</li>`).join("")}</ul>` +
      `<p class="hint">${escapeHtml(a.pierce_rule)}</p>` +
      a.notes.map((n) => `<p class="hint">${escapeHtml(n)}</p>`).join("");
    $("sheet-status").textContent =
      a.status === "sheet"
        ? "Measured from the exact B-Rep. Adding the lines proposes them with estimated times; nothing is committed until you accept a time."
        : "";
    $("sheet-add-lines").disabled = a.status !== "sheet";
    // A new measurement is a new blank: an earlier nest no longer describes it.
    sheetStockClear();
    $("sheet-stock-block").classList.toggle("hidden", !a.flat_pattern);
  }

  async function sheetMeasure() {
    if (!state.file) return;
    clearError();
    const body = new FormData();
    body.append("file", state.file);
    const k = $("sheet-k").value.trim();
    if (k !== "") body.append("k_factor", k);
    $("sheet-measure").disabled = true;
    $("sheet-status").textContent = "Measuring…";
    try {
      sheetRender(await api("/cad/sheet-metal", { method: "POST", body }));
    } catch (error) {
      $("sheet-status").textContent = "Could not measure this model as sheet metal.";
      showError(error.message);
    }
    $("sheet-measure").disabled = !state.file;
  }

  // The server's lines and setups go into the plan as returned. Lines with the
  // same ids — an earlier measurement's — are replaced, setups included, so a
  // re-measured blank starts unaccepted again.
  async function sheetAddLines() {
    if (!sheet.analysis || sheet.analysis.status !== "sheet") {
      $("sheet-card").classList.remove("hidden");
      $("sheet-status").textContent = "Measure the part as sheet metal first.";
      $("sheet-card").scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    let lines;
    try {
      lines = await api(`/operations/sheet-metal-lines${timeProfileQuery()}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ analysis: sheet.analysis }),
      });
    } catch (error) {
      showError(error.message);
      return;
    }
    if (!state.plan) {
      state.plan = {
        setups: [], operations: [], units: "mm", source_filename: state.file ? state.file.name : "",
        route_processes: decidedRouteProcesses(), as_formed_feature_types: decidedAsFormedTypes(),
      };
    }
    const operationIds = new Set(lines.operations.map((o) => o.operation_id));
    const setupIds = new Set(lines.setups.map((s) => s.setup_id));
    state.plan.operations = state.plan.operations.filter((o) => !operationIds.has(o.operation_id));
    state.plan.setups = (state.plan.setups || []).filter((s) => !setupIds.has(s.setup_id));
    const after = state.plan.setups.length;
    lines.setups.forEach((setup, index) => state.plan.setups.push({ ...setup, sequence_position: after + index + 1 }));
    lines.operations.forEach((operation) => state.plan.operations.push({ ...operation }));
    if (!state.plan.time_profile_id) {
      state.plan.time_profile_id = lines.time_profile_id;
      state.plan.time_profile_version = lines.time_profile_version;
      state.plan.time_profile_name = lines.time_profile_name;
    }
    state.planWarnings = lines.warnings || [];
    renumberSetups();
    renderPlan();
    $("sheet-status").textContent =
      `${lines.operations.length} line(s) added to the plan with estimated times, not accepted. ` +
      "Accept or enter their times in the operations table.";
  }

  $("sheet-measure").addEventListener("click", sheetMeasure);
  $("sheet-add-lines").addEventListener("click", sheetAddLines);
  $("sheet-nest-run").addEventListener("click", sheetNestRun);
  $("sheet-stock-confirm").addEventListener("click", () => sheetStockDecide("confirm"));
  $("sheet-stock-withdraw").addEventListener("click", () => sheetStockDecide("withdraw"));
  $("sheet-stock-use").addEventListener("click", () =>
    sheetStockDecide("edit", { parts_per_sheet: sheetInput("sheet-parts") })
  );
  // The route stage rows' "+ Add from flat pattern" buttons are drawn by plan.js.
  document.addEventListener("click", (event) => {
    if (event.target.closest("button.add-sheet-lines")) sheetAddLines();
  });

  const previousRender = typeof renderProcessClassification === "function" ? renderProcessClassification : null;
  renderProcessClassification = async function (classification) {
    if (previousRender) await previousRender(classification);
    sheetShowCard();
  };

  const previousReset = typeof resetFeaturesPanel === "function" ? resetFeaturesPanel : null;
  // Called by app.js on every file selection, so a previous file's blank never
  // sits beside the model now loaded.
  resetFeaturesPanel = function () {
    if (previousReset) previousReset();
    sheetClear();
    sheetShowCard();
  };

  sheetClear();
  sheetShowCard();
})();
