"""Eviction filing scraper for Franklin County, OH.

Source: Franklin County Municipal Court Clerk of Courts (Lori M. Tyack).

Reports index: https://www.fcmcclerk.com/reports/evictions

The clerk publishes pre-sliced monthly CSVs of all Forcible Entry & Detainer
(FED / eviction) filings. Each CSV is regenerated nightly. Path pattern:

  /storage/shared/civil-fed/FCMC Civil F.E.D. (Eviction) Case List
  YYYY-MM-DD to YYYY-MM-DD.csv?<cachebuster>

No authentication, plain HTTPS, ~1,500-2,500 filings/month.

For REI: the PLAINTIFF (landlord) is the marketing target — tired landlord =
potential seller. The DEFENDANT (tenant) is irrelevant for marketing but
their address IS the rental property we want to research.

Field mapping (NoticeData):
  CASE_FILE_DATE                   → date_added
  FIRST_DEFENDANT_ADDRESS_LINE_1   → address (rental property)
  FIRST_DEFENDANT_CITY/STATE/ZIP   → city/state/zip
  FIRST_PLAINTIFF_COMPANY_NAME    → owner_name (or first+last if individual)
  FIRST_PLAINTIFF_ADDRESS_LINE_1   → owner_street (landlord mailing)
  FIRST_PLAINTIFF_CITY/STATE/ZIP   → owner_city/state/zip
  CASE_NUMBER                      → source_url (clerk case search)
"""

import csv
import io
import logging
import re
from datetime import date, datetime
from typing import Optional

import requests

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

INDEX_URL = "https://www.fcmcclerk.com/reports/evictions"
BASE_URL = "https://www.fcmcclerk.com"
CASE_SEARCH_URL_TMPL = "https://www.fcmcclerk.com/case/search?case_number={case}"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _list_csv_urls() -> list[tuple[str, date, date]]:
    """Scrape the index page for available monthly CSVs.

    Returns:
        list of (full_url, start_date, end_date) tuples, sorted newest first.
    """
    resp = requests.get(INDEX_URL, headers={"User-Agent": UA}, timeout=20)
    resp.raise_for_status()
    body = resp.text

    # Pattern: /storage/shared/civil-fed/...YYYY-MM-DD to YYYY-MM-DD.csv?<n>
    pattern = re.compile(
        r'href="(/storage/shared/civil-fed/[^"]*?(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})\.csv\?[^"]*)"',
        re.IGNORECASE,
    )

    results: list[tuple[str, date, date]] = []
    for m in pattern.finditer(body):
        rel = m.group(1)
        try:
            start = datetime.strptime(m.group(2), "%Y-%m-%d").date()
            end = datetime.strptime(m.group(3), "%Y-%m-%d").date()
        except ValueError:
            continue
        results.append((BASE_URL + rel, start, end))

    # Sort newest first (by end date)
    results.sort(key=lambda x: x[2], reverse=True)
    return results


def _download_csv(url: str) -> str:
    """Download a single CSV and return its text content."""
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    resp.raise_for_status()
    return resp.text


