// Estimate approval (History detail) and the audit log (Audit tab).
//
// The approval state is the server's, derived from its append-only events;
// this file shows it, lists the events and sends a person's decision. It works
// out nothing: which buttons appear follows the server's `state`, and whether
// the person may press one (role, separation of duties) is decided by the
// server and comes back as a message. Approving changes no figure.
//
// Declares no global functions (one shared scope with every classic script);
// it publishes `renderApprovalPanel` and `openAudit`, which app.js calls
// guarded, like actuals.js.
(() => {
  const STATE_LABELS = {
    draft: "Draft — not submitted",
    submitted: "Submitted — awaiting review",
    approved: "Approved",
    rejected: "Rejected",
    withdrawn: "Withdrawn",
  };
  const ACTIONS_BY_STATE = {
    draft: [["submit", "Submit for review"]],
    rejected: [["submit", "Submit again"]],
    withdrawn: [["submit", "Submit again"]],
    submitted: [["approve", "Approve"], ["reject", "Reject"], ["withdraw", "Withdraw"]],
    approved: [["withdraw", "Withdraw approval"]],
  };

  const when = (value) => new Date(value + "Z").toLocaleString();

  const eventRow = (event) => `
    <tr>
      <td>${event.sequence}</td>
      <td>${escapeHtml(event.kind)}</td>
      <td>${escapeHtml(event.actor_name)}${event.actor_role ? ` (${escapeHtml(event.actor_role)})` : ""}
        ${event.authenticated ? "" : "<span class='muted'>· auth off</span>"}</td>
      <td>${when(event.recorded_at)}</td>
      <td>${escapeHtml(event.comment || "")}</td>
    </tr>`;

  const panelMarkup = (status) => `
    <h3>Approval</h3>
    <p><span class="approval-state approval-${escapeHtml(status.state)}" id="approval-state">${
      escapeHtml(STATE_LABELS[status.state] || status.state)
    }</span>
    <span class="muted">Created by ${
      status.created_by ? escapeHtml(status.created_by) : "not recorded"
    }.</span></p>
    <p class="muted">${escapeHtml(status.note)}${
      status.auth_enabled ? " With auth on, a quotation is issued only for an approved estimate." : ""
    }</p>
    ${status.events.length
      ? `<table class="mini approval-events">
          <thead><tr><th>#</th><th>Event</th><th>By</th><th>When</th><th>Comment</th></tr></thead>
          <tbody>${status.events.map(eventRow).join("")}</tbody></table>`
      : "<p class='muted'>No approval events yet.</p>"}
    <div class="row approval-actions">
      <input id="approval-comment" maxlength="2000" placeholder="Comment (required to reject)">
      ${(ACTIONS_BY_STATE[status.state] || [])
        .map(([action, label]) =>
          `<button type="button" class="${action === "approve" ? "" : "secondary"} approval-action"
             data-action="${action}">${label}</button>`)
        .join("")}
    </div>
    <p id="approval-message" class="muted"></p>`;

  const renderInto = (host, estimateId, status) => {
    host.innerHTML = panelMarkup(status);
    host.querySelectorAll("button.approval-action").forEach((button) =>
      button.addEventListener("click", async () => {
        const message = host.querySelector("#approval-message");
        button.disabled = true;
        try {
          const updated = await api(
            `/estimates/${encodeURIComponent(estimateId)}/approval/${button.dataset.action}`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ comment: host.querySelector("#approval-comment").value }),
            }
          );
          renderInto(host, estimateId, updated);
        } catch (error) {
          message.textContent = error.message;
          button.disabled = false;
        }
      })
    );
  };

  window.renderApprovalPanel = async (estimate) => {
    const host = $("history-approval");
    if (!host) return;
    host.innerHTML = "<p class='muted'>Loading approval…</p>";
    try {
      const status = await api(`/estimates/${encodeURIComponent(estimate.estimate_id)}/approval`);
      renderInto(host, estimate.estimate_id, status);
    } catch (error) {
      host.innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
    }
  };

  // ------------------------------------------------------------- audit tab

  const auditRow = (item) => `
    <tr>
      <td>${when(item.recorded_at)}</td>
      <td>${escapeHtml(item.actor_id)}${item.actor_role ? ` (${escapeHtml(item.actor_role)})` : ""}</td>
      <td class="mono">${escapeHtml(item.method)} ${escapeHtml(item.route || item.path)}</td>
      <td class="mono">${escapeHtml(item.target_id || "—")}</td>
      <td class="mono">${escapeHtml(item.created_ids.join(", ") || "—")}</td>
      <td>${item.status_code} ${escapeHtml(item.outcome)}</td>
    </tr>`;

  const loadAudit = async () => {
    const body = $("audit-body");
    const status = $("audit-status");
    const target = $("audit-target").value.trim();
    status.textContent = "Loading…";
    try {
      const page = await api(`/audit?limit=200${target ? `&target_id=${encodeURIComponent(target)}` : ""}`);
      body.innerHTML = page.items.length
        ? page.items.map(auditRow).join("")
        : "<tr><td colspan='6' class='muted'>No audit rows.</td></tr>";
      status.textContent = `${page.items.length} of ${page.total} shown, newest first.`;
    } catch (error) {
      body.innerHTML = "";
      status.textContent = error.message;
    }
  };

  window.openAudit = () => {
    const auth = window.CadAuth && window.CadAuth.status();
    $("audit-mode").textContent = auth && !auth.auth_enabled
      ? "Auth off — actions recorded as local."
      : "Every mutating request, with who made it and how it ended. No request body, query string or token is recorded.";
    loadAudit();
  };

  const form = $("audit-filter");
  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      loadAudit();
    });
  }
})();
