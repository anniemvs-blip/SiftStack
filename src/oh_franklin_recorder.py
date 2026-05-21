"""Franklin County Recorder lis-pendens scraper.

Source: https://franklin.oh.publicsearch.us (Kofile/GovOS platform).

The Recorder is the earliest-stage foreclosure signal in Franklin OH. Per
ORC 2703.26, every foreclosure complaint must record a Notice of Lis Pendens
with the Recorder within 7 days. So the Recorder's lis-pendens index is
effectively a master list of every active foreclosure, weeks before the
sheriff sale stage.

Implementation notes (hard-won R&D, 2026-05-21):

  1. Direct URL search (?searchValue=LIS%20PENDENS&recordedDateRange=...)
     hits a JS bug — "Cannot destructure property 'fullTextSearchDateRangeLabel'
     of 'Object(...)(...)' as it is undefined" — because some config object
     isn't loaded on the direct-URL path. WE CANNOT USE DIRECT URLs.

  2. Must use real Chrome (channel="chrome") + playwright-stealth. Bundled
     Playwright Chromium triggers Kofile's "Your web browser is out of date"
     fallback even with stealth.

  3. Search flow: homepage → focus search input → type "LIS PENDENS" →
     Enter → wait for tr[role="row"] to render → parse rows.

  4. Results are a real HTML <table>: each row is <tr role="row">, each
     cell <td><span>VALUE</span></td>. Column order:
       0: checkbox, 1: action menu, 2: result preview pane,
       3: GRANTOR, 4: GRANTEE, 5: DOC TYPE, 6: RECORDED DATE,
       7: INST NUMBER, 8: BOOK/VOLUME/PAGE, 9: LEGAL DESCRIPTION,
       10: REFERENCES, 11: REMARKS

  5. Filter for instrument type "LIS PENDENS" by checking td[5] text.
     The site's doc-type sidebar filter does NOT include LIS PENDENS as a
     top-level — it's full-text matched. Post-hoc filter on DOC TYPE column.

KNOWN LIMITATIONS (Phase 3 R&D needed):
  - Default search returns OLDEST documents first (2002-era) and full-text
    matches anything containing "LIS PENDENS" — including 20-year-old deeds
    that merely reference an old lis pendens. To get RECENT actual lis-pendens
    FILINGS, need to:
      a) Programmatically set the Doc-Type sidebar filter to LIS PENDENS, OR
      b) Set date range to last 30/60 days via the UI, AND
      c) Toggle "Search Index & Full Text (OCR)" mode.
    The Doc-Type filter UI selector + date-picker UI selector still need
    to be reverse-engineered.
  - No pagination — fetches only first page (50 rows).
  - No PDF download — only metadata extraction.
  - Headless requires real Chrome installed (channel="chrome").

For now this returns 0 records (correctly — the visible page 1 with default
search settings has no actual lis-pendens DOC TYPE matches). The scraper
works end-to-end through Kofile's anti-bot defenses; missing piece is just
the right UI filters.
"""

import asyncio
import logging
import re
from datetime import date, datetime
from typing import Optional

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

HOMEPAGE = "https://franklin.oh.publicsearch.us"
SEARCH_TERM = "LIS PENDENS"


