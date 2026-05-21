"""Franklin County, OH distress property scrapers — 3 sources.

Sources implemented:
  1. Foreclosure — RealForeclose sheriff sale portal (Playwright, requires free account)
     URL: https://franklin.sheriffsaleauction.ohio.gov
     Cadence: Weekly auctions on Fridays
     Env: FRANKLIN_OH_SHERIFF_USERNAME + FRANKLIN_OH_SHERIFF_PASSWORD

  2. Probate — Franklin County Probate Court NetData case index (HTTP, no auth)
     URL: http://probatesearch.franklincountyohio.gov/netdata/
     Cadence: Daily filings
     State file: tracks last scanned case number for incremental runs

  3. Tax Sale — Franklin County Treasurer annual tax lien list CSV (Playwright, 403 on direct HTTP)
     URL: https://treasurer.franklincountyohio.gov/Delinquent-Taxes/Tax-Lien-Sale
     Cadence: Annual (Oct/Nov release); returns all records from current year list

Entry point:
  from oh_franklin_scraper import scrape_franklin_oh
  notices = await scrape_franklin_oh(since_date="2026-04-15", types=["probate", "tax_sale"])
"""

import csv
import io
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

PROBATE_DETAIL_URL = (
    "http://probatesearch.franklincountyohio.gov/netdata/PBCaseTypeE.ndm/ESTATE_DETAIL"
    "?caseno={case_num};;"
)
PROBATE_FID_URL = (
    "http://probatesearch.franklincountyohio.gov/netdata/PBFidDetail.ndm/FID_DETAIL"
    "?caseno={case_num};;01"
)
SHERIFF_BASE = "https://franklin.sheriffsaleauction.ohio.gov"
SHERIFF_LOGIN = f"{SHERIFF_BASE}/index.cfm?zaction=USER&zmethod=LOGIN"

# Probate estate subtype prefixes to include
ESTATE_SUBTYPES = {
    "FULL ADMINISTRATION WITH WILL",
    "FULL ADMINISTRATION WITHOUT WILL",
    "RELEASE FROM ADMINISTRATION",
    "SUMMARY ADMINISTRATION",
    "SMALL ESTATE AFFIDAVIT",
    "ADMINISTRATOR WITH WILL ANNEXED",
}

# Approximate case numbers bracketing each month (maintained empirically).
# Used to estimate start-of-scan for date-range queries on first run.
# Format: (case_number, date_string)
PROBATE_CASE_MILESTONES: list[tuple[int, str]] = [
    (640000, "2025-08-21"),
    (642000, "2025-12-04"),
    (643000, "2026-02-04"),
    (644000, "2026-03-25"),
    (644500, "2026-04-15"),
    (644700, "2026-04-21"),
]

TAX_LIEN_PAGE_URL = "https://treasurer.franklincountyohio.gov/Delinquent-Taxes/Tax-Lien-Sale"
# URL template (year = year of sale) — requires browser session to avoid 403
TAX_LIEN_CSV_URL = (
    "https://treasurer.franklincountyohio.gov/"
    "files/assets/treasurer/v/1/documents/final-tax-lien-list-{year}.csv"
)

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# ── Helpers ────────────────────────────────────────────────────────────────


def _parse_date(s: str) -> Optional[date]:
    """Parse MM/DD/YYYY or YYYY-MM-DD to date, returning None on failure."""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except (ValueError, AttributeError):
            pass
    return None


def _fmt(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _name_from_last_first(raw: str) -> str:
    """Convert 'LAST, FIRST M.' probate format to 'First M. Last'."""
    raw = raw.strip().strip("&nbsp;").rstrip(";")
    if "," in raw:
        parts = raw.split(",", 1)
        last = parts[0].strip().title()
        first = parts[1].strip().title()
        return f"{first} {last}".strip()
    return raw.title()


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "")
    return s.strip().strip("&nbsp;").strip()


def _get(url: str, retries: int = 3, delay: float = 1.5) -> Optional[requests.Response]:
    """GET with retry + delay. Returns None on persistent failure."""
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=_HTTP_HEADERS, timeout=20)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
                continue
            logger.warning("GET failed after %d tries: %s — %s", retries, url, exc)
            return None


