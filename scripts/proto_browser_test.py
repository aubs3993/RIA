"""Throwaway prototype: can a headless browser recover contacts where httpx failed?

Renders / and /team for a sample of no-contact domains, reports rendered text
length, emails found, and cascade-title hits.
"""
import asyncio
import re
import sys

sys.path.insert(0, ".")

from playwright.async_api import async_playwright

from src.scrape_primary_contact import CASCADE
from src.scrape_websites import EMAIL_REGEX
import config

SAMPLES = {
    "js_shell": ["riverpartners.com", "raineyrandall.com", "cypresspointwealth.com"],
    "no_cache": ["wilkinsinvest.com", "htgadvisors.com", "mgfinancial.com"],
    "empty_dir": ["elgethuncapitalmanagement.com", "kingwealthmanagementgroup.com"],
}
PATHS = ["/", "/team"]


def summarize(html: str) -> tuple[int, set, list]:
    text = re.sub(r"(?s)<(script|style|noscript).*?</\1>", "", html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    emails = set(EMAIL_REGEX.findall(html))
    titles = [label for label, pat in CASCADE if pat.search(text)]
    return len(text), emails, titles


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=config.USER_AGENT,
                                        viewport={"width": 1280, "height": 900})
        for group, domains in SAMPLES.items():
            print(f"\n=== {group} ===")
            for d in domains:
                for path in PATHS:
                    url = f"https://{d}{path}"
                    page = await ctx.new_page()
                    try:
                        resp = await page.goto(url, timeout=20000, wait_until="domcontentloaded")
                        await page.wait_for_timeout(2500)  # let JS settle
                        html = await page.content()
                        n, emails, titles = summarize(html)
                        status = resp.status if resp else "?"
                        print(f"  {url:<55} HTTP {status}  text={n:>6,}  "
                              f"emails={len(emails)}  titles={titles}")
                        if emails:
                            print(f"      {sorted(emails)[:4]}")
                    except Exception as exc:
                        print(f"  {url:<55} FAILED: {type(exc).__name__}: {str(exc)[:80]}")
                    finally:
                        await page.close()
        await browser.close()


asyncio.run(main())
