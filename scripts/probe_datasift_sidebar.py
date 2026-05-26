"""Diagnostic: log into DataSift fresh and dump all 'Upload'-related elements
in the sidebar + a screenshot so we can identify the actual selectors.

This bypasses the cached cookies that may be leaving the session in a stuck
upload-wizard state from previous failed attempts."""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))


async def main() -> None:
    from playwright.async_api import async_playwright

    # Force a fresh session — delete any cached cookies file
    cookie_files = [
        Path("datasift_cookies.json"),
        Path("datasift_cookies.json.bak"),
    ]
    for cf in cookie_files:
        if cf.exists():
            cf.unlink()
            print(f"Removed cached cookie file: {cf}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        print("\n1. Navigating to login page...")
        await page.goto("https://app.reisift.io/login", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        print("2. Filling credentials...")
        await page.fill('input[type="email"], input[name="email"]', os.getenv("DATASIFT_EMAIL", ""))
        await page.fill('input[type="password"], input[name="password"]', os.getenv("DATASIFT_PASSWORD", ""))
        # Click the hidden checkboxes (per CLAUDE.md: hidden inputs, click labels)
        for label_text in ("Remember", "Terms"):
            try:
                lbl = page.locator(f'label:has-text("{label_text}")').first
                if await lbl.count() > 0:
                    await lbl.click()
            except Exception:
                pass
        # Click login
        await page.click('button:has-text("Login"), button:has-text("Log in"), button:has-text("Sign in")')
        print("3. Waiting for SPA login redirect...")
        await page.wait_for_timeout(8000)

        print(f"4. Current URL: {page.url}")
        await page.screenshot(path="probe_after_login.png", full_page=False)
        print("   Screenshot: probe_after_login.png")

        # Now find ALL elements containing "Upload" text in the visible viewport
        print("\n5. Scanning page for 'Upload'-related elements...")
        elements = await page.evaluate("""() => {
            const all = Array.from(document.querySelectorAll('*'));
            const matches = [];
            for (const el of all) {
                const text = (el.textContent || '').trim();
                if (text.toLowerCase().includes('upload') && text.length < 60) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        matches.push({
                            tag: el.tagName.toLowerCase(),
                            text: text,
                            className: (el.className || '').toString().slice(0, 80),
                            x: Math.round(rect.x),
                            y: Math.round(rect.y),
                            w: Math.round(rect.width),
                            h: Math.round(rect.height),
                            visible: rect.x >= 0 && rect.y >= 0 && rect.x < 1440,
                        });
                    }
                }
            }
            // Dedupe by text+y, prefer smaller elements
            const seen = new Map();
            for (const m of matches) {
                const key = m.text + '|' + m.y;
                if (!seen.has(key) || seen.get(key).w > m.w) seen.set(key, m);
            }
            return Array.from(seen.values()).sort((a, b) => a.y - b.y);
        }""")

        print(f"\n=== Found {len(elements)} 'Upload'-text elements (visible) ===")
        for el in elements:
            print(f"  {el['tag']:6s} y={el['y']:4d} x={el['x']:4d} w={el['w']:4d} text={el['text']!r:35s} class={el['className']!r}")

        # Specifically look for a sidebar-like Upload File entry (x < 250 typically)
        sidebar = [e for e in elements if e["x"] < 250 and "upload" in e["text"].lower()]
        print(f"\n=== Sidebar candidates (x < 250) ===")
        for el in sidebar:
            print(f"  {el['tag']} {el['text']!r}  pos=({el['x']},{el['y']}) class={el['className']!r}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
