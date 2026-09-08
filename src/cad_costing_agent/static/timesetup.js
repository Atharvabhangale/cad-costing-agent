"use strict";

// Time setup: publishing versioned machining-assumption profiles.
//
// A classic script loaded after app.js, sharing `$`, `api`, `state`,
// `escapeHtml` and `number` with it.
//
// The exact counterpart of the costing-setup screen, and separate for the same
// reason the models are: a rate set decides what an hour costs, a time profile
// decides how long the work takes. Nothing here has a currency.
//
// **A published version is never edited.** There is no update call in this file
// because there is none in the API. Changing an assumption means publishing a
// new version, which leaves every estimate already made pointing at the version
// that produced it — which is the whole reason a historical time can still be
// explained.
//
// The backend remains the source of truth for whether a profile is publishable:
// the Validate button asks it rather than deciding here.

// Every editable parameter, discovered from the markup rather than listed twice.
const timeParamInputs = () => [...document.querySelectorAll("#tab-timesetup input[data-param]")];

let timeDraft = null;

function blankTimeDraft() {
  return {
    time_profile_id: "",
    name: "",
    version: "1.0.0",
    description: "",
    is_illustrative: false,
    assumptions: {},
    isNewProfile: true,
  };
}

// ------------------------------------------------------------------ listing

async function loadTimeProfiles() {
  $("timeset-status").textContent = "Loading…";
  try {
    state.timeProfiles = await api("/time-profiles");
  } catch (error) {
    $("timeset-status").textContent = "";
    showError(error.message);
    return;
  }
  $("timeset-status").textContent = "";
  renderTimeProfileCards();
}

function latestTimeVersionOf(profileId) {
  const versions = (state.timeProfiles || [])
    .filter((profile) => profile.time_profile_id === profileId)
    .map((profile) => profile.version);
  return versions.sort(compareVersions)[versions.length - 1];
}

function renderTimeProfileCards() {
  const families = new Map();
  (state.timeProfiles || []).forEach((profile) => {
    if (!families.has(profile.time_profile_id)) families.set(profile.time_profile_id, []);
    families.get(profile.time_profile_id).push(profile);
  });

  if (!families.size) {
    $("time-profile-cards").innerHTML =
      `<p class="hint">No time profiles yet. Publish one to replace the built-in baseline.</p>`;
    return;
  }

  $("time-profile-cards").innerHTML = [...families.entries()]
    .map(([profileId, versions]) => {
      const sorted = [...versions].sort((a, b) => compareVersions(a.version, b.version));
      const latest = sorted[sorted.length - 1];
      const badge = latest.is_illustrative
        ? `<span class="badge badge-warn">Illustrative parameters</span>`
        : `<span class="badge badge-ok">Real parameters</span>`;
      return `<div class="profile-card">
          <div class="profile-head">
            <strong>${escapeHtml(latest.name)}</strong>
            <span class="mono muted">${escapeHtml(profileId)}</span>
          </div>
          <div class="badges"><span class="badge">v${escapeHtml(latest.version)}</span>${badge}</div>
          <p class="hint">${escapeHtml(latest.description || "No description.")}</p>
          <p class="hint muted">${sorted.length} version${sorted.length === 1 ? "" : "s"}:
            ${sorted.map((v) => escapeHtml(v.version)).join(", ")}</p>
          <div class="profile-actions">
            <button type="button" class="secondary tiny use-time-profile"
              data-id="${escapeHtml(profileId)}">Use for estimate</button>
            <button type="button" class="secondary tiny new-time-version"
              data-id="${escapeHtml(profileId)}">Publish new version</button>
          </div>
        </div>`;
    })
    .join("");

  $("time-profile-cards")
    .querySelectorAll(".use-time-profile")
    .forEach((button) =>
      button.addEventListener("click", () => {
        $("time_profile_id").value = button.dataset.id;
        onTimeProfileFamilyChange();
        showTab("estimate");
      })
    );
  $("time-profile-cards")
    .querySelectorAll(".new-time-version")
    .forEach((button) =>
      button.addEventListener("click", () => startNewTimeVersion(button.dataset.id))
    );
}

// ------------------------------------------------------------------ editing

$("new-time-profile").addEventListener("click", () => {
  timeDraft = blankTimeDraft();
  // A new profile starts from the built-in baseline rather than an empty form,
  // so the user edits real numbers instead of inventing a whole parameter set.
  timeDraft.assumptions = { ...(baselineAssumptions() || {}) };
  $("time-editor-title").textContent = "New time profile";
  $("time-editor-subtitle").textContent =
    "Starting from the built-in baseline. Change what your shop actually runs, then publish.";
  openTimeEditor();
});

