"""Prepare a running CAD Costing Agent for a live demo.

This is a demo tool, not part of the application. It talks to a running server
over the ordinary HTTP API and does only things a person could do through the
interface:

  1. reports whether the database behind the server is a clean one,
  2. publishes a rate set version whose material prices are realistic, because
     the seeded ones are placeholders (steel at Rs 0.80/kg) that make material
     look free beside the machining,
  3. warms OpenCASCADE with one geometry read, so the first upload in front of
     an audience is not the five-second one,
  4. prints the steps that have to be done by hand.

It writes no estimate and deletes nothing. Publishing a rate set adds an
immutable new version beside the existing ones; it never edits or removes one.

    python scripts/demo_preflight.py
    python scripts/demo_preflight.py --base-url http://127.0.0.1:8000
    python scripts/demo_preflight.py --skip-rates

The realistic figures below are representative Indian market prices for
demonstration. They are *still* marked illustrative, deliberately: they are not
this shop's measured costs, and the warning the application shows about that is
worth more than a tidier screen.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_PART = Path("cadmodels/model1_1.stp")

# The family to add a version to, and the version this script publishes.
#
# `poc-default` rather than `poc-machine-shop`: the machine-shop profile prices
# only drilling and milling, and the demo assembly contains turning, so costing
# it against that profile fails with "has no rate for process type 'turning'".
RATE_SET_ID = "poc-default"
DEMO_VERSION = "9.0.0"

# Every process the demo's two files produce operations for. Checked after
# publishing, because an unpriced process is a live-demo failure, not a warning.
PROCESSES_THE_DEMO_NEEDS = ("drilling", "milling", "turning")

# `poc-default` seeds these as zero, which makes the breakdown five noughts
# under one number. These are the values the other seeded profile
# (`poc-machine-shop`) already carries, so the demo shows a complete breakdown
# without this script inventing a figure of its own.
DEMO_ADDITIONAL_COSTS = {
    "overhead_rate_percent": 12.5,
    "scrap_rate_percent": 6.0,
    "tooling_cost_per_batch": 300.0,
}

# Representative market prices, in INR per kg. The seeded values are 0.80 and
# 2.20, which are placeholders rather than anything a shop would recognise.
REALISTIC_MATERIAL_PRICES = {
    "steel": 75.0,
    "aluminium": 280.0,
    "stainless_steel": 260.0,
    "brass": 620.0,
}


def _request(url: str, *, method: str = "GET", payload: dict | None = None,
             timeout: float = 120.0):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body) if body else None


def _upload(url: str, path: Path, timeout: float = 300.0):
    """Post one file as multipart/form-data, the way the browser does."""
    boundary = "----demo-preflight-boundary"
    body = b"".join([
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(),
        path.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    request = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def check_server(base_url: str) -> bool:
    try:
        health = _request(f"{base_url}/health", timeout=10)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"  FAIL  no server at {base_url} ({exc})")
        return False
    print(f"  ok    server responding: {health}")
    return True


def check_database_is_clean(base_url: str) -> bool:
    """A demo should not open onto someone else's development history."""
    page = _request(f"{base_url}/estimates?page=1&size=1")
    total = page.get("total", page.get("total_items", 0)) if isinstance(page, dict) else 0
    if total == 0:
        print("  ok    database is empty - History will open clean")
        return True
    print(f"  WARN  this database already holds {total} estimate(s).")
    print("        History will show them. Restart against a fresh file:")
    print("          CAD_COSTING_DATABASE_URL=sqlite:///./demo.db "
          "uvicorn cad_costing_agent.main:app")
    return False


