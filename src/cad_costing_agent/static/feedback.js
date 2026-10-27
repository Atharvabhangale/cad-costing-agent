// The Feedback & Learning panel.
//
// **Every figure here is one the server computed.** Not a rate, not a median,
// not a ratio, not a count — this page is almost entirely percentages, and a
// percentage invented in JavaScript beside a real one is indistinguishable from
// it. So the browser formats and lays out, and a test greps this file for
// arithmetic on any of the numeric fields.
//
// It is also read-only in the same sense the backend is: there is no request
// here that writes anything, because nothing in Step 29 may change a rule, a
// profile, a confidence value or an estimate. The disclaimer the API returns is
// printed at the top rather than paraphrased.
//
// A statistic with too small a sample is shown *with* that fact, never hidden
// and never rounded into looking solid: the guard the API attaches to each rate
// is what decides whether the number or the caveat is the prominent thing.

const FEEDBACK_TABS = [
  ["summary", "Summary"],
  ["operations", "Operations"],
  ["times", "Times"],
  ["setups", "Setups"],
  ["classifications", "Make / buy"],
  ["confidence", "Confidence"],
  ["evidence", "Evidence"],
];

state.feedback = {
  view: "summary",
  filters: {},
  data: null,
  drill: null,
};

// ------------------------------------------------------------------ formatting

function percent(rate) {
  // The server sends a fraction. Turning 0.91 into "91%" is presentation, not
  // arithmetic on a measurement — there is nothing here to get wrong except
  // the number of decimals.
  if (rate === null || rate === undefined) return "—";
  return `${(rate * 100).toFixed(0)}%`;
}

function guardNote(guard) {
  if (!guard) return "";
  if (guard.is_actionable) {
    return `<span class="tiny muted">${guard.sample_count} observation(s)</span>`;
  }
  return `<span class="tiny warn">${escapeHtml(
    guard.note || `${guard.sample_count} observation(s)`
  )} — ${guard.minimum_required} needed</span>`;
}

// A rate, its two counts, and whether it rests on enough evidence. The three
// always travel together: the whole failure mode of a page like this is a
// confident-looking percentage over four observations.
function rateRow(label, rate) {
  if (!rate) return "";
  return `
    <tr class="${rate.guard && rate.guard.is_actionable ? "" : "thin-evidence"}">
      <th>${escapeHtml(label)}</th>
      <td class="num">${percent(rate.rate)}</td>
      <td class="num tiny muted">${rate.numerator} / ${rate.denominator}${
    rate.excluded ? `<div>${rate.excluded} excluded</div>` : ""
  }</td>
      <td>${guardNote(rate.guard)}</td>
    </tr>`;
}

function minutes(value, digits = 2) {
  if (value === null || value === undefined) return "—";
  return `${number(value, digits)} min`;
}

function ratio(value) {
  if (value === null || value === undefined) return "—";
  return `${number(value, 2)}×`;
}

function query(extra) {
  const search = new URLSearchParams();
  Object.entries(state.feedback.filters).forEach(([key, value]) => {
    if (value !== "" && value !== null && value !== undefined) search.set(key, value);
  });
  Object.entries(extra || {}).forEach(([key, value]) => {
    if (value !== "" && value !== null && value !== undefined) search.set(key, value);
  });
  const text = search.toString();
  return text ? `?${text}` : "";
}

// ------------------------------------------------------------------ sections

function renderDisclaimer(body) {
  return `<div class="notice-learning">${escapeHtml(body.disclaimer)}</div>`;
}

function renderWindow(body) {
  if (!body.window) return "";
  return `<p class="provenance">${escapeHtml(body.window.note)}${
    body.minimum_sample
      ? ` A pattern is called actionable at ${body.minimum_sample} or more observations.`
      : ""
  }</p>`;
}