function baselineAssumptions() {
  // Whatever the estimate page currently has loaded is the most convenient
  // starting point; the backend's own defaults fill any gap.
  return (state.timeProfile && state.timeProfile.assumptions) || null;
}

async function startNewTimeVersion(profileId) {
  let existing;
  try {
    existing = await api(`/time-profiles/${encodeURIComponent(profileId)}`);
  } catch (error) {
    showError(error.message);
    return;
  }
  timeDraft = {
    time_profile_id: existing.time_profile_id,
    name: existing.name,
    version: bumpMinor(existing.version),
    description: existing.description,
    is_illustrative: existing.is_illustrative,
    assumptions: { ...existing.assumptions },
    isNewProfile: false,
  };
  $("time-editor-title").textContent = `New version of ${existing.name}`;
  $("time-editor-subtitle").textContent =
    `Copied from v${existing.version}, which stays exactly as it is. Estimates already made ` +
    `with it keep it.`;
  openTimeEditor();
}

function openTimeEditor() {
  $("timeset-list").classList.add("hidden");
  $("timeset-editor").classList.remove("hidden");
  $("timeset-review").classList.add("hidden");
  clearTimeErrors();
  writeTimeDraftToForm();
  $("t-id").disabled = !timeDraft.isNewProfile;
}

$("cancel-time-edit").addEventListener("click", () => {
  timeDraft = null;
  $("timeset-editor").classList.add("hidden");
  $("timeset-list").classList.remove("hidden");
  loadTimeProfiles();
});

function writeTimeDraftToForm() {
  $("t-id").value = timeDraft.time_profile_id;
  $("t-name").value = timeDraft.name;
  $("t-version").value = timeDraft.version;
  $("t-description").value = timeDraft.description;
  $("t-illustrative").value = String(timeDraft.is_illustrative);
  timeParamInputs().forEach((input) => {
    const value = timeDraft.assumptions[input.dataset.param];
    input.value = value === undefined || value === null ? "" : String(value);
  });
}

function readTimeFormIntoDraft() {
  timeDraft.time_profile_id = $("t-id").value.trim();
  timeDraft.name = $("t-name").value.trim();
  timeDraft.version = $("t-version").value.trim();
  timeDraft.description = $("t-description").value.trim();
  timeDraft.is_illustrative = $("t-illustrative").value === "true";

  const assumptions = { is_illustrative: timeDraft.is_illustrative };
  timeParamInputs().forEach((input) => {
    const raw = input.value.trim();
    // A blank field is *omitted*, so the backend's own default applies rather
    // than a zero being sent — a feed of 0 would divide by zero downstream.
    if (raw !== "") assumptions[input.dataset.param] = Number(raw);
  });
  timeDraft.assumptions = assumptions;
}

// The profile id follows the name until the user types one, exactly as the
// costing setup screen behaves.
$("t-name").addEventListener("input", () => {
  if (timeDraft && timeDraft.isNewProfile && !$("t-id").dataset.touched) {
    $("t-id").value = slugify($("t-name").value) + (slugify($("t-name").value) ? "-time" : "");
  }
});
$("t-id").addEventListener("input", () => {
  $("t-id").dataset.touched = "1";
});

// ------------------------------------------------------------------ publishing

function draftToTimeProfile() {
  readTimeFormIntoDraft();
  return {
    time_profile_id: timeDraft.time_profile_id,
    name: timeDraft.name,
    version: timeDraft.version,
    description: timeDraft.description,
    is_illustrative: timeDraft.is_illustrative,
    assumptions: timeDraft.assumptions,
  };
}

function showTimeErrors(message) {
  $("timeset-errors").textContent = message;
  $("timeset-errors").classList.remove("hidden");
}

function clearTimeErrors() {
  $("timeset-errors").classList.add("hidden");
  $("timeset-errors").textContent = "";
}

$("validate-time-profile").addEventListener("click", async () => {
  clearTimeErrors();
  $("time-publish-status").textContent = "Validating…";
  let verdict;
  try {
    verdict = await api("/time-profiles/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(draftToTimeProfile()),
    });
  } catch (error) {
    $("time-publish-status").textContent = "";
    showTimeErrors(error.message);
    return;
  }
  $("time-publish-status").textContent = "";
  renderTimeReview(verdict);
});

