"""Publish an ILLUSTRATIVE India rate set through the ordinary API.

    python scripts\\publish_illustrative_india_rates.py --base-url http://127.0.0.1:8001

Nothing here is client data. The figures were gathered from public web pages on
2026-10-27 — mostly marketplace asking prices and trade-index summaries read
through search results, not transaction prices — and every rate's note says
which of three kinds it is:

  SOURCED      a published figure (source and date in the note)
  DERIVED      a published figure combined with a stated assumption
  ESTIMATED    no usable Indian figure was found; the assistant's own estimate of a
               typical small-batch Indian job-shop figure, added at the user's request
               "for demo purposes only" — replace it before any real use

v1.1.0 adds those estimates (pattern, die and mould charges, forging, moulding,
drilling, grinding, and a demo-only purchased-part price) on top of v1.0.0, which
left them out. Rate set versions are immutable, so v1.0.0 stays as published.

The rate set is published with is_illustrative = true, which keeps the app's
"illustrative rates" warning on every estimate made with it. Publishing goes
through POST /rates like any other rate set; a version that already exists is
refused (409) and never overwritten.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

RATE_SET = {
    "rate_set_id": "illustrative-india-2026",
    "name": "Illustrative India rates (web research, Oct 2026)",
    "version": "1.1.0",
    "currency": "INR",
    "is_illustrative": True,
    "description": (
        "ILLUSTRATIVE, not client data. Gathered from public web pages on 2026-10-27: trade "
        "indices (BigMint) for steel, marketplace asking prices (IndiaMART, TradeIndia, "
        "OfBusiness) for most others; gaps filled with the assistant's ESTIMATED figures for "
        "demo use. Each stage rate's note says SOURCED, DERIVED or ESTIMATED. Not for quoting."
    ),
    "materials": {
        # SOURCED: BigMint, IS 2062 E250 HR plate, ex-Mumbai trade level, excl. GST, 25 Mar 2026.
        "steel": {"name": "Steel", "cost_per_kg": 61.4, "density_kg_m3": 7850},
        # SOURCED: BigMint alloy round bar, EN8 110-150 mm ex-works Mumbai, 22 Apr 2026.
        "en8": {"name": "EN8", "cost_per_kg": 72.75, "density_kg_m3": 7850},
        # SOURCED (weak): IndiaMART/TradeIndia EN24 bar listings 80-125, undated; middle value.
        "en24": {"name": "EN24", "cost_per_kg": 96.0, "density_kg_m3": 7850},
        # SOURCED (weak): IndiaMART + blog, SS304 round bar 150-190, undated; middle value.
        "ss304": {"name": "SS304", "cost_per_kg": 170.0, "density_kg_m3": 8000},
        # SOURCED (weak): single IndiaMART 6061 rod listing, undated.
        "aluminium": {"name": "Aluminium", "cost_per_kg": 200.0, "density_kg_m3": 2700},
        # SOURCED: BigMint via AL Circle, ADC12 ingot Delhi, Jun 2025 (stale).
        "aluminium adc12": {"name": "Aluminium ADC12", "cost_per_kg": 232.0, "density_kg_m3": 2740},
        # SOURCED (weak): two listings from one seller, 558-600, undated.
        "brass": {"name": "Brass", "cost_per_kg": 580.0, "density_kg_m3": 8500},
        # SOURCED: copper metal ~1,420/kg (Coinswitch, 9 Oct 2026). Rod trades higher.
        "copper": {"name": "Copper", "cost_per_kg": 1420.0, "density_kg_m3": 8960},
        # SOURCED (weak): OfBusiness ABS natural 145-165, undated.
        "abs": {"name": "ABS", "cost_per_kg": 155.0, "density_kg_m3": 1050},
        # SOURCED (weak): OfBusiness PP homopolymer 105-120, undated.
        "pp": {"name": "PP", "cost_per_kg": 112.0, "density_kg_m3": 905},
        # SOURCED (weak): IndiaMART PC granules 145-300, undated; lower end of the common listings.
        "polycarbonate": {"name": "Polycarbonate", "cost_per_kg": 150.0, "density_kg_m3": 1200},
    },
    # Per hour. Labour from the statutory skilled minimum wage, Maharashtra engineering
    # industry, Jan-Jun 2026 (17,736-18,961/month / 208 h = 85-91/h; no PF/ESI loading).
    # Machine = published job-work rate (which includes the operator) minus that labour.
    "operations": {
        # DERIVED: VMC job work 250-600/h (IndiaMART Surat/Rajkot; NIT Calicut 600/h); 450 - 90.
        "milling": {"machine_rate_per_hour": 360.0, "labor_rate_per_hour": 90.0},
        # DERIVED: CNC turning job work 300/h (IndiaMART Surat); 300 - 90.
        "turning": {"machine_rate_per_hour": 210.0, "labor_rate_per_hour": 90.0},
        # ESTIMATED (assistant, demo only): no Indian drilling rate found; radial/CNC drill
        # set below the VMC rate.
        "drilling": {"machine_rate_per_hour": 150.0, "labor_rate_per_hour": 90.0},
        # ESTIMATED (assistant, demo only): surface/cylindrical grinder; no figure found.
        "grinding": {"machine_rate_per_hour": 400.0, "labor_rate_per_hour": 90.0},
    },
    # Example Indian should-cost sheet (slideshare, ZBC in commodities): overhead 20%,
    # rejection 2%. An example, not a standard.
    "additional_costs": {
        "overhead_rate_percent": 20.0,
        "scrap_rate_percent": 2.0,
        "tooling_cost_per_batch": 0.0,
    },
    "stage_rates": {
        "casting": {
            "unit_basis": "per_kg",
            "unit_rate": 85.0,
            "batch_charge": 25000.0,
            "note": (
                "SOURCED (marketplace): grey iron FG260 castings 70-100/kg of casting (IndiaMART, "
                "undated); SG iron 100-130, Al gravity ~200, HPDC 200-450 also seen. Rate "
                "includes metal. Pattern 25,000 per batch is ESTIMATED (assistant, demo only)."
            ),
        },
        "forging": {
            "unit_basis": "per_kg",
            "unit_rate": 130.0,
            "batch_charge": 75000.0,
            "note": (
                "ESTIMATED (assistant, demo only): closed-die steel forging ~110-160/kg and a "
                "small die ~50,000-1,50,000; published listings (15-5,000/kg) were unusable. "
                "Rate includes metal. Replace with a forge shop quote."
            ),
        },
        "sheet_cutting": {
            "unit_basis": "per_piece",
            "unit_rate": 450.0,
            "batch_charge": 0.0,
            "note": (
                "DERIVED: laser cutting MS 450/m of cut (IndiaMART Techpro, undated) x an "
                "ASSUMED 1 m of cut per part. The app has no per-metre unit; adjust per part."
            ),
        },
        "bending": {
            "unit_basis": "per_piece",
            "unit_rate": 50.0,
            "batch_charge": 0.0,
            "note": (
                "DERIVED: press-brake 5-20 per stroke (IndiaMART Daijo, undated; may be a minimum-"
                "charge artefact) x an ASSUMED 4 bends per part at about 12.5 each."
            ),
        },
        "cutting": {
            "unit_basis": "per_piece",
            "unit_rate": 25.0,
            "batch_charge": 0.0,
            "note": (
                "DERIVED: no bandsaw per-cut rate found; ASSUMED 5 min per cut at a 300/h "
                "machine-and-operator rate (from the turning job-work rate)."
            ),
        },
        "welding": {
            "unit_basis": "per_piece",
            "unit_rate": 100.0,
            "batch_charge": 0.0,
            "note": (
                "DERIVED: aluminium welding 100/m (IndiaMART Elegant Fab Tech, undated) x an "
                "ASSUMED 1 m of weld per part. MIG labour 200-500/h (blog). No MS per-kg figure."
            ),
        },
        "finishing": {
            "unit_basis": "per_kg",
            "unit_rate": 85.0,
            "batch_charge": 0.0,
            "note": (
                "SOURCED (marketplace): powder coating 50-120/kg (IndiaMART Shakti Coat, "
                "undated). Others found: hardening 30/kg, gas nitriding 40/kg, vacuum HT 140/kg, "
                "hot-dip galvanizing 21-110/kg. Enter the mass the finish is charged on."
            ),
        },
        "moulding": {
            "unit_basis": "per_piece",
            "unit_rate": 15.0,
            "batch_charge": 250000.0,
            "note": (
                "ESTIMATED (assistant, demo only): ~30 g ABS part, 30 s cycle at ~800/h plus "
                "resin; simple single-cavity mould 2-5 lakh (Zetwerk blog quotes USD 3,500-8,000 "
                "for a simple 2-plate mould). Rate includes the resin."
            ),
        },
        "additive_build": {
            "unit_basis": "per_kg",
            "unit_rate": 6000.0,
            "batch_charge": 0.0,
            "note": (
                "SOURCED (marketplace): FDM PLA about 6/g = 6,000/kg (iamrapid, Zbotic, undated). "
                "SLS/MJF nylon 32-40/cm3; metal AM not found. Enter the build mass."
            ),
        },
        "purchase": {
            "unit_basis": "per_piece",
            "unit_rate": 500.0,
            "batch_charge": 0.0,
            "note": (
                "ESTIMATED (assistant, demo only): one flat price for any bought part, so the "
                "Purchased route can be shown. Meaningless for a real part - use its own quote."
            ),
        },
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    request = urllib.request.Request(
        f"{args.base_url.rstrip('/')}/rates",
        data=json.dumps(RATE_SET).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            stored = json.load(response)
    except urllib.error.HTTPError as error:
        print(f"Not published: HTTP {error.code} {error.read().decode()[:400]}")
        return 1
    print(
        f"Published {stored['rate_set_id']} v{stored['version']} (illustrative): "
        f"{len(stored['materials'])} materials, {len(stored['operations'])} processes, "
        f"{len(stored['stage_rates'])} stage rates."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
