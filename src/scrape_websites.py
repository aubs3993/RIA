"""Stage 4: politely scrape firm websites for advisor emails.

Constraints (all enforced):
- httpx.AsyncClient, max 10 domains in parallel
- One in-flight request per domain at a time
- 2-second delay between successive requests to the same domain
- robots.txt is checked once per domain; disallowed paths are skipped
- 10-second timeout, up to 3 retries with exponential backoff
- 4xx is fatal (no retry); 429/503 trigger longer back-off
- Pages are cached to data/raw/scrape_cache/<domain>/<path>.html
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx
import pandas as pd
from selectolax.parser import HTMLParser
from tqdm.asyncio import tqdm as atqdm

import config
from src.utils import domain_of, get_logger, safe_filename

log = get_logger("scrape_websites", config.SCRAPE_LOG)
# Quieten httpx's default per-request INFO chatter.
logging.getLogger("httpx").setLevel(logging.WARNING)

EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Local-part validity: standard chars only, no %, no whitespace, no leading punctuation.
_VALID_EMAIL_LOCAL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._+\-]*[A-Za-z0-9])?$")
# URL-encoded whitespace prefixes that show up in mailto: hrefs ("mailto:%20jane@…").
_URL_ESCAPE_WS = ("%20", "%09", "%0a", "%0d", "%0A", "%0D")
# Local-part prefixes/substrings indicating tracking pixels, image filenames, bounce traps.
_PIXEL_LOCAL_PREFIXES = ("img", "image", "track", "pixel", "wf-")
_PIXEL_LOCAL_SUBSTRINGS = ("+noreply", "+bounce")


@dataclass
class Contact:
    name: str | None
    title: str | None
    email: str
    source: str  # "scraped_mailto" | "scraped_text"


@dataclass
class FirmScrapeResult:
    crd_number: str | None
    domain: str | None                       # original filed host
    final_domain: str | None = None          # post-redirect host (if known)
    contacts: list[Contact] = field(default_factory=list)
    fetched_paths: list[str] = field(default_factory=list)
    skipped_reason: str | None = None
    # Possible skipped_reason values:
    #   "no_website", "bad_url", "social_url", "robots", "all_failed",
    #   "redirected_to_social", "shared_redirect_target", "error:<Type>"


# ---------- Email extraction ------------------------------------------------


def _domain_root(host: str) -> str:
    """Last two labels of the host (e.g. 'mail.smithcap.com' -> 'smithcap.com').

    Good enough for our purposes; we are not doing eTLD+1 with the public-suffix
    list. False positives on .co.uk-style two-part TLDs are rare in our dataset
    and only cause us to keep an extra email occasionally.
    """
    if not host:
        return ""
    parts = host.lower().split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host.lower()


def _email_domain_matches(email: str, firm_host: str) -> bool:
    if "@" not in email:
        return False
    email_host = email.rsplit("@", 1)[1].lower()
    if email_host in config.DEFAULT_SCRAPER.junk_email_domains:
        return False
    return _domain_root(email_host) == _domain_root(firm_host)


def _clean_email_candidate(raw: str) -> str:
    """Normalize a raw email string: strip whitespace, URL-encoded space prefixes,
    and stray leading/trailing punctuation that sneaks in from mailto: hrefs."""
    if not raw:
        return ""
    s = raw.strip().lower()
    changed = True
    while changed:
        changed = False
        for esc in _URL_ESCAPE_WS:
            esc_lower = esc.lower()
            if s.startswith(esc_lower):
                s = s[len(esc_lower):]
                changed = True
            if s.endswith(esc_lower):
                s = s[: -len(esc_lower)]
                changed = True
    s = s.lstrip(".:,;<>\"' \t")
    s = s.rstrip(".:,;<>\"' \t")
    return s


def _is_valid_email(email: str) -> bool:
    """Reject obviously malformed addresses, tracking-pixel locals, and bounces."""
    if not email or "@" not in email:
        return False
    local, _, host = email.partition("@")
    if not local or not host:
        return False
    if "." not in host:
        return False
    if not _VALID_EMAIL_LOCAL_RE.match(local):
        return False
    if any(local.startswith(p) for p in _PIXEL_LOCAL_PREFIXES):
        return False
    if any(s in local for s in _PIXEL_LOCAL_SUBSTRINGS):
        return False
    return True


def _is_junk_email(email: str, scraper_cfg: config.ScraperConfig) -> bool:
    local, _, host = email.lower().partition("@")
    if not host:
        return True
    if local in scraper_cfg.junk_email_local_parts:
        return True
    if host in scraper_cfg.junk_email_domains:
        return True
    # Trim very obvious tracker / asset paths shoved into mailto by mistake.
    if any(host.endswith(d) for d in scraper_cfg.junk_email_domains):
        return True
    return False


def _is_low_value_email(email: str, scraper_cfg: config.ScraperConfig) -> bool:
    local, _, _host = email.lower().partition("@")
    return local in scraper_cfg.low_value_local_parts


def _nearby_text(node) -> str:
    """Collect a few hundred chars of text near the node for name/title heuristics."""
    cur = node
    for _ in range(4):
        cur = cur.parent if cur is not None else None
        if cur is None:
            break
    if cur is None:
        cur = node
    text = cur.text(separator=" ", strip=True) if cur else ""
    return re.sub(r"\s+", " ", text)[:400]


_NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+(?:-[A-Z][a-z]+)?)\b")
_TITLE_KEYWORDS = (
    "advisor", "adviser", "wealth", "portfolio", "principal", "founder",
    "partner", "president", "ceo", "cco", "cfo", "cio", "manager",
    "director", "officer", "associate", "analyst", "planner", "vice",
    "chief", "managing", "senior",
)

# Tokens that show up in page chrome / address blocks and never in real names.
# Lowercase; checked against each token of a candidate name.
_NAME_NAV_STOPLIST = frozenset({
    "contact", "connect", "schedule", "quick", "links", "about", "home",
    "email", "phone", "office", "locations", "customer", "service",
    "portfolio", "manager", "old", "new", "north", "south", "east", "west",
    "hill", "drive", "road", "street", "avenue", "boulevard", "way", "lane",
    "court", "plaza", "square", "center", "suite",
})
_VOWEL_PAIR_RE = re.compile(r"[aeiouAEIOU]{2}")
# Words a real job-title is likely to contain (case-insensitive). If none
# appear, we drop the title rather than carry page-chrome noise.
_TITLE_VOCAB = frozenset({
    "advisor", "adviser", "officer", "manager", "director", "partner",
    "president", "principal", "vice", "chief", "senior", "founder", "head",
    "lead", "wealth", "investment", "financial", "portfolio", "compliance",
})


def _looks_like_person_name(s: str | None, firm_legal_name: str | None = None) -> bool:
    """True iff `s` plausibly looks like a real human name and not page chrome.

    Rules (must satisfy ALL):
      - 2 to 4 whitespace-separated tokens
      - every token starts with an uppercase letter
      - no token is in the nav/address stoplist
      - at least one token contains a vowel pair, OR the full string contains
        a comma followed by a space (suggests "Last, First" form)
      - the candidate is not a bare echo of the firm's own legal name
    """
    if not s:
        return False
    s = s.strip()
    tokens = s.split()
    if not (2 <= len(tokens) <= 4):
        return False
    if not all(t[:1].isalpha() and t[:1].isupper() for t in tokens):
        return False
    bare = [t.lower().strip(".,") for t in tokens]
    if any(b in _NAME_NAV_STOPLIST for b in bare):
        return False
    has_vowel_pair = any(_VOWEL_PAIR_RE.search(t) for t in tokens)
    has_comma_space = ", " in s
    if not (has_vowel_pair or has_comma_space):
        return False
    if firm_legal_name:
        firm_tokens = {t.lower().strip(".,&") for t in firm_legal_name.split() if len(t) > 2}
        if any(b in firm_tokens for b in bare):
            return False
    return True


def _looks_like_title(s: str | None) -> bool:
    """True iff `s` contains at least one common job-title word."""
    if not s:
        return False
    lower = s.lower()
    return any(w in lower for w in _TITLE_VOCAB)


def _guess_name_and_title(snippet: str, email: str) -> tuple[str | None, str | None]:
    if not snippet:
        return None, None

    # Try to derive a candidate name from the local part: "first.last@firm.com" → "First Last"
    local = email.split("@", 1)[0]
    local_tokens = re.split(r"[._\-]+", local)
    name_from_local = None
    if len(local_tokens) >= 2 and all(t.isalpha() and len(t) >= 2 for t in local_tokens[:2]):
        name_from_local = " ".join(t.title() for t in local_tokens[:2])

    # Best-effort name: first capitalized two-word run in the snippet, else from local
    name = None
    m = _NAME_RE.search(snippet)
    if m:
        name = m.group(1)
    elif name_from_local:
        name = name_from_local

    # Title: first sentence-ish chunk containing a known role keyword
    title = None
    lower_snip = snippet.lower()
    for kw in _TITLE_KEYWORDS:
        idx = lower_snip.find(kw)
        if idx == -1:
            continue
        start = max(0, idx - 30)
        end = min(len(snippet), idx + 60)
        chunk = snippet[start:end].strip(" .,;:|-")
        title = re.sub(r"\s+", " ", chunk)
        break
    return name, title


def extract_contacts_from_html(
    html: str,
    firm_host: str,
    source_label: str,
    firm_legal_name: str | None = None,
) -> list[Contact]:
    """Pull contacts out of one HTML page. Caller dedupes across pages.

    `firm_host` is the host the page actually came from (post-redirect).
    Emails on any other registrable domain are rejected.
    `firm_legal_name` is used to reject candidate names that are echoes of
    the firm's own legal name (e.g. "Ironvine Capital").
    """
    if not html:
        return []
    tree = HTMLParser(html)
    found: dict[str, Contact] = {}

    # 1) mailto links — highest confidence
    for a in tree.css('a[href^="mailto:"], a[href^="MAILTO:"]'):
        href = (a.attributes.get("href") or "").strip()
        # Strip mailto: prefix and any ?subject=... params, then clean.
        raw_addr = href.split(":", 1)[1].split("?", 1)[0]
        email = _clean_email_candidate(raw_addr)
        if not _is_valid_email(email):
            continue
        if _is_junk_email(email, config.DEFAULT_SCRAPER):
            continue
        if not _email_domain_matches(email, firm_host):
            continue
        snippet = _nearby_text(a)
        name, title = _guess_name_and_title(snippet, email)
        # Anchor's own text is often the person's name
        anchor_text = (a.text(strip=True) or "").strip()
        if anchor_text and "@" not in anchor_text and not name:
            name = anchor_text
        if not _looks_like_person_name(name, firm_legal_name):
            name = None
        if not _looks_like_title(title):
            title = None
        found[email] = Contact(name=name, title=title, email=email, source="scraped_mailto")

    # 2) visible-text regex sweep (skips emails already captured)
    body_text = tree.body.text(separator=" ", strip=True) if tree.body else tree.text(separator=" ", strip=True)
    for raw in EMAIL_REGEX.findall(body_text or ""):
        email = _clean_email_candidate(raw)
        if not _is_valid_email(email):
            continue
        if email in found:
            continue
        if _is_junk_email(email, config.DEFAULT_SCRAPER):
            continue
        if not _email_domain_matches(email, firm_host):
            continue
        # Heuristic: search for the email substring in the page and grab nearby text.
        idx = body_text.lower().find(email)
        snippet = body_text[max(0, idx - 200): idx + 200] if idx >= 0 else ""
        name, title = _guess_name_and_title(snippet, email)
        if not _looks_like_person_name(name, firm_legal_name):
            name = None
        if not _looks_like_title(title):
            title = None
        found[email] = Contact(name=name, title=title, email=email, source="scraped_text")

    # Drop low-value emails (info@, support@, ...) if we have any other email.
    has_personal = any(not _is_low_value_email(c.email, config.DEFAULT_SCRAPER) for c in found.values())
    if has_personal:
        found = {e: c for e, c in found.items() if not _is_low_value_email(c.email, config.DEFAULT_SCRAPER)}

    if source_label and found:
        log.debug("Extracted %d emails from %s", len(found), source_label)

    return list(found.values())


# ---------- Robots, caching, fetching ---------------------------------------


async def _fetch_robots(client: httpx.AsyncClient, domain: str) -> urllib.robotparser.RobotFileParser:
    """Fetch and parse robots.txt. Tries https then http, following redirects.

    - 200 → parse and use those rules.
    - 401/403 on **both** schemes → conservative Disallow: / (auth-walled).
    - 404 / 5xx / transport error / non-2xx with no other scheme succeeding →
      treat as no robots restrictions (allow all). We still call rp.parse([])
      so that last_checked is set; otherwise can_fetch() returns False for
      every path on an unparsed RobotFileParser.
    """
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(f"http://{domain}/robots.txt")
    saw_auth_block = False
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}/robots.txt"
        try:
            resp = await client.get(url, timeout=8.0, follow_redirects=True)
        except Exception as exc:
            log.debug("robots fetch failed (%s): %s", url, exc)
            continue
        sc = resp.status_code
        if sc == 200:
            try:
                rp.parse(resp.text.splitlines())
            except Exception as exc:
                log.warning("robots parse failed for %s: %s", domain, exc)
                rp.parse([])
            return rp
        if sc in (401, 403):
            saw_auth_block = True
            continue
        # 404, 5xx, or any other non-2xx → fall through to next scheme.
    if saw_auth_block:
        rp.parse(["User-agent: *", "Disallow: /"])
    else:
        rp.parse([])
    return rp


def _cache_path_for(domain: str, path: str) -> Path:
    safe_path = safe_filename(path) or "_root"
    return config.SCRAPE_CACHE_DIR / safe_filename(domain) / f"{safe_path}.html"


def _read_cache(domain: str, path: str) -> str | None:
    p = _cache_path_for(domain, path)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _write_cache(domain: str, path: str, html: str) -> None:
    p = _cache_path_for(domain, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(html, encoding="utf-8", errors="replace")
    except Exception as exc:
        log.debug("cache write failed (%s%s): %s", domain, path, exc)


def _redirect_meta_path(original_host: str) -> Path:
    """Sidecar JSON mapping original filed-host → final host after redirects."""
    return config.SCRAPE_CACHE_DIR / safe_filename(original_host) / "_redirect.json"


def _read_redirect_meta(original_host: str) -> str | None:
    """Return the cached final_host for this original_host, or None if unknown."""
    p = _redirect_meta_path(original_host)
    if not p.exists():
        return None
    try:
        import json

        return json.loads(p.read_text(encoding="utf-8")).get("final_host")
    except Exception:
        return None


def _write_redirect_meta(original_host: str, final_host: str) -> None:
    p = _redirect_meta_path(original_host)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        import json

        p.write_text(
            json.dumps({"original_host": original_host, "final_host": final_host}),
            encoding="utf-8",
        )
    except Exception as exc:
        log.debug("redirect meta write failed (%s -> %s): %s", original_host, final_host, exc)


async def _fetch_with_retries(
    client: httpx.AsyncClient,
    url: str,
    cfg: config.ScraperConfig,
) -> tuple[int | None, str | None, str | None]:
    """Return (status, body, final_url).

    `final_url` is the URL httpx ended up on after following redirects (None
    if every attempt failed). Lets the caller key the cache by post-redirect
    host and validate emails against where the content actually came from.
    """
    for attempt in range(cfg.max_retries):
        try:
            resp = await client.get(url, timeout=cfg.request_timeout_seconds, follow_redirects=True)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            wait = cfg.backoff_base_seconds * (2**attempt)
            log.debug("transport error %s on %s (attempt %d/%d) — waiting %.1fs",
                      type(exc).__name__, url, attempt + 1, cfg.max_retries, wait)
            await asyncio.sleep(wait)
            continue

        sc = resp.status_code
        final_url = str(resp.url)
        if sc == 200:
            return sc, resp.text, final_url
        if 400 <= sc < 500 and sc not in (408, 429):
            return sc, None, final_url  # don't retry plain 4xx
        if sc in (429, 503):
            wait = cfg.backoff_base_seconds * (2 ** (attempt + 1))
            log.info("rate-limit %s on %s — backing off %.1fs", sc, url, wait)
            await asyncio.sleep(wait)
            continue
        # Other 5xx: retry with normal backoff
        wait = cfg.backoff_base_seconds * (2**attempt)
        await asyncio.sleep(wait)
    return None, None, None


# ---------- Per-firm scraping -----------------------------------------------


async def _scrape_one_firm(
    firm: dict,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    cfg: config.ScraperConfig,
    redirect_owner: dict | None = None,
) -> FirmScrapeResult:
    """Scrape one firm.

    `redirect_owner` is a shared dict {host: crd_number_of_first_owner} keyed
    by both filed and post-redirect hosts. It ensures that when two firms file
    the identical website, or their filed sites redirect to the same
    destination, only the first firm gets the contacts. Subsequent firms are
    skipped with reason "shared_redirect_target". Pass None when scraping
    standalone (e.g. tests).
    """
    website = firm.get("website")
    # Generic entity key: RIA firms carry crd_number; FDIC banks carry
    # cert_number. crd stays first so RIA behaviour is unchanged.
    crd = firm.get("crd_number") or firm.get("cert_number")
    result = FirmScrapeResult(crd_number=crd, domain=None)
    social_blocklist = {d.lower() for d in getattr(config, "SOCIAL_URL_BLOCKLIST", [])}

    if not website:
        result.skipped_reason = "no_website"
        return result

    host = domain_of(website)
    if not host:
        result.skipped_reason = "bad_url"
        return result
    result.domain = host

    if host in social_blocklist:
        log.info("social URL %s — skipping", host)
        result.skipped_reason = "social_url"
        return result

    # If we already learned this filed host redirects to a known final host,
    # short-circuit the social/shared checks before doing any HTTP work.
    cached_final = _read_redirect_meta(host) if cfg.cache_pages else None
    if cached_final and cached_final in social_blocklist:
        log.info("cached redirect %s -> social %s — skipping", host, cached_final)
        result.final_domain = cached_final
        result.skipped_reason = "redirected_to_social"
        return result
    if cached_final and redirect_owner is not None:
        owner = redirect_owner.setdefault(cached_final, crd)
        if owner != crd:
            log.warning("shared redirect target %s already owned by CRD %s — skipping CRD %s",
                        cached_final, owner, crd)
            result.final_domain = cached_final
            result.skipped_reason = "shared_redirect_target"
            return result

    # Claim the FILED host up front. Two firms that file the identical website
    # (common for affiliated RIAs) must not both scrape it and both receive the
    # same contacts — the first claimant wins and later firms are skipped before
    # any cache read or network fetch (which also keeps requests to that domain
    # sequential rather than concurrent across firms).
    if redirect_owner is not None:
        owner = redirect_owner.setdefault(host, crd)
        if owner != crd:
            log.warning("filed host %s already owned by CRD %s — skipping CRD %s",
                        host, owner, crd)
            result.skipped_reason = "shared_redirect_target"
            return result

    async with sem:
        # robots.txt for the FILED host (we'll re-check after we know the final host).
        rp = await _fetch_robots(client, host)
        await asyncio.sleep(cfg.per_domain_delay_seconds)

        contacts: dict[str, Contact] = {}
        last_request_ts = 0.0
        robots_blocked = 0
        attempted = 0
        # Seed the final host from the redirect sidecar so cache lookups hit the
        # post-redirect directory immediately instead of re-fetching every run.
        final_host: str | None = cached_final
        if final_host:
            result.final_domain = final_host
        rp_final = None  # robots parser for the final host, if different

        for path in cfg.team_paths:
            url = urljoin(f"http://{host}/", path)
            url_https = url.replace("http://", "https://", 1)
            attempted += 1

            # ROBOTS_UA (not the full Mozilla-style USER_AGENT): robotparser
            # matches on the token before "/", so the full string would apply
            # rules aimed at "mozilla" instead of ours.
            if not rp.can_fetch(config.ROBOTS_UA, url_https):
                log.info("robots disallow %s%s — skipping", host, path)
                robots_blocked += 1
                continue
            # Once a redirect is known, the FINAL host's robots apply to every
            # subsequent page too (mirrors scrape_primary_contact).
            if rp_final is not None and not rp_final.can_fetch(config.ROBOTS_UA, url_https):
                log.info("robots (final host %s) disallow %s — skipping", final_host, path)
                robots_blocked += 1
                continue

            # Cache key uses post-redirect host. On first fetch we don't know it
            # yet, so we have to do the live fetch; subsequent fetches reuse the
            # learned final_host.
            cache_host = final_host or host
            cached = _read_cache(cache_host, path) if cfg.cache_pages else None
            if cached is not None:
                html = cached
                log.debug("cache hit %s%s", cache_host, path)
            else:
                wait = cfg.per_domain_delay_seconds - (time.monotonic() - last_request_ts)
                if wait > 0:
                    await asyncio.sleep(wait)
                status, html, fu = await _fetch_with_retries(client, url_https, cfg)
                if status is None:
                    # No TLS listener / broken cert — some small firms are still
                    # http-only. Fall back to plain http, mirroring _fetch_robots'
                    # https-then-http scheme fallback.
                    status, html, fu = await _fetch_with_retries(client, url, cfg)
                last_request_ts = time.monotonic()
                if status is None:
                    log.warning("fetch failed (transport) %s%s", host, path)
                    continue
                if status != 200 or not html:
                    log.debug("non-200 %s on %s%s", status, host, path)
                    continue

                # Learn the final host on this first successful fetch.
                if final_host is None and fu:
                    fh = domain_of(fu)
                    if fh and fh != host:
                        log.info("redirect %s -> %s (final url for path %s)", host, fh, path)
                        final_host = fh
                        result.final_domain = fh
                        if cfg.cache_pages:
                            _write_redirect_meta(host, fh)

                        # Post-redirect social check
                        if fh in social_blocklist:
                            log.info("filed site %s redirects to social %s — skipping", host, fh)
                            result.skipped_reason = "redirected_to_social"
                            return result

                        # Shared-target check: another firm in this run already owns this final_host
                        if redirect_owner is not None:
                            owner = redirect_owner.setdefault(fh, crd)
                            if owner != crd:
                                log.warning(
                                    "shared redirect target %s already owned by CRD %s — skipping CRD %s",
                                    fh, owner, crd,
                                )
                                result.skipped_reason = "shared_redirect_target"
                                return result

                        # Re-check robots on the FINAL host before trusting the page.
                        rp_final = await _fetch_robots(client, fh)
                        if not rp_final.can_fetch(config.ROBOTS_UA, fu):
                            log.info("robots (post-redirect) disallow %s — skipping page", fu)
                            continue
                    elif fh:
                        final_host = fh
                        result.final_domain = fh
                        if redirect_owner is not None:
                            redirect_owner.setdefault(fh, crd)
                if cfg.cache_pages:
                    _write_cache(final_host or host, path, html)

            result.fetched_paths.append(path)
            page_host = final_host or host  # validate emails against where the bytes came from
            page_contacts = extract_contacts_from_html(
                html,
                page_host,
                source_label=f"{page_host}{path}",
                firm_legal_name=firm.get("firm_legal_name"),
            )
            for c in page_contacts:
                existing = contacts.get(c.email)
                if existing is None or (existing.source == "scraped_text" and c.source == "scraped_mailto"):
                    contacts[c.email] = c

            if contacts:
                break

        result.contacts = list(contacts.values())
        if not result.contacts and not result.fetched_paths:
            if robots_blocked == attempted and attempted > 0:
                result.skipped_reason = "robots"
            else:
                result.skipped_reason = "all_failed"

    return result


# ---------- Public entry point ----------------------------------------------


def _firm_key(d: dict) -> str:
    """Stable per-firm result key: first entity id present (RIA crd_number,
    FDIC cert_number, NCUA cu_number, then sec_number), else the legal name.
    Single source of truth — scrape_websites attaches it as _key and
    _flatten_results joins results back on it."""
    return str(d.get("crd_number") or d.get("cert_number") or d.get("cu_number")
               or d.get("sec_number") or d.get("firm_legal_name") or id(d))


async def _scrape_all(
    firms: pd.DataFrame,
    cfg: config.ScraperConfig,
) -> dict[str, FirmScrapeResult]:
    sem = asyncio.Semaphore(cfg.max_concurrent_domains)
    timeout = httpx.Timeout(cfg.request_timeout_seconds, connect=cfg.request_timeout_seconds)
    headers = {"User-Agent": config.USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    limits = httpx.Limits(max_connections=cfg.max_concurrent_domains * 2,
                          max_keepalive_connections=cfg.max_concurrent_domains)

    results: dict[str, FirmScrapeResult] = {}
    # filed or final host -> CRD of first firm to claim it. Subsequent firms
    # that file the same site, or whose filed site redirects to the same
    # destination, get skipped to prevent contact duplication across firm rows.
    redirect_owner: dict[str, str] = {}

    async def _run_one(key: str, firm_dict: dict) -> tuple[str, FirmScrapeResult]:
        # Wrap the per-firm coroutine so each result carries its own key.
        # This is critical: as_completed yields in completion order, so we
        # MUST NOT correlate results to keys by submission order.
        try:
            res = await _scrape_one_firm(firm_dict, client, sem, cfg, redirect_owner=redirect_owner)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("unexpected error scraping %s: %s", key, exc)
            _id = firm_dict.get("crd_number") or firm_dict.get("cert_number")
            res = FirmScrapeResult(
                crd_number=str(_id) if _id else None,
                domain=None,
                skipped_reason=f"error:{type(exc).__name__}",
            )
        return key, res

    async with httpx.AsyncClient(headers=headers, timeout=timeout, limits=limits, http2=False) as client:
        coros = []
        for _, row in firms.iterrows():
            d = row.to_dict()
            # Reuse the _key scrape_websites attached; derive only when absent
            # (e.g. tests calling _scrape_all directly).
            key = d.get("_key") or _firm_key(d)
            coros.append(_run_one(key, d))

        for fut in atqdm.as_completed(coros, total=len(coros), desc="scraping"):
            key, res = await fut
            results[key] = res
    return results


def scrape_websites(
    targeted_csv: Path | None = None,
    output_path: Path | None = None,
    limit: int | None = None,
    scraper_cfg: config.ScraperConfig = config.DEFAULT_SCRAPER,
) -> Path:
    """Read targeted firms, scrape, write enriched CSV. Returns output path."""
    config.ensure_dirs()
    targeted_csv = targeted_csv or config.TARGETED_CSV
    if not targeted_csv.exists():
        raise FileNotFoundError(f"Missing input: {targeted_csv}. Run filter_firms first.")

    firms = pd.read_csv(targeted_csv)
    # --limit always slices the highest-scoring firms, regardless of CSV order.
    if "match_score" in firms.columns:
        firms = firms.sort_values("match_score", ascending=False).reset_index(drop=True)
    if limit is not None:
        firms = firms.head(limit).copy()
    log.info("Scraping %d firms (cfg: %d parallel domains, %.1fs delay)",
             len(firms), scraper_cfg.max_concurrent_domains, scraper_cfg.per_domain_delay_seconds)

    # Attach each firm's result key; _scrape_all reads it back off the row.
    firms = firms.assign(_key=[_firm_key(row.to_dict()) for _, row in firms.iterrows()])

    results = asyncio.run(_scrape_all(firms, scraper_cfg))

    rows = _flatten_results(firms, results)
    out_df = _order_output_columns(pd.DataFrame(rows))

    output_path = output_path or (config.ENRICHED_DIR / f"ria_targets_{datetime.now():%Y%m%d}.csv")
    out_df.to_csv(output_path, index=False)
    _print_scrape_summary(firms, results, out_df, output_path)
    return output_path


CONTACT_COLUMNS = ["contact_name", "contact_title", "contact_email", "email_source"]


def _order_output_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Place the contact columns immediately after crd_number, keeping every
    other (firm) column in its natural order. So the wide sheet reads:
    firm identity → contact → firm detail. Drops the internal _key if present.
    """
    cols = [c for c in df.columns if c != "_key"]
    contact = [c for c in CONTACT_COLUMNS if c in cols]
    rest = [c for c in cols if c not in contact]
    if "crd_number" in rest:
        i = rest.index("crd_number") + 1
        ordered = rest[:i] + contact + rest[i:]
    else:
        ordered = contact + rest
    return df[ordered]


