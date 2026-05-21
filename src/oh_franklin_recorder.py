"""Franklin County Recorder scraper (Kofile/GovOS platform).

Source: https://franklin.oh.publicsearch.us

Pulls public-records filings for the Real Property department by instrument
type — the EARLIEST distress signal we can capture. Per ORC 2703.26 every
foreclosure complaint must record a Notice of Lis Pendens with the Recorder
within 7 days, weeks before sheriff sale. We also pull other adjacent
distress instruments (Federal Tax Liens, Mechanics Liens, Sheriffs Deeds,
Assignment of Rents, Certificate of Transfer, Trusts).

Implementation notes (R&D 2026-05-21):

  1. Direct URL navigation works for the `searchType=quickSearch` flow IF
     the session is warmed up by hitting the homepage first. Without the
     warm-up, results render as 0 rows even with valid filter params.

  2. Bundled Playwright Chromium triggers a "Your browser is out of date"
     fallback even with stealth. MUST use real system Chrome via
     channel="chrome" + playwright-stealth.

  3. URL pattern (confirmed working):
        /results
          ?_docTypes=<comma-list-of-2-3-letter-codes>
          &department=RP
          &recordedDateRange=YYYYMMDD%2CYYYYMMDD
          &searchOcrText=true
          &searchType=quickSearch
          &limit=50&offset=N

  4. Doc-type codes (decoded by probing the live UI):
       NO  = Notice (parent — includes LIS PENDENS and NOTICE OF COMMENCEMENT)
       CT  = Certificate of Transfer
       TR  = Trust
       FLN = Federal Lien
       FT  = Federal Tax Lien
       ML  = Mechanics Lien
       LN  = Lien (generic)
       AR  = Assign of Rents
       SD  = Sheriffs Deed
       OP  = Option to Purchase
       CN  = Continuation (various)

  5. The DOC TYPE column on results shows the SUB-TYPE — e.g., when filtering
     by code `NO`, rows return either "NOTICE" (= lis pendens) or
     "NOTICE OF COMMENCEMENT" (= construction, not relevant). We filter the
     latter out post-fetch.

  6. Results render in <tr role="row"> with <td><span>VALUE</span></td>
     cells in column order:
       0  checkbox       1  action menu       2  preview pane
       3  GRANTOR        4  GRANTEE           5  DOC TYPE
       6  RECORDED DATE  7  INST NUMBER       8  BOOK/VOL/PG
       9  LEGAL DESC.    10 REFERENCES        11 REMARKS
"""

import asyncio
import logging
import re
from datetime import date, timedelta, datetime
from typing import Optional

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

HOMEPAGE = "https://franklin.oh.publicsearch.us"
RESULTS_URL_TMPL = (
    "https://franklin.oh.publicsearch.us/results"
    "?_docTypes={types}"
    "&department=RP"
    "&keywordSearch=false"
    "&limit=50&offset={offset}"
    "&recordedDateRange={start}%2C{end}"
    "&searchOcrText=true"
    "&searchType=quickSearch"
)

PAGE_SIZE = 50
MAX_PAGES = 20  # 50 * 20 = 1000-row safety cap

# Default v1 instrument types — distress signals selected with user
DEFAULT_DOC_CODES = ["NO", "CT", "TR", "FLN", "FT", "ML", "AR", "SD"]

# Map the human-readable DOC TYPE column (post-fetch) to SiftStack notice_types.
# Keys are uppercase, exact match (with one prefix-match special case below).
DOC_TYPE_TO_NOTICE_TYPE = {
    "NOTICE":                 "foreclosure",   # lis pendens (confirmed by user)
    "SHERIFFS DEED":          "foreclosure",   # completed foreclosure
    "ASSIGN OF RENTS":        "foreclosure",   # pre-foreclosure default
    "MECHANICS LIEN":         "lien",
    "FEDERAL TAX LIEN":       "tax_delinquent",
    "FEDERAL LIEN":           "tax_delinquent",
    "LIEN":                   "lien",
    "CERTIFICATE OF TRANSFER": "probate",
    "TRUST":                  "probate",       # estate planning proxy
}

# Doc types we want to DISCARD post-fetch even if they came back in our filter
EXCLUDED_DOC_TYPES = {
    "NOTICE OF COMMENCEMENT",  # construction filing, not lis pendens
    "OPTION TO PURCHASE",      # too generic for distress
}