# ── Probate Scraper ────────────────────────────────────────────────────────


def _probate_estimate_start(since: date) -> int:
    """Binary-search the milestone table to estimate first case number to scan."""
    best = PROBATE_CASE_MILESTONES[0][0] - 500
    for case_num, date_str in PROBATE_CASE_MILESTONES:
        ms_date = _parse_date(date_str)
        if ms_date and ms_date <= since:
            best = case_num
        elif ms_date and ms_date > since:
            break
    return max(0, best - 300)


def _probate_find_max_case(known_max: int) -> int:
    """Walk forward from known_max until no more cases are found."""
    current = known_max
    step = 100
    while True:
        resp = _get(PROBATE_DETAIL_URL.format(case_num=current + step))
        if resp and "CASE IS NOT FOUND" not in resp.text and len(resp.text) > 500:
            current += step
        else:
            # Narrow down
            if step == 1:
                break
            step = max(1, step // 10)
    return current


def _parse_probate_detail(html: str) -> dict:
    """Extract key fields from estate detail page HTML."""
    rows = re.findall(
        r'<font size="2" color="#07528B">(.*?)</font>.*?<font size="2">(.*?)</font>',
        html, re.DOTALL
    )
    data = {}
    for label, value in rows:
        label = _clean(label)
        value = _clean(value)
        if label:
            data[label] = value
    return data


def _parse_probate_fid(html: str) -> dict:
    """Extract PR/administrator info from fiduciary detail page HTML."""
    return _parse_probate_detail(html)  # same table structure


def _scrape_probate_case(case_num: int) -> Optional[NoticeData]:
    """Fetch and parse a single probate case. Returns None if not an estate case."""
    detail_url = PROBATE_DETAIL_URL.format(case_num=case_num)
    resp = _get(detail_url)
    if not resp:
        return None
    html = resp.text
    if "CASE IS NOT FOUND" in html or len(html) < 500:
        return None

    fields = _parse_probate_detail(html)
    case_type = fields.get("Case Type", "")
    subtype = fields.get("Case Subtype", "").upper()

    if "ESTATE" not in case_type.upper():
        return None
    if subtype and not any(s in subtype for s in (
        "ADMINISTRATION", "RELEASE FROM", "SUMMARY", "SMALL ESTATE"
    )):
        return None

    opened_raw = fields.get("Date Opened", "")
    opened = _parse_date(opened_raw)
    if not opened:
        return None

    decedent_raw = fields.get("Case Name", "")
    decedent_name = _name_from_last_first(decedent_raw)

    dod_raw = fields.get("Date of Death", "")
    dod = _parse_date(dod_raw)

    # Decedent address (often N/A in Franklin County)
    street = fields.get("Decedent Street", "N/A")
    city = fields.get("City", "N/A")
    zip_ = fields.get("Zip", "")

    # Fetch PR/administrator info from fiduciary page
    pr_name = ""
    pr_street = ""
    pr_city = ""
    pr_state = ""
    pr_zip = ""

    time.sleep(0.5)
    # The ;;01 URL serves fiduciary detail directly — no link-following needed
    fid_resp = _get(PROBATE_FID_URL.format(case_num=case_num))
    if fid_resp and len(fid_resp.text) > 300:
        fid_fields = _parse_probate_fid(fid_resp.text)
        pr_name_raw = fid_fields.get("Estate Fiduciaries Name", "")
        pr_name = _name_from_last_first(pr_name_raw) if pr_name_raw else ""
        pr_street = fid_fields.get("Street", "")
        pr_city = fid_fields.get("City", "")
        pr_state = fid_fields.get("State", "OH")
        pr_zip = fid_fields.get("Zi", "")  # HTML truncates "Zip" label to "Zi"
        if pr_street.upper() == "N/A":
            pr_street = ""

    notice = NoticeData(
        date_added=_fmt(opened),
        notice_type="probate",
        county="Franklin",
        state="OH",
        owner_name=pr_name or decedent_name,
        decedent_name=decedent_name,
        date_of_death=_fmt(dod) if dod else "",
        owner_street=pr_street,
        owner_city=pr_city,
        owner_state=pr_state,
        owner_zip=pr_zip,
        source_url=detail_url,
        raw_text=f"{subtype} | Opened: {opened_raw} | DOD: {dod_raw}",
    )

    if street and street.upper() not in ("N/A", ""):
        notice.address = street
        notice.city = city if city.upper() != "N/A" else "Columbus"
        notice.zip = zip_
    else:
        notice.city = "Columbus"

    return notice


def scrape_probate(
    since: date,
    until: date,
    state: dict,
) -> list[NoticeData]:
    """Scan probate case numbers and return estate cases opened in [since, until].

    Args:
        since: Start date (inclusive).
        until: End date (inclusive).
        state: Mutable dict used to persist last_case_num between runs.
                Set state["oh_franklin_probate_last_case"] to resume from a checkpoint.
    """
    last_known = state.get("oh_franklin_probate_last_case", 0)
    start_case = last_known or _probate_estimate_start(since)
    logger.info("Probate scan: cases %d → current max (looking for %s – %s)", start_case, since, until)

    # Find current maximum case number (walk forward from last milestone)
    max_case = PROBATE_CASE_MILESTONES[-1][0]
    max_case = _probate_find_max_case(max_case)
    logger.info("Probate current max case: %d", max_case)

    notices: list[NoticeData] = []
    scanned = 0
    found = 0
    new_max = start_case

    for case_num in range(start_case, max_case + 1):
        time.sleep(0.4)
        notice = _scrape_probate_case(case_num)
        scanned += 1

        if notice:
            new_max = case_num
            opened = _parse_date(notice.date_added)
            if opened and since <= opened <= until:
                notices.append(notice)
                found += 1
                logger.info("  Probate %d: %s (%s)", case_num, notice.decedent_name, notice.date_added)
            elif opened and opened > until:
                logger.debug("  Case %d opened after window (%s), continuing", case_num, notice.date_added)
        else:
            pass  # NOT_FOUND or non-estate — skip

        if scanned % 50 == 0:
            logger.info("  Probate: scanned %d cases, found %d so far (at case %d)", scanned, found, case_num)

    # Update state checkpoint to avoid re-scanning old cases next run
    if new_max > last_known:
        state["oh_franklin_probate_last_case"] = new_max

    logger.info("Probate done: scanned %d cases, found %d in date range", scanned, found)
    return notices


# ── Tax Sale Scraper ───────────────────────────────────────────────────────


async def scrape_tax_sale(year: Optional[int] = None) -> list[NoticeData]:
    """Download and parse the Franklin County annual tax lien list CSV.

    The treasurer site returns 403 on direct HTTP requests; must use a Playwright
    browser session. Visits the Tax Lien Sale page first for referrer/cookie context,
    then downloads the CSV link found on that page.

    The list is published each fall (Oct/Nov) for that year's delinquencies.
    All records are returned — there's no date filtering since it's a point-in-time
    annual snapshot. Returns empty list if the year's list hasn't been published yet.

    Args:
        year: The tax lien list year (defaults to current year, falls back to prior year).
    """
    from playwright.async_api import async_playwright

    today = date.today()
    if year is None:
        year = today.year

    today_str = _fmt(today)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
        )
        page = await ctx.new_page()

        try:
            # Establish session context on the Tax Lien Sale page
            await page.goto(TAX_LIEN_PAGE_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)

            # Try current year then prior year. Short expect_download timeout so a
            # 404 page (no download event) fails fast instead of hanging 30s.
            csv_content: Optional[str] = None
            used_year = year
            for try_year in ([year, year - 1] if year == today.year else [year]):
                csv_url = TAX_LIEN_CSV_URL.format(year=try_year)
                logger.info("Tax sale: downloading %s", csv_url)
                try:
                    async with page.expect_download(timeout=8000) as dl_info:
                        try:
                            await page.goto(csv_url, wait_until="domcontentloaded", timeout=15000)
                        except Exception:
                            pass  # "Download is starting" error is expected when a CSV triggers
                    download = await dl_info.value
                    path = await download.path()
                    csv_content = Path(path).read_text(encoding="utf-8-sig", errors="replace")
                    used_year = try_year
                    break
                except Exception as exc:
                    logger.info("Tax sale: %d not available (%s), trying next", try_year, exc)
                    continue

        finally:
            await browser.close()

    if csv_content is None:
        logger.warning("Tax sale: no list found for %d or %d", year, year - 1)
        return []

    reader = csv.DictReader(io.StringIO(csv_content))
    notices: list[NoticeData] = []

    for i, row in enumerate(reader):
        parcel = row.get("Dist/Parc/Ext #", "").strip()
        location = row.get("Location Address", "").strip()
        owner = row.get("Owner Name 1", "").strip()
        mail_street = row.get("Mail Address 1", "").strip()
        mail_city = row.get("Mail City", "").strip()
        mail_state = row.get("Mail State", "OH").strip()
        mail_zip = row.get("Mail Zip", "").strip()
        cdq_year = row.get("CDQ Year", "").strip()
        net_tax = row.get("Net Tax Due", "").strip()
        lien_val = row.get("Net Lien Value", "").strip()

        if not location and not owner:
            continue

        url = TAX_LIEN_CSV_URL.format(year=used_year)
        notice = NoticeData(
            date_added=today_str,
            notice_type="tax_sale",
            county="Franklin",
            state="OH",
            address=location,
            city="Columbus",
            owner_name=owner.title() if owner else "",
            owner_street=mail_street.title() if mail_street else "",
            owner_city=mail_city.title() if mail_city else "",
            owner_state=mail_state,
            owner_zip=mail_zip,
            parcel_id=parcel,
            tax_delinquent_amount=net_tax,
            tax_delinquent_years=cdq_year,
            source_url=url,
            raw_text=(
                f"Tax lien {used_year} | Parcel: {parcel} | "
                f"Tax due: ${net_tax} | Lien value: ${lien_val} | "
                f"Delinquent since: {cdq_year}"
            ),
        )
        notices.append(notice)

        if (i + 1) % 500 == 0:
            logger.info("  Tax sale: parsed %d records", i + 1)

    logger.info("Tax sale: %d records from %d list", len(notices), used_year)
    return notices


