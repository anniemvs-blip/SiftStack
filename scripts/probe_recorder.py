"""One-shot diagnostic: dump every cell of a few Recorder result rows so we
can see whether the search-results table exposes a property address column we're
currently ignoring.

Also fetches the per-document detail page for one row to check whether the
detail view exposes a structured address (so we could enrich at scrape time
instead of relying on the downstream Auditor parcel lookup)."""

import asyncio
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oh_franklin_recorder import (  # noqa: E402
    DEFAULT_DOC_CODES, HOMEPAGE, RESULTS_URL_TMPL,
)


async def main() -> None:
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    today = date.today()
    since = today - timedelta(days=7)
    types_param = "%2C".join(DEFAULT_DOC_CODES)
    url = RESULTS_URL_TMPL.format(
        types=types_param, offset=0,
        start=since.strftime("%Y%m%d"), end=today.strftime("%Y%m%d"),
    )

    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        print(f"Warmup: {HOMEPAGE}")
        await page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2500)

        print(f"Results: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_selector('tr[role="row"]', timeout=25000)
        await page.wait_for_timeout(2000)

        # === Header inspection ===
        header_cells = page.locator('tr[role="row"]').first.locator("th, td")
        n_hdr = await header_cells.count()
        print(f"\n=== Header row: {n_hdr} cells ===")
        for i in range(n_hdr):
            txt = (await header_cells.nth(i).inner_text()).strip()
            print(f"  cell[{i}]: {txt!r}")

        # === First 2 data rows: every cell ===
        rows = page.locator('tr[role="row"]')
        n = await rows.count()
        print(f"\n=== {n} total rows. Dumping first 2 data rows: ===")
        first_inst = ""
        # row 0 is header, so start at 1
        for r_idx in range(1, min(3, n)):
            cells = rows.nth(r_idx).locator("td")
            nc = await cells.count()
            print(f"\n--- Row {r_idx}: {nc} cells ---")
            for c_idx in range(nc):
                txt = (await cells.nth(c_idx).inner_text()).strip()
                print(f"  cell[{c_idx}]: {txt[:200]!r}")
            if r_idx == 1:
                first_inst = (await cells.nth(7).inner_text()).strip()

        # === Detail page for the first row ===
        if first_inst:
            doc_url = f"{HOMEPAGE}/doc/{first_inst}"
            print(f"\n=== Detail page: {doc_url} ===")
            await page.goto(doc_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            body = await page.content()
            print(f"  HTML size: {len(body):,} bytes")
            # Look for known address signals
            patterns = [
                r'"propertyAddress"\s*:\s*"([^"]+)"',
                r'"siteAddress"\s*:\s*"([^"]+)"',
                r'"address1"\s*:\s*"([^"]+)"',
                r'"propertyAddresses"\s*:\s*\[[^\]]*"([^"]+)"',
                r'Property Address[:\s<]+([^<\n]+)',
                r'Site Address[:\s<]+([^<\n]+)',
            ]
            any_hit = False
            for pat in patterns:
                m = re.search(pat, body)
                if m:
                    any_hit = True
                    print(f"  HIT {pat!r}: {m.group(1)[:200]!r}")
            if not any_hit:
                print("  No structured address pattern matched. Sampling 'address' contexts:")
                for m in re.finditer(r".{60}[Aa]ddress.{120}", body):
                    snippet = m.group(0).replace("\n", " ")
                    print(f"  …{snippet[:250]}…")
                    break  # one example is enough

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