async def _scrape_async(
    since: date,
    until: date,
    doc_codes: list[str],
) -> list[NoticeData]:
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    types_param = "%2C".join(doc_codes)
    start_str = since.strftime("%Y%m%d")
    end_str = until.strftime("%Y%m%d")
    today_str = date.today().isoformat()

    notices: list[NoticeData] = []
    seen_inst_numbers: set[str] = set()
    skipped_excluded = 0
    skipped_unmapped = 0

    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(
            headless=True,
            channel="chrome",  # REQUIRED — bundled Chromium fails fingerprint
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        # Warm up the session by visiting homepage first. Without this,
        # direct navigation to /results returns 0 rows even with valid params.
        logger.info("Recorder: warming up session via homepage...")
        await page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2500)

        for page_idx in range(MAX_PAGES):
            offset = page_idx * PAGE_SIZE
            url = RESULTS_URL_TMPL.format(
                types=types_param, offset=offset, start=start_str, end=end_str,
            )
            logger.info(
                "Recorder: page %d (offset %d) %s → %s",
                page_idx + 1, offset, since, until,
            )

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector('tr[role="row"]', timeout=25000)
            except Exception:
                logger.info("Recorder: no rows rendered at offset %d — done", offset)
                break

            await page.wait_for_timeout(2000)

            rows = page.locator('tr[role="row"]')
            n_rows = await rows.count()

            if n_rows == 0:
                logger.info("Recorder: zero rows at offset %d — done", offset)
                break

            new_in_page = 0
            for i in range(n_rows):
                try:
                    cells = rows.nth(i).locator("td")
                    nc = await cells.count()
                    if nc < 10:
                        continue

                    grantor  = (await cells.nth(3).inner_text()).strip()
                    grantee  = (await cells.nth(4).inner_text()).strip()
                    doc_type = (await cells.nth(5).inner_text()).strip().upper()
                    rec_date = (await cells.nth(6).inner_text()).strip()
                    inst_num = (await cells.nth(7).inner_text()).strip()
                    legal    = (await cells.nth(9).inner_text()).strip()

                    if doc_type in EXCLUDED_DOC_TYPES:
                        skipped_excluded += 1
                        continue

                    notice_type = DOC_TYPE_TO_NOTICE_TYPE.get(doc_type)
                    if not notice_type:
                        skipped_unmapped += 1
                        logger.debug("Recorder: unmapped doc type %r — skipping", doc_type)
                        continue

                    # Dedup by instrument number (some filings show twice across types)
                    if inst_num and inst_num in seen_inst_numbers:
                        continue
                    if inst_num:
                        seen_inst_numbers.add(inst_num)

                    recorded_iso = ""
                    try:
                        recorded_iso = datetime.strptime(rec_date, "%m/%d/%Y").date().isoformat()
                    except ValueError:
                        pass

                    parcel_match = re.search(r"Pcl#?\s*([0-9-]+)", legal, re.IGNORECASE)
                    parcel = parcel_match.group(1) if parcel_match else ""

                    raw_summary = (
                        f"Recorder {doc_type} {inst_num} recorded {rec_date}. "
                        f"Grantor: {grantor}. Grantee: {grantee}. "
                        f"Legal: {legal[:200]}"
                    )

                    notices.append(NoticeData(
                        date_added=recorded_iso or today_str,
                        address="",          # Index has no street address — Auditor lookup needed
                        city="",
                        state="OH",
                        zip="",
                        owner_name=grantor,  # Defendant/grantor = property owner
                        notice_type=notice_type,
                        county="Franklin",
                        source_url=f"{HOMEPAGE}/doc/{inst_num}" if inst_num else HOMEPAGE,
                        raw_text=raw_summary,
                        parcel_id=parcel,
                    ))
                    new_in_page += 1
                except Exception as e:
                    logger.debug("Recorder: row %d parse error: %s", i, e)
                    continue

            logger.info("Recorder: page %d → %d new records (%d total)",
                        page_idx + 1, new_in_page, len(notices))

            # Stop pagination if this page didn't fill (last page)
            if n_rows < PAGE_SIZE:
                logger.info("Recorder: partial page → done")
                break

        await browser.close()

    logger.info(
        "Recorder done: %d records (skipped %d excluded, %d unmapped)",
        len(notices), skipped_excluded, skipped_unmapped,
    )
    return notices


def scrape_recorder(
    since: Optional[date] = None,
    until: Optional[date] = None,
    doc_codes: Optional[list[str]] = None,
) -> list[NoticeData]:
    """Pull Franklin County Recorder filings by instrument type.

    Args:
        since: First recording date (inclusive). Defaults to yesterday.
        until: Last recording date (inclusive). Defaults to today.
        doc_codes: List of 2-3 letter Recorder doc-type codes. Defaults
            to DEFAULT_DOC_CODES (the v1 distress-signal mix).

    Returns:
        list[NoticeData] with notice_type set based on the doc type. The
        `address` field is empty — Recorder index has only legal descriptions
        and parcel IDs. Downstream Auditor parcel lookup resolves the address.
    """
    today = date.today()
    if since is None:
        # Overlap by 1 day to avoid missing same-day overnight filings
        since = today - timedelta(days=1)
    if until is None:
        until = today
    if doc_codes is None:
        doc_codes = DEFAULT_DOC_CODES

    return asyncio.run(_scrape_async(since=since, until=until, doc_codes=doc_codes))


# Backwards-compat alias for the orchestrator
scrape_recorder_lis_pendens = scrape_recorder