function renderSummary(body) {
  const counts = body.correction_counts || {};
  return `
    ${renderDisclaimer(body)}
    <div class="headline">
      <div class="headline-item"><span class="label">Reviewed operations</span>
        <span class="big">${body.reviewed_operations}</span></div>
      <div class="headline-item"><span class="label">Accepted unchanged</span>
        <span class="big">${percent(body.acceptance.rate)}</span></div>
      <div class="headline-item"><span class="label">Never finished</span>
        <span class="big">${body.not_finished}</span></div>
    </div>
    <p class="hint small">
      An operation nobody committed a time for was neither accepted nor
      corrected — it was never finished, usually because the rate set prices no
      rate for the process suggested. Those are excluded from the acceptance
      denominator rather than counted as rejections, and shown above so the
      figure can be read for what it is.
    </p>
    <table class="mini">
      <tbody>
        ${rateRow("Accepted without change", body.acceptance)}
        ${rateRow("Process changed", body.operation_correction)}
        ${rateRow("Cycle time changed", body.time_correction)}
        ${rateRow("Moved between setups", body.setup_correction)}
        ${rateRow("Classification changed", body.classification_correction)}
      </tbody>
    </table>
    <h3>Corrections by kind</h3>
    <table class="mini">
      <tbody>${Object.keys(counts)
        .filter((key) => counts[key])
        .map(
          (key) =>
            `<tr><th>${escapeHtml(key.replace(/_/g, " "))}</th><td class="num">${
              counts[key]
            }</td></tr>`
        )
        .join("")}</tbody>
    </table>
    <h3>Cycle time, system against engineer</h3>
    ${renderTimeStats(body.cycle_time)}
    <h3>Learning dataset readiness</h3>
    ${renderReadiness(body.readiness)}
    ${renderWindow(body)}`;
}

function renderTimeStats(stats) {
  if (!stats || !stats.samples) {
    return "<p class='muted small'>No paired predictions and decisions in this slice.</p>";
  }
  return `
    <table class="mini">
      <tbody>
        <tr><th>Samples</th><td class="num">${stats.samples}</td><td>${guardNote(
    stats.guard
  )}</td></tr>
        <tr><th>System median</th><td class="num">${minutes(stats.system_median)}</td><td></td></tr>
        <tr><th>Engineer median</th><td class="num">${minutes(stats.user_median)}</td><td></td></tr>
        <tr><th>Median ratio</th><td class="num">${ratio(stats.median_ratio)}</td>
            <td class="tiny muted">above 1 means the system under-estimates</td></tr>
        <tr><th>Median absolute error</th><td class="num">${minutes(
          stats.median_absolute_error
        )}</td><td></td></tr>
        <tr><th>Median signed error</th><td class="num">${minutes(
          stats.median_signed_error
        )}</td><td class="tiny muted">positive means under-estimated</td></tr>
        <tr><th>Median relative error</th><td class="num">${percent(
          stats.median_relative_error
        )}</td>
            <td class="tiny muted">${
              stats.near_zero_excluded
                ? `${stats.near_zero_excluded} near-zero prediction(s) excluded from this figure only`
                : ""
            }</td></tr>
        <tr><th>Outliers</th><td class="num">${stats.outlier_count}</td>
            <td class="tiny muted">counted here, listed separately</td></tr>
      </tbody>
    </table>`;
}

// Per-field evidence completeness.
//
// The four states are exhaustive and every count comes from the server. The
// distinction they carry is the whole substance of this view: "not applicable"
// is not a gap, and "unrecoverable" is a permanent limitation rather than work
// outstanding — so the two are never shown as one number.
function renderEvidenceTable(readiness) {
  const fields = (readiness && readiness.evidence) || [];
  if (!fields.length) return "";
  return `
    <table class="mini bom">
      <thead><tr><th>Evidence</th><th class="num">Known</th><th class="num">Of</th>
                 <th class="num">Known %</th><th class="num">Not applicable</th>
                 <th class="num">Recoverable</th><th class="num">Permanent</th>
                 <th></th></tr></thead>
      <tbody>${fields
        .map(
          (field) => `
          <tr class="${field.guard.is_actionable ? "" : "thin-evidence"}">
            <td>${escapeHtml(field.label)}
              <div class="tiny muted">${escapeHtml(field.source_of_truth)}</div></td>
            <td class="num">${field.known}</td>
            <td class="num">${field.applicable}</td>
            <td class="num">${percent(field.percent_known)}</td>
            <td class="num muted">${field.not_applicable}</td>
            <td class="num">${field.unrecorded}</td>
            <td class="num ${field.unrecoverable ? "warn" : "muted"}">${
            field.unrecoverable
          }</td>
            <td>${guardNote(field.guard)}</td>
          </tr>
          <tr class="reason-row"><td colspan="8" class="tiny muted">Of ${escapeHtml(
            field.basis
          )}.${field.note ? ` ${escapeHtml(field.note)}` : ""}</td></tr>`
        )
        .join("")}</tbody>
    </table>
    <p class="hint small">
      <strong>Not applicable</strong> means the value cannot exist for that
      record — a standalone part has no component, an operation nobody finished
      has no paired time — so it is excluded from the percentage rather than
      counted as a gap. <strong>Permanent</strong> means it was never captured
      and nothing stored can recover it: the CAD file is not kept, so a feature
      confidence that was not written down is gone. Neither is ever filled with
      a guess.
    </p>`;
}

