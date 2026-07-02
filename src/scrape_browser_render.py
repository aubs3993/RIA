"""Stage 6b: headless-browser pass for firms the static scraper came up empty on.

The httpx scraper (stages 5/6) fails on two kinds of sites: JS-rendered
single-page apps (the HTML is an empty shell) and bot-blocked sites (every
request 403s or times out). A real Chromium fixes both. This stage:

  1. Selects firms from the master with no primary contact at all, whose
     cache is missing, empty, or a JS shell (< MIN_TEXT chars of visible
     text). --all-none widens it to every no-contact firm.
  2. Renders each firm's leadership paths in headless Chromium and writes
     the rendered HTML into data/raw/scrape_cache — the same cache stages
     5/6 read — recording redirects in the usual _redirect.json sidecar.
     Robots.txt is honored when it is reachable; pages stop early once a
     named CFO is rendered (nothing outranks one).
  3. Re-runs the primary-contact cascade cache-only, which merges the new
     contacts into the master CSV + xlsx with its usual checkpointing.

Progress is checkpointed per domain in data/enriched/browser_render_done.txt;
rerunning after a crash resumes where it left off. The done-file is removed
after a fully successful render pass.

Usage:
    python -m src.scrape_browser_render                  # render + rescan + export
    python -m src.scrape_browser_render --limit 10       # first 10 target firms
    python -m src.scrape_browser_render --all-none       # include firms with static-HTML caches
    python -m src.scrape_browser_render --no-rescan      # render only, skip the cascade re-run
    python -m src.scrape_browser_render --restart        # ignore the done-file and start over
"""
from __future__ import annotations

import asyncio
import re
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

import config
from src.utils import domain_of, get_logger, safe_filename
from src.scrape_primary_contact import (
    CASCADE,
    LEADERSHIP_PATHS,
    _best,
    _done_searching,
    candidates_from_html,
)
from src.scrape_websites import (
    _read_cache,
    _read_redirect_meta,
    _write_cache,
    _write_redirect_meta,
)

log = get_logger("scrape_browser_render", config.SCRAPE_LOG)

DONE_FILE = config.ENRICHED_DIR / "browser_render_done.txt"

MIN_TEXT = 800           # rendered/cached text below this ≈ JS shell
SETTLE_MS = 2500         # post-load wait for JS to populate the DOM
PAGE_TIMEOUT_MS = 20000
CONCURRENT_PAGES = 5     # Chromium tabs in flight at once

_TAG_STRIP = re.compile(r"(?s)<(script|style|noscript).*?</\1>")
_HTML_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _visible_text(html: str) -> str:
    return _WS.sub(" ", _HTML_TAGS.sub(" ", _TAG_STRIP.sub("", html)))


@dataclass
class RenderStats:
    firms: int = 0
    pages_rendered: int = 0
    pages_failed: int = 0
    dead_domains: list = field(default_factory=list)


# ---------- Target selection -----------------------------------------------------


def _cache_state(host: str | None) -> str:
    """'no_cache' | 'shell' | 'has_content' for a firm's cached pages."""
    if not host:
        return "no_cache"
    hosts = [host]
    final = _read_redirect_meta(host)
    if final and final != host:
        hosts.append(final)
    best_len = -1
    for h in hosts:
        d = config.SCRAPE_CACHE_DIR / safe_filename(h)
        if not d.is_dir():
            continue
        for f in d.glob("*.html"):
            try:
                best_len = max(best_len, len(_visible_text(
                    f.read_text(encoding="utf-8", errors="replace"))))
            except Exception:
                continue
    if best_len < 0:
        return "no_cache"
    return "shell" if best_len < MIN_TEXT else "has_content"


