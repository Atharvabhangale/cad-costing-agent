"use strict";

// The bar a turned part is cut from: the system's proposal and the engineer's
// answer beside it.
//
// A classic script loaded after process.js, sharing `$`, `api`, `state`,
// `escapeHtml`, `number` and `showError` with the files before it.
//
// The proposal arrives with `/cad/operations/suggest` and is kept in
// `state.rawStock`. Every decision goes through `/cad/stock/decide`, which
// returns the whole object with its size and volume recomputed — this file
// computes no size, volume or mass. `app.js` sends the object with the cost
// request; the backend costs material from the bar only once it is confirmed.

function resetRawStock() {
  state.rawStock = null;
  $("stock-card").classList.add("hidden");
  $("stock-summary").innerHTML = "";
  $("stock-basis").innerHTML = "";
}

function renderRawStock(stock) {
  state.rawStock = stock || null;
  if (!stock) {
    resetRawStock();
    return;
  }
  const status = stock.confirmed
    ? stock.source === "user"
      ? `<span class="pill pill-ok">Your bar — material costed from it</span>`
      : `<span class="pill pill-ok">Confirmed — material costed from it</span>`
    : `<span class="pill pill-off">Proposed — not used until confirmed</span>`;
  // Turning times were estimated from the proposed bar; a different bar makes
  // them stale, and that is said rather than silently re-estimated.
  const stale =
    stock.user_diameter_mm != null
      ? `<p class="warning-inline">The turning times in the plan were estimated for the proposed
          Ø${escapeHtml(String(stock.system_diameter_mm))} mm bar. Re-enter or check them for the
          Ø${escapeHtml(String(stock.diameter_mm))} mm bar you chose.</p>`
      : "";

  $("stock-summary").innerHTML =
    `<p><strong>Round bar Ø${escapeHtml(String(stock.diameter_mm))} × ` +
    `${escapeHtml(String(stock.length_mm))} mm</strong> ${status}</p>` +
    `<p class="hint">Part Ø${escapeHtml(String(stock.part_max_diameter_mm))} × ` +
    `${escapeHtml(String(stock.part_length_mm))} mm · bar volume ` +
    `${number(stock.volume_mm3, 0)} mm³` +
    (stock.source === "user"
      ? ` · proposed was Ø${escapeHtml(String(stock.system_diameter_mm))} × ` +
        `${escapeHtml(String(stock.system_length_mm))} mm`
      : "") +
    `</p>` +
    (stock.confirmed
      ? ""
      : `<p class="hint">Until you confirm a bar, material is costed from the finished part's
          mass, as before — which leaves out every chip turned off the bar.</p>`) +
    stale;
  $("stock-basis").innerHTML =
    `<ul class="declined-list">${stock.basis.map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul>`;
  $("stock-diameter").value = stock.diameter_mm;
  $("stock-length").value = stock.length_mm;
  $("stock-confirm").disabled = stock.confirmed && stock.source === "system";
  $("stock-withdraw").disabled = !stock.confirmed;
  $("stock-card").classList.remove("hidden");
}

async function decideStock(action, sizes) {
  if (!state.rawStock) return;
  try {
    const updated = await api("/cad/stock/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stock: state.rawStock, action, ...(sizes || {}) }),
    });
    renderRawStock(updated);
  } catch (error) {
    showError(error.message);
  }
}

$("stock-confirm").addEventListener("click", () => decideStock("confirm"));
$("stock-use").addEventListener("click", () => {
  const diameter = $("stock-diameter").value.trim();
  const length = $("stock-length").value.trim();
  decideStock("edit", {
    diameter_mm: diameter === "" ? null : Number(diameter),
    length_mm: length === "" ? null : Number(length),
  });
});
$("stock-withdraw").addEventListener("click", () => decideStock("withdraw"));