function renderEvidence(body) {
  const readiness = body.readiness || {};
  return `
    ${renderDisclaimer(body)}
    <div class="headline">
      <div class="headline-item"><span class="label">Feedback records</span>
        <span class="big">${readiness.total_records}</span></div>
      <div class="headline-item"><span class="label">Recoverable gaps</span>
        <span class="big">${readiness.safe_backfill_available}</span></div>
      <div class="headline-item"><span class="label">Permanently unknown</span>
        <span class="big">${readiness.permanently_unknown}</span></div>
    </div>
    <h3>Evidence completeness</h3>
    ${renderEvidenceTable(readiness)}
    <h3>Learning dataset readiness</h3>
    ${renderReadiness(readiness)}
    ${renderWindow(body)}`;
}

function renderReadiness(readiness) {
  if (!readiness) return "";
  const rows = [
    ["Total feedback records", readiness.total_records],
    ["With a system prediction and a user outcome", readiness.with_system_prediction_and_user_outcome],
    ["With a usable time correction", readiness.with_usable_time_correction],
    ["With a usable operation correction", readiness.with_usable_operation_correction],
    ["With component identity", readiness.with_component_identity],
    ["With feature identity", readiness.with_feature_identity],
    ["With a rule id", readiness.with_rule_id],
    ["With a known source type", readiness.with_known_source_type],
    ["With a known feature confidence", readiness.with_known_feature_confidence],
    ["With a time-profile version", readiness.with_time_profile_version],
    ["Potential training rows", readiness.potential_training_rows],
    ["Insufficient rows", readiness.insufficient_rows],
  ];
  return `
    <table class="mini">
      <tbody>${rows
        .map(([label, value]) => `<tr><th>${label}</th><td class="num">${value}</td></tr>`)
        .join("")}</tbody>
    </table>
    <h3>Missing where the field could have existed</h3>
    <table class="mini">
      <tbody>${Object.entries(readiness.missing_fields || {})
        .map(
          // `name`, not `field`: an `EvidenceField` is also called `field` in
          // this file, and reusing the word for a plain string made a
          // field-name check read `field.replace` as an API field.
          ([name, count]) =>
            `<tr><th>${escapeHtml(name.replace(/_/g, " "))}</th><td class="num">${count}</td></tr>`
        )
        .join("")}</tbody>
    </table>
    <p class="hint small">Records the field does not apply to are excluded from these
      counts. The <strong>Evidence</strong> tab carries the full breakdown.</p>
    <p class="provenance">${escapeHtml(readiness.note)}</p>`;
}

