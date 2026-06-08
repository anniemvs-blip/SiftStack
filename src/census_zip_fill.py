"""Fill missing ZIP codes via the US Census Geocoder.

Free, no API key required, no rate limits documented. Use as a backstop
when Smarty is unavailable (no subscription) or as the primary path for
ZIP-only enrichment.

Endpoint: https://geocoding.geo.census.gov/geocoder/locations/onelineaddress

Returns the USPS ZIP5 for a street + city + state input. Does NOT provide
DPV validation or vacant flag — for that use Smarty. But for the common
case of "I have an address, what's its ZIP," this is sufficient.
"""

import logging
import time
from typing import Iterable

import requests

logger = logging.getLogger(__name__)

GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
REQUEST_DELAY_SEC = 0.2
TIMEOUT_SEC = 15


def lookup_components(street: str, city: str = "", state: str = "OH", zip_: str = "") -> dict:
    """Return matched {city, state, zip} for an address, or {} if no match.

    Accepts whatever fields are available — Census Geocoder's onelineaddress
    endpoint will match on any combination of street + (city OR zip) + state.
    Used to fill missing city for recorder records (have street+state+zip)
    and missing zip for probate records (have street+city+state).
    """
    if not street:
        return {}

    parts = [street]
    if city:
        parts.append(city)
    parts.append(state or "OH")
    if zip_ and not city:
        # When city is missing, include zip in the one-line to disambiguate
        parts.append(zip_)
    one_line = ", ".join(parts)

    try:
        resp = requests.get(
            GEOCODER_URL,
            params={
                "address": one_line,
                "benchmark": "Public_AR_Current",
                "format": "json",
            },
            timeout=TIMEOUT_SEC,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.debug("Census Geocoder failed for %r: %s", one_line, e)
        return {}

    matches = (resp.json().get("result") or {}).get("addressMatches") or []
    if not matches:
        return {}
    comps = matches[0].get("addressComponents") or {}
    return {
        "city": (comps.get("city") or "").strip(),
        "state": (comps.get("state") or "").strip(),
        "zip": (comps.get("zip") or "").strip(),
    }


def lookup_zip(street: str, city: str, state: str = "OH") -> str:
    """Return ZIP5 for an address (legacy single-field helper)."""
    return lookup_components(street, city, state).get("zip", "")


def backfill_address_fields(notices: Iterable, delay_sec: float = REQUEST_DELAY_SEC) -> int:
    """Fill missing city and/or zip on each notice via Census Geocoder.

    Mutates notices in place. Returns the count of records that had at
    least one field filled. Records without a street (the only required
    seed) are skipped. Already-complete records are skipped too.
    """
    candidates = [
        n for n in notices
        if n.address and ((not n.zip) or (not n.city))
    ]
    if not candidates:
        return 0

    logger.info("Census backstop: %d records missing city and/or ZIP", len(candidates))
    touched = 0
    for i, n in enumerate(candidates, 1):
        if delay_sec:
            time.sleep(delay_sec)
        comps = lookup_components(n.address, n.city, n.state or "OH", n.zip)
        changed = False
        if not n.city and comps.get("city"):
            n.city = comps["city"]
            changed = True
        if not n.zip and comps.get("zip"):
            n.zip = comps["zip"]
            changed = True
        if changed:
            touched += 1
        if i % 25 == 0:
            logger.info("  Census backstop: %d/%d processed (%d filled)",
                        i, len(candidates), touched)

    logger.info("Census backstop done: %d/%d filled", touched, len(candidates))
    return touched


# Backwards-compatible alias for the original ZIP-only entry point
fill_missing_zips = backfill_address_fields


if __name__ == "__main__":
    # Smoke test
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    for street, city in [
        ("3471 BINBROOK RD N", "COLUMBUS"),
        ("3401 LACOSTE LN", "COLUMBUS"),
        ("1302 N 6TH ST", "COLUMBUS"),
    ]:
        z = lookup_zip(street, city)
        print(f"  {street}, {city}, OH → {z or '(no match)'}")
