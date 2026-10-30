"""Publish an ILLUSTRATIVE welding time profile through the ordinary API.

    python scripts\\publish_illustrative_welding_time_profile.py --base-url http://127.0.0.1:8001
    python scripts\\publish_illustrative_welding_time_profile.py --dry-run   # print, publish nothing

Nothing here is client data. The seeded baseline profile (`poc-baseline-time`) describes a
machining shop and deliberately carries no welding figures, so a weld operation's time is
"needs input" under it. This script publishes a *new* profile that has two parts:

  1. the baseline's machining parameters, read from the running server's own
     `poc-baseline-time` and copied unchanged — so an estimate made under this profile times
     milling, drilling and turning exactly as the baseline does; and
  2. the five welding parameters, each labelled below. None of them has a published source
     behind it: every one is ESTIMATED — the assistant's own typical-shop figure, added at the
     user's request "for demo purposes only" — and must be replaced before any real use.

The profile is published with is_illustrative = true, which keeps the app's illustrative-time
warning on every time estimated with it. Publishing goes through POST /time-profiles like any
other profile; a version that already exists is immutable, so this script refuses to publish it
again (and the server would answer 409). The seeded baseline is only read, never changed. Weld
length and size are never read from the CAD model: the engineer types them on the weld.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

BASELINE_ID = "poc-baseline-time"

PROFILE_ID = "illustrative-welding-2026"
PROFILE_VERSION = "1.0.0"
PROFILE_NAME = "Illustrative welding + machining (demo, Oct 2026)"
PROFILE_DESCRIPTION = (
    "ILLUSTRATIVE, not client data. The machining parameters are the seeded poc-baseline-time "
    "values copied unchanged; the five welding parameters are ESTIMATED typical manual MIG/TIG "
    "figures for demo use only. Replace with a shop's own values before treating any time as "
    "usable."
)

# The five welding parameters. All ESTIMATED (assistant, demo only): no published source was
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


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def build_profile(baseline_assumptions: dict, version: str = PROFILE_VERSION) -> dict:
    """The baseline's assumptions, unchanged, plus the welding parameters."""
    already = sorted(k for k in WELDING_PARAMETERS if baseline_assumptions.get(k) is not None)
    if already:
        raise ValueError(
            "The baseline already carries welding parameters "
            f"({', '.join(already)}); this script would silently change them. Not publishing."
        )
    return {
        "time_profile_id": PROFILE_ID,
        "name": PROFILE_NAME,
        "version": version,
        "description": PROFILE_DESCRIPTION,
        "is_illustrative": True,
        "assumptions": {**baseline_assumptions, **WELDING_PARAMETERS},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--dry-run", action="store_true", help="print the profile, publish nothing")
    parser.add_argument(
        "--version",
        default=PROFILE_VERSION,
        help="version to publish; raise it when this one already exists (versions are immutable)",
    )
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    try:
        baseline = _get_json(f"{base}/time-profiles/{BASELINE_ID}")
        existing = _get_json(f"{base}/time-profiles")
    except (urllib.error.URLError, OSError) as error:
        print(f"Could not read the baseline profile from {base}: {error}")
        return 1

    # A server that predates timed welding would accept the profile and silently drop the five
    # welding fields, leaving an immutable profile with no welding in it. Ask its own schema first.
    try:
        known = _get_json(f"{base}/openapi.json")["components"]["schemas"]["TimeAssumptions"]["properties"]
    except (urllib.error.URLError, OSError, KeyError) as error:
        print(f"Could not read the server's TimeAssumptions schema from {base}: {error}")
        return 1
    unsupported = sorted(k for k in WELDING_PARAMETERS if k not in known)
    if unsupported:
        print(
            f"Not published: the server at {base} predates timed welding (its TimeAssumptions has "
            f"no {', '.join(unsupported)}). Restart it on the current code and try again."
        )
        return 1

    if any(p["time_profile_id"] == PROFILE_ID and p["version"] == args.version for p in existing):
        print(
            f"Not published: {PROFILE_ID} v{args.version} already exists on {base}. A published "
            "version is immutable; publish a higher one with --version."
        )
        return 1

    try:
        profile = build_profile(baseline["assumptions"], args.version)
    except ValueError as error:
        print(error)
        return 1

    if args.dry_run:
        print(json.dumps(profile, indent=2))
        return 0

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
        print(f"Not published: HTTP {error.code} {error.read().decode()[:400]}")
        return 1

    stored_assumptions = stored["assumptions"]
    # The server must have kept the five welding values, or an older server dropped them.
    kept = {k: stored_assumptions.get(k) for k in WELDING_PARAMETERS}
    if kept != WELDING_PARAMETERS:
        print(
            f"Published {stored['time_profile_id']} v{stored['version']} but the server did not "
            f"keep the welding parameters (got {kept}); it likely predates timed welding."
        )
        return 1
    machining = {k: v for k, v in baseline["assumptions"].items() if k not in WELDING_PARAMETERS}
    changed = [k for k, v in machining.items() if stored_assumptions.get(k) != v]
    print(
        f"Published {stored['time_profile_id']} v{stored['version']} (illustrative): "
        f"{len(machining)} machining parameters copied from {BASELINE_ID} "
        f"({len(changed)} differ), {len(WELDING_PARAMETERS)} welding parameters added."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