// Named apart from setup.js's renderOperations: every classic script shares one
// global scope, and the later declaration silently replaced the rate-set editor's.
function renderOperationAnalytics(body) {
  const cells = body.confusion || [];
  return `
    ${renderDisclaimer(body)}
    <table class="mini">
      <tbody>${rateRow("Engineer kept the suggested process", body.accuracy)}</tbody>
    </table>
    <h3>System → engineer</h3>
    <table class="mini bom">
      <thead><tr><th>System suggested</th><th>Engineer chose</th><th class="num">Count</th>
                 <th></th></tr></thead>
      <tbody>${cells
        .map(
          (cell) => `
          <tr class="${cell.changed ? "row-uncosted" : ""}">
            <td>${escapeHtml(cell.system_operation)}</td>
            <td>${escapeHtml(cell.user_operation)}</td>
            <td class="num">${cell.count}</td>
            <td>${
              cell.changed
                ? `<button type="button" class="link drill" data-system="${escapeHtml(
                    cell.system_operation
                  )}" data-user="${escapeHtml(cell.user_operation)}">see the records</button>`
                : ""
            }</td>
          </tr>`
        )
        .join("")}</tbody>
    </table>
    <h3>Rule performance</h3>
    <table class="mini bom">
      <thead><tr><th>Rule</th><th class="num">Suggested</th><th class="num">Accepted</th>
                 <th class="num">Changed</th><th class="num">Unfinished</th>
                 <th class="num">Acceptance</th><th></th></tr></thead>
      <tbody>${(body.rules || [])
        .map(
          (rule) => `
          <tr>
            <td class="mono small">${escapeHtml(rule.rule_id)}</td>
            <td class="num">${rule.suggestions}</td>
            <td class="num">${rule.accepted}</td>
            <td class="num">${rule.changed}</td>
            <td class="num muted">${rule.not_finished}</td>
            <td class="num">${percent(rule.acceptance.rate)}</td>
            <td>${guardNote(rule.acceptance.guard)}</td>
          </tr>
          ${
            Object.keys(rule.became || {}).length
              ? `<tr class="reason-row"><td colspan="7" class="tiny muted">changed to ${Object.entries(
                  rule.became
                )
                  .map(([process, count]) => `${escapeHtml(process)} ×${count}`)
                  .join(", ")}</td></tr>`
              : ""
          }`
        )
        .join("")}</tbody>
    </table>
    ${renderWindow(body)}
    <div id="feedback-drill"></div>`;
}

function renderTimes(body) {
  return `
    ${renderDisclaimer(body)}
    <h3>Overall</h3>
    ${renderTimeStats(body.overall)}
    <h3>By process</h3>
    <table class="mini bom">
      <thead><tr><th>Process</th><th class="num">Samples</th><th class="num">System</th>
                 <th class="num">Engineer</th><th class="num">Ratio</th><th>Finding</th></tr></thead>
      <tbody>${(body.by_operation || [])
        .map(
          (finding) => `
          <tr class="${finding.direction === "insufficient_data" ? "thin-evidence" : ""}">
            <td>${escapeHtml(finding.group)}</td>
            <td class="num">${finding.stats.samples}</td>
            <td class="num">${minutes(finding.stats.system_median)}</td>
            <td class="num">${minutes(finding.stats.user_median)}</td>
            <td class="num">${ratio(finding.stats.median_ratio)}</td>
            <td class="small">${escapeHtml(finding.statement)}</td>
          </tr>`
        )
        .join("")}</tbody>
    </table>
    <h3>By time-profile version</h3>
    <table class="mini bom">
      <thead><tr><th>Version</th><th class="num">Reviewed</th><th class="num">Time samples</th>
                 <th class="num">Median ratio</th><th></th></tr></thead>
      <tbody>${(body.by_time_profile || [])
        .map(
          (entry) => `
          <tr>
            <td>${escapeHtml(entry.group)}</td>
            <td class="num">${entry.reviewed}</td>
            <td class="num">${entry.time.samples}</td>
            <td class="num">${ratio(entry.time.median_ratio)}</td>
            <td>${guardNote(entry.time.guard)}</td>
          </tr>`
        )
        .join("")}</tbody>
    </table>
    <p class="hint small">A lower correction rate on a newer version is not evidence it is
      better unless the sample sizes support that. The counts are shown so you can judge.</p>
    <h3>Outliers</h3>
    <table class="mini bom">
      <thead><tr><th>Operation</th><th>Component</th><th>Feature</th><th class="num">System</th>
                 <th class="num">Engineer</th><th class="num">Ratio</th></tr></thead>
      <tbody>${(body.outliers || [])
        .map(
          (item) => `
          <tr>
            <td class="mono small">${escapeHtml(item.operation_id)}</td>
            <td>${escapeHtml(item.component_name || "—")}</td>
            <td class="mono small">${escapeHtml(item.feature_id || "—")}</td>
            <td class="num">${minutes(item.system_cycle_time)}</td>
            <td class="num">${minutes(item.user_cycle_time)}</td>
            <td class="num">${ratio(item.ratio)}</td>
          </tr>`
        )
        .join("")}</tbody>
    </table>
    <p class="provenance">${escapeHtml(body.outlier_note)}</p>
    ${renderWindow(body)}`;
}

