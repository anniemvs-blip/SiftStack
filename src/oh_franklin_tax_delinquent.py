"""Tax-delinquent property scraper for Franklin County, OH.

Source: Franklin County Auditor's public ArcGIS REST feature service.

  https://gis.franklincountyohio.gov/hosting/rest/services/RealEstate/Neighborhood_Detail/MapServer/2

No authentication. Plain HTTPS. JSON responses paginated at 2000 records per
query. Total parcel count varies (~7,700 as of 2026-05-21) and is rolling —
parcels drop off as taxes are paid.

Why this matters for REI: tax-delinquent owners are earlier in the distress
funnel than tax-lien-sale or sheriff-foreclosure owners. CdqYear tells us
how long they've been behind. TotalBalance shows current exposure.

NOTE: This endpoint does NOT expose owner names (the `CNVYNAME` field is
"Sub or Condo Name", not owner). Owner name has to come from the downstream
Auditor parcel lookup or Smarty/Tracerfy enrichment.
"""

import logging
from datetime import date
from typing import Optional

import requests

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

ARCGIS_BASE = (
    "https://gis.franklincountyohio.gov/hosting/rest/services/"
    "RealEstate/Neighborhood_Detail/MapServer/2"
)
PAGE_SIZE = 2000

# Fields we care about. Excludes geometry, tax-dist codes, audit timestamps.
OUT_FIELDS = ",".join([
    "PARCELID",
    "SITEADDRESS",
    "ZIPCD",
    "CdqYear",
    "NetAnnualTax",
    "TotalOwed",
    "TotalBalance",
    "LASTUPDATE",
])

AUDITOR_PARCEL_URL_TMPL = (
    "https://property.franklincountyauditor.com/_web/search/commonsearch.aspx"
    "?mode=parid&parid={parcel}"
)


def _fetch_count() -> int:
    """Return the live parcel count for sanity checking."""
    try:
        resp = requests.get(
            f"{ARCGIS_BASE}/query",
            params={"where": "1=1", "returnCountOnly": "true", "f": "json"},
            timeout=15,
        )
        resp.raise_for_status()
        return int(resp.json().get("count", 0))
    except Exception as e:
        logger.warning("Tax delinquent count probe failed: %s", e)
        return -1


def _fetch_page(offset: int) -> list[dict]:
    """Fetch a single page of parcel attributes."""
    resp = requests.get(
        f"{ARCGIS_BASE}/query",
        params={
            "where": "1=1",
            "outFields": OUT_FIELDS,
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "orderByFields": "OBJECTID ASC",
            "f": "json",
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS error at offset {offset}: {data['error']}")
    return [f.get("attributes", {}) for f in data.get("features", [])]


def scrape_tax_delinquent(min_balance: float = 1.0) -> list[NoticeData]:
    """Pull all tax-delinquent parcels from Franklin County Auditor's GIS feed.

    Args:
        min_balance: Minimum TotalBalance ($) to include — filters parcels that
            are technically in the dataset but have a zero/negative balance.

    Returns:
        list[NoticeData] — one per delinquent parcel, with notice_type set to
        "tax_delinquent". Owner name is empty; downstream enrichment fills it.
    """
    today_str = date.today().strftime("%Y-%m-%d")
    today_year = date.today().year

    total_expected = _fetch_count()
    if total_expected > 0:
        logger.info("Tax delinquent: %d total parcels live in feed", total_expected)

    notices: list[NoticeData] = []
    offset = 0
    skipped_no_addr = 0
    skipped_low_balance = 0

    while True:
        try:
            attrs_list = _fetch_page(offset)
        except Exception as e:
            logger.error("Tax delinquent page fetch failed at offset %d: %s", offset, e)
            break

        if not attrs_list:
            break

        for attrs in attrs_list:
            parcel = (attrs.get("PARCELID") or "").strip()
            addr = (attrs.get("SITEADDRESS") or "").strip()
            zip_code = (attrs.get("ZIPCD") or "").strip()
            cdq_year = attrs.get("CdqYear")
            total_owed = float(attrs.get("TotalOwed") or 0)
            total_balance = float(attrs.get("TotalBalance") or 0)

            if not addr:
                skipped_no_addr += 1
                continue
            if total_balance < min_balance:
                skipped_low_balance += 1
                continue

            years_delinquent = ""
            if cdq_year and isinstance(cdq_year, int):
                years_delinquent = str(max(0, today_year - cdq_year))

            raw_summary = (
                f"Parcel {parcel} delinquent since {cdq_year or 'unknown'}. "
                f"Total owed: ${total_owed:,.2f}. Current balance: ${total_balance:,.2f}."
            )

            notices.append(NoticeData(
                date_added=today_str,
                address=addr,
                city="",  # Smarty enrichment fills city from zip
                state="OH",
                zip=zip_code,
                notice_type="tax_delinquent",
                county="Franklin",
                source_url=AUDITOR_PARCEL_URL_TMPL.format(parcel=parcel),
                raw_text=raw_summary,
                parcel_id=parcel,
                tax_delinquent_amount=f"{total_balance:.2f}",
                tax_delinquent_years=years_delinquent,
            ))

        if len(attrs_list) < PAGE_SIZE:
            break

        offset += PAGE_SIZE
        if offset % (PAGE_SIZE * 2) == 0:
            logger.info("  Tax delinquent: %d records so far (offset %d)", len(notices), offset)

    logger.info(
        "Tax delinquent done: %d records (skipped %d no-address, %d low-balance)",
        len(notices), skipped_no_addr, skipped_low_balance,
    )
    return notices
