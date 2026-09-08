/* Estimate sections: Overview → CAD → Manufacturing → Cost → Assembly → Quote.
 *
 * This file only decides *where* things are shown. Every card it reveals was
 * already in the page and is still owned by the file that has always owned it —
 * `app.js` fills the geometry table, `features.js` the feature list, `plan.js`
 * the operations table. Nothing here renders a domain object of its own.
 *
 * The overview panel deliberately mirrors strings that are *already on the
 * page* rather than reformatting the API responses a second time. A summary
 * that re-derives its own figures is a summary that can disagree with the
 * detail it summarises, and this application's whole discipline is that a
 * number has one source. So the headline cost is read from the result panel
 * `app.js` filled, and the geometry figures from the table it filled — the
 * browser measures nothing and adds nothing up here either.
 *
 * Loaded last, so every helper it borrows (`plan.js`'s time-resolution rule in
 * particular) already exists. It still guards each borrow, the way `app.js`
 * guards its forward calls.
 */

const FLOWS = ["overview", "cad", "manufacturing", "cost", "assembly", "quote"];

function flowSection() {
  return document.getElementById("tab-estimate");
}

/** Move to a section. Idempotent, so the router can also be used to refresh. */
function setFlow(name) {
  if (!FLOWS.includes(name)) return;
  const section = flowSection();
  if (!section) return;

  section.dataset.flow = name;
  section.querySelectorAll(".flow-tab").forEach((tab) =>
    tab.classList.toggle("active", tab.dataset.flow === name)
  );
  section.querySelectorAll(".flow-pane").forEach((pane) =>
    pane.classList.toggle("hidden", pane.dataset.pane !== name)
  );

  // Re-read on every switch, so a section is never showing a figure that an
  // edit in another section has already moved past.
  refreshFlowChrome();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* ---------------------------------------------------------------- overview */

function statRow(label, value, note) {
  if (value === null || value === undefined || value === "" || value === "—") return "";
  return `<div class="stat">
    <span class="label">${escapeHtml(label)}</span>
    <strong>${escapeHtml(String(value))}</strong>
    ${note ? `<span class="stat-note">${escapeHtml(note)}</span>` : ""}
  </div>`;
}

/** The text `app.js` already put in an element, or null if it has not yet.
 *
 * A placeholder is an absence, not a value: `app.js` writes an em dash, on its
 * own or followed by why the figure does not exist yet ("calculate an estimate
 * to derive it"). Repeating that inside a summary tile reads as data.
 */
function shown(id) {
  const element = document.getElementById(id);
  if (!element) return null;
  const text = element.textContent.trim();
  if (!text || text.startsWith("—")) return null;
  return text;
}

/** Whether a calculated cost describes what is currently loaded.
 *
 * `app.js` hides the result panel when a new file is selected, so its
 * visibility is the signal for "there is a current cost". Reading `state.result`
 * instead would show the previous file's total in the context bar.
 */
function hasCurrentResult() {
  const panel = document.getElementById("result");
  return !!panel && !panel.classList.contains("hidden");
}

/** Whether this upload is an assembly at all.
 *
 * `/cad/import` declares component structure at identification, before anything
 * is decomposed, and that is the honest answer from the first second. Asking
 * only whether a decomposition exists would label a 102-component export a
 * "single part" until someone pressed a button.
 */
function isAssemblyUpload() {
  if (state.assembly && (state.assembly.definitions || []).length) return true;
  return !!state.document && state.document.structure_state === "structure_available";
}

/** How many planned operations still have no committed time.
 *
 * `plan.js` owns the three-branch rule that decides which time counts; this
 * asks it rather than repeating it, exactly as `assembly.js` does.
 */
function uncostedCount() {
  if (typeof activeOperations !== "function" || typeof isCostable !== "function") return null;
  const operations = activeOperations();
  if (!operations || !operations.length) return null;
  return operations.filter((operation) => !isCostable(operation)).length;
}

function renderOverview() {
  const summary = document.getElementById("overview-summary");
  const empty = document.getElementById("overview-empty");
  if (!summary) return;

  const document_ = state.document;
  if (!document_) {
    summary.classList.add("hidden");
    summary.innerHTML = "";
    if (empty) empty.classList.remove("hidden");
    renderOverviewWarnings();
    renderNextAction();
    return;
  }
  if (empty) empty.classList.add("hidden");

  const isAssembly = isAssemblyUpload();
  const current = authoritativeEstimate();
  const featureCount = state.features ? (state.features.features || []).length : null;
  const componentCount = state.assembly ? (state.assembly.occurrences || []).length : null;

  summary.innerHTML = [
    statRow(isAssembly ? "Assembly" : "Part", $("part_name").value || $("part_id").value),
    // The quantity that belongs to the figures below it. For an assembly the
    // part form's quantity is not the number of assemblies, and printing it
    // beside a per-assembly total invites multiplying the two.
    current
      ? statRow(current.quantityLabel, current.quantity)
      : statRow("Quantity", $("quantity").value),
    statRow("Material", $("material").selectedOptions[0]?.textContent),
    statRow("Bounding box", shown("mi-bbox")),
    statRow("Volume", shown("mi-volume")),
    statRow("Mass", shown("mi-mass")),
    componentCount === null ? "" : statRow("Components", componentCount),
    featureCount === null ? "" : statRow("Recognized features", featureCount),
    statRow(current ? current.unitLabel : "Unit cost", current ? current.unitCost : null),
    statRow("Total cost", current ? current.total : null),
  ]
    .filter(Boolean)
    .join("");
  summary.classList.remove("hidden");

  renderOverviewWarnings();
  renderNextAction();
}

/** Warnings worth interrupting for, and nothing else. */
function renderOverviewWarnings() {
  const host = document.getElementById("overview-warnings");
  if (!host) return;

  const warnings = [];

  if (state.gap && !state.completion) {
    warnings.push(
      `<strong>Incomplete CAD geometry.</strong> This file does not enclose a solid, so no
       volume can be measured from it. Supply one below and it is recorded as your
       assumption — never as a measurement.`
    );
  }
  if (state.completion) {
    warnings.push(
      `<strong>Volume assumed, not measured from CAD.</strong> This estimate rests on a
       figure you supplied.`
    );
  }
  if (state.rateSet && state.rateSet.is_illustrative) {
    warnings.push(
      `<strong>Illustrative rates.</strong> The selected rate set holds placeholder
       figures, not your shop's. Publish your own before quoting anything real.`
    );
  }
  const uncosted = uncostedCount();
  if (uncosted) {
    warnings.push(
      `<strong>${uncosted} operation${uncosted === 1 ? "" : "s"} not costed.</strong>
       An operation with no committed cycle time is left out of the total rather than
       counted as no work. Accept or enter its time in <em>Manufacturing</em>.`
    );
  }

  host.innerHTML = warnings
    .map((text) => `<p class="warning-inline">${text}</p>`)
    .join("");
}

/** One recommended next step, so the workflow never dead-ends. */
function renderNextAction() {
  const host = document.getElementById("overview-next");
  if (!host) return;

  const step = nextStep();
  if (!step) {
    host.classList.add("hidden");
    host.innerHTML = "";
    return;
  }
  host.classList.remove("hidden");
  host.innerHTML = `<span class="label">Next</span>
    <span class="next-text">${escapeHtml(step.text)}</span>
    <button type="button" class="secondary tiny" data-goto="${step.flow}">
      Go to ${escapeHtml(step.where)}
    </button>`;
  host.querySelector("[data-goto]").addEventListener("click", (event) =>
    setFlow(event.currentTarget.dataset.goto)
  );
}

function nextStep() {
  if (!state.document) return null;

  // `capabilities` is a list of reports, each naming its capability — the same
  // shape `assembly.js` reads. Treating it as a keyed object silently made
  // every capability look supported.
  const report = (state.document.capabilities || []).find(
    (entry) => entry.capability === "features"
  );
  const featuresUsable = !report || report.level !== "unsupported";

  if (state.gap && !state.completion) {
    return { text: "Supply the missing volume, or re-export as a solid.", flow: "overview", where: "the geometry panel" };
  }
  // An assembly's estimate comes from its components. Whole-file feature
  // analysis is available and sometimes interesting, but it is not the way to
  // an assembly's cost, so it must not be the recommended next step.
  if (isAssemblyUpload()) {
    if (!state.assembly) {
      return { text: "Decompose the file into its components.", flow: "assembly", where: "Assembly" };
    }
    if (!authoritativeEstimate()) {
      return {
        text: state.assemblyCostStale
          ? "The roll-up is out of date. Cost every component again."
          : "Cost every component to produce the assembly estimate.",
        flow: "assembly",
        where: "Assembly",
      };
    }
    return { text: "Produce the quotation for this assembly.", flow: "quote", where: "Quote" };
  }
  if (featuresUsable && !state.features) {
    return { text: "Analyse the model to see the features it contains.", flow: "cad", where: "CAD" };
  }
  if (state.features && !state.suggestions) {
    return { text: "Suggest operations from the recognized features.", flow: "manufacturing", where: "Manufacturing" };
  }
  if (uncostedCount()) {
    return { text: "Accept or enter a cycle time for each operation.", flow: "manufacturing", where: "Manufacturing" };
  }
  // An assembly's estimate is its roll-up, so this must not send someone to
  // press Calculate — that costs the whole file as one solid part and answers
  // a question nobody asked.
  if (!state.result) {
    return { text: "Calculate the estimate.", flow: "cost", where: "Cost" };
  }
  return { text: "Produce the quotation for this estimate.", flow: "quote", where: "Quote" };
}

/* -------------------------------------------- the estimate that is current */

/** The one stored estimate that describes what is currently loaded.
 *
 * A file can have produced two estimates that answer different questions.
 * `/cost/cad-part` costs the whole upload as a single solid; `/cost/assembly`
 * costs an assembly component by component from each component's own plan.
 * Both are saved, both are immutable, and for an assembly only the second one
 * means anything: the first reported a 20-piece bearing assembly as material
 * with no operations at all, because there is no manufacturing plan for the
 * file as one lump.
 *
 * So for an assembly the roll-up is the answer or there is no answer. There is
 * deliberately no fall-back to the part estimate here — a competing estimate
 * for the same file must never stand in for the roll-up, and a roll-up whose
 * inputs have moved must never be quoted at all. The part estimate is still
 * saved and still quotable from History, where choosing it is a decision
 * rather than an accident.
 *
 * Every figure is a string the backend sent, formatted by the shared `money`
 * helper. Nothing on this screen multiplies anything.
 */
function authoritativeEstimate() {
  if (isAssemblyUpload()) {
    const rollUp = state.assemblyCost;
    if (!rollUp || !rollUp.estimate_id) return null;
    return {
      kind: "assembly",
      id: rollUp.estimate_id,
      subjectLabel: "Assembly",
      subject: rollUp.assembly_name || $("part_name").value,
      quantityLabel: "Assemblies to build",
      quantity: rollUp.assembly_quantity,
      // `total_cost` is one assembly and `production_total_cost` is the run.
      // The multiplication between them happened once, on the server.
      unitLabel: "Per assembly",
      unitCost:
        rollUp.total_cost === null || rollUp.total_cost === undefined
          ? null
          : money(rollUp.total_cost, rollUp.currency),
      total:
        rollUp.production_total_cost === null || rollUp.production_total_cost === undefined
          ? null
          : money(rollUp.production_total_cost, rollUp.currency),
      incomplete: rollUp.is_complete === false,
      note: rollUp.completeness_note || "",
    };
  }
  if (hasCurrentResult() && state.result && state.result.part_cost) {
    return {
      kind: "part",
      id: state.result.part_cost.estimate_id,
      subjectLabel: "Part",
      subject: $("part_name").value,
      quantityLabel: "Quantity",
      quantity: $("quantity").value,
      unitLabel: "Unit cost",
      unitCost: shown("r-unit"),
      total: shown("r-total"),
      incomplete: false,
      note: "",
    };
  }
  return null;
}

/** Why there is no current estimate, phrased as what to do about it. */
function estimateGuidance() {
  if (!state.document) return "Upload a CAD model to begin.";
  if (!isAssemblyUpload()) {
    return "Calculate the estimate in Cost, and it can be issued as a document.";
  }
  if (state.assemblyCostStale) {
    return `A classification changed, so the assembly roll-up no longer matches these
      components and has been discarded. Cost them again in Assembly — an out-of-date
      roll-up is not shown here and not quoted.`;
  }
  return `This file is an assembly, and its estimate is the component roll-up. Classify the
    components in Assembly and choose <em>Cost every component</em>.`;
}

/** A button that moves to another section, wired after it is written. */
function wireGoto(host) {
  host.querySelectorAll("[data-goto]").forEach((button) =>
    button.addEventListener("click", (event) => setFlow(event.currentTarget.dataset.goto))
  );
}

/* -------------------------------------------------------------------- cost */

/** Which estimate the Cost section is actually looking at.
 *
 * The result panel below this banner belongs to `/cost/cad-part` and always
 * has. For a single part that is the estimate, and the banner stays out of the
 * way entirely. For an assembly it is a second opinion about a different
 * question, and showing it unlabelled beside a roll-up is what let a
 * material-only figure read as the cost of an assembly.
 */
function renderCostAuthority() {
  const host = document.getElementById("cost-authority");
  if (!host) return;

  if (!isAssemblyUpload()) {
    host.classList.add("hidden");
    host.innerHTML = "";
    return;
  }

  const target = authoritativeEstimate();
  host.classList.remove("hidden");

  if (!target) {
    host.innerHTML = `<p class="warning-inline">${estimateGuidance()}
      <button type="button" class="secondary tiny" data-goto="assembly">Go to Assembly</button></p>
      ${hasCurrentResult() ? whatTheResultPanelIs(null) : ""}`;
    wireGoto(host);
    return;
  }

  // The note leads with "Partial assembly estimate — …" itself, so it is shown
  // as written rather than introduced again in front of its own first words.
  const partial = target.incomplete
    ? `<p class="warning-inline">${escapeHtml(target.note)}</p>`
    : "";

  host.innerHTML = `
    <p class="hint"><strong>This file's estimate is the assembly roll-up</strong> — every
      component's contribution, costed from its own plan. It is what <em>Quote</em> issues${
        target.incomplete ? ", once it is complete" : ""
      }.</p>
    ${partial}
    <div class="stat-grid">
      ${statRow("Assembly", target.subject)}
      ${statRow(target.incomplete ? "Per assembly (partial)" : target.unitLabel, target.unitCost)}
      ${statRow(target.quantityLabel, target.quantity)}
      ${statRow("Total", target.total)}
    </div>
    <p class="provenance">Estimate <span class="mono">${escapeHtml(target.id)}</span> (assembly).
      <button type="button" class="secondary tiny" data-goto="assembly">See the components</button>
      <button type="button" class="secondary tiny" data-goto="quote">Go to Quote</button></p>
    ${hasCurrentResult() ? whatTheResultPanelIs(target) : ""}`;
  wireGoto(host);
}

/** Said out loud, because the panel underneath looks exactly like an answer. */
function whatTheResultPanelIs(assemblyTarget) {
  const partId = state.result && state.result.part_cost && state.result.part_cost.estimate_id;
  // Only what is certainly true of it. Whether it carries operations depends on
  // what the Manufacturing table happened to hold when Calculate was pressed —
  // which for an assembly is one component's plan applied to the whole file, so
  // the figure is not wrong about a part, it is about the wrong part.
  return `<p class="warning-inline"><strong>The panel below is a different estimate.</strong>
    It is this whole file costed as one solid part${
      partId ? ` (<span class="mono">${escapeHtml(partId)}</span>)` : ""
    }, against whatever plan the operations table held — not the sum of the components,
    each costed from its own. ${
      assemblyTarget
        ? "It is kept for comparison, and it is not what Quote issues."
        : "It is not the assembly's cost."
    }</p>`;
}

/* ------------------------------------------------------------------- quote */

function renderQuote() {
  const host = document.getElementById("quote-body");
  if (!host) return;

  const target = authoritativeEstimate();
  if (!target) {
    host.innerHTML = `<p class="hint">Nothing to quote yet. ${estimateGuidance()}</p>
      ${
        isAssemblyUpload()
          ? `<p><button type="button" class="secondary tiny" data-goto="assembly">
             Go to Assembly</button></p>`
          : ""
      }`;
    wireGoto(host);
    return;
  }

  const subjectLine =
    target.kind === "assembly"
      ? `This quotation is for the <strong>assembly roll-up</strong> — every component's
         contribution, costed from its own plan. It is a different estimate from the same
         file costed as a single part, and that one is never issued from here.`
      : `This quotation is for the <strong>part estimate</strong> calculated in
         <em>Cost</em>.`;

  const incomplete = target.incomplete
    ? `<p class="warning-inline"><strong>This assembly is not fully costed.</strong>
       ${escapeHtml(target.note)} The document reports it rather than filling the gap.</p>`
    : "";

  // Two views of the same stored estimate. The customer document omits the
  // engineering section entirely rather than merely not printing it.
  host.innerHTML = `
    <p class="hint">${subjectLine}</p>
    ${incomplete}
    <div class="stat-grid">
      ${statRow(target.subjectLabel, target.subject)}
      ${statRow(target.quantityLabel, target.quantity)}
      ${statRow(target.unitLabel, target.unitCost)}
      ${statRow("Total", target.total)}
    </div>
    <div class="row quote-actions">
      <a class="button" id="open-quotation" target="_blank"
         href="quotation.html?estimate=${encodeURIComponent(target.id)}">
        Open customer quotation
      </a>
      <a class="button secondary" target="_blank"
         href="quotation.html?estimate=${encodeURIComponent(target.id)}&view=engineering">
        Engineering detail
      </a>
    </div>
    <p class="provenance">Estimate <span class="mono">${escapeHtml(target.id)}</span>
    (${escapeHtml(target.kind)}). The customer view leaves the engineering section out of
    the payload, not merely off the page.</p>`;
}

/* ------------------------------------------------------------------ chrome */

/** The context bar, the Assembly tab's availability, and the panels above. */
function refreshFlowChrome() {
  const section = flowSection();
  if (!section) return;

  const name = document.getElementById("eb-name");
  const sub = document.getElementById("eb-sub");
  if (name && sub) {
    if (state.document) {
      const isAssembly = isAssemblyUpload();
      name.textContent = $("part_name").value || $("part_id").value || "Untitled part";
      const bits = [shown("file-name") || "", isAssembly ? "assembly" : "single part"];
      sub.textContent = bits.filter(Boolean).join(" · ");
    } else {
      name.textContent = "No CAD file yet";
      sub.textContent = "Upload a model to begin";
    }
  }

  // The bar reads the *current* estimate, which for an assembly is the roll-up
  // and not the result panel. Reading the panel alone put a single-part figure
  // above every section of an assembly, contradicting the roll-up on screen.
  const unit = document.getElementById("eb-unit");
  const total = document.getElementById("eb-total");
  const current = authoritativeEstimate();
  if (unit) unit.textContent = (current && current.unitCost) || "—";
  if (total) total.textContent = (current && current.total) || "—";

  // The Assembly section is offered as soon as the file *declares* component
  // structure, which `/cad/import` says at identification — not only once it
  // has been decomposed, or the section a user needs to visit to decompose it
  // would be the one dimmed. It is dimmed rather than removed for a single
  // part, because "this is a single part" is an answer rather than a gap.
  const assemblyTab = document.getElementById("flow-tab-assembly");
  if (assemblyTab) {
    assemblyTab.classList.toggle("dim", !isAssemblyUpload());
    assemblyTab.title = isAssemblyUpload()
      ? ""
      : "This file declares no component structure — it is a single part.";
  }

  renderOverview();
  renderCostAuthority();
  renderQuote();
}

document.querySelectorAll(".flow-tab").forEach((tab) =>
  tab.addEventListener("click", () => setFlow(tab.dataset.flow))
);

// The feature count belongs beside the disclosure that hides the table, so the
// evidence is discoverable without being unavoidable.
function refreshFeatureDetailCount() {
  const label = document.getElementById("feature-detail-count");
  if (!label) return;
  const rows = document.querySelectorAll("#features-body tr").length;
  label.textContent = rows ? `(${rows} rows)` : "";
}

refreshFlowChrome();