function renderSetups(body) {
  return `
    <table class="mini">
      <tbody>
        ${rateRow("Left in the setup the system chose", body.grouping_accepted)}
        ${rateRow("Moved to another setup", body.moved_between_setups)}
        ${rateRow("Setup time changed", body.setup_time_changed)}
        ${rateRow("One assumed setup became several", body.split_rate)}
      </tbody>
    </table>
    <table class="mini">
      <tbody>
        <tr><th>Reviewed operations</th><td class="num">${body.reviewed_operations}</td></tr>
        <tr><th>Estimates the system gave one setup</th>
            <td class="num">${body.estimates_with_one_system_setup}</td></tr>
        <tr><th>…of those, split into more</th>
            <td class="num">${body.estimates_split_into_more}</td></tr>
      </tbody>
    </table>
    <p class="provenance">${escapeHtml(body.note)}</p>`;
}

function renderClassifications(body) {
  return `
    <table class="mini">
      <tbody>
        <tr><th>Components reviewed</th><td class="num">${body.reviewed}</td></tr>
        ${rateRow("Engineer chose differently", body.changed)}
      </tbody>
    </table>
    <h3>System → engineer</h3>
    <table class="mini bom">
      <thead><tr><th>System suggested</th><th>Engineer chose</th><th class="num">Count</th>
                 <th>Examples</th></tr></thead>
      <tbody>${(body.pairs || [])
        .map(
          (pair) => `
          <tr class="${
            pair.system_classification === pair.user_classification ? "" : "row-uncosted"
          }">
            <td><span class="chip class-${escapeHtml(
              pair.system_classification
            )}">${escapeHtml(pair.system_classification)}</span></td>
            <td><span class="chip class-${escapeHtml(pair.user_classification)}">${escapeHtml(
            pair.user_classification
          )}</span></td>
            <td class="num">${pair.count}</td>
            <td class="tiny muted">${escapeHtml((pair.examples || []).join(", "))}</td>
          </tr>`
        )
        .join("")}</tbody>
    </table>
    <p class="provenance">${escapeHtml(body.note)}</p>`;
}

function renderConfidence(body) {
  const table = (title, groups, note) => `
    <h3>${title}</h3>
    <table class="mini bom">
      <thead><tr><th>Band</th><th class="num">Reviewed</th><th class="num">Accepted</th>
                 <th class="num">Unfinished</th><th class="num">Acceptance</th>
                 <th></th></tr></thead>
      <tbody>${(groups || [])
        .map(
          (group) => `
          <tr class="${group.acceptance.guard.is_actionable ? "" : "thin-evidence"}">
            <td>${escapeHtml(group.group)}</td>
            <td class="num">${group.reviewed}</td>
            <td class="num">${group.accepted}</td>
            <td class="num muted">${group.not_finished}</td>
            <td class="num">${percent(group.acceptance.rate)}</td>
            <td>${guardNote(group.acceptance.guard)}</td>
          </tr>`
        )
        .join("")}</tbody>
    </table>
    ${note ? `<p class="hint small">${note}</p>` : ""}`;

  return `
    ${renderDisclaimer(body)}
    ${table("Operation-suggestion confidence", body.by_operation_confidence)}
    ${table("Feature-recognition confidence", body.by_feature_confidence)}
    ${table("Time-estimate confidence", body.by_time_confidence)}
    ${table(
      "Evidence type",
      body.by_source_type,
      "B-Rep and faceted evidence are never combined: a feature reconstructed from polygons is weaker evidence than one read from an exact surface."
    )}
    <p class="provenance">${escapeHtml(body.calibration_note)}</p>
    ${renderWindow(body)}`;
}

// ------------------------------------------------------------------ drill-down

