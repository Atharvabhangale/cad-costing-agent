"""Publish the ILLUSTRATIVE 4-axis indexer time profile through the ordinary API.

    python scripts\\publish_illustrative_indexer_time_profile.py --base-url http://127.0.0.1:8001
    python scripts\\publish_illustrative_indexer_time_profile.py --dry-run   # print, publish nothing

Nothing here is client data. The seeded baseline profile (`poc-baseline-time`) states no index
time, so the index line a 4-axis setup proposal adds needs a time typed under it. This script
publishes one profile version made of:

  1. the baseline's parameters, read from the running server's own `poc-baseline-time` and
     copied unchanged, so every machining estimate under this profile equals the baseline's; and
  2. one index time, `rotary_index_minutes_per_position`, labelled below. It has no published
     source behind it: it is ESTIMATED — the assistant's own figure, added for demo use — and
     must be replaced with a shop's own before any real use.

Versions (a published version is immutable, so a change is always a new one):

  1.0.0  the baseline plus 0.05 min (3 s) per index move.

Running it is idempotent: a version the server already has is left exactly as it is, after
checking that its index time is the one listed here. The profile is published with
is_illustrative = true, which keeps the app's illustrative-time warning on every time estimated
with it. Plans must be built under this profile (choose it before suggesting operations) for the
index line to be timed from it.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

BASELINE_ID = "poc-baseline-time"
PROFILE_ID = "illustrative-indexer-2026"
PROFILE_NAME = "Illustrative 4-axis indexer + machining (demo, Nov 2026)"

# ESTIMATED (assistant, demo only; no published source used): a small NC rotary table turning
# a few tens of degrees between positions, with unclamping and clamping, is commonly a matter
# of seconds; 3 s per move is taken as a round mid figure.
INDEXER_PARAMETERS = {"rotary_index_minutes_per_position": 0.05}

DESCRIPTION_1_0_0 = (
    "ILLUSTRATIVE, not client data. The machining parameters are the seeded poc-baseline-time "
    "values copied unchanged; the index time (0.05 min = 3 s per move: unclamp, turn, clamp) is "
    "ESTIMATED for demo use only, with no published source behind it. Replace with a shop's own "
    "value before treating any time as usable."
)

VERSIONS = {"1.0.0": (INDEXER_PARAMETERS, DESCRIPTION_1_0_0)}
ALL_KEYS = tuple(INDEXER_PARAMETERS)


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def build_profile(baseline_assumptions: dict, version: str = "1.0.0") -> dict:
    """The baseline's assumptions, unchanged, plus that version's index time."""
    parameters, description = VERSIONS[version]
    already = sorted(k for k in ALL_KEYS if baseline_assumptions.get(k) is not None)
    if already:
        raise ValueError(
            f"The baseline already states {', '.join(already)}; this script would silently "
            "change it. Not publishing."
        )
    return {
        "time_profile_id": PROFILE_ID,
        "name": PROFILE_NAME,
        "version": version,
        "description": description,
        "is_illustrative": True,
        "assumptions": {**baseline_assumptions, **parameters},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--dry-run", action="store_true", help="print the profile, publish nothing")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    try:
        baseline = _get_json(f"{base}/time-profiles/{BASELINE_ID}")
        existing = _get_json(f"{base}/time-profiles")
        known = _get_json(f"{base}/openapi.json")["components"]["schemas"]["TimeAssumptions"]["properties"]
    except (urllib.error.URLError, OSError, KeyError) as error:
        print(f"Could not read the baseline profile or the server's schema from {base}: {error}")
        return 1

    published = {p["version"] for p in existing if p["time_profile_id"] == PROFILE_ID}
    failed = False
    for version, (parameters, _) in VERSIONS.items():
        if version in published:
            stored = _get_json(f"{base}/time-profiles/{PROFILE_ID}?version={version}")
            held = {k: stored["assumptions"].get(k) for k in parameters}
            if held == parameters:
                print(f"Already published: {PROFILE_ID} v{version} on {base}; left as it is.")
            else:
                print(
                    f"{PROFILE_ID} v{version} exists on {base} with a different index time "
                    f"({held}). A published version is immutable; nothing was changed."
                )
                failed = True
            continue

        missing = sorted(k for k in parameters if k not in known)
        if missing:
            print(
                f"Not published: the server at {base} has no {', '.join(missing)}. Restart it on "
                "the current code and try again."
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
                json.load(response)
        except urllib.error.HTTPError as error:
            print(f"Not published: v{version}: HTTP {error.code} {error.read().decode()[:400]}")
            failed = True
            continue
        print(f"Published {PROFILE_ID} v{version} on {base} (illustrative).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
