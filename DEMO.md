# Running the demo

Everything here was measured against the running application. The numbers are
what it actually produces, not estimates.

There is no demo mode and nothing is faked: the pre-flight only publishes a rate
set through the ordinary API and reads one CAD file to warm the geometry
libraries. The illustrative-rate warnings stay on screen, because they are true.

---

## Pre-flight

Do all of this before anyone is watching. It takes about three minutes.

All commands are **PowerShell**, run from `C:\Users\AtharvaBhangale\Desktop\costing-agent`.

### 1. Stop anything already using the real database

A development server left running holds `./cad_costing_agent.db` and keeps
writing to it. Find it and stop it:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
  Select-Object LocalPort, OwningProcess
```

If that prints a row, stop that process:

```powershell
Stop-Process -Id <OwningProcess> -Force
```

### 2. Start on a fresh database

The real database holds development history — probe estimates, an assembly that
costs ₹0.02, a bracket that costs ₹38,585,326 from an experimental rate set. The
History screen would open onto all of it.

Point the app at a new file instead. Startup creates it, migrates it to the
current schema and seeds the rate sets and time profiles:

```powershell
# only this throwaway file is ever removed, never cad_costing_agent.db
if (Test-Path demo.db) { Remove-Item demo.db -Force }

$env:CAD_COSTING_DATABASE_URL = "sqlite:///./demo.db"
.\.venv\Scripts\python.exe -m uvicorn cad_costing_agent.main:app --port 8000
```

Leave that window running. The real database is never opened by this and stays
exactly as it is.

> `$env:VAR = "..."` on its own line is required. PowerShell has no inline
> `VAR=value command` prefix — that form is a parse error.

### 3. Run the pre-flight

In a **second** PowerShell window, in the same directory:

```powershell
.\.venv\Scripts\python.exe scripts\demo_preflight.py --base-url http://127.0.0.1:8000
```

It reports whether the database is clean, publishes rate set **`poc-default`
v9.0.0**, and warms OpenCASCADE. Expect:

```
  ok    database is empty - History will open clean
  ok    published poc-default v9.0.0 (INR/kg: steel 0.8 -> 75.0, aluminium 2.2 -> 280.0)
        overhead 12.5%, scrap 6.0%, tooling 300.0 per batch
  ok    prices every process the demo needs: drilling, milling, turning
  ok    geometry warmed with model1_1.stp: ~5s cold, ~0.3s warm