# ── Foreclosure Scraper ────────────────────────────────────────────────────


async def scrape_foreclosures(
    since: date,
    until: date,
    username: str,
    password: str,
) -> list[NoticeData]:
    """Scrape RealForeclose sheriff sale listings for Franklin County, OH.

    Requires a free RealForeclose account at:
        https://franklin.sheriffsaleauction.ohio.gov

    Set FRANKLIN_OH_SHERIFF_USERNAME and FRANKLIN_OH_SHERIFF_PASSWORD in .env.

    Franklin County holds sales on Fridays. This scraper:
      1. Logs in to the RealForeclose portal
      2. Checks each Friday in [since, until] for auction listings
      3. Extracts property address, parcel, case number, and minimum bid
    """
    if not username or not password:
        logger.warning(
            "Foreclosure: FRANKLIN_OH_SHERIFF_USERNAME / FRANKLIN_OH_SHERIFF_PASSWORD not set — skipping"
        )
        return []

    from playwright.async_api import async_playwright

    notices: list[NoticeData] = []

    # Collect Fridays in the date range
    fridays: list[date] = []
    d = since
    while d <= until:
        if d.weekday() == 4:  # Friday
            fridays.append(d)
        d += timedelta(days=1)

    if not fridays:
        logger.info("Foreclosure: no Fridays in %s–%s — nothing to scrape", since, until)
        return []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()

        try:
            # ── Login ──────────────────────────────────────────────────────
            logger.info("Foreclosure: logging in to RealForeclose...")
            await page.goto(SHERIFF_BASE, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            # Fill login form
            await page.fill("#LogName", username)
            await page.fill("#LogPass", password)

            # Submit — the "button" is a <div id="LogButton"> inside a <label>
            await page.locator("#LogButton").click()
            await page.wait_for_timeout(3000)

            # Check for lockout or error
            page_text = await page.text_content("body") or ""
            if "locked out" in page_text.lower():
                logger.error("Foreclosure: RealForeclose account locked — contact customerservice@realauction.com")
                return []
            if "invalid" in page_text.lower() or "incorrect" in page_text.lower():
                logger.error("Foreclosure: login failed — check FRANKLIN_OH_SHERIFF_USERNAME / PASSWORD")
                return []

            logger.info("Foreclosure: logged in successfully")

            # ── Scrape each Friday auction ─────────────────────────────────
            for auction_date in fridays:
                date_str = auction_date.strftime("%m/%d/%Y")
                url = f"{SHERIFF_BASE}/index.cfm?zaction=AUCTION&zmethod=PREVIEW&AuctionDate={date_str}"
                logger.info("Foreclosure: checking auction %s (%s)", date_str, url)

                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)

                body = await page.text_content("body") or ""
                if "no auction" in body.lower() or "no properties" in body.lower():
                    logger.info("  No listings for %s", date_str)
                    continue

                # Extract auction listings
                # RealForeclose uses divs with class patterns like "AD_LotDetails", "AUCTION_DETAILS"
                rows = await page.query_selector_all(
                    ".AD_LotDetails, .AUCTION_DETAILS, [id*='SaleDataRow'], tr.dataRow"
                )

                if not rows:
                    # Try to get raw HTML and parse
                    html = await page.content()
                    notices.extend(_parse_realforeclose_html(html, auction_date, url))
                    continue

                for row in rows:
                    row_text = await row.text_content() or ""
                    notice = _extract_realforeclose_row(row_text, auction_date, url)
                    if notice:
                        notices.append(notice)

                logger.info("  Foreclosure: found %d listings for %s", len(notices), date_str)
                await page.wait_for_timeout(1500)

        except Exception as exc:
            logger.error("Foreclosure scraper error: %s", exc, exc_info=True)
        finally:
            await browser.close()

    logger.info("Foreclosure: total %d listings", len(notices))
    return notices