async function drillInto(system, user) {
  const panel = $("feedback-drill");
  panel.innerHTML = "<p class='muted'>Loading the records…</p>";
  try {
    const body = await api(
      `/feedback/records${query({
        system_operation: system,
        user_operation: user,
        limit: 25,
      })}`
    );
    panel.innerHTML = `
      <h3>${escapeHtml(system)} → ${escapeHtml(user)} · ${body.total} correction(s)</h3>
      <table class="mini bom">
        <thead><tr><th>Estimate</th><th>Component</th><th>Feature</th><th>Operation</th>
                   <th class="num">System</th><th class="num">Engineer</th>
                   <th>Rule</th><th>Evidence</th></tr></thead>
        <tbody>${body.items
          .map(
            (record) => `
            <tr>
              <td class="mono tiny">${escapeHtml((record.estimate_id || "").slice(0, 8))}</td>
              <td>${escapeHtml(record.component_name || "—")}</td>
              <td class="mono small">${escapeHtml(record.feature_id || "—")}</td>
              <td class="mono small">${escapeHtml(record.operation_id)}</td>
              <td class="num">${minutes(record.system_cycle_time)}</td>
              <td class="num">${minutes(record.user_cycle_time)}</td>
              <td class="mono tiny">${escapeHtml(record.rule_id || "—")}</td>
              <td class="tiny">${escapeHtml(record.source_type)}${
              record.source_type_derived ? " (derived)" : ""
            }</td>
            </tr>`
          )
          .join("")}</tbody>
      </table>
      <p class="hint small">Showing ${body.items.length} of ${body.total}. Every value is the
        one recorded when the estimate was made.</p>`;
  } catch (error) {
    panel.innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
  }
}

// ------------------------------------------------------------------ loading

const FEEDBACK_RENDERERS = {
  summary: renderSummary,
  evidence: renderEvidence,
  operations: renderOperationAnalytics,
  times: renderTimes,
  setups: renderSetups,
  classifications: renderClassifications,
  confidence: renderConfidence,
};

async function loadFeedback() {
  const view = state.feedback.view;
  const body = $("feedback-body");
  body.innerHTML = "<p class='muted'>Reading the recorded corrections…</p>";
  try {
    const data = await api(`/feedback/${view}${query()}`);
    state.feedback.data = data;
    body.innerHTML = FEEDBACK_RENDERERS[view](data);
    body.querySelectorAll("button.drill").forEach((button) =>
      button.addEventListener("click", () =>
        drillInto(button.dataset.system, button.dataset.user)
      )
    );
  } catch (error) {
    body.innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
  }
}

function renderFeedbackTabs() {
  $("feedback-tabs").innerHTML = FEEDBACK_TABS.map(
    ([id, label]) =>
      `<button type="button" class="secondary tiny feedback-tab${
        state.feedback.view === id ? " picked" : ""
      }" data-view="${id}">${label}</button>`
  ).join("");
  $("feedback-tabs")
    .querySelectorAll("button.feedback-tab")
    .forEach((button) =>
      button.addEventListener("click", () => {
        state.feedback.view = button.dataset.view;
        renderFeedbackTabs();
        loadFeedback();
      })
    );
}

$("feedback-apply").addEventListener("click", () => {
  state.feedback.filters = {
    component: $("f-component").value.trim(),
    operation_type: $("f-operation").value.trim(),
    source_type: $("f-source").value,
    confidence: $("f-confidence").value,
    material: $("f-material").value.trim(),
    time_profile_version: $("f-profile").value.trim(),
    minimum_sample: $("f-minimum").value,
    corrected_only: $("f-corrected").checked ? "true" : "",
  };
  loadFeedback();
});

$("feedback-reset").addEventListener("click", () => {
  // `f-minimum` included: leaving a threshold of 100000 behind after a reset
  // would keep every figure labelled as thin evidence with nothing on screen
  // saying why.
  ["f-component", "f-operation", "f-material", "f-profile", "f-minimum"].forEach((id) => {
    $(id).value = "";
  });
  $("f-source").value = "";
  $("f-confidence").value = "";
  $("f-corrected").checked = false;
  state.feedback.filters = {};
  loadFeedback();
});

// Loaded on demand, so opening the application does not read the whole history.
function openFeedback() {
  renderFeedbackTabs();
  if (!state.feedback.data) loadFeedback();
}