function renderTimeReview(verdict) {
  const messages = verdict.messages.length
    ? `<ul>${verdict.messages.map((m) => `<li>${escapeHtml(m)}</li>`).join("")}</ul>`
    : `<p class="hint">Nothing to flag.</p>`;
  $("timeset-review").innerHTML =
    `<h3>${verdict.valid ? "Ready to publish" : "Cannot publish"}</h3>` +
    `<p class="hint"><strong>${escapeHtml(verdict.name)}</strong> ` +
    `<span class="mono">${escapeHtml(verdict.time_profile_id)}</span> ` +
    `v${escapeHtml(verdict.version)}` +
    (verdict.is_new_profile ? " — a new profile" : " — a new version of an existing profile") +
    `</p>${messages}` +
    `<p class="provenance">Advisory notes never block publishing: the parameters are yours to ` +
    `choose and the estimator uses them exactly as entered.</p>`;
  $("timeset-review").classList.remove("hidden");
}

$("publish-time-profile").addEventListener("click", async () => {
  clearTimeErrors();
  $("time-publish-status").textContent = "Publishing…";
  const payload = draftToTimeProfile();
  const isNew = timeDraft.isNewProfile;
  const path = isNew
    ? "/time-profiles"
    : `/time-profiles/${encodeURIComponent(payload.time_profile_id)}/versions`;
  const body = isNew ? payload : (({ time_profile_id, ...rest }) => rest)(payload);

  try {
    await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (error) {
    $("time-publish-status").textContent = "";
    showTimeErrors(error.message);
    return;
  }

  $("time-publish-status").textContent = "Published.";
  timeDraft = null;
  $("timeset-editor").classList.add("hidden");
  $("timeset-list").classList.remove("hidden");
  await loadTimeProfiles();
  await loadTimeProfileOptions();
});

// ------------------------------------------- the estimate page's selector

async function loadTimeProfileOptions() {
  try {
    state.timeProfiles = await api("/time-profiles");
  } catch (error) {
    showError(error.message);
    return;
  }
  const families = [...new Set(state.timeProfiles.map((p) => p.time_profile_id))];
  const previous = $("time_profile_id").value;
  $("time_profile_id").innerHTML = families
    .map((id) => {
      const latest = state.timeProfiles
        .filter((p) => p.time_profile_id === id)
        .sort((a, b) => compareVersions(a.version, b.version))
        .slice(-1)[0];
      return `<option value="${escapeHtml(id)}">${escapeHtml(latest.name)}</option>`;
    })
    .join("");
  if (families.includes(previous)) $("time_profile_id").value = previous;
  await onTimeProfileFamilyChange();
}

async function onTimeProfileFamilyChange() {
  const id = $("time_profile_id").value;
  const versions = (state.timeProfiles || [])
    .filter((p) => p.time_profile_id === id)
    .map((p) => p.version)
    .sort(compareVersions)
    .reverse();
  $("time_profile_version").innerHTML =
    `<option value="">Latest (${escapeHtml(versions[0] || "—")})</option>` +
    versions.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
  await loadSelectedTimeProfile();
}

async function loadSelectedTimeProfile() {
  const id = $("time_profile_id").value;
  if (!id) return;
  const version = $("time_profile_version").value;
  const query = version ? `?version=${encodeURIComponent(version)}` : "";
  try {
    state.timeProfile = await api(`/time-profiles/${encodeURIComponent(id)}${query}`);
  } catch (error) {
    showError(error.message);
    return;
  }
  renderTimeProfileDetail();
}

function renderTimeProfileDetail() {
  const profile = state.timeProfile;
  if (!profile) return;
  const badges = [`<span class="badge">v${escapeHtml(profile.version)}</span>`];
  badges.push(
    profile.is_illustrative
      ? `<span class="badge badge-warn">Illustrative parameters</span>`
      : `<span class="badge badge-ok">Real parameters</span>`
  );
  badges.push(`<span class="badge">No currency — duration only</span>`);
  $("time-profile-badges").innerHTML = badges.join("");
  $("time-profile-desc").textContent = profile.description || "";
}

$("time_profile_id").addEventListener("change", onTimeProfileFamilyChange);
$("time_profile_version").addEventListener("change", loadSelectedTimeProfile);

// The estimate page needs a profile from the moment it loads: an estimated time
// with no profile behind it would be unattributable.
loadTimeProfileOptions();