def _parse_realforeclose_html(html: str, auction_date: date, url: str) -> list[NoticeData]:
    """Parse RealForeclose auction listing HTML into NoticeData records."""
    notices = []
    today_str = _fmt(date.today())
    auction_str = _fmt(auction_date)

    # Look for property address blocks — RealForeclose typically puts them in
    # divs with case info. Try multiple patterns.
    #
    # Pattern: table rows with parcel, address, case number, min bid columns
    address_blocks = re.findall(
        r"(?:Property Address|Location)[:\s]*([\w\s,\.#]+?)(?:\n|<|Parcel|Case)",
        html, re.IGNORECASE
    )
    parcel_blocks = re.findall(
        r"(?:Parcel|Parcel #)[:\s]*([A-Z0-9\-]+)",
        html, re.IGNORECASE
    )
    min_bid_blocks = re.findall(
        r"(?:Min(?:imum)?\s*Bid|Opening\s*Bid)[:\s]*\$?([\d,\.]+)",
        html, re.IGNORECASE
    )

    for i, addr in enumerate(address_blocks):
        addr = addr.strip()
        if len(addr) < 5:
            continue

        notice = NoticeData(
            date_added=today_str,
            auction_date=auction_str,
            notice_type="foreclosure",
            county="Franklin",
            state="OH",
            address=addr.split(",")[0].strip() if "," in addr else addr,
            city="Columbus",
            source_url=url,
            raw_text=addr,
        )
        if i < len(parcel_blocks):
            notice.parcel_id = parcel_blocks[i]
        if i < len(min_bid_blocks):
            notice.raw_text += f" | Min bid: ${min_bid_blocks[i]}"

        notices.append(notice)

    return notices


