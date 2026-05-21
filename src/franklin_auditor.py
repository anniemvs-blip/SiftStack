"""Franklin County, OH Auditor parcel → address lookup.

Hits the same ArcGIS REST service used by oh_franklin_tax_delinquent, but
against the master parcel layer (Parcel_Features) so non-delinquent parcels
resolve too. Used by the enrichment pipeline to fill `address` and `zip` on
recorder records (which arrive with parcel_id but no street address).

PARCELID normalization
----------------------
Recorder publishes parcel ids as ``DDD-BBBBBBB-SS`` where ``-SS`` is a condo /
subdivision suffix (often ``-00``) and the base segment may carry an extra
leading zero. The GIS layer stores canonical ``DDD-NNNNNN`` (district + 6-digit
base, no suffix). We normalize before querying:

  ``010-0077728-00`` → ``010-077728``
  ``010-295406-00``  → ``010-295406``
  ``010-104327``     → ``010-104327`` (already canonical)

The trailing suffix is dropped because the master parcel layer keys on the
underlying real-estate parcel, not the condo unit — the SITEADDRESS is the same
for every unit in a building.
"""

from __future__ import annotations

import logging
from typing import Iterable

import requests

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

PARCEL_LAYER = (
    "https://gis.franklincountyohio.gov/hosting/rest/services/"
    "ParcelFeatures/Parcel_Features/MapServer/0"
)
OUT_FIELDS = "PARCELID,SITEADDRESS,ZIPCD,OWNERNME1"
BATCH_SIZE = 50  # ArcGIS WHERE clauses with many ORs stay performant up to a few hundred


def normalize_parcel_id(parcel_id: str) -> str:
    """Convert recorder-format parcel id to GIS canonical form.

    Returns the input stripped of whitespace if it doesn't have the expected
    ``district-base[-suffix]`` shape — caller decides whether to query anyway.
    """
    parts = parcel_id.strip().split("-")
    if len(parts) < 2:
        return parcel_id.strip()
    district = parts[0].strip()
    base = parts[1].strip().lstrip("0") or "0"
    base = base.zfill(6)
    return f"{district}-{base}"