```

**Why a new rate set.** The seeded values price steel at ₹0.80/kg, which makes
material 0.4% of the cost and is the first thing a manufacturing audience will
question. The published version uses representative market prices and the
overhead, scrap and tooling percentages the project's other seeded profile
already carries. It is **still marked illustrative**, so the warning the
application shows about it remains — that honesty is worth more than a tidier
screen.

**Why warming matters.** The first geometry call loads OpenCASCADE's native
libraries and takes about five seconds. Every call after it takes about a third
of a second. Warming means the audience never sees the slow one.

### 4. Set up the browser

- **Ctrl+0** for 100% zoom.
- **Maximize the window.** Below roughly 1080 CSS pixels the layout drops to a
  single column and the setup band wraps. This machine's desktop is 1280 CSS px,
  so maximized is comfortable and a restored window is not.
- **Hard-reload once: Ctrl+Shift+R.** Only needed on a browser that used the app
  before the assets began carrying `cache-control: no-cache`; such a browser can
  still hold a superseded copy of the JavaScript, which shows up as fixed
  behaviour that appears not to work. One hard reload clears it for good — every
  load after that revalidates on its own.

### 5. Pre-fill the quotation metadata

Run one throwaway estimate, open **Quote → Open customer quotation**, click
**Customer / project details**, fill in customer, project, reference, prepared by
and validity, then **Apply**. Leaving *Reference* blank prints the 32-character
estimate id as the document's headline number.

---

## The files

| Use | File | Why |
|---|---|---|
| Single part | `cadmodels/model1_1.stp` (63 KB) | Analytic B-Rep, 39 features in one solid. Reads in about a second and every feature is recognisable. |
| Assembly | `Assembly.stp` (233 KB) | Analytic, 20 placements across 4 product definitions, 3 with measurable geometry. The only assembly available whose components can actually be planned and costed. |

**Do not demo `Figure 3.stp`.** It is a 198 MB faceted export: identification
alone takes 4.4 s, the mesh 25–35 s, decomposition 26 s, and component planning
51 s — and it produces **zero** component plans, because it carries no
measurable component geometry. That behaviour is correct and honestly reported,
but it is not a demo.

---

## The sequence

### Single part — about four minutes

1. **Estimate**, empty. "This is the whole workflow."
2. Drag in `model1_1.stp`. Model appears in about a second.
3. **Overview** — nine tiles, warnings, the recommended next step.
4. **CAD** — 39 features from 43 faces in ~270 ms. Open *View detailed feature
   analysis* briefly to show the evidence exists, then close it.
5. **Manufacturing** — the summary strip, then the routing. Then **both** clicks:
   - **Use all estimated times** — commits the 31 operation cycle times
   - **Use estimate** on the **Setup** band — commits the 10-minute setup

   Miss the second and setup cost stays ₹0.00 with a warning on screen.
6. **Cost** — the headline, the breakdown, the assumptions.
7. **Quote** — Open customer quotation → Print / Save as PDF.

### Assembly — about three minutes

8. Upload `Assembly.stp`. The part name now follows the file automatically.
9. **Assembly → Decompose into components** (~2 s): 4 definitions, 20 occurrences.
   The component plans then build themselves (~2.5 s) — 3 plans, every time in
   them the system's estimate with nothing committed. Say that out loud: the
   plans appear, the decisions do not.
10. **Explode** slider to ~55%, then **Reset explosion**.
11. **Component classification** — "no CAD format states make/buy".
12. Open **Drawn Cup**'s plan (4 operations) and show a **Details** row.
13. For each of the three: *Use all estimated times* **and** *Use estimate* on
    the Setup band.
14. **Cost every component**.

Worth doing deliberately: press **Cost every component** *before* committing
anything. The total comes out **₹902.57** and every screen calls it a *partial
assembly estimate — manufacturing plans incomplete*, naming the components that
have committed no time, and the customer quotation is refused. Then commit the
times and cost again for ₹2,221.62. Showing the refusal is a stronger point than
hiding it.
14. **Cost** — the section leads with *This file's estimate is the assembly
    roll-up*, its id and its total. If a whole-file part estimate is also on
    screen it is labelled as a different estimate, with its own id. Worth saying
    out loud: the same file costed as one solid is a different question, not a
    cross-check.
15. **Quote** — issues the roll-up, not the part estimate. Both buttons carry the
    assembly's id.

Without committing the times the roll-up is ₹902.57 — material and tooling for
three components and nothing for making them — and it says so rather than
presenting itself as a cost.

---

## Expected results

### Single part — `model1_1.stp`, quantity 10, `poc-default` v9.0.0

| | |
|---|---|
| Recognized features | 39, in 1 solid, from 43 faces and 124 edges |
| Operations / setups | 31 in 1 setup |
| Mass | 1.7319 kg per part |
| Material | ₹1,298.91 |
| Operations | ₹3,347.00 (₹300.00 setup + ₹3,047.00 run) |
| Direct cost | ₹4,645.91 |
| Overhead | ₹580.74 |
| Scrap allowance | ₹278.75 |
| Tooling | ₹300.00 |
| **Total** | **₹5,805.40** |
| **Per unit** | **₹580.54** |

Setup time 10 min charged once; cycle time 93.9 min over 10 parts.

### Assembly — `Assembly.stp`, all three components committed

| Component | Classification | Qty | Per unit | Extended |
|---|---|---|---|---|
| Assembly | sub-assembly | — | — | not costed |
| Drawn Cup | Manufactured | ×1 | ₹758.22 | ₹758.22 |
| Needle Roller | Manufactured | ×18 | ₹38.38 | ₹690.87 |
| cage | Manufactured | ×1 | ₹772.54 | ₹772.54 |
| **Assembly total** | | | | **₹2,221.62** |

The Cost section, the context bar and the Quote screen all name the same estimate
id as this roll-up. If any of them shows a smaller figure with no operations, the
browser is running a stale copy of the UI — hard-reload.

Components costed 3 of 3 (20 of 20 part occurrences). The sub-assembly is
reported as not costed because its cost is the sum of its own components and
counting it again would double it.

---

## Things to know while presenting

- **Don't open the Engineering tab unprompted.** On a fresh database it reads
  "Accepted unchanged 100%" with no corrections, which invites "so it's never
  wrong?" It is honest, but it is not a story.
- **The *cage* component has 79 operations.** Opening its plan is a long scroll.
  Show *Drawn Cup* (4 operations) instead.
- **Clicking a component isolates it in the 3D view.** *Show all components*
  brings the rest back.
- **The feature summary is mostly planes.** The holes and fillets lead; don't
  scroll past the first eight lines.
- **The quotation carries two critical warnings** about illustrative rates and
  illustrative time assumptions. Address them directly — refusing to pass
  placeholders off as a price is the point.

---

## Afterwards

```powershell
# stop the server window with Ctrl+C, then:
Remove-Item demo.db -Force
```

`cad_costing_agent.db` was never touched.