def _missing_email_status(res: FirmScrapeResult | None) -> str:
    """Status code for a firm that produced no contact.

    Recorded in the `email_source` column so the single combined output says
    *why* an email is absent rather than just leaving it blank. The values
    line up with the scraper's own skip reasons; the extra "no_email_found"
    covers the case where pages were fetched fine but held no usable address,
    and "not_scraped" covers a firm that never had a result (e.g. excluded by
    --limit).
    """
    if res is None:
        return "not_scraped"
    if res.skipped_reason:
        return res.skipped_reason  # no_website | social_url | robots | all_failed | ...
    # Pages were fetched OK, but no usable email was on any of them.
    return "no_email_found"


def _flatten_results(firms: pd.DataFrame, results: dict[str, FirmScrapeResult]) -> list[dict]:
    """One row per (firm, contact). Every targeted firm appears at least once.

    Firms with at least one scraped contact get one row per unique email.
    Firms with no contact get a single row with blank contact_* fields and an
    `email_source` status code explaining why (e.g. "no_email_found",
    "robots", "all_failed"). The contact_email cell is left *empty* rather than
    a sentinel string so CRM/sequencer imports treat it correctly as "no email"
    — the reason lives in email_source instead.

    Every firm column present on the input (all of firms_targeted) is carried
    through unchanged, so the result is one wide, self-contained sheet: full
    firm detail + contact, no re-join needed. The internal _key is dropped.
    """
    out_rows: list[dict] = []

    def _emit(row, *, contact_name, contact_title, contact_email, email_source):
        firm_cols = {k: v for k, v in row.items() if k != "_key"}
        firm_cols.update(
            contact_name=contact_name,
            contact_title=contact_title,
            contact_email=contact_email,
            email_source=email_source,
        )
        out_rows.append(firm_cols)

    for _, row in firms.iterrows():
        key = row["_key"]
        res = results.get(key)

        emitted = 0
        if res and res.contacts:
            seen = set()
            for c in res.contacts:
                email = (c.email or "").lower()
                if not email or email in seen:  # dedupe by email within firm
                    continue
                seen.add(email)
                _emit(
                    row,
                    contact_name=c.name,
                    contact_title=c.title,
                    contact_email=c.email,
                    email_source=c.source,
                )
                emitted += 1

        if emitted == 0:
            # Keep the firm with a blank email + a reason in email_source.
            _emit(
                row,
                contact_name=None,
                contact_title=None,
                contact_email=None,
                email_source=_missing_email_status(res),
            )
    return out_rows


