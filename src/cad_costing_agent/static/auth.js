// Sign-in for opt-in access control. Loaded before app.js.
//
// With auth off (the default) the server attributes every action to `local`
// and this file only says so. With auth on, a person pastes the API token an
// admin gave them; it is kept in sessionStorage (this tab only, gone when the
// tab closes) and sent as `Authorization: Bearer ...` on every same-origin
// request by wrapping fetch once, here, so no other file handles a credential.
//
// The page decides nothing about access: who may do what is the server's role
// table, and a refusal arrives as an ordinary error message.
(() => {
  const STORAGE_KEY = "cadCostingApiToken";

  const readToken = () => {
    try {
      return window.sessionStorage.getItem(STORAGE_KEY) || "";
    } catch {
      return "";
    }
  };
  const writeToken = (value) => {
    try {
      if (value) window.sessionStorage.setItem(STORAGE_KEY, value);
      else window.sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      // Storage blocked: the token lives only as long as this page.
    }
    memoryToken = value;
  };
  let memoryToken = readToken();

  const sameOrigin = (input) => {
    try {
      const url = new URL(typeof input === "string" ? input : input.url, window.location.href);
      return url.origin === window.location.origin;
    } catch {
      return false;
    }
  };

  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    if (!memoryToken || !sameOrigin(input)) return originalFetch(input, init);
    const options = { ...(init || {}) };
    const headers = new Headers(options.headers || (typeof input === "string" ? undefined : input.headers));
    if (!headers.has("Authorization")) headers.set("Authorization", `Bearer ${memoryToken}`);
    options.headers = headers;
    return originalFetch(input, options);
  };

  const escape = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);

  let status = null;

  const showAuditTab = () => {
    const tab = document.querySelector('.tab[data-tab="audit"]');
    if (!tab) return;
    const visible = status && (!status.auth_enabled || (status.actor && status.actor.role === "admin"));
    tab.classList.toggle("hidden", !visible);
  };

  const render = () => {
    const bar = document.getElementById("auth-bar");
    if (!bar) return;
    if (!status) {
      bar.innerHTML = "<span class='muted'>Checking sign-in…</span>";
      return;
    }
    if (!status.auth_enabled) {
      bar.innerHTML = `<span class="auth-note">Auth off — actions recorded as local</span>`;
    } else if (status.signed_in) {
      bar.innerHTML = `<span class="auth-note">Signed in as <strong>${escape(status.actor.name)}</strong>
        (${escape(status.actor.role)})</span>
        <button type="button" class="secondary" id="auth-sign-out">Sign out</button>`;
      document.getElementById("auth-sign-out").addEventListener("click", () => {
        writeToken("");
        window.location.reload();
      });
    } else {
      bar.innerHTML = `<form id="auth-form" class="auth-form">
          <span class="auth-note">Auth on — sign in</span>
          <input id="auth-token" type="password" autocomplete="off" placeholder="API token" aria-label="API token">
          <button type="submit">Sign in</button>
          <span id="auth-error" class="muted"></span>
        </form>`;
      document.getElementById("auth-form").addEventListener("submit", async (event) => {
        event.preventDefault();
        const candidate = document.getElementById("auth-token").value.trim();
        if (!candidate) return;
        writeToken(candidate);
        const checked = await loadStatus();
        if (checked && checked.signed_in) {
          // Reload so every panel that loaded while signed out loads again.
          window.location.reload();
        } else {
          writeToken("");
          await loadStatus();
          const error = document.getElementById("auth-error");
          if (error) error.textContent = "That token was not recognised.";
        }
      });
    }
    showAuditTab();
  };

  const loadStatus = async () => {
    try {
      const response = await window.fetch("/auth/status");
      status = await response.json();
    } catch {
      status = null;
    }
    render();
    return status;
  };

  window.CadAuth = {
    status: () => status,
    refresh: loadStatus,
  };

  render();
  loadStatus();
})();
