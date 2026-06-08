"""Per-parcel tax lookup from the Franklin County OH Treasurer.

Source: https://treapropsearch.franklincountyohio.gov/Details.aspx

Used as a freshness backstop for the Auditor's ArcGIS tax-delinquent layer
which froze 2025-07-17. The Treasurer's per-parcel detail page updates
nightly with current balance, owner name(s), and mailing address — none of
which the ArcGIS feed exposes.

URL format:
    Details.aspx?district={DDD}&parcel={NNNNNN}&ext={EE}

Derived from canonical parcel ID `DDD-NNNNNN-EE`. Leading zeros on
district/ext are stripped (e.g. `232-000172-00` → district=232, parcel=172,
ext=0).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from typing import Optional

import requests
from bs4 import BeautifulSoup

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

BASE = "https://treapropsearch.franklincountyohio.gov"
DETAILS_URL = f"{BASE}/Details.aspx"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Polite default delay between requests
REQUEST_DELAY_SEC = 0.5

# Lines that are section headers, not address content
_LABEL_LINES = {
    "Billing Address", "Mailing Address", "Tax Address",
    "Location Address", "Site Address", "Property Address",
}

# Matches "CITY OH 12345" or "CITY OH, 12345-1234"
_CSZ_RE = re.compile(r"^[A-Z][A-Z .'-]+,?\s+[A-Z]{2}\s*,?\s*\d{5}(?:-\d{4})?$")


def parcel_id_to_url_parts(parcel_id: str) -> Optional[tuple[int, int, int]]:
    """Parse `DDD-NNNNNN[-EE]` into (district, parcel, ext) ints.

    The `-EE` suffix is optional — ArcGIS returns 2-segment IDs while the
    canonical Auditor format has 3 segments. When missing, ext defaults to 0
    (the predominant value for single-parcel properties).

    Leading zeros are normalized — `010-022100-00` → (10, 22100, 0).
    """
    m = re.fullmatch(r"\s*(\d{3})-(\d{6})(?:-(\d{2}))?\s*", parcel_id or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def build_session() -> requests.Session:
    """Return a session prewarmed with the search homepage cookie."""
    sess = requests.Session()
    sess.headers.update(DEFAULT_HEADERS)
    sess.get(BASE + "/", timeout=20)
    return sess


def _money(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    s = s.replace("$", "").replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _clean(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def parse_details(html: str) -> dict:
    """Extract tax/owner fields from a Details.aspx response.

    Returns a dict with keys:
        parcel_id, current_owner, mailing_owner, owners (list),
        site_street, site_city_state_zip,
        mailing_street, mailing_city_state_zip,
        balance_due, delinquent_tax,
        legal_description, found (bool)
    """
    soup = BeautifulSoup(html, "html.parser")

    out: dict = {
        "parcel_id": "",
        "current_owner": "",
        "mailing_owner": "",
        "owners": [],
        "site_street": "",
        "site_city_state_zip": "",
        "mailing_street": "",
        "mailing_city_state_zip": "",
        "balance_due": None,
        "delinquent_tax": None,
        "legal_description": "",
        "found": False,
    }

    # Parcel ID header (lblDNum1 in markup: "Parcel: 232-000172-00")
    p_el = soup.find(id="ctl00_cphBodyContent_lblDNum1")
    if p_el:
        m = re.search(r"(\d{3}-\d{6}-\d{2})", p_el.get_text(" ", strip=True))
        if m:
            out["parcel_id"] = m.group(1)
            out["found"] = True

    if not out["found"]:
        return out

    # Site address — labeled "Location Address" in a box-item
    for box in soup.select(".box-item"):
        text = box.get_text("\n", strip=True)
        if "Location Address" in text:
            lines = [ln.strip() for ln in text.split("\n") if ln.strip() and ln.strip() not in _LABEL_LINES]
            if lines:
                out["site_street"] = lines[0]
                # Only accept the next line if it looks like CITY ST ZIP
                for ln in lines[1:]:
                    if _CSZ_RE.match(ln):
                        out["site_city_state_zip"] = ln
                        break
            break

    # Mailing address block (fcDetailsHeader_MailingPersonName1 + sibling lines)
    mailing_name = soup.find(id="ctl00_cphBodyContent_fcDetailsHeader_MailingPersonName1")
    if mailing_name:
        out["mailing_owner"] = _clean(mailing_name.get_text(" ", strip=True))
        parent = mailing_name.find_parent("div")
        if parent:
            lines = [
                ln.strip() for ln in parent.get_text("\n", strip=True).split("\n")
                if ln.strip() and ln.strip() != out["mailing_owner"] and ln.strip() not in _LABEL_LINES
            ]
            if lines:
                out["mailing_street"] = lines[0]
                for ln in lines[1:]:
                    if _CSZ_RE.match(ln):
                        out["mailing_city_state_zip"] = ln
                        break

    # Current Owner (header summary)
    owner_anchor = soup.find(string=re.compile(r"\bCurrent Owner\s*:"))
    if owner_anchor:
        # Walk forward to next non-empty sibling text
        parent = owner_anchor.parent
        while parent:
            nxt = parent.find_next(string=True)
            if nxt and nxt.strip() and "Current Owner" not in nxt:
                out["current_owner"] = _clean(nxt)
                break
            parent = parent.find_next()

    # Owners list (Owner(s) section)
    owners_anchor = soup.find(string=re.compile(r"Owner\(s\)"))
    if owners_anchor:
        block = owners_anchor.find_parent("div") or owners_anchor.find_parent("table")
        if block:
            text = block.get_text("\n", strip=True)
            lines = [
                ln.strip() for ln in text.split("\n")
                if ln.strip() and "Owner(s)" not in ln and ln.strip() != ":"
            ]
            # First few lines are the names — drop after we hit known section markers
            stop_markers = ("Mailing", "Location", "Legal", "Tax", "Balance", "Year")
            for ln in lines:
                if any(ln.startswith(m) for m in stop_markers):
                    break
                if ln and ln not in out["owners"]:
                    out["owners"].append(ln)

    # Balance Due (current total owed including penalties + interest)
    balance_label = soup.find(string=re.compile(r"^\s*Balance Due\s*:?\s*$"))
    if balance_label:
        # Money value is the next td/span
        nxt = balance_label.find_next(string=re.compile(r"\$[\d,]"))
        if nxt:
            out["balance_due"] = _money(nxt.strip())

    # Delinquent Tax (prior-year accrued delinquent portion)
    del_label = soup.find(string=re.compile(r"^\s*Delinquent Tax\s*$"))
    if del_label:
        nxt = del_label.find_next(string=re.compile(r"\$[\d,]"))
        if nxt:
            out["delinquent_tax"] = _money(nxt.strip())

    # Legal description block
    legal_hdr = soup.find(id="ctl00_cphBodyContent_fcDetailsHeader_legaldescheader")
    if legal_hdr:
        block = legal_hdr.find_parent("div") or legal_hdr.parent
        if block:
            text = block.get_text(" ", strip=True)
            text = re.sub(r"^Legal Description\s*", "", text)
            out["legal_description"] = _clean(text)

    return out


def lookup_parcel(
    parcel_id: str,
    session: Optional[requests.Session] = None,
    delay_sec: float = REQUEST_DELAY_SEC,
) -> Optional[dict]:
    """Fetch and parse Treasurer details for one parcel.

    Returns the parsed dict (see parse_details) or None if the parcel ID
    couldn't be normalized into URL parts.
    """
    parts = parcel_id_to_url_parts(parcel_id)
    if not parts:
        logger.debug("Treasurer: unrecognized parcel format %r", parcel_id)
        return None
    district, parcel, ext = parts

    if session is None:
        session = build_session()

    if delay_sec:
        time.sleep(delay_sec)

    try:
        r = session.get(
            DETAILS_URL,
            params={"district": district, "parcel": parcel, "ext": ext},
            timeout=30,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Treasurer fetch failed for %s: %s", parcel_id, e)
        return None

    return parse_details(r.text)


_MAILING_CSZ_RE = re.compile(
    r"^([A-Z][A-Z .'-]+?),?\s+([A-Z]{2})\s*,?\s*(\d{5})(?:-\d{4})?$"
)


def refresh_delinquent_via_treasurer(
    min_balance: float = 5000.0,
    max_years_delinquent: int = 5,
    class_codes: Optional[set[str]] = None,
    limit: Optional[int] = None,
    delay_sec: float = 0.3,
    progress_every: int = 100,
) -> list[NoticeData]:
    """Build a fresh tax-delinquent list by enriching ArcGIS seeds via Treasurer.

    Steps:
      1. Pull seed parcel IDs from the (stale) ArcGIS layer using the existing
         scrape_tax_delinquent() with an empty state — gives us the
         ~7,700 known delinquents with their stale balances + CdqYear + CLASSCD
         residential filter already applied.
      2. For each seed parcel, look up the Treasurer Details.aspx page.
      3. Drop parcels whose CURRENT balance is below min_balance (owner caught
         up). Update owner name + mailing address from Treasurer.
      4. Return NoticeData list with fresh balances.

    New entrants (parcels that became delinquent after the ArcGIS freeze date
    of 2025-07-17) are NOT captured — the seed list is stale. This catches
    growth on known delinquents only.
    """
    from oh_franklin_tax_delinquent import scrape_tax_delinquent

    logger.info("Treasurer refresh: pulling seed list from ArcGIS...")
    seed = scrape_tax_delinquent(
        state={},
        min_balance=min_balance,
        max_years_delinquent=max_years_delinquent,
        class_codes=class_codes,
    )
    logger.info("Treasurer refresh: %d seed parcels (stale ArcGIS snapshot)", len(seed))

    if limit:
        seed = seed[:limit]
        logger.info("Treasurer refresh: limited to %d parcels for this run", limit)

    sess = build_session()
    today_str = date.today().isoformat()
    kept: list[NoticeData] = []
    skipped_no_details = 0
    skipped_caught_up = 0

    for i, n in enumerate(seed, 1):
        details = lookup_parcel(n.parcel_id, session=sess, delay_sec=delay_sec)
        if not details or not details.get("found"):
            skipped_no_details += 1
            continue

        balance = details.get("balance_due") or 0.0
        if balance < min_balance:
            skipped_caught_up += 1
            continue

        # Overwrite stale fields with fresh Treasurer data
        n.date_added = today_str
        n.tax_delinquent_amount = f"{balance:.2f}"
        if details.get("current_owner"):
            n.owner_name = details["current_owner"]
            n.tax_owner_name = details["current_owner"]
        if details.get("mailing_street"):
            n.owner_street = details["mailing_street"]
        m_mail = _MAILING_CSZ_RE.match(details.get("mailing_city_state_zip") or "")
        if m_mail:
            n.owner_city = m_mail.group(1).title()
            n.owner_state = m_mail.group(2)
            n.owner_zip = m_mail.group(3)
        # Site city from Treasurer fills the gap left by ArcGIS (zip-only).
        # Pattern is unpunctuated: "COLUMBUS OH 43219".
        site_csz = details.get("site_city_state_zip") or ""
        m_site = re.match(r"^([A-Z][A-Z .'-]+?)\s+([A-Z]{2})\s+(\d{5})", site_csz)
        if m_site:
            if not n.city:
                n.city = m_site.group(1).title()
            if not n.zip:
                n.zip = m_site.group(3)

        kept.append(n)

        if i % progress_every == 0:
            logger.info(
                "  Treasurer refresh: %d/%d processed (%d kept, %d caught up, %d no details)",
                i, len(seed), len(kept), skipped_caught_up, skipped_no_details,
            )

    logger.info(
        "Treasurer refresh done: %d kept (of %d seed) — %d caught up, %d no details",
        len(kept), len(seed), skipped_caught_up, skipped_no_details,
    )
    return kept


if __name__ == "__main__":
    # Smoke test against known delinquent parcels from the stale ArcGIS layer
    import argparse, json, logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    ap = argparse.ArgumentParser()
    ap.add_argument("parcels", nargs="*",
                    default=["232-000172-00", "234-000127-00", "234-000129-00"],
                    help="Parcel IDs to look up")
    args = ap.parse_args()

    sess = build_session()
    for pid in args.parcels:
        print(f"\n=== {pid} ===")
        result = lookup_parcel(pid, session=sess)
        if not result:
            print("  (no result)")
            continue
        print(json.dumps(result, indent=2, default=str))