def select_targets(master: pd.DataFrame, all_none: bool = False,
                   limit: int | None = None) -> list[dict]:
    """No-contact firms worth a browser visit, one entry per domain."""
    firms = master.drop_duplicates("crd_number")
    none = firms[firms["primary_contact_name"].isna()
                 & firms["primary_contact_email"].isna()]
    social = {d.lower() for d in config.SOCIAL_URL_BLOCKLIST}
    targets, seen = [], set()
    for _, row in none.iterrows():
        host = domain_of(row["website"]) if isinstance(row["website"], str) else None
        if not host or host in social or host in seen:
            continue
        state = _cache_state(host)
        if state == "has_content" and not all_none:
            continue
        seen.add(host)
        targets.append({"crd_number": row["crd_number"], "host": host,
                        "firm_legal_name": row.get("firm_legal_name"),
                        "cache_state": state})
        if limit is not None and len(targets) >= limit:
            break
    return targets


# ---------- Rendering -------------------------------------------------------------


async def _robots_for(ctx, host: str) -> urllib.robotparser.RobotFileParser:
    """Robots via the browser's network stack; unreachable → allow all.

    Unlike the httpx scraper we do NOT treat 403 as Disallow-everything:
    these domains bot-block plain clients, which is exactly why we're here.
    An explicit, reachable robots.txt is still honored.
    """
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(f"https://{host}/robots.txt")
    try:
        resp = await ctx.request.get(f"https://{host}/robots.txt",
                                     timeout=PAGE_TIMEOUT_MS)
        if resp.status == 200:
            rp.parse((await resp.text()).splitlines())
        else:
            rp.parse([])
    except Exception:
        rp.parse([])
    return rp


async def _render_firm(ctx, firm: dict, sem: asyncio.Semaphore,
                       stats: RenderStats) -> None:
    host = firm["host"]
    social = {d.lower() for d in config.SOCIAL_URL_BLOCKLIST}
    async with sem:
        rp = await _robots_for(ctx, host)
        final_host = _read_redirect_meta(host)
        rp_final = None  # robots parser for the redirect-target host, if different
        if final_host and final_host != host:
            # Redirect target already learned (e.g. by the httpx pass) — honor
            # its robots for every page we render/cache under it.
            rp_final = await _robots_for(ctx, final_host)
        candidates: list = []
        paths = ["/"] + [p for p in LEADERSHIP_PATHS if p != "/"]
        for path in paths:
            cache_host = final_host or host
            cached = _read_cache(cache_host, path)
            if cached is not None and len(_visible_text(cached)) >= MIN_TEXT:
                candidates.extend(candidates_from_html(
                    cached, cache_host, path, firm["firm_legal_name"]))
                if _done_searching(_best(candidates)):
                    break
                continue
            url = f"https://{host}{path}"
            # ROBOTS_UA, not USER_AGENT: robotparser matches the token before
            # "/", so the full string would check as "mozilla".
            if not rp.can_fetch(config.ROBOTS_UA, url):
                continue
            if rp_final is not None and not rp_final.can_fetch(config.ROBOTS_UA, url):
                continue
            page = await ctx.new_page()
            try:
                resp = await page.goto(url, timeout=PAGE_TIMEOUT_MS,
                                       wait_until="domcontentloaded")
                if resp is None or resp.status != 200:
                    if path == "/" and (resp is None or resp.status >= 500):
                        stats.dead_domains.append(host)
                        return
                    continue
                # nudge lazy-loaded team grids, then let JS settle
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(SETTLE_MS)
                html = await page.content()
                fh = domain_of(page.url)
                if fh and fh != host and fh != final_host:
                    final_host = fh
                    _write_redirect_meta(host, fh)
                    if fh in social:
                        return
                    # Re-check robots on the redirect-target host before caching
                    # anything from it (mirrors rp_final in the httpx scrapers).
                    rp_final = await _robots_for(ctx, fh)
                    if not rp_final.can_fetch(config.ROBOTS_UA, page.url):
                        log.info("robots (post-redirect) disallow %s — skipping page", page.url)
                        continue
                _write_cache(final_host or host, path, html)
                stats.pages_rendered += 1
                candidates.extend(candidates_from_html(
                    html, final_host or host, path, firm["firm_legal_name"]))
                if _done_searching(_best(candidates)):
                    break
            except Exception as exc:
                stats.pages_failed += 1
                log.debug("render failed %s: %s: %s", url, type(exc).__name__, exc)
                if path == "/":
                    stats.dead_domains.append(host)
                    return
            finally:
                await page.close()


