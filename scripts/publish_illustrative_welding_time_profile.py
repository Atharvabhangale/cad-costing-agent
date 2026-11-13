"""Publish the ILLUSTRATIVE welding time profile through the ordinary API.

    python scripts\\publish_illustrative_welding_time_profile.py --base-url http://127.0.0.1:8001
    python scripts\\publish_illustrative_welding_time_profile.py --dry-run   # print, publish nothing

Nothing here is client data. The seeded baseline profile (`poc-baseline-time`) describes a
machining shop and deliberately carries no welding figures, so a weld operation's time is
"needs input" under it. This script publishes the versions of one profile, each with two parts:

  1. the baseline's machining parameters, read from the running server's own
     `poc-baseline-time` and copied unchanged — so an estimate made under this profile times
     milling, drilling and turning exactly as the baseline does; and
  2. the welding parameters, each labelled below. None of them has a published source behind
     it: every one is ESTIMATED — the assistant's own typical-shop figure, added at the user's
     request "for demo purposes only" — and must be replaced before any real use.

Versions (a published version is immutable, so a change is always a new one):

  1.0.0  the five arc-time parameters: travel speeds, size per pass, arc-on factor, setup.
  1.1.0  the same five, unchanged, plus two per-weld handling times (reposition, clamp and
         tack) that are added to each weld's cycle time.
  1.2.0  1.1.0 unchanged, plus two length-driven rates: butt-weld edge preparation and weld
         finishing (grinding and cleaning), each turned into minutes from the typed weld length.
         Weld finishing is also a route stage: a weld timed under 1.2.0 carries its own
         finishing time, so /cost/cad-part refuses it while that stage is priced by its stage
         rate; price weld finishing by weld time instead (it is then charged once, there).

The clamp-and-tack time in 1.1.0 and 1.2.0 is likewise the work of a route's fit-up stage
("Fit-up and tacking"): with welding timed, /cost/cad-part refuses a weld carrying it while
fit-up is priced by its stage rate, so price fit-up by weld time too. Repositioning is no
stage's work and is never refused. The published versions themselves are unchanged.

Running it is idempotent. Every version above that the server does not have is published, in
order; one it already has is left exactly as it is and reported, after checking that its
welding parameters are the ones listed here. So a database that already holds 1.0.0 gains
1.1.0 and nothing else, and a fresh one ends up with the same two versions. Earlier estimates
keep the version they were made under.

The profile is published with is_illustrative = true, which keeps the app's illustrative-time
warning on every time estimated with it. Publishing goes through POST /time-profiles like any
other profile. The seeded baseline is only read, never changed. Weld length and size are never
read from the CAD model: the engineer types them on the weld.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

BASELINE_ID = "poc-baseline-time"

PROFILE_ID = "illustrative-welding-2026"
PROFILE_NAME = "Illustrative welding + machining (demo, Oct 2026)"

# The five arc-time parameters. All ESTIMATED (assistant, demo only): no published source was
# used; each is a typical figure for manual welding of mild steel in a small job shop.
WELDING_PARAMETERS = {
    # ESTIMATED: manual semi-automatic MIG/MAG travel along the joint, roughly 3-6 mm/s
    # (180-360 mm/min) depending on thickness and position; 300 mm/min is a mid value for a
    # single pass on 3-8 mm mild steel.
    "welding_mig_travel_speed_mm_per_min": 300.0,
    # ESTIMATED: manual TIG is slower, typically 1-2 mm/s (60-120 mm/min) because the filler
    # is fed by hand; 100 mm/min.
    "welding_tig_travel_speed_mm_per_min": 100.0,
    # ESTIMATED: weld size one pass deposits. A single MIG pass runs a fillet of about 6 mm
    # leg (or ~6 mm of plate thickness for a butt); larger legs take more passes.
    "welding_mm_per_pass": 6.0,
    # ESTIMATED: fraction of welding time the arc is actually on. Manual welding is commonly
    # quoted at 30-50% arc-on once positioning, chipping/cleaning and wire changes are counted;
    # 0.45 is within that range.
    "welding_arc_on_factor": 0.45,
    # ESTIMATED: fixturing and tacking one weldment, once per setup (not per weld).
    "welding_setup_minutes": 15.0,
}

# The two per-weld handling times added in 1.1.0. Both ESTIMATED (assistant, demo only): no
# published source was found or used for either number. Each is added once to every weld's
# cycle time — per weld, per part — and never to the setup.
#
# The arc-on factor above is left at 0.45 so that 1.1.0 differs from 1.0.0 by these two lines
# only. It already allows for handling in proportion to a weld's length; these are fixed per
# weld. A shop entering its own figures must decide what its arc-on factor already covers, or
# the same minutes are counted twice.
HANDLING_PARAMETERS = {
    # ESTIMATED: moving the torch, or turning the work, to reach the next weld's start.
    "welding_reposition_minutes_per_weld": 0.5,
    # ESTIMATED: clamping the joint and placing tacks before the weld is run.
    "welding_tack_minutes_per_weld": 1.0,
}

# The two length-driven rates added in 1.2.0. Both ESTIMATED (assistant, demo only): no
# published source was found or used for either number. Each becomes minutes as weld length /
# rate and is added to the weld's cycle time.
PREP_AND_FINISHING_PARAMETERS = {
    # ESTIMATED: bevelling a butt joint before welding, both plate edges together, by hand
    # grinder or bevelling tool on mild steel; 100 mm of joint per minute. Fillet welds are
    # never charged it.
    "welding_butt_prep_mm_per_min": 100.0,
    # ESTIMATED: grinding and cleaning the finished weld (spatter, cap dressing), once along its
    # length; 200 mm of weld per minute.
    "welding_finishing_mm_per_min": 200.0,
}

DESCRIPTION_1_0_0 = (
    "ILLUSTRATIVE, not client data. The machining parameters are the seeded poc-baseline-time "
    "values copied unchanged; the five welding parameters are ESTIMATED typical manual MIG/TIG "
    "figures for demo use only. Replace with a shop's own values before treating any time as "
    "usable."
)
DESCRIPTION_1_1_0 = (
    "ILLUSTRATIVE, not client data. Version 1.0.0 unchanged, plus two per-weld handling times "
    "(reposition 0.5 min, clamp and tack 1 min) added to each weld's cycle time. Both are "
    "ESTIMATED for demo use only, with no published source behind them. Replace with a shop's "
    "own values before treating any time as usable."
)

DESCRIPTION_1_2_0 = (
    "ILLUSTRATIVE, not client data. Version 1.1.0 unchanged, plus butt-weld edge preparation "
    "(100 mm of joint per minute) and weld finishing (200 mm of weld per minute), each turned "
    "into minutes from the weld's typed length. Both are ESTIMATED for demo use only, with no "
    "published source behind them. Welds timed with this version carry their own finishing "
    "time: price a route's weld-finishing stage by weld time, or it would be charged twice "
    "(the estimate is refused). Replace with a shop's own values before treating any time as "
    "usable."
)

# Every version this script knows, oldest first: (welding parameters, description).
VERSIONS = {
    "1.0.0": (WELDING_PARAMETERS, DESCRIPTION_1_0_0),
    "1.1.0": ({**WELDING_PARAMETERS, **HANDLING_PARAMETERS}, DESCRIPTION_1_1_0),
    "1.2.0": (
        {**WELDING_PARAMETERS, **HANDLING_PARAMETERS, **PREP_AND_FINISHING_PARAMETERS},
        DESCRIPTION_1_2_0,
    ),
}
PROFILE_VERSION = list(VERSIONS)[-1]
ALL_WELDING_KEYS = tuple(VERSIONS[PROFILE_VERSION][0])


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def build_profile(baseline_assumptions: dict, version: str = PROFILE_VERSION) -> dict:
    """The baseline's assumptions, unchanged, plus that version's welding parameters."""
    welding, description = VERSIONS[version]
    already = sorted(k for k in ALL_WELDING_KEYS if baseline_assumptions.get(k) is not None)
    if already:
        raise ValueError(
            "The baseline already carries welding parameters "
            f"({', '.join(already)}); this script would silently change them. Not publishing."
        )
    return {
        "time_profile_id": PROFILE_ID,
        "name": PROFILE_NAME,
        "version": version,
        "description": description,
        "is_illustrative": True,
        "assumptions": {**baseline_assumptions, **welding},
    }


def _welding_of(assumptions: dict) -> dict:
    """The welding parameters an assumptions object actually sets (absent and null alike)."""
    return {k: assumptions[k] for k in ALL_WELDING_KEYS if assumptions.get(k) is not None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--dry-run", action="store_true", help="print the profiles, publish nothing")
    parser.add_argument(
        "--version",
        choices=list(VERSIONS),
        default=None,
        help="publish only this version; by default every version the server lacks is published",
    )
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    wanted = [args.version] if args.version else list(VERSIONS)

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
    for version in wanted:
        welding = VERSIONS[version][0]

        if version in published:
            # Immutable: never re-posted. Only checked, so a version someone published under
            # this id with other figures is reported rather than taken for this script's.
            stored = _get_json(f"{base}/time-profiles/{PROFILE_ID}?version={version}")
            if _welding_of(stored["assumptions"]) == welding:
                print(f"Already published: {PROFILE_ID} v{version} on {base}; left as it is.")
            else:
                print(
                    f"{PROFILE_ID} v{version} exists on {base} with different welding parameters "
                    f"({_welding_of(stored['assumptions'])}). A published version is immutable; "
                    "nothing was changed."
                )
                failed = True
            continue

        unsupported = sorted(k for k in welding if k not in known)
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

        stored_assumptions = stored["assumptions"]
        # The server must have kept exactly this version's welding values.
        kept = _welding_of(stored_assumptions)
        if kept != welding:
            print(
                f"Published {stored['time_profile_id']} v{stored['version']} but the server did "
                f"not keep the welding parameters (got {kept}); it likely predates them."
            )
            failed = True
            continue
        machining = {k: v for k, v in baseline["assumptions"].items() if k not in ALL_WELDING_KEYS}
        changed = [k for k, v in machining.items() if stored_assumptions.get(k) != v]
        print(
            f"Published {stored['time_profile_id']} v{stored['version']} (illustrative): "
            f"{len(machining)} machining parameters copied from {BASELINE_ID} "
            f"({len(changed)} differ), {len(welding)} welding parameters set."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