def _print_scrape_summary(
    firms: pd.DataFrame,
    results: dict[str, FirmScrapeResult],
    out_df: pd.DataFrame,
    output_path: Path,
) -> None:
    n_firms = len(firms)
    n_with_contacts = sum(1 for r in results.values() if r.contacts)

    # Bucket firms by their skipped_reason (None = succeeded with at least one
    # fetched page, regardless of whether contacts were found).
    bucket_labels = {
        "no_website":              "no website on file",
        "bad_url":                 "malformed website URL",
        "social_url":              "social-media URL (skipped)",
        "redirected_to_social":    "filed site redirects to social",
        "shared_redirect_target":  "filed site shares redirect target",
        "robots":                  "all paths blocked by robots.txt",
        "all_failed":              "all fetches failed (timeout/4xx/5xx)",
    }
    buckets: dict[str, int] = {k: 0 for k in bucket_labels}
    error_buckets: dict[str, int] = {}
    for r in results.values():
        reason = r.skipped_reason
        if reason is None:
            continue
        if reason.startswith("error:"):
            error_buckets[reason] = error_buckets.get(reason, 0) + 1
        elif reason in buckets:
            buckets[reason] += 1
        else:
            error_buckets[reason] = error_buckets.get(reason, 0) + 1

    # Only rows that carry a real address count as "contacts"; the rest are
    # firm rows kept with a blank email and a reason in email_source.
    if not out_df.empty:
        email_rows = out_df[out_df["contact_email"].notna() & (out_df["contact_email"] != "")]
    else:
        email_rows = out_df
    by_source = email_rows["email_source"].value_counts().to_dict() if not email_rows.empty else {}

    print("\n[scrape_websites] summary")
    print(f"  firms attempted:        {n_firms:,}")
    print(f"  firms with >=1 contact: {n_with_contacts:,}")
    print(f"  firms w/o any contact:  {n_firms - n_with_contacts:,}")
    print(f"  contact rows (w/ email):{len(email_rows):>6,}")
    print(f"  total output rows:      {len(out_df):,}")
    print("  skip-reason breakdown:")
    for key, label in bucket_labels.items():
        print(f"    {label:<35} {buckets[key]:>5}")
    for reason, n in error_buckets.items():
        print(f"    {reason:<35} {n:>5}")
    if by_source:
        print("  contacts by source:")
        for src, n in by_source.items():
            print(f"    {src:<35} {n:>5}")
    print(f"  → {output_path}")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()
    scrape_websites(limit=args.limit)
