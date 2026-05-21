"""Tax-delinquent property scraper for Franklin County, OH.

Source: Franklin County Auditor's public ArcGIS REST feature service.

  https://gis.franklincountyohio.gov/hosting/rest/services/RealEstate/Neighborhood_Detail/MapServer/2

No authentication. Plain HTTPS. JSON responses paginated at 2000 records per
query. Total parcel count varies (~7,700 as of 2026-05-21) and is rolling —
parcels drop off as taxes are paid.

Why this matters for REI: tax-delinquent owners are earlier in the distress
funnel than tax-lien-sale or sheriff-foreclosure owners. CdqYear tells us
how long they've been behind. TotalBalance shows current exposure.

INCREMENTAL MODE (default): on subsequent runs only returns parcels where the
`LASTUPDATE` field has changed since the previous run — typically ~30-100
new/changed parcels per day vs. the full ~7,700-row snapshot. The first run
(empty state) does one full pull to seed the database, then stores the max
LASTUPDATE timestamp in `state["oh_franklin_tax_delinquent_last_update"]`.

NOTE: This endpoint does NOT expose owner names (the `CNVYNAME` field is
"Sub or Condo Name", not owner). Owner name has to come from the downstream
Auditor parcel lookup or Smarty/Tracerfy enrichment.
"""

import logging
from datetime import date, datetime, timezone
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

# State dict key for incremental cursor
STATE_KEY = "oh_franklin_tax_delinquent_last_update"


def _fetch_count(where: str = "1=1") -> int:
    """Return the live parcel count for sanity checking."""
    try:
        resp = requests.get(
            f"{ARCGIS_BASE}/query",
            params={"where": where, "returnCountOnly": "true", "f": "json"},
            timeout=15,
        )
        resp.raise_for_status()
        return int(resp.json().get("count", 0))
    except Exception as e:
        logger.warning("Tax delinquent count probe failed: %s", e)
        return -1


