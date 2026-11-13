"""Publish the ILLUSTRATIVE injection-moulding time profile through the ordinary API.

    python scripts\\publish_illustrative_moulding_time_profile.py --base-url http://127.0.0.1:8001
    python scripts\\publish_illustrative_moulding_time_profile.py --dry-run   # print, publish nothing

Nothing here is client data. The seeded baseline profile (`poc-baseline-time`) describes a
machining shop and deliberately carries no moulding figures, so a moulding line's time is
"needs input" under it. This script publishes the versions of one profile, each with two parts:

  1. the baseline's machining parameters, read from the running server's own
     `poc-baseline-time` and copied unchanged — so an estimate made under this profile times
     milling, drilling and turning exactly as the baseline does; and
  2. the moulding parameters, each labelled below. None of them has a published source behind
     it: every one is ESTIMATED — the assistant's own typical figure for a general-purpose
     grade of each polymer and a small toggle press, for demo purposes only — and must be
     replaced before any real use. The thermal rows are material properties; a shop should
     take them from its resin supplier's datasheet.

Versions (a published version is immutable, so a change is always a new one):

  1.0.0  thermal rows for ABS and polypropylene, fill, pack and hold, open/eject/close, and the
         moulding setup.

Running it is idempotent. A version the server does not have is published; one it already has
is left exactly as it is and reported, after checking that its moulding parameters are the
ones listed here. Earlier estimates keep the version they were made under.

The profile is published with is_illustrative = true, which keeps the app's illustrative-time
warning on every time estimated with it. The governing wall is never typed: it is measured from
the part (POST /cad/governing-wall). The cavity count is the engineer's, entered per estimate.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

BASELINE_ID = "poc-baseline-time"

PROFILE_ID = "illustrative-moulding-2026"
PROFILE_NAME = "Illustrative injection moulding + machining (demo, Nov 2026)"

# Material names match the rate set's material keys, which is how the estimator finds a row.
MOULDING_MATERIALS = [
    {
        "material": "abs",
        # ESTIMATED: thermal diffusivity of a general-purpose ABS, about 1e-7 m²/s.
        "thermal_diffusivity_mm2_per_s": 0.1,
        # ESTIMATED: melt 230 °C, mould 60 °C, ejection 90 °C — typical processing window.
        "melt_temperature_c": 230.0,
        "mould_temperature_c": 60.0,
        "eject_temperature_c": 90.0,
        "note": "ESTIMATED, no published source: replace with the resin datasheet's values",
    },
    {
        "material": "pp",
        # ESTIMATED: thermal diffusivity of a homopolymer polypropylene, about 1e-7 m²/s.
        "thermal_diffusivity_mm2_per_s": 0.1,
        # ESTIMATED: melt 220 °C, mould 40 °C, ejection 90 °C — typical processing window.
        "melt_temperature_c": 220.0,
        "mould_temperature_c": 40.0,
        "eject_temperature_c": 90.0,
        "note": "ESTIMATED, no published source: replace with the resin datasheet's values",
    },
]

MOULDING_PARAMETERS = {
    "moulding_materials": MOULDING_MATERIALS,
    # ESTIMATED: injecting a small shot, about 1.5 s.
    "moulding_fill_seconds": 1.5,
    # ESTIMATED: packing and holding pressure after the fill, about 3 s.
    "moulding_pack_seconds": 3.0,
    # ESTIMATED: opening, ejecting and closing a small toggle press, about 5 s.
    "moulding_open_eject_seconds": 5.0,
    # ESTIMATED: hanging, heating and proving a mould, once per setup.
    "moulding_setup_minutes": 60.0,
}

DESCRIPTION_1_0_0 = (
    "ILLUSTRATIVE, not client data. The machining parameters are the seeded poc-baseline-time "
    "values copied unchanged; the moulding thermal rows (ABS, polypropylene: diffusivity, melt, "
    "mould and ejection temperatures), the fill, pack, open/eject times and the moulding setup "
    "are ESTIMATED typical figures for demo use only, with no published source behind them. "
    "Replace with a resin datasheet's and a shop's own values before treating any time as usable."
)

# Every version this script knows, oldest first: (moulding parameters, description).
VERSIONS = {
    "1.0.0": (MOULDING_PARAMETERS, DESCRIPTION_1_0_0),
}
PROFILE_VERSION = list(VERSIONS)[-1]
ALL_MOULDING_KEYS = tuple(VERSIONS[PROFILE_VERSION][0])


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def build_profile(baseline_assumptions: dict, version: str = PROFILE_VERSION) -> dict:
    """The baseline's assumptions, unchanged, plus that version's moulding parameters."""
    moulding, description = VERSIONS[version]
    already = sorted(k for k in ALL_MOULDING_KEYS if baseline_assumptions.get(k) is not None)
    if already:
        raise ValueError(
            "The baseline already carries moulding parameters "
            f"({', '.join(already)}); this script would silently change them. Not publishing."
        )
    return {
        "time_profile_id": PROFILE_ID,
        "name": PROFILE_NAME,
        "version": version,
        "description": description,
        "is_illustrative": True,
        "assumptions": {**baseline_assumptions, **moulding},
    }


def _moulding_of(assumptions: dict) -> dict:
    """The moulding parameters an assumptions object actually sets (absent and null alike)."""
    return {k: assumptions[k] for k in ALL_MOULDING_KEYS if assumptions.get(k) is not None}


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
        moulding = VERSIONS[version][0]

        if version in published:
            stored = _get_json(f"{base}/time-profiles/{PROFILE_ID}?version={version}")
            if _moulding_of(stored["assumptions"]) == moulding:
                print(f"Already published: {PROFILE_ID} v{version} on {base}; left as it is.")
            else:
                print(
                    f"{PROFILE_ID} v{version} exists on {base} with different moulding "
                    "parameters. A published version is immutable; nothing was changed."
                )
                failed = True
            continue

        unsupported = sorted(k for k in moulding if k not in known)
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

        kept = _moulding_of(stored["assumptions"])
        if kept != moulding:
            print(
                f"Published {stored['time_profile_id']} v{stored['version']} but the server did "
                f"not keep the moulding parameters (got {kept}); it likely predates them."
            )
            failed = True
            continue
        others = {k: v for k, v in baseline["assumptions"].items() if k not in ALL_MOULDING_KEYS}
        changed = [k for k, v in others.items() if stored["assumptions"].get(k) != v]
        print(
            f"Published {stored['time_profile_id']} v{stored['version']} (illustrative): "
            f"{len(others)} other parameters copied from {BASELINE_ID} "
            f"({len(changed)} differ), {len(moulding)} moulding parameters set."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