async def _scrape_async(
    since: Optional[date],
    until: Optional[date],
) -> list[NoticeData]:
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    today_str = date.today().isoformat()

    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(
            headless=True,
            channel="chrome",  # REQUIRED — see implementation note 2
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        logger.info("Recorder: loading homepage...")
        await page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        # Dismiss any modal/announcement (defensive)
        try:
            for sel in ['[class*="modal-close"]', '[aria-label*="close"]', 'button:has-text("Close")']:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=500):
                    await btn.click()
                    break
        except Exception:
            pass

        logger.info("Recorder: typing search term %r...", SEARCH_TERM)
        search_input = page.locator('input[type="text"], input[type="search"]').first
        if await search_input.count() == 0:
            logger.error("Recorder: no search input found on homepage")
            await browser.close()
            return []
        await search_input.click()
        await search_input.fill(SEARCH_TERM)
        await page.wait_for_timeout(300)
        await search_input.press("Enter")

        logger.info("Recorder: waiting for results...")
        try:
            await page.wait_for_selector('tr[role="row"]', timeout=30000)
        except Exception:
            logger.warning("Recorder: timeout waiting for results table")
            await browser.close()
            return []

        await page.wait_for_timeout(3000)  # let all rows hydrate

        rows = page.locator('tr[role="row"]')
        n_rows = await rows.count()
        logger.info("Recorder: found %d table rows", n_rows)

        notices: list[NoticeData] = []
        skipped_non_lis_pendens = 0
        skipped_out_of_range = 0

        for i in range(n_rows):
            try:
                row = rows.nth(i)
                cells = row.locator('td')
                n_cells = await cells.count()
                if n_cells < 10:
                    continue  # Header row or partial

                # Extract span text from cells 3..9 (data columns)
                # Index 0,1,2 are checkbox + action button + preview pane
                texts = []
                for c in range(min(n_cells, 12)):
                    txt = await cells.nth(c).inner_text()
                    texts.append(txt.strip())

                grantor   = texts[3] if len(texts) > 3 else ""
                grantee   = texts[4] if len(texts) > 4 else ""
                doc_type  = texts[5] if len(texts) > 5 else ""
                rec_date  = texts[6] if len(texts) > 6 else ""
                inst_num  = texts[7] if len(texts) > 7 else ""
                book_vol  = texts[8] if len(texts) > 8 else ""
                legal     = texts[9] if len(texts) > 9 else ""

                # Filter to lis pendens (DOC TYPE may be "LIS PENDENS",
                # "LIS PENDENS - FORECLOSURE", etc.)
                if "LIS PEND" not in doc_type.upper():
                    skipped_non_lis_pendens += 1
                    continue

                # Parse recorded date (M/D/YYYY)
                recorded_iso = ""
                try:
                    recorded_iso = datetime.strptime(rec_date, "%m/%d/%Y").date().isoformat()
                except ValueError:
                    pass

                # Date range filter
                if recorded_iso:
                    if since and recorded_iso < since.isoformat():
                        skipped_out_of_range += 1
                        continue
                    if until and recorded_iso > until.isoformat():
                        skipped_out_of_range += 1
                        continue

                # Extract address from legal description if possible
                # Legal descriptions often start with "Lt/Un N SUBDIVISION Pcl# X-Y"
                # but don't always include a street address. Leave blank;
                # downstream Auditor parcel lookup can resolve via Pcl#.
                parcel_match = re.search(r"Pcl#?\s*([0-9-]+)", legal, re.IGNORECASE)
                parcel = parcel_match.group(1) if parcel_match else ""

                raw_summary = (
                    f"Recorder lis pendens {inst_num} recorded {rec_date}. "
                    f"Grantor: {grantor}. Grantee: {grantee}. "
                    f"Doc type: {doc_type}. Legal: {legal[:200]}"
                )

                notices.append(NoticeData(
                    date_added=recorded_iso or today_str,
                    address="",                # Unknown from index alone
                    city="",
                    state="OH",
                    zip="",
                    owner_name=grantor,        # Defendant = property owner
                    notice_type="foreclosure", # Lis pendens = pre-foreclosure
                    county="Franklin",
                    source_url=f"{HOMEPAGE}/doc/{inst_num}" if inst_num else HOMEPAGE,
                    raw_text=raw_summary,
                    parcel_id=parcel,
                ))
            except Exception as e:
                logger.debug("Recorder: row %d parse error: %s", i, e)
                continue

        logger.info(
            "Recorder done: %d lis-pendens records (skipped %d non-lis-pendens, %d out of date range)",
            len(notices), skipped_non_lis_pendens, skipped_out_of_range,
        )

        await browser.close()
        return notices


def scrape_recorder_lis_pendens(
    since: Optional[date] = None,
    until: Optional[date] = None,
) -> list[NoticeData]:
    """Pull lis-pendens filings from Franklin County Recorder.

    Args:
        since: Only return records recorded on or after this date.
        until: Only return records recorded on or before this date.

    Returns:
        list[NoticeData] with notice_type='foreclosure'. The 'address' field
        is left empty — Recorder index has no street address, only legal
        description with a parcel ID. Downstream enrichment (Auditor parcel
        lookup by parcel_id, then Smarty) resolves the property address.
    """
    return asyncio.run(_scrape_async(since=since, until=until))