def publish_realistic_rates(base_url: str) -> str | None:
    """Add a version whose material prices are not placeholders.

    Versions are immutable, so this adds one beside the seeded versions rather
    than changing them. Running the script twice is harmless: the second run
    finds the version already published and leaves it alone.
    """
    existing = _request(f"{base_url}/rates")
    rows = existing if isinstance(existing, list) else existing.get("items", [])
    if any(r.get("rate_set_id") == RATE_SET_ID and r.get("version") == DEMO_VERSION
           for r in rows):
        print(f"  ok    {RATE_SET_ID} v{DEMO_VERSION} already published")
        return DEMO_VERSION

    latest = _request(f"{base_url}/rates/{RATE_SET_ID}")
    payload = {k: v for k, v in latest.items() if k != "rate_set_id"}
    payload["version"] = DEMO_VERSION
    payload["description"] = (
        "Demo rates: representative market material prices with the project's "
        "machine rates. Still illustrative — not a shop's measured costs."
    )
    # Left true on purpose. The prices are realistic, not verified, and the
    # warning the application raises about that is the honest thing to show.
    payload["is_illustrative"] = True

    changed = []
    for key, price in REALISTIC_MATERIAL_PRICES.items():
        if key in payload.get("materials", {}):
            was = payload["materials"][key].get("cost_per_kg")
            payload["materials"][key]["cost_per_kg"] = price
            changed.append(f"{key} {was} -> {price}")
    payload["additional_costs"] = dict(DEMO_ADDITIONAL_COSTS)

    _request(f"{base_url}/rates/{RATE_SET_ID}/versions", method="POST", payload=payload)
    print(f"  ok    published {RATE_SET_ID} v{DEMO_VERSION} (INR/kg: {', '.join(changed)})")
    print(f"        overhead {DEMO_ADDITIONAL_COSTS['overhead_rate_percent']}%, "
          f"scrap {DEMO_ADDITIONAL_COSTS['scrap_rate_percent']}%, "
          f"tooling {DEMO_ADDITIONAL_COSTS['tooling_cost_per_batch']} per batch")

    published = _request(f"{base_url}/rates/{RATE_SET_ID}?version={DEMO_VERSION}")
    missing = [p for p in PROCESSES_THE_DEMO_NEEDS if p not in published.get("operations", {})]
    if missing:
        print(f"  FAIL  this rate set prices no {', '.join(missing)} — costing will refuse")
        print("        any plan that uses one. Pick a different family.")
    else:
        print(f"  ok    prices every process the demo needs: "
              f"{', '.join(PROCESSES_THE_DEMO_NEEDS)}")
    print("        It is the latest version, so it is selected by default.")
    return DEMO_VERSION


def warm_geometry(base_url: str, part: Path) -> None:
    """The first geometry call loads OpenCASCADE's native libraries.

    Measured on this project: about 5 s cold, about 0.3 s afterwards. Doing it
    here means the audience never sees the cold one.
    """
    if not part.exists():
        print(f"  WARN  {part} not found - the first live upload will be the slow one")
        return
    started = time.perf_counter()
    _upload(f"{base_url}/cad/mesh", part)
    cold = time.perf_counter() - started

    started = time.perf_counter()
    _upload(f"{base_url}/cad/mesh", part)
    warm = time.perf_counter() - started
    print(f"  ok    geometry warmed with {part.name}: {cold:.2f}s cold, {warm:.2f}s warm")


MANUAL_STEPS = """
  Still to do by hand:
    [ ] Browser at 100% zoom (Ctrl+0) and the window maximized.
        Below about 1080 CSS px the layout drops to a single column.
    [ ] Open the Quote screen once, fill Customer / project details
        (customer, project, reference, prepared by, validity) and Apply.
        Leaving Reference blank prints the 32-character estimate id instead.
    [ ] Have both files to hand:
          single part  cadmodels/model1_1.stp
          assembly     Assembly.stp  (analytic, 20 placements / 4 definitions)
        Do not demo Figure 3.stp: it carries no measurable component geometry,
        so it produces no component plans, and it is slow.
    [ ] In Manufacturing, remember BOTH clicks:
          "Use all estimated times"   commits the operation cycle times
          "Use estimate" on the Setup band   commits the setup time
        Without the second, setup cost is 0.00 and a warning stays on screen.
    [ ] For the assembly, generate component plans and commit times for each
        component you intend to show, or the roll-up reads 0.02.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--part", type=Path, default=DEFAULT_PART)
    parser.add_argument("--skip-rates", action="store_true")
    parser.add_argument("--skip-warm", action="store_true")
    args = parser.parse_args()

    print(f"Demo pre-flight against {args.base_url}\n")
    if not check_server(args.base_url):
        return 1
    clean = check_database_is_clean(args.base_url)
    if not args.skip_rates:
        publish_realistic_rates(args.base_url)
    if not args.skip_warm:
        warm_geometry(args.base_url, args.part)

    print(MANUAL_STEPS)
    print("Pre-flight complete." if clean else
          "Pre-flight complete, but the database is not clean - see the warning above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