def _extract_realforeclose_row(row_text: str, auction_date: date, url: str) -> Optional[NoticeData]:
    """Extract NoticeData from a single RealForeclose table row's text content."""
    row_text = row_text.strip()
    if not row_text or len(row_text) < 10:
        return None

    # Try to extract address (look for street number pattern)
    addr_match = re.search(r"\b(\d{2,6}\s+[\w\s\.]+(?:ST|AVE|RD|DR|LN|CT|BLVD|PL|WAY|TRL)\b)", row_text, re.IGNORECASE)
    address = addr_match.group(1).strip() if addr_match else ""

    parcel_match = re.search(r"\b(\d{3}-\d{6}-\d{2})\b", row_text)
    parcel = parcel_match.group(1) if parcel_match else ""

    return NoticeData(
        date_added=_fmt(date.today()),
        auction_date=_fmt(auction_date),
        notice_type="foreclosure",
        county="Franklin",
        state="OH",
        address=address,
        city="Columbus",
        parcel_id=parcel,
        source_url=url,
        raw_text=row_text[:500],
    )


# ── Main Entry Point ───────────────────────────────────────────────────────


async def scrape_franklin_oh(
    mode: str = "daily",
    since_date: Optional[str] = None,
    types: Optional[list[str]] = None,
    state: Optional[dict] = None,
    sheriff_username: str = "",
    sheriff_password: str = "",
) -> list[NoticeData]:
    """Scrape Franklin County, OH notices. Returns list[NoticeData].

    Args:
        mode: "daily" (last 7 days) or "historical" (last 12 months).
        since_date: ISO date string override for start date. Overrides mode.
        types: List of notice types to scrape. Defaults to all three.
        state: Mutable dict for persisting scraper state between runs.
                Caller is responsible for loading/saving this dict.
        sheriff_username: RealForeclose username (from env var).
        sheriff_password: RealForeclose password (from env var).
    """
    if state is None:
        state = {}

    if types is None:
        # Default: omit "foreclosure" (Sheriff Auction) — Recorder gives the
        # same signal 4-12 weeks earlier. Run `--types foreclosure` to opt back in.
        types = ["probate", "tax_sale", "tax_delinquent", "eviction", "recorder"]

    today = date.today()

    if since_date:
        since = _parse_date(since_date) or (today - timedelta(days=7))
    elif mode == "historical":
        since = today - timedelta(days=365)
    else:
        since = today - timedelta(days=7)

    until = today

    logger.info(
        "Franklin County OH scraper: %s → %s | types: %s",
        since, until, ", ".join(types)
    )

    all_notices: list[NoticeData] = []

    if "foreclosure" in types:
        logger.info("── Foreclosure ──")
        fc_notices = await scrape_foreclosures(
            since=since,
            until=until,
            username=sheriff_username,
            password=sheriff_password,
        )
        all_notices.extend(fc_notices)
        logger.info("Foreclosure: %d records", len(fc_notices))

    if "probate" in types:
        logger.info("── Probate ──")
        pb_notices = scrape_probate(since=since, until=until, state=state)
        all_notices.extend(pb_notices)
        logger.info("Probate: %d records", len(pb_notices))

    if "tax_sale" in types:
        logger.info("── Tax Sale ──")
        ts_notices = await scrape_tax_sale()
        all_notices.extend(ts_notices)
        logger.info("Tax sale: %d records", len(ts_notices))

    if "tax_delinquent" in types:
        logger.info("── Tax Delinquent ──")
        from oh_franklin_tax_delinquent import scrape_tax_delinquent
        td_notices = scrape_tax_delinquent(state=state)
        all_notices.extend(td_notices)
        logger.info("Tax delinquent: %d records", len(td_notices))

    if "eviction" in types:
        logger.info("── Eviction ──")
        from oh_franklin_eviction import scrape_evictions
        ev_notices = scrape_evictions(since=since)
        all_notices.extend(ev_notices)
        logger.info("Eviction: %d records", len(ev_notices))

    if "recorder" in types or "lis_pendens" in types:
        # Recorder scraper — earliest distress signal. Pulls LIS PENDENS
        # (foreclosure within 7 days of complaint per ORC 2703.26) plus
        # FEDERAL TAX LIEN, MECHANICS LIEN, ASSIGN OF RENTS, CERTIFICATE OF
        # TRANSFER, TRUST, SHERIFFS DEED. See oh_franklin_recorder.py.
        logger.info("── Recorder (Notice/Lien/Trust/Transfer) ──")
        from oh_franklin_recorder import scrape_recorder_async, DEFAULT_DOC_CODES
        rc_notices = await scrape_recorder_async(
            since=since, until=until, doc_codes=DEFAULT_DOC_CODES,
        )
        all_notices.extend(rc_notices)
        logger.info("Recorder: %d records", len(rc_notices))

    logger.info("Franklin County OH total: %d notices", len(all_notices))
    return all_notices
