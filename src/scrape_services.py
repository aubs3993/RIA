"""Politely fetch each firm's dedicated *services* pages to improve service
detection. Same politeness machinery as the email scraper (robots, 2s/domain,
retries, redirect handling) and it writes into the SAME scrape_cache, so
detect_services picks the new pages up with no further wiring.

We only fetch service-ish paths that aren't already cached, and stop after the
first couple of hits per firm to bound the run. Run, then re-run detect_services.
"""
from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urljoin, urlparse

import httpx
import pandas as pd
from selectolax.parser import HTMLParser
from tqdm.asyncio import tqdm as atqdm

import config
from src.utils import domain_of, get_logger, safe_filename
from src.scrape_websites import (
    _fetch_robots,
    _fetch_with_retries,
    _read_cache,
    _write_cache,
    _read_redirect_meta,
    _write_redirect_meta,
)

log = get_logger("scrape_services", config.LOG_DIR / "scrape.log")

# Common locations of a firm's services/offerings content (fallback guesses).
SERVICE_PATHS = [
    "/services", "/our-services", "/services-overview", "/what-we-do",
    "/wealth-management", "/solutions", "/how-we-help", "/our-process",
    "/planning", "/private-wealth",
]
# Link href/text that signals a services/offerings page.
SERVICE_LINK_RE = re.compile(
    r"service|what.?we.?do|solution|wealth.?manage|planning|offering|"
    r"expertise|approach|how.?we.?help|our.?firm|capabilit|advisory",
    re.I,
)
MAX_HITS_PER_FIRM = 2  # stop after this many successful service pages


def _candidate_paths(host: str, final_host: str | None) -> list[str]:
    """Real service-ish links pulled from the firm's already-cached homepage
    (tried FIRST — far higher hit rate), then a few guessed paths as fallback."""
    link_paths: list[str] = []
    seen: set[str] = set()
    roots = {h for h in (host, final_host) if h}
    for h in (final_host, host):
        if not h:
            continue
        html = _read_cache(h, "/")
        if not html:
            continue
        tree = HTMLParser(html)
        for a in tree.css("a[href]"):
            href = (a.attributes.get("href") or "").strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            text = a.text() or ""
            if not (SERVICE_LINK_RE.search(href) or SERVICE_LINK_RE.search(text)):
                continue
            absu = href if re.match(r"^https?://", href, re.I) else urljoin(f"https://{h}/", href)
            pu = urlparse(absu)
            ph = pu.netloc.lower().split(":")[0]
            if ph.startswith("www."):
                ph = ph[4:]
            if ph and ph not in roots:
                continue  # off-site link
            path = pu.path or "/"
            if path not in ("/", "") and path not in seen:
                seen.add(path)
                link_paths.append(path)
        break  # first homepage we find is enough
    # Real links first, then guessed fallbacks not already covered.
    return (link_paths + [p for p in SERVICE_PATHS if p not in seen])[:10]


def _has_cached_pages(website) -> bool:
    """True if we already have at least one cached page for this firm — i.e. its
    site was reachable in the email pass. Firms without any cache are skipped:
    link-following needs a homepage and their site was unreachable anyway."""
    host = domain_of(website) if isinstance(website, str) else None
    if not host:
        return False
    dirs = [config.SCRAPE_CACHE_DIR / safe_filename(host)]
    fin = _read_redirect_meta(host)
    if fin:
        dirs.append(config.SCRAPE_CACHE_DIR / safe_filename(fin))
    return any(d.is_dir() and any(d.glob("*.html")) for d in dirs)