def _parse_date_mmddyyyy(s: str) -> str:
    """Convert MM/DD/YYYY → YYYY-MM-DD; empty string if unparseable."""
    s = (s or "").strip()
    if not s:
        return ""
    try:
        return datetime.strptime(s, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return ""


def _plaintiff_name(row: dict) -> str:
    """Construct the landlord/plaintiff display name.

    Prefers business name if present; falls back to individual first+last.
    """
    biz = (row.get("FIRST_PLAINTIFF_COMPANY_NAME") or "").strip()
    if biz:
        return biz
    first = (row.get("FIRST_PLAINTIFF_FIRST_NAME") or "").strip()
    last = (row.get("FIRST_PLAINTIFF_LAST_NAME") or "").strip()
    if first and last:
        return f"{first} {last}"
    return (first or last)


def _row_to_notice(row: dict, today_str: str) -> Optional[NoticeData]:
    """Map a single CSV row to NoticeData. Returns None if the row is unusable."""
    rental_addr = (row.get("FIRST_DEFENDANT_ADDRESS_LINE_1") or "").strip()
    if not rental_addr:
        return None

    landlord = _plaintiff_name(row)
    if not landlord:
        return None

    case_no = (row.get("CASE_NUMBER") or "").strip()
    filed = _parse_date_mmddyyyy(row.get("CASE_FILE_DATE", ""))

    disposition = (row.get("LAST_DISPOSITION_DESCRIPTION") or "").strip()
    raw_summary = (
        f"Eviction case {case_no} filed {filed or 'unknown'}. "
        f"Landlord (plaintiff): {landlord}. "
        f"Tenant address (rental property): {rental_addr}. "
        f"Disposition: {disposition or 'pending'}."
    )

    return NoticeData(
        date_added=filed or today_str,
        address=rental_addr,
        city=(row.get("FIRST_DEFENDANT_CITY") or "").strip(),
        state=(row.get("FIRST_DEFENDANT_STATE") or "OH").strip() or "OH",
        zip=(row.get("FIRST_DEFENDANT_ZIP") or "").strip(),
        owner_name=landlord,
        notice_type="eviction",
        county="Franklin",
        source_url=CASE_SEARCH_URL_TMPL.format(case=case_no.replace(" ", "+")),
        raw_text=raw_summary,
        owner_street=(row.get("FIRST_PLAINTIFF_ADDRESS_LINE_1") or "").strip(),
        owner_city=(row.get("FIRST_PLAINTIFF_CITY") or "").strip(),
        owner_state=(row.get("FIRST_PLAINTIFF_STATE") or "OH").strip() or "OH",
        owner_zip=(row.get("FIRST_PLAINTIFF_ZIP") or "").strip(),
    )


def scrape_evictions(since: Optional[date] = None) -> list[NoticeData]:
    """Pull eviction filings from Franklin County Municipal Court.

    Args:
        since: Only return cases filed on or after this date. If None,
            returns the entire current month's CSV.

    Returns:
        list[NoticeData] — one per eviction filing.
    """
    today = date.today()
    today_str = today.isoformat()
    if since is None:
        since = today.replace(day=1)  # Default: start of current month

    csv_urls = _list_csv_urls()
    if not csv_urls:
        logger.error("Eviction: no CSV links found at %s", INDEX_URL)
        return []

    logger.info("Eviction: found %d monthly CSVs available", len(csv_urls))

    # Pick CSVs that overlap our date range [since, today]
    needed = [
        (url, s, e) for (url, s, e) in csv_urls
        if e >= since  # CSV's end date is on/after `since` → has relevant data
    ]

    if not needed:
        logger.warning("Eviction: no CSVs match window since=%s", since)
        return []

    logger.info("Eviction: downloading %d CSV(s) covering %s onward", len(needed), since)

    notices: list[NoticeData] = []
    parse_errors = 0
    skipped_before_since = 0
    skipped_apartments = 0
    apt_pattern = re.compile(r"\b(APT|APARTMENT)\b", re.IGNORECASE)

    for url, start, end in needed:
        logger.info("  Eviction: downloading %s to %s", start, end)
        try:
            body = _download_csv(url)
        except Exception as e:
            logger.warning("  Eviction: download failed for %s: %s", url, e)
            continue

        reader = csv.DictReader(io.StringIO(body))
        for row in reader:
            try:
                notice = _row_to_notice(row, today_str)
            except Exception as e:
                parse_errors += 1
                logger.debug("Eviction: row parse error: %s", e)
                continue

            if notice is None:
                continue

            # Filter by `since` — case_file_date must be on/after
            if notice.date_added and notice.date_added < since.isoformat():
                skipped_before_since += 1
                continue

            # Drop apartment units — tenants in apartments don't map to
            # single-family REI targets, and the landlord (plaintiff) is
            # typically a large property management company we already
            # filter out at the entity-owner step.
            if apt_pattern.search(notice.address or ""):
                skipped_apartments += 1
                continue

            notices.append(notice)

    logger.info(
        "Eviction done: %d records (skipped %d before %s, %d apartments, %d parse errors)",
        len(notices), skipped_before_since, since, skipped_apartments, parse_errors,
    )
    return notices
