"""Publish the ILLUSTRATIVE sheet-cutting and bending time profile through the ordinary API.

    python scripts\\publish_illustrative_sheet_time_profile.py --base-url http://127.0.0.1:8001
    python scripts\\publish_illustrative_sheet_time_profile.py --dry-run   # print, publish nothing

Nothing here is client data. The seeded baseline profile (`poc-baseline-time`) describes a
machining shop and deliberately carries no sheet figures, so a cut or bend operation's time is
"needs input" under it. This script publishes the versions of one profile, each with two parts:

  1. the baseline's machining parameters, read from the running server's own
     `poc-baseline-time` and copied unchanged — so an estimate made under this profile times
     milling, drilling and turning exactly as the baseline does; and
  2. the sheet parameters, each labelled below. None of them has a published source behind it:
     every one is ESTIMATED — the assistant's own typical-shop figure for a fibre laser and a
     CNC press brake on mild steel, for demo purposes only — and must be replaced before any
     real use.

Versions (a published version is immutable, so a change is always a new one):

  1.0.0  a six-row cutting table (cut speed and pierce time by thickness, 1–10 mm), cutting
         setup and unloading, and bending per bend, setup, repositioning and handling.

Running it is idempotent. A version the server does not have is published; one it already has
is left exactly as it is and reported, after checking that its sheet parameters are the ones
listed here. Earlier estimates keep the version they were made under.

The profile is published with is_illustrative = true, which keeps the app's illustrative-time
warning on every time estimated with it. Publishing goes through POST /time-profiles like any
other profile. The seeded baseline is only read, never changed. Cut perimeter, pierce count,
thickness and bend count are never typed: they come from the part's measured flat pattern
(POST /cad/sheet-metal), for a K-factor the engineer enters.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

BASELINE_ID = "poc-baseline-time"

PROFILE_ID = "illustrative-sheet-2026"
PROFILE_NAME = "Illustrative sheet cutting + bending + machining (demo, Nov 2026)"

# ESTIMATED (assistant, demo only; no published source was used): a fibre laser of a few kW on
# mild steel. Cut speed falls steeply with thickness and pierce time rises with it. Thinnest
# first; the estimator interpolates linearly between rows and gives no time outside 1–10 mm.
CUTTING_TABLE = [
    {"thickness_mm": 1.0, "cut_speed_mm_per_min": 10000.0, "pierce_seconds": 0.3},
    {"thickness_mm": 2.0, "cut_speed_mm_per_min": 6000.0, "pierce_seconds": 0.5},
    {"thickness_mm": 3.0, "cut_speed_mm_per_min": 4000.0, "pierce_seconds": 0.8},
    {"thickness_mm": 5.0, "cut_speed_mm_per_min": 2800.0, "pierce_seconds": 1.5},
    {"thickness_mm": 8.0, "cut_speed_mm_per_min": 1600.0, "pierce_seconds": 3.0},
    {"thickness_mm": 10.0, "cut_speed_mm_per_min": 1100.0, "pierce_seconds": 4.0},
]

SHEET_PARAMETERS = {
    "sheet_cutting_table": CUTTING_TABLE,
    # ESTIMATED: loading the program and a sheet, once per setup (not per part).
    "sheet_cutting_setup_minutes": 10.0,
    # ESTIMATED: taking one cut part out of the skeleton and sorting it.
    "sheet_cutting_handling_minutes_per_part": 0.3,
    # ESTIMATED: one press-brake stroke with back-gauge positioning, about 15 s.
    "bending_minutes_per_bend": 0.25,
    # ESTIMATED: fitting punch and die and proving the first part, once per setup.
    "bending_setup_minutes": 20.0,
    # ESTIMATED: turning or flipping the part between bends, about 9 s.
    "bending_reposition_minutes_per_bend": 0.15,
    # ESTIMATED: picking the blank up and putting the bent part down, once per part.
    "bending_handling_minutes_per_part": 0.3,
}

DESCRIPTION_1_0_0 = (
    "ILLUSTRATIVE, not client data. The machining parameters are the seeded poc-baseline-time "
    "values copied unchanged; the sheet-cutting table (1–10 mm, cut speed and pierce time), "
    "cutting setup and unloading, and the bending times per bend, setup, repositioning and "
    "handling are ESTIMATED typical fibre-laser and press-brake figures on mild steel for demo "
    "use only, with no published source behind them. Replace with a shop's own values before "
    "treating any time as usable."
)

# Every version this script knows, oldest first: (sheet parameters, description).
VERSIONS = {
    "1.0.0": (SHEET_PARAMETERS, DESCRIPTION_1_0_0),
}
PROFILE_VERSION = list(VERSIONS)[-1]
ALL_SHEET_KEYS = tuple(VERSIONS[PROFILE_VERSION][0])


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def build_profile(baseline_assumptions: dict, version: str = PROFILE_VERSION) -> dict:
    """The baseline's assumptions, unchanged, plus that version's sheet parameters."""
    sheet, description = VERSIONS[version]
    already = sorted(k for k in ALL_SHEET_KEYS if baseline_assumptions.get(k) is not None)
    if already:
        raise ValueError(
            "The baseline already carries sheet parameters "
            f"({', '.join(already)}); this script would silently change them. Not publishing."
        )
    return {
        "time_profile_id": PROFILE_ID,
        "name": PROFILE_NAME,
        "version": version,
        "description": description,
        "is_illustrative": True,
        "assumptions": {**baseline_assumptions, **sheet},
    }


def _sheet_of(assumptions: dict) -> dict:
    """The sheet parameters an assumptions object actually sets (absent and null alike)."""
    return {k: assumptions[k] for k in ALL_SHEET_KEYS if assumptions.get(k) is not None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--dry-run", action="store_true", help="print the profiles, publish nothing")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    try:
        baseline = _get_json(f"{base}/time-profiles/{BASELINE_ID}")
        existing = _get_json(f"{base}/time-profiles")
    except (urllib.error.URLError, OSError) as error:
        print(f"Could not read the baseline profile from {base}: {error}")
        return 1

    # A server that predates a parameter would accept the profile and silently drop the field,
    # leaving an immutable version without it. Ask its own schema first.
    try:
        known = _get_json(f"{base}/openapi.json")["components"]["schemas"]["TimeAssumptions"]["properties"]
    except (urllib.error.URLError, OSError, KeyError) as error:
        print(f"Could not read the server's TimeAssumptions schema from {base}: {error}")
        return 1

    published = {p["version"] for p in existing if p["time_profile_id"] == PROFILE_ID}
    failed = False
    for version in VERSIONS:
        sheet = VERSIONS[version][0]

        if version in published:
            stored = _get_json(f"{base}/time-profiles/{PROFILE_ID}?version={version}")
            if _sheet_of(stored["assumptions"]) == sheet:
                print(f"Already published: {PROFILE_ID} v{version} on {base}; left as it is.")
            else:
                print(
                    f"{PROFILE_ID} v{version} exists on {base} with different sheet parameters. "
                    "A published version is immutable; nothing was changed."
                )
                failed = True
            continue

        unsupported = sorted(k for k in sheet if k not in known)
        if unsupported:
            print(
                f"Not published: v{version} needs parameters the server at {base} does not have "
                f"(its TimeAssumptions has no {', '.join(unsupported)}). Restart it on the "
                "current code and try again."
            )
            failed = True
            continue

        try:
            profile = build_profile(baseline["assumptions"], version)
        except ValueError as error:
            print(error)
            return 1

        if args.dry_run:
            print(json.dumps(profile, indent=2))
            continue

        request = urllib.request.Request(
            f"{base}/time-profiles",
            data=json.dumps(profile).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                stored = json.load(response)
        except urllib.error.HTTPError as error:
            print(f"Not published: v{version}: HTTP {error.code} {error.read().decode()[:400]}")
            failed = True
            continue

        kept = _sheet_of(stored["assumptions"])
        if kept != sheet:
            print(
                f"Published {stored['time_profile_id']} v{stored['version']} but the server did "
                f"not keep the sheet parameters (got {kept}); it likely predates them."
            )
            failed = True
            continue
        machining = {k: v for k, v in baseline["assumptions"].items() if k not in ALL_SHEET_KEYS}
        changed = [k for k, v in machining.items() if stored["assumptions"].get(k) != v]
        print(
            f"Published {stored['time_profile_id']} v{stored['version']} (illustrative): "
            f"{len(machining)} other parameters copied from {BASELINE_ID} "
            f"({len(changed)} differ), {len(sheet)} sheet parameters set."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