async def _crawl_one(firm: dict, client: httpx.AsyncClient, sem: asyncio.Semaphore,
                     cfg: config.ScraperConfig) -> str:
    website = firm.get("website")
    host = domain_of(website) if isinstance(website, str) else None
    if not host:
        return "bad_url"

    async with sem:
        rp = await _fetch_robots(client, host)
        await asyncio.sleep(cfg.per_domain_delay_seconds)
        final_host = _read_redirect_meta(host)  # may already be known from the email pass
        rp_final = None
        hits = 0
        last_ts = 0.0

        for path in _candidate_paths(host, final_host):
            cache_host = final_host or host
            if _read_cache(cache_host, path) is not None:
                continue  # already have this page cached

            url_https = urljoin(f"https://{host}/", path)
            if not rp.can_fetch(config.USER_AGENT, url_https):
                continue
            if rp_final is not None and not rp_final.can_fetch(config.USER_AGENT, url_https):
                continue

            wait = cfg.per_domain_delay_seconds - (time.monotonic() - last_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            status, html, fu = await _fetch_with_retries(client, url_https, cfg)
            last_ts = time.monotonic()
            if status != 200 or not html:
                continue

            # Learn final host on first redirect and re-check its robots once.
            if fu:
                fh = domain_of(fu)
                if fh and fh != host and final_host is None:
                    final_host = fh
                    _write_redirect_meta(host, fh)
                    rp_final = await _fetch_robots(client, fh)
                    if not rp_final.can_fetch(config.USER_AGENT, fu):
                        continue

            _write_cache(final_host or host, path, html)
            hits += 1
            if hits >= MAX_HITS_PER_FIRM:
                break

        return "ok" if hits else "none"


async def _crawl_all(firms: pd.DataFrame, cfg: config.ScraperConfig) -> dict:
    sem = asyncio.Semaphore(cfg.max_concurrent_domains)
    timeout = httpx.Timeout(cfg.request_timeout_seconds, connect=cfg.request_timeout_seconds)
    headers = {"User-Agent": config.USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    limits = httpx.Limits(max_connections=cfg.max_concurrent_domains * 2,
                          max_keepalive_connections=cfg.max_concurrent_domains)
    counts = {"ok": 0, "none": 0, "bad_url": 0}

    async def _run_one(d: dict) -> str:
        try:
            return await _crawl_one(d, client, sem, cfg)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("services crawl error for %s: %s", d.get("website"), exc)
            return "none"

    async with httpx.AsyncClient(headers=headers, timeout=timeout, limits=limits, http2=False) as client:
        coros = [_run_one(row.to_dict()) for _, row in firms.iterrows()]
        for fut in atqdm.as_completed(coros, total=len(coros), desc="services"):
            outcome = await fut
            counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def scrape_services(limit: int | None = None,
                    scraper_cfg: config.ScraperConfig | None = None) -> dict:
    config.ensure_dirs()
    # Tuned for breadth: more domains in parallel (still 2s between requests to
    # the *same* domain, so per-site politeness is unchanged), fewer retries.
    scraper_cfg = scraper_cfg or config.ScraperConfig(
        max_concurrent_domains=20, max_retries=2, request_timeout_seconds=8.0
    )
    firms = pd.read_csv(config.TARGETED_CSV)
    if "match_score" in firms.columns:
        firms = firms.sort_values("match_score", ascending=False).reset_index(drop=True)
    # Only crawl firms whose site we already reached (cached homepage to mine
    # links from); firms with no cache were unreachable and stay blank.
    before = len(firms)
    firms = firms[firms["website"].map(_has_cached_pages)].reset_index(drop=True)
    if limit is not None:
        firms = firms.head(limit).copy()
    log.info("Services crawl over %d of %d firms (%d parallel, %.1fs/domain)",
             len(firms), before, scraper_cfg.max_concurrent_domains, scraper_cfg.per_domain_delay_seconds)

    counts = asyncio.run(_crawl_all(firms, scraper_cfg))
    print(f"\n[scrape_services] firms={len(firms):,}  "
          f"with service page(s)={counts.get('ok', 0):,}  "
          f"none found={counts.get('none', 0):,}  bad_url={counts.get('bad_url', 0):,}")
    return counts


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()
    scrape_services(limit=args.limit)
