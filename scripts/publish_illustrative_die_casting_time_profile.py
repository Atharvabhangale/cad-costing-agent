"""Publish the ILLUSTRATIVE high-pressure die-casting time profile through the ordinary API.

    python scripts\\publish_illustrative_die_casting_time_profile.py --base-url http://127.0.0.1:8001
    python scripts\\publish_illustrative_die_casting_time_profile.py --dry-run   # print, publish nothing

Nothing here is client data. The seeded baseline profile (`poc-baseline-time`) describes a
machining shop and deliberately carries no die-casting figures, so a die-casting line's time is
"needs input" under it. This script publishes the versions of one profile, each with two parts:

  1. the baseline's machining parameters, read from the running server's own
     `poc-baseline-time` and copied unchanged — so an estimate made under this profile times
     milling, drilling and turning exactly as the baseline does; and
  2. the die-casting parameters, each labelled below.

The solidification time is Chvorinov's rule. Its form is sourced; its constants are not.

  - The rule, fetched and read at https://en.wikipedia.org/wiki/Chvorinov%27s_rule :
    "Chvorinov's rule is a physical relationship that relates the solidification time for a
    simple casting to the volume and surface area of the casting." t = B(V/A)^n, where "B is
    the mold constant", and "According to Askeland, the constant n is usually 2, however
    Degarmo claims it is between 1.5 and 2." "The mold constant B depends on the properties of
    the metal, such as density, heat capacity, heat of fusion and superheat, and the mold, such
    as initial temperature, density, thermal conductivity, heat capacity and wall thickness."
    The page gives no numerical value of B.
  - So every B below, and every shot time, is ESTIMATED — the assistant's own typical figure for
    a general-purpose alloy in a steel die on a mid-size cold-chamber machine, for demo purposes
    only, with no published source behind it — and must be replaced before any real use. n = 2
    is the exponent the source quotes from Askeland.

Versions (a published version is immutable, so a change is always a new one):

  1.0.0  Chvorinov rows for aluminium and zinc, fill, open/extract/close, die spray, setup, and
         the time per confirmed core.

Running it is idempotent. A version the server does not have is published; one it already has
is left exactly as it is and reported, after checking that its die-casting parameters are the
ones listed here. Earlier estimates keep the version they were made under.

The profile is published with is_illustrative = true, which keeps the app's illustrative-time
warning on every time estimated with it. The volume and surface area are never typed: they are
measured from the part (POST /cad/casting-modulus). The cavity and core counts are the
engineer's, entered per estimate; a core proposed by POST /cad/cores/propose is timed only once
the engineer confirms the count.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

BASELINE_ID = "poc-baseline-time"

PROFILE_ID = "illustrative-die-casting-2026"
PROFILE_NAME = "Illustrative die casting + machining (demo, Nov 2026)"

# Alloy names match the rate set's material keys, which is how the estimator finds a row.
DIE_CASTING_ALLOYS = [
    {
        "material": "aluminium",
        # ESTIMATED: B for an A380-type alloy in a steel die, chosen so a 3 mm wall (V/A about
        # 1.5 mm) solidifies in a little over 2 s. No published source.
        "mould_constant": 1.0,
        # From the source: "According to Askeland, the constant n is usually 2".
        "exponent": 2.0,
        "note": "B ESTIMATED, no published source; n = 2 per Askeland as quoted on Wikipedia",
    },
    {
        "material": "zinc",
        # ESTIMATED: B for a Zamak-type alloy, below aluminium's. No published source.
        "mould_constant": 0.8,
        # From the source: "According to Askeland, the constant n is usually 2".
        "exponent": 2.0,
        "note": "B ESTIMATED, no published source; n = 2 per Askeland as quoted on Wikipedia",
    },
]

DIE_CASTING_PARAMETERS = {
    "die_casting_alloys": DIE_CASTING_ALLOYS,
    # ESTIMATED: injecting a shot on a cold-chamber machine, about 0.1 s.
    "die_casting_fill_seconds": 0.1,
    # ESTIMATED: opening the die, extracting the shot and closing again, about 10 s.
    "die_casting_open_eject_seconds": 10.0,
    # ESTIMATED: spraying die release agent and blowing off, about 5 s.
    "die_casting_spray_seconds": 5.0,
    # ESTIMATED: hanging, heating and proving a die, once per setup.
    "die_casting_setup_minutes": 120.0,
    # ESTIMATED: setting and pulling one hydraulic core per shot, about 2 s.
    "die_casting_core_seconds_per_core": 2.0,
}

DESCRIPTION_1_0_0 = (
    "ILLUSTRATIVE, not client data. The machining parameters are the seeded poc-baseline-time "
    "values copied unchanged. Die-casting solidification is Chvorinov's rule t = B(V/A)^n "
    "(https://en.wikipedia.org/wiki/Chvorinov%27s_rule; n = 2 per Askeland as quoted there). The "
    "mould constants B (aluminium, zinc), the fill, open/extract, die spray, setup and per-core "
    "times are ESTIMATED typical figures for demo use only, with no published source behind them. "
    "Replace with a foundry's own values before treating any time as usable."
)

# Every version this script knows, oldest first: (die-casting parameters, description).
VERSIONS = {
    "1.0.0": (DIE_CASTING_PARAMETERS, DESCRIPTION_1_0_0),
}
PROFILE_VERSION = list(VERSIONS)[-1]
ALL_DIE_CASTING_KEYS = tuple(VERSIONS[PROFILE_VERSION][0])


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def build_profile(baseline_assumptions: dict, version: str = PROFILE_VERSION) -> dict:
    """The baseline's assumptions, unchanged, plus that version's die-casting parameters."""
    die_casting, description = VERSIONS[version]
    already = sorted(k for k in ALL_DIE_CASTING_KEYS if baseline_assumptions.get(k) is not None)
    if already:
        raise ValueError(
            "The baseline already carries die-casting parameters "
            f"({', '.join(already)}); this script would silently change them. Not publishing."
        )
    return {
        "time_profile_id": PROFILE_ID,
        "name": PROFILE_NAME,
        "version": version,
        "description": description,
        "is_illustrative": True,
        "assumptions": {**baseline_assumptions, **die_casting},
    }


def _die_casting_of(assumptions: dict) -> dict:
    """The die-casting parameters an assumptions object actually sets (absent and null alike)."""
    return {k: assumptions[k] for k in ALL_DIE_CASTING_KEYS if assumptions.get(k) is not None}


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
        die_casting = VERSIONS[version][0]

        if version in published:
            stored = _get_json(f"{base}/time-profiles/{PROFILE_ID}?version={version}")
            if _die_casting_of(stored["assumptions"]) == die_casting:
                print(f"Already published: {PROFILE_ID} v{version} on {base}; left as it is.")
            else:
                print(
                    f"{PROFILE_ID} v{version} exists on {base} with different die-casting "
                    "parameters. A published version is immutable; nothing was changed."
                )
                failed = True
            continue

        unsupported = sorted(k for k in die_casting if k not in known)
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

        kept = _die_casting_of(stored["assumptions"])
        if kept != die_casting:
            print(
                f"Published {stored['time_profile_id']} v{stored['version']} but the server did "
                f"not keep the die-casting parameters (got {kept}); it likely predates them."
            )
            failed = True
            continue
        others = {k: v for k, v in baseline["assumptions"].items() if k not in ALL_DIE_CASTING_KEYS}
        changed = [k for k, v in others.items() if stored["assumptions"].get(k) != v]
        print(
            f"Published {stored['time_profile_id']} v{stored['version']} (illustrative): "
            f"{len(others)} other parameters copied from {BASELINE_ID} "
            f"({len(changed)} differ), {len(die_casting)} die-casting parameters set."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