async def _render_all(targets: list[dict], on_done) -> RenderStats:
    from playwright.async_api import async_playwright
    from tqdm import tqdm

    stats = RenderStats()
    sem = asyncio.Semaphore(CONCURRENT_PAGES)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=config.USER_AGENT,
                                        viewport={"width": 1280, "height": 900})

        async def _one(f: dict) -> None:
            try:
                await _render_firm(ctx, f, sem, stats)
            except Exception as exc:  # defensive: one firm never kills the run
                log.exception("browser pass failed for %s: %s", f["host"], exc)
            stats.firms += 1
            on_done(f["host"])

        tasks = [asyncio.ensure_future(_one(f)) for f in targets]
        with tqdm(total=len(tasks), desc="browser-render") as bar:
            for fut in asyncio.as_completed(tasks):
                await fut
                bar.update(1)
        await browser.close()
    return stats


# ---------- Entry point ------------------------------------------------------------


def run(master_path: Path | None = None, limit: int | None = None,
        all_none: bool = False, rescan: bool = True, export_xlsx: bool = True,
        resume: bool = True) -> None:
    config.ensure_dirs()
    master_path = Path(master_path) if master_path else config.latest_ria_master()
    master = pd.read_csv(master_path, low_memory=False)

    targets = select_targets(master, all_none=all_none, limit=limit)

    done: set[str] = set()
    if DONE_FILE.exists():
        if resume:
            done = {l.strip() for l in DONE_FILE.read_text(encoding="utf-8").splitlines()
                    if l.strip()}
            before = len(targets)
            targets = [t for t in targets if t["host"] not in done]
            print(f"Resuming: {before - len(targets):,} domains already rendered, "
                  f"{len(targets):,} remaining")
        else:
            DONE_FILE.unlink()

    log.info("Browser render over %d domains (all_none=%s)", len(targets), all_none)
    print(f"Rendering {len(targets):,} domains in headless Chromium "
          f"({CONCURRENT_PAGES} tabs)")

    def _mark_done(host: str) -> None:
        with DONE_FILE.open("a", encoding="utf-8") as fh:
            fh.write(host + "\n")

    stats = asyncio.run(_render_all(targets, _mark_done))

    print(f"\n=== Browser render ===")
    print(f"  domains visited:   {stats.firms:,}")
    print(f"  pages rendered:    {stats.pages_rendered:,}")
    print(f"  pages failed:      {stats.pages_failed:,}")
    print(f"  dead domains:      {len(stats.dead_domains):,}")
    if stats.dead_domains:
        log.info("dead domains: %s", ", ".join(sorted(set(stats.dead_domains))))

    if DONE_FILE.exists():
        DONE_FILE.unlink()

    if rescan:
        print("\nRe-running the primary-contact cascade over the cache...")
        from src.scrape_primary_contact import run as primary_run
        primary_run(master_path=master_path, cache_only=True,
                    export_xlsx=export_xlsx, resume=False)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        description="Headless-browser pass for firms with no primary contact")
    p.add_argument("--master", type=Path, default=None, help="path to the master CSV")
    p.add_argument("--limit", type=int, default=None, help="only the first N target firms")
    p.add_argument("--all-none", action="store_true",
                   help="also rerender firms whose static cache had content")
    p.add_argument("--no-rescan", action="store_true",
                   help="render only; skip the cascade re-run / master update")
    p.add_argument("--no-excel", action="store_true", help="skip re-exporting the xlsx")
    p.add_argument("--restart", action="store_true",
                   help="ignore the done-file and render everything again")
    args = p.parse_args()
    run(master_path=args.master, limit=args.limit, all_none=args.all_none,
        rescan=not args.no_rescan, export_xlsx=not args.no_excel,
        resume=not args.restart)