def lookup_parcel_address(parcel_id: str) -> dict | None:
    """Resolve a single parcel id to address+zip+owner. Returns None on miss."""
    norm = normalize_parcel_id(parcel_id)
    if not norm:
        return None
    try:
        resp = requests.get(
            f"{PARCEL_LAYER}/query",
            params={
                "where": f"PARCELID='{norm}'",
                "outFields": OUT_FIELDS,
                "returnGeometry": "false",
                "f": "json",
            },
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Franklin Auditor lookup failed for %s: %s", parcel_id, e)
        return None
    feats = resp.json().get("features", [])
    if not feats:
        return None
    a = feats[0].get("attributes", {})
    return {
        "address": (a.get("SITEADDRESS") or "").strip(),
        "zip": str(a.get("ZIPCD") or "").strip(),
        "owner_gis": (a.get("OWNERNME1") or "").strip(),
        "parcel_canonical": (a.get("PARCELID") or "").strip(),
    }


def _batch_query(norm_to_orig: dict[str, str]) -> dict[str, dict]:
    """Bulk lookup. Returns {original_parcel_id: attrs}."""
    if not norm_to_orig:
        return {}
    where_in = ",".join(f"'{p}'" for p in norm_to_orig.keys())
    try:
        resp = requests.get(
            f"{PARCEL_LAYER}/query",
            params={
                "where": f"PARCELID IN ({where_in})",
                "outFields": OUT_FIELDS,
                "returnGeometry": "false",
                "f": "json",
            },
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Franklin Auditor batch lookup failed: %s", e)
        return {}

    results: dict[str, dict] = {}
    for f in resp.json().get("features", []):
        a = f.get("attributes", {})
        norm = (a.get("PARCELID") or "").strip()
        orig = norm_to_orig.get(norm)
        if not orig:
            continue
        results[orig] = {
            "address": (a.get("SITEADDRESS") or "").strip(),
            "zip": str(a.get("ZIPCD") or "").strip(),
            "owner_gis": (a.get("OWNERNME1") or "").strip(),
            "parcel_canonical": norm,
        }
    return results


def lookup_address_by_owner_name(owner_name: str) -> dict | None:
    """Resolve an owner name to a property address via the Auditor parcel layer.

    Returns the single match if exactly one parcel comes back. Returns None on
    zero matches or multiple matches — we don't guess between candidates, since
    a federal tax lien or foreclosure attaches to a specific property and the
    recorder filing doesn't tell us which one.

    Used as a fallback when the recorder index didn't expose a parcel_id, which
    is common for federal tax liens (filed against the person, not a parcel)
    and for lis pendens NOTICE filings with abbreviated legal descriptions.

    Name format: GIS uses ``LAST FIRST [MIDDLE]`` for individuals and
    ``COMPANY NAME`` verbatim for entities. The recorder index uses the same
    convention, so a substring-anchored LIKE generally hits.
    """
    name = (owner_name or "").strip().upper()
    if not name:
        return None
    parts = name.split()
    if len(parts) < 2:
        return None
    last, first = parts[0], parts[1]
    # Escape single quotes in the unlikely case a name has one (e.g. O'BRIEN).
    last_q = last.replace("'", "''")
    first_q = first.replace("'", "''")
    where = f"OWNERNME1 LIKE '{last_q}%{first_q}%'"
    try:
        resp = requests.get(
            f"{PARCEL_LAYER}/query",
            params={
                "where": where,
                "outFields": OUT_FIELDS,
                "returnGeometry": "false",
                "resultRecordCount": 5,
                "f": "json",
            },
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Franklin Auditor name lookup failed for %s: %s", name, e)
        return None
    feats = resp.json().get("features", [])
    if len(feats) != 1:
        return None
    a = feats[0].get("attributes", {})
    return {
        "address": (a.get("SITEADDRESS") or "").strip(),
        "zip": str(a.get("ZIPCD") or "").strip(),
        "owner_gis": (a.get("OWNERNME1") or "").strip(),
        "parcel_canonical": (a.get("PARCELID") or "").strip(),
    }


def fill_addresses_from_owner_names(
    notices: Iterable[NoticeData],
    notice_types: tuple[str, ...] = ("tax_delinquent", "foreclosure"),
) -> tuple[int, int]:
    """Fallback resolver for recorder records that have no parcel_id.

    Only fires for the notice types where the named grantor IS the property
    owner — federal tax liens (lien attaches to the person) and lis pendens
    foreclosures (defendant = owner being foreclosed on). Probate certificates
    of transfer are intentionally excluded: the named grantor is often the heir
    receiving the property, not the current owner of record.

    Mutates the passed notices in place. Returns ``(filled, attempted)``.
    """
    targets = [
        n for n in notices
        if (n.county or "").strip().lower() == "franklin"
        and not (n.address or "").strip()
        and not (n.parcel_id or "").strip()
        and (n.owner_name or "").strip()
        and n.notice_type in notice_types
    ]
    if not targets:
        return (0, 0)

    filled = 0
    for n in targets:
        match = lookup_address_by_owner_name(n.owner_name)
        if match and match.get("address"):
            n.address = match["address"]
            if not (n.zip or "").strip() and match["zip"]:
                n.zip = match["zip"]
            if not (n.parcel_id or "").strip() and match["parcel_canonical"]:
                n.parcel_id = match["parcel_canonical"]
            filled += 1
    return (filled, len(targets))


def fill_addresses_from_parcels(notices: Iterable[NoticeData]) -> tuple[int, int]:
    """Populate empty `address` / `zip` on Franklin notices that have a parcel_id.

    Mutates the passed notices in place. Returns ``(filled, attempted)``.

    Only touches notices where:
      - ``county`` is Franklin
      - ``parcel_id`` is non-empty
      - ``address`` is empty (won't overwrite existing addresses)
    """
    targets = [
        n for n in notices
        if (n.county or "").strip().lower() == "franklin"
        and (n.parcel_id or "").strip()
        and not (n.address or "").strip()
    ]
    if not targets:
        return (0, 0)

    # Build normalized→list-of-notices map (multiple recorder rows can share a parcel)
    norm_to_notices: dict[str, list[NoticeData]] = {}
    for n in targets:
        norm = normalize_parcel_id(n.parcel_id)
        if norm:
            norm_to_notices.setdefault(norm, []).append(n)

    # Batch in chunks to keep WHERE IN clauses sane
    keys = list(norm_to_notices.keys())
    filled = 0
    for i in range(0, len(keys), BATCH_SIZE):
        chunk = keys[i:i + BATCH_SIZE]
        results = _batch_query({k: k for k in chunk})
        for norm, attrs in results.items():
            if not attrs.get("address"):
                continue
            for n in norm_to_notices.get(norm, []):
                n.address = attrs["address"]
                if not (n.zip or "").strip() and attrs["zip"]:
                    n.zip = attrs["zip"]
                filled += 1

    return (filled, len(targets))
