"""Filter scraped leads to workable properties via Franklin County Auditor data.

Applies to ALL notice types (probate, lis pendens, liens). Keeps residential
single-family / condo parcels within a value band that resolve to a real
parcel. Drops apartments, commercial, institutional (nursing homes), vacant
land, and out-of-band values. FLAGS (keeps, with a note) records that don't
resolve to a parcel, and probate records whose owner of record doesn't relate
to the decedent/PR (a likely rental rather than an owned home).

Auditor fields used: CLASSDSCRP (property class), TOTVALUEBASE (appraised
value), OWNERNME1 (owner of record), SITEADDRESS / PARCELID.
"""
from __future__ import annotations

import logging
import re

import requests

from franklin_auditor import PARCEL_LAYER, normalize_parcel_id

logger = logging.getLogger(__name__)

KEEP_CLASS_KEYWORDS = ("SINGLE FAMILY", "CONDO")
DEFAULT_MIN_VALUE = 50_000
DEFAULT_MAX_VALUE = 600_000
_OUT_FIELDS = "PARCELID,SITEADDRESS,CLASSCD,CLASSDSCRP,TOTVALUEBASE,OWNERNME1"


def _query(where: str) -> dict | None:
    try:
        r = requests.get(
            f"{PARCEL_LAYER}/query",
            params={"where": where, "outFields": _OUT_FIELDS,
                    "returnGeometry": "false", "f": "json"},
            timeout=20,
        )
        r.raise_for_status()
        feats = r.json().get("features", [])
        return feats[0]["attributes"] if feats else None
    except requests.RequestException as e:
        logger.debug("Auditor query failed (%s): %s", where, e)
        return None


def lookup_attrs(street: str = "", parcel_id: str = "") -> dict | None:
    """Resolve a property's Auditor attributes by parcel id, else by address."""
    if parcel_id:
        a = _query(f"PARCELID='{normalize_parcel_id(parcel_id)}'")
        if a:
            return a
    if street:
        s = street.replace("'", "''").strip().upper()
        a = _query(f"SITEADDRESS LIKE '{s}%'")
        if a:
            return a
        toks = s.split()
        if len(toks) >= 2:  # looser: house number + first street word
            return _query(f"SITEADDRESS LIKE '{toks[0]} {toks[1]}%'")
    return None


def _surname(name: str) -> str:
    """Last token of a 'First Middle Last' name (decedent / PR surname)."""
    parts = re.sub(r"[.,]", " ", name or "").split()
    return parts[-1].upper() if parts else ""


_ENTITY_RE = re.compile(
    r"\b(LLC|INC|LP|LLP|LTD|CORP|PROPERTIES|PROPERTY|HOLDINGS|TRUST|PARTNERS|"
    r"INVESTMENTS|REALTY|RENTALS|MANAGEMENT|ENTERPRISES|CAPITAL|VENTURES)\b"
)


def _is_entity(name: str) -> bool:
    return bool(_ENTITY_RE.search((name or "").upper()))


def evaluate(
    street: str,
    parcel_id: str = "",
    notice_type: str = "",
    decedent: str = "",
    pr_last: str = "",
    min_value: int = DEFAULT_MIN_VALUE,
    max_value: int = DEFAULT_MAX_VALUE,
) -> tuple[str, str, dict]:
    """Return (verdict, reason, attrs). verdict is KEEP | FLAG | DROP."""
    a = lookup_attrs(street, parcel_id) or {}
    if not a:
        return "FLAG", "no parcel match — verify manually", {}
    cls = (a.get("CLASSDSCRP") or "").upper()
    val = a.get("TOTVALUEBASE") or 0
    if not any(k in cls for k in KEEP_CLASS_KEYWORDS):
        return "DROP", f"class: {cls.title() or 'unknown'}", a
    if val and not (min_value <= val <= max_value):
        return "DROP", f"value ${val:,.0f} outside ${min_value:,.0f}-${max_value:,.0f}", a
    if notice_type == "probate":  # noqa: SIM102
        owner_raw = a.get("OWNERNME1") or ""
        # Auditor stores owners as "LASTNAME FIRST MIDDLE", so match the
        # decedent's/PR's surname against the FULL owner token set.
        owner_tokens = set(re.sub(r"[.,]", " ", owner_raw).upper().split())
        dec_sn, pr_sn = _surname(decedent), _surname(pr_last)
        related = (dec_sn and dec_sn in owner_tokens) or (pr_sn and pr_sn in owner_tokens)
        if owner_tokens and not related:
            if _is_entity(owner_raw):
                return "DROP", f"owned by entity {owner_raw!r} (rental)", a
            return "FLAG", f"owner {owner_raw!r} != decedent/PR (verify ownership)", a
    return "KEEP", f"{cls.title()} ${val:,.0f}", a


def filter_notices(notices, min_value=DEFAULT_MIN_VALUE, max_value=DEFAULT_MAX_VALUE):
    """Filter NoticeData list to workable leads. Returns (kept, dropped).

    KEEP + FLAG are retained (FLAG records get a 'review:' note in
    missing_data_flags so they surface in the Data Flags column); DROP records
    are excluded. Applies to all notice types.
    """
    kept, dropped = [], []
    counts = {"KEEP": 0, "FLAG": 0, "DROP": 0}
    for n in notices:
        verdict, reason, _ = evaluate(
            street=getattr(n, "address", "") or "",
            parcel_id=getattr(n, "parcel_id", "") or "",
            notice_type=getattr(n, "notice_type", "") or "",
            decedent=getattr(n, "decedent_name", "") or "",
            pr_last=getattr(n, "owner_name", "") or "",
            min_value=min_value,
            max_value=max_value,
        )
        counts[verdict] = counts.get(verdict, 0) + 1
        if verdict == "DROP":
            dropped.append((n, reason))
            continue
        note = ("review: " + reason) if verdict == "FLAG" else ("auditor: " + reason)
        existing = getattr(n, "missing_data_flags", "") or ""
        n.missing_data_flags = (existing + " | " + note).strip(" |") if existing else note
        kept.append(n)
    logger.info(
        "Lead filter: %d kept (%d ok, %d flagged), %d dropped",
        len(kept), counts["KEEP"], counts["FLAG"], counts["DROP"],
    )
    return kept, dropped