def _fetch_page(offset: int, where: str = "1=1") -> list[dict]:
    """Fetch a single page of parcel attributes."""
    resp = requests.get(
        f"{ARCGIS_BASE}/query",
        params={
            "where": where,
            "outFields": OUT_FIELDS,
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "orderByFields": "LASTUPDATE ASC",
            "f": "json",
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS error at offset {offset}: {data['error']}")
    return [f.get("attributes", {}) for f in data.get("features", [])]


def _epoch_ms_to_arcgis_timestamp(epoch_ms: int) -> str:
    """Convert epoch milliseconds to ArcGIS TIMESTAMP literal.

    ArcGIS REST date filtering requires `TIMESTAMP 'YYYY-MM-DD HH:MM:SS'`
    literal syntax — direct epoch-ms comparisons silently return 0 rows.
    """
    dt = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return f"TIMESTAMP '{dt.strftime('%Y-%m-%d %H:%M:%S')}'"


def scrape_tax_delinquent(
    state: Optional[dict] = None,
    min_balance: float = 5000.0,
    max_years_delinquent: int = 5,
    class_codes: Optional[set[str]] = None,
) -> list[NoticeData]:
    """Pull tax-delinquent parcels from Franklin County Auditor's GIS feed.

    Default filters match user buying criteria (set 2026-05-21):
      - min_balance=$5,000 — drops low-stakes parcels where the owner isn't
        under meaningful pressure
      - max_years_delinquent=5 — drops long-dead leads (estate/abandoned
        situations or already on someone else's list)
      - class_codes={"510"} — single-family dwellings only; excludes
        commercial, industrial, multi-family, condos, vacant land

    Args:
        state: Mutable dict used to persist the LASTUPDATE cursor between
            runs. If `state[STATE_KEY]` is set, only parcels with
            LASTUPDATE > that value are returned (incremental mode).
            On first run (empty state), does a full pull and stores the
            max LASTUPDATE for next time.
        min_balance: Minimum TotalBalance ($) to include. Pushed into the
            server-side WHERE clause for efficiency.
        max_years_delinquent: Drop parcels first delinquent more than N
            years ago. Computed as ``CdqYear >= today_year - N``. Pushed
            server-side.
        class_codes: Set of Auditor CLASSCD values to include. Defaults to
            ``{"510"}`` (single-family dwelling, platted lot). CLASSCD isn't
            on the tax-delinquent layer, so this filter applies post-fetch
            via a Parcel_Features batch lookup. Pass an empty set or None
            with explicit override to skip the property-type filter.

    Returns:
        list[NoticeData] — one per delinquent parcel matching all filters.
        notice_type is "tax_delinquent". Owner name is empty; downstream
        enrichment fills it.
    """
    if state is None:
        state = {}
    if class_codes is None:
        class_codes = {"510"}

    today_str = date.today().isoformat()
    today_year = date.today().year
    min_cdq_year = today_year - max_years_delinquent

    # Build WHERE clause: incremental cursor + buying-criteria filters
    # (server-side). CLASSCD filter is applied post-fetch via a join because
    # CLASSCD lives on the master parcel layer, not this delinquency layer.
    last_seen_ms = int(state.get(STATE_KEY, 0))
    clauses = [
        f"TotalBalance >= {min_balance}",
        f"CdqYear >= {min_cdq_year}",
    ]
    if last_seen_ms > 0:
        ts_literal = _epoch_ms_to_arcgis_timestamp(last_seen_ms)
        clauses.append(f"LASTUPDATE > {ts_literal}")
        logger.info(
            "Tax delinquent: INCREMENTAL mode (LASTUPDATE > %s)",
            datetime.fromtimestamp(last_seen_ms / 1000, tz=timezone.utc).isoformat(),
        )
    else:
        logger.info("Tax delinquent: FULL pull (empty state — first run / seeding)")
    logger.info(
        "Tax delinquent filters: balance >= $%s, CdqYear >= %d, CLASSCD in %s",
        f"{min_balance:,.0f}", min_cdq_year, sorted(class_codes) or "ANY",
    )
    where = " AND ".join(clauses)

    total_expected = _fetch_count(where=where)
    if total_expected >= 0:
        logger.info("Tax delinquent: %d parcels match window", total_expected)
    if total_expected == 0:
        logger.info("Tax delinquent: nothing new since last run — done")
        return []

    notices: list[NoticeData] = []
    offset = 0
    skipped_no_addr = 0
    max_lastupdate_seen = last_seen_ms

    while True:
        try:
            attrs_list = _fetch_page(offset, where=where)
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
            last_update_ms = attrs.get("LASTUPDATE") or 0

            # Track max LASTUPDATE for cursor update — even if we skip this row
            if isinstance(last_update_ms, (int, float)) and last_update_ms > max_lastupdate_seen:
                max_lastupdate_seen = int(last_update_ms)

            if not addr:
                skipped_no_addr += 1
                continue
            # Balance + CdqYear filters already applied server-side in WHERE.

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

    # Update state cursor for next run
    if max_lastupdate_seen > last_seen_ms:
        state[STATE_KEY] = max_lastupdate_seen
        logger.info(
            "Tax delinquent: cursor advanced to LASTUPDATE = %s",
            datetime.fromtimestamp(max_lastupdate_seen / 1000, tz=timezone.utc).isoformat(),
        )

    # CLASSCD filter — post-fetch via batch Parcel_Features lookup
    skipped_wrong_class = 0
    if class_codes and notices:
        from franklin_auditor import PARCEL_LAYER, normalize_parcel_id
        import requests as _requests

        norm_map: dict[str, list[NoticeData]] = {}
        for n in notices:
            norm = normalize_parcel_id(n.parcel_id)
            if norm:
                norm_map.setdefault(norm, []).append(n)

        keep: list[NoticeData] = []
        BATCH = 50
        keys = list(norm_map.keys())
        class_by_parcel: dict[str, str] = {}
        for i in range(0, len(keys), BATCH):
            chunk = keys[i:i + BATCH]
            where_in = ",".join(f"'{p}'" for p in chunk)
            try:
                resp = _requests.get(
                    f"{PARCEL_LAYER}/query",
                    params={
                        "where": f"PARCELID IN ({where_in})",
                        "outFields": "PARCELID,CLASSCD",
                        "returnGeometry": "false",
                        "f": "json",
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                for f in resp.json().get("features", []):
                    a = f.get("attributes", {})
                    class_by_parcel[(a.get("PARCELID") or "").strip()] = str(
                        a.get("CLASSCD") or ""
                    ).strip()
            except _requests.RequestException as e:
                logger.warning(
                    "Tax delinquent CLASSCD lookup failed for batch %d: %s — keeping records",
                    i, e,
                )

        for norm, group in norm_map.items():
            cls = class_by_parcel.get(norm, "")
            if cls in class_codes:
                keep.extend(group)
            else:
                skipped_wrong_class += len(group)
        notices = keep

    logger.info(
        "Tax delinquent done: %d records (skipped %d no-address, %d wrong CLASSCD)",
        len(notices), skipped_no_addr, skipped_wrong_class,
    )
    return notices
