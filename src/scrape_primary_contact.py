"""Stage 6: find each firm's best finance/leadership contact ("primary contact").

For every firm in the enriched master we look for one person, walking a strict
title cascade and stopping at the first level that yields someone:

    1. Chief Financial Officer (CFO)
    2. Chief Operating Officer (COO)
    3. Director of Finance
    4. Controller / Comptroller
    5. Managing Partner
    6. Any contact we can find (best existing scraped contact, with their title)

Three columns are merged back onto the master CSV (firm-level, so they repeat
across a firm's contact rows):

    primary_contact_title   the title as it appeared on the page
    primary_contact_name    the person's name
    primary_contact_email   their email (seen on-page, or matched from the
                            firm's already-scraped emails by name pattern)

How it searches, per firm:
  1. Cache-first: every page already saved under data/raw/scrape_cache for the
     firm's domain (and its redirect target) is scanned — no network.
  2. Network second: leadership-likely paths not yet cached (/team,
     /leadership, /management, ...) are fetched with the same politeness rules
     as the main scraper (robots.txt, one request at a time per domain,
     2s delay, backoff). Fetched pages are cached for future runs.
  3. Fetching stops early once a named CFO is found (nothing outranks level 1).

Usage:
    python -m src.scrape_primary_contact                # full run, updates master + xlsx
    python -m src.scrape_primary_contact --limit 25     # first 25 firms only
    python -m src.scrape_primary_contact --cache-only   # no network, cached pages only
    python -m src.scrape_primary_contact --dry-run      # print results, write nothing
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pandas as pd
from selectolax.parser import HTMLParser
from tqdm.asyncio import tqdm as atqdm

import config
from src.utils import domain_of, get_logger, safe_filename
from src.scrape_websites import (
    EMAIL_REGEX,
    _email_domain_matches,
    _fetch_robots,
    _fetch_with_retries,
    _is_junk_email,
    _is_low_value_email,
    _is_valid_email,
    _looks_like_person_name,
    _looks_like_title,
    _NAME_RE,
    _read_cache,
    _read_redirect_meta,
    _write_cache,
    _write_redirect_meta,
    extract_contacts_from_html,
)

log = get_logger("scrape_primary_contact", config.SCRAPE_LOG)

PRIMARY_COLS = ["primary_contact_title", "primary_contact_name", "primary_contact_email"]

# --- The title cascade, best first. Level = index. ----------------------------
CASCADE: list[tuple[str, re.Pattern]] = [
    # The lookbehinds keep out "your personal/family CFO" marketing copy —
    # firms selling a 'personal CFO' service, not employing a CFO.
    ("CFO", re.compile(
        r"(?i:(?<!personal\s)(?<!family\s)(?<!your\s)chief\s+financial\s+officer)"
        r"|(?i:(?<!personal\s)(?<!family\s)(?<!your\s))\bCFO\b")),
    ("COO", re.compile(r"(?i:chief\s+operat(?:ing|ions)\s+officer)|\bCOO\b")),
    ("Director of Finance", re.compile(r"(?i:director\s+of\s+finance|finance\s+director)")),
    # "(?<!data\s)" keeps GDPR "data controller" boilerplate out.
    ("Controller", re.compile(r"(?i:(?<!data\s)\b(?:controller|comptroller)\b)")),
    ("Managing Partner", re.compile(r"(?i:managing\s+partner)")),
]
FALLBACK_LABEL = "any_contact"

# Pages most likely to list leadership. Includes the original team_paths plus
# a few management-specific ones the first scrape never tried.
LEADERSHIP_PATHS = [
    "/team", "/our-team", "/leadership", "/about", "/about-us", "/people",
    "/our-people", "/management", "/who-we-are", "/our-firm", "/advisors",
    "/staff", "/contact", "/",
]

# Tokens that disqualify a "name" candidate — title fragments that happen to
# match the capitalized-two-words name regex (e.g. "Chief Financial").
_TITLE_TOKEN_STOP = frozenset({
    "chief", "officer", "financial", "finance", "operating", "operations",
    "managing", "partner", "partners", "director", "controller", "comptroller",
    "president", "executive", "investment", "investments", "wealth", "advisor",
    "adviser", "advisors", "advisers", "planning", "planner", "senior", "vice",
    "principal", "founder", "founding", "compliance", "portfolio", "client",
    "clients", "associate", "analyst", "team", "view", "bio", "read", "more",
    "meet", "email", "linkedin", "group", "capital", "management", "services",
    "officers", "leadership", "firm", "company", "resources", "legal",
    # junk observed in real runs: page chrome, marketing copy, address bits
    "head", "chairman", "trader", "retired", "certified", "public",
    "securities", "equity", "series", "mission", "statement", "download",
    "goals", "allies", "families", "family", "great", "free", "toll",
    "airlines", "the", "your", "our", "now", "have", "why", "trust",
    "trusted", "counseling", "gen", "statements", "by", "touch", "focus",
    "area", "saint",
})

# Tags worth checking as potential title elements on team/leadership pages.
_TITLE_NODE_TAGS = "h1,h2,h3,h4,h5,h6,p,span,div,li,td,dt,dd,strong,em,a,figcaption,small"


@dataclass
class Candidate:
    level: int            # index into CASCADE; len(CASCADE) = fallback
    title: str | None
    name: str | None
    email: str | None
    source: str           # page path or "existing_contacts"

    def sort_key(self) -> tuple:
        # Lower is better: cascade level, then having an email, then a name.
        return (self.level, self.email is None, self.name is None)


@dataclass
class FirmPrimaryResult:
    crd_number: object
    candidate: Candidate | None
    pages_fetched: int = 0
    skipped_reason: str | None = None


# ---------- Title / name / email heuristics -----------------------------------


def _title_level(text: str) -> int | None:
    """Lowest cascade level whose pattern matches `text`, or None."""
    level, _m = _first_cascade_match(text)
    return level


def _first_cascade_match(text: str) -> tuple[int | None, re.Match | None]:
    for i, (_label, pat) in enumerate(CASCADE):
        m = pat.search(text)
        if m:
            return i, m
    return None, None


# Overlapping name scan: a plain finditer on "Chairman David Smith" consumes
# "Chairman David" and never sees "David Smith". The lookahead form yields a
# candidate at every start position.
_NAME_SCAN_RE = re.compile(r"(?=(" + _NAME_RE.pattern + r"))")


def _adjacent_name(text: str, t_start: int, t_end: int,
                   firm_legal_name: str | None, max_gap: int = 30) -> str | None:
    """A plausible person name directly next to the title span in `text`.

    Team pages render name and title adjacently ("Jane Doe Chief Operating
    Officer", "Chris Euell, COO"); a capitalized pair 100 chars away is almost
    always chrome, so anything outside `max_gap` is ignored. The name just
    before the title wins; just after is the fallback.
    """
    before_best = None
    after_best = None
    for m in _NAME_SCAN_RE.finditer(text):
        s, e = m.start(1), m.end(1)
        if s > t_end + max_gap:
            break
        name = m.group(1)
        if not _plausible_person(name, firm_legal_name):
            continue
        if e <= t_start and t_start - e <= max_gap:
            before_best = name          # keep the latest one before the title
        elif s >= t_end and s - t_end <= max_gap and after_best is None:
            after_best = name
    return before_best or after_best


def _plausible_person(name: str | None, firm_legal_name: str | None) -> bool:
    if not _looks_like_person_name(name, firm_legal_name):
        return False
    tokens = [t.lower().strip(".,&|") for t in name.split()]
    return not any(t in _TITLE_TOKEN_STOP for t in tokens)


def _emails_in(text: str, page_host: str) -> list[str]:
    out = []
    for raw in EMAIL_REGEX.findall(text or ""):
        e = raw.strip().lower()
        if (_is_valid_email(e)
                and not _is_junk_email(e, config.DEFAULT_SCRAPER)
                and _email_domain_matches(e, page_host)):
            out.append(e)
    return out


def _block_of(node, max_chars: int = 450, max_up: int = 3):
    """Climb ancestors while the block stays small — a 'team card' sized chunk."""
    best = node
    cur = node
    for _ in range(max_up):
        cur = cur.parent
        if cur is None or cur.tag in ("body", "html", "main", "section"):
            break
        txt = cur.text(separator=" ", strip=True)
        if len(txt) > max_chars:
            break
        best = cur
    return best


def _block_emails(block, block_text: str, page_host: str) -> list[str]:
    emails: list[str] = []
    for a in block.css('a[href^="mailto:"], a[href^="MAILTO:"]'):
        href = (a.attributes.get("href") or "").strip()
        e = href.split(":", 1)[1].split("?", 1)[0].strip().lower()
        if (_is_valid_email(e)
                and not _is_junk_email(e, config.DEFAULT_SCRAPER)
                and _email_domain_matches(e, page_host)):
            emails.append(e)
    emails.extend(e for e in _emails_in(block_text, page_host) if e not in emails)
    return emails


def _name_tokens(name: str) -> tuple[str, str] | None:
    """('first', 'last') lowercased alpha-only, or None if not derivable."""
    parts = [re.sub(r"[^a-z]", "", p.lower()) for p in name.split()]
    parts = [p for p in parts if len(p) >= 2]  # drop middle initials
    if len(parts) < 2:
        return None
    return parts[0], parts[-1]


def match_email_by_name(name: str | None, emails: list[str]) -> str | None:
    """Pick the firm email whose local part looks like this person's name."""
    if not name or not emails:
        return None
    toks = _name_tokens(name)
    if not toks:
        return None
    first, last = toks
    patterns = {
        f"{first}.{last}", f"{first}{last}", f"{first}_{last}", f"{first}-{last}",
        f"{first[0]}{last}", f"{first[0]}.{last}", f"{first}.{last[0]}",
        f"{first}{last[0]}", f"{last}{first[0]}", f"{last}.{first}", f"{last}{first}",
    }
    for e in emails:
        if e.split("@", 1)[0].lower() in patterns:
            return e
    # First-name-only locals (jane@firm.com) — accept only when unambiguous.
    hits = [e for e in emails if e.split("@", 1)[0].lower() in (first, last)]
    if len(hits) == 1:
        return hits[0]
    return None


def _name_from_email(email: str) -> str | None:
    local = email.split("@", 1)[0]
    parts = re.split(r"[._\-]+", local)
    if len(parts) >= 2 and all(p.isalpha() and len(p) >= 2 for p in parts[:2]):
        return f"{parts[0].title()} {parts[1].title()}"
    return None


# ---------- Per-page candidate extraction --------------------------------------


_ANY_CASCADE = re.compile("|".join(f"(?:{p.pattern})" for _l, p in CASCADE))


def candidates_from_html(html: str, page_host: str, source: str,
                         firm_legal_name: str | None) -> list[Candidate]:
    """Find (title, name, email) candidates matching the cascade on one page."""
    if not html:
        return []
    tree = HTMLParser(html)
    for n in tree.css("script, style, noscript"):
        try:
            n.decompose()
        except Exception:
            pass

    # Fast gate: if no cascade title appears anywhere in the visible text,
    # skip the (expensive) element scan entirely. Most pages exit here.
    body = tree.body or tree.root
    text = re.sub(r"\s+", " ", body.text(separator=" ", strip=True)) if body else ""
    if not _ANY_CASCADE.search(text):
        return []

    out: list[Candidate] = []

    # Pass 1 — element-based: short nodes whose text IS a title ("Partner & CFO").
    for node in tree.css(_TITLE_NODE_TAGS):
        own = node.text(deep=True, strip=True)
        if not own or len(own) > 90:
            continue
        own_clean = re.sub(r"\s+", " ", own)
        level, m = _first_cascade_match(own_clean)
        if level is None:
            continue
        # The element's own text often holds the name too, CSS-squished:
        # "Jennifer N. OlsonChief Financial Officer" / "Chris Euell, COO".
        name = _adjacent_name(own_clean, m.start(), m.end(), firm_legal_name, max_gap=40)
        block = _block_of(node)
        block_text = re.sub(r"\s+", " ", block.text(separator=" ", strip=True))[:500]
        if not name:
            idx = block_text.find(m.group(0))
            if idx >= 0:
                name = _adjacent_name(block_text, idx, idx + len(m.group(0)), firm_legal_name)
        emails = _block_emails(block, block_text, page_host)
        email = (match_email_by_name(name, emails) or (emails[0] if len(emails) == 1 else None))
        if name or email:
            title = own_clean
            if name and name in title:  # title cell shouldn't repeat the name
                title = re.sub(r"\s+", " ", title.replace(name, " ")).strip(" |,–—-")
                title = re.sub(r"(?i)^by\b[\s,|–—-]*", "", title).strip(" |,–—-")
            out.append(Candidate(level, title or m.group(0), name, email, source))

    # Pass 2 — flat-text fallback: title match in running text, adjacent name.
    for level, (_label, pat) in enumerate(CASCADE):
        for m in pat.finditer(text):
            w_start = max(0, m.start() - 80)
            window = text[w_start:m.end() + 80]
            name = _adjacent_name(window, m.start() - w_start, m.end() - w_start,
                                  firm_legal_name)
            if not name:
                continue
            e_window = text[max(0, m.start() - 200):m.end() + 200]
            emails = _emails_in(e_window, page_host)
            email = match_email_by_name(name, emails) or (emails[0] if len(emails) == 1 else None)
            out.append(Candidate(level, m.group(0), name, email, source))

    return out


# ---------- Per-firm search -----------------------------------------------------


def _clean_existing_title(title, name) -> str | None:
    """Existing contact_title values carry noise ('Jane Doe Partner View Bio').

    Clean what's recoverable; anything that still doesn't read like a job title
    (URLs, phone numbers, sentence fragments) becomes None — blank beats junk.
    """
    if not isinstance(title, str) or not title.strip():
        return None
    t = re.sub(r"\s+", " ", title).strip()
    t = re.sub(r"\bview bio\b.*$", "", t, flags=re.I).strip(" |,–—-")
    if isinstance(name, str) and name and t.lower().startswith(name.lower()):
        t = t[len(name):].strip(" |,–—-")
    # A leading person-name prefix even when it differs from contact_name
    # ("Tom Hawley Partner, Co-Founder ..." with name "Thomas Hawley"). Only
    # strip when the prefix really reads like a person, so title openers like
    # "Senior Wealth ..." survive.
    m = _NAME_RE.match(t)
    if m and len(t) > m.end():
        prefix_tokens = [w.lower().strip(".,") for w in m.group(1).split()]
        rest = t[m.end():].strip(" |,–—-")
        if not any(w in _TITLE_TOKEN_STOP for w in prefix_tokens) and _looks_like_title(rest):
            t = rest
    # Quality gate: short, no emails/URLs/digits, contains real title vocab.
    if "@" in t or "http" in t.lower() or re.search(r"\d", t):
        return None
    if len(t) > 60 or len(t.split()) > 7:
        return None
    if not _looks_like_title(t):
        return None
    return t or None


def _fallback_candidate(existing: list[dict], harvested: list) -> Candidate | None:
    """Level-6: best 'any contact' from existing master rows + harvested contacts.

    Preference: personal email + name + title > personal email + name >
    personal email > low-value email (info@) > name + title with no email.
    """
    pool: list[tuple[tuple, Candidate]] = []
    rows = list(existing) + [
        {"contact_name": c.name, "contact_title": c.title, "contact_email": c.email}
        for c in harvested
    ]
    for r in rows:
        email = r.get("contact_email")
        email = email.strip().lower() if isinstance(email, str) and email.strip() else None
        name = r.get("contact_name")
        name = name.strip() if isinstance(name, str) and name.strip() else None
        # Re-validate names from the earlier scrape with today's stricter rules
        # ("Resources Legal" etc.) — blank beats junk.
        if name and not _plausible_person(name, None):
            name = None
        title = _clean_existing_title(r.get("contact_title"), name)
        if not email and not name:
            continue
        if not name and email:
            name = _name_from_email(email)
        low_value = bool(email) and _is_low_value_email(email, config.DEFAULT_SCRAPER)
        rank = (
            email is None,         # any email beats none
            low_value,             # personal beats info@/office@
            name is None,
            title is None,
        )
        pool.append((rank, Candidate(len(CASCADE), title, name, email, "existing_contacts")))
    if not pool:
        return None
    pool.sort(key=lambda t: t[0])
    return pool[0][1]


def _best(candidates: list[Candidate]) -> Candidate | None:
    return min(candidates, key=Candidate.sort_key) if candidates else None


def _cached_pages(host: str | None) -> list[tuple[str, str, str]]:
    """All cached (page_host, path_label, html) for a filed host + its redirect."""
    if not host:
        return []
    hosts = [host]
    final = _read_redirect_meta(host)
    if final and final != host:
        hosts.append(final)
    pages = []
    for h in hosts:
        d = config.SCRAPE_CACHE_DIR / safe_filename(h)
        if not d.is_dir():
            continue
        for f in d.glob("*.html"):
            try:
                pages.append((h, f.stem, f.read_text(encoding="utf-8", errors="replace")))
            except Exception:
                continue
    return pages


def _done_searching(best: Candidate | None) -> bool:
    """Nothing on a further page can outrank a named CFO."""
    return best is not None and best.level == 0 and best.name is not None


async def _search_one_firm(
    firm: dict,
    client: httpx.AsyncClient | None,
    sem: asyncio.Semaphore,
    cfg: config.ScraperConfig,
) -> FirmPrimaryResult:
    crd = firm.get("crd_number")
    firm_name = firm.get("firm_legal_name")
    website = firm.get("website")
    existing = firm.get("existing_contacts") or []
    known_emails = {
        str(r.get("contact_email")).strip().lower()
        for r in existing
        if isinstance(r.get("contact_email"), str) and r.get("contact_email").strip()
    }

    host = domain_of(website) if isinstance(website, str) else None
    social = {d.lower() for d in config.SOCIAL_URL_BLOCKLIST}
    if host in social:
        host = None

    candidates: list[Candidate] = []
    harvested: list = []
    pages_seen: list[tuple[str, str, str]] = []  # (page_host, label, html)

    def _scan_page(page_host: str, label: str, html: str) -> None:
        candidates.extend(candidates_from_html(html, page_host, label, firm_name))
        pages_seen.append((page_host, label, html))

    # --- 1) cached pages (no network) ---
    for page_host, label, html in _cached_pages(host):
        _scan_page(page_host, label, html)

    fetched = 0
    # --- 2) network pass over leadership paths not yet cached ---
    if client is not None and host and not _done_searching(_best(candidates)):
        async with sem:
            final_host = _read_redirect_meta(host)
            if final_host in social:
                host = None  # filed site is just a social redirect
            else:
                rp = await _fetch_robots(client, host)
                await asyncio.sleep(cfg.per_domain_delay_seconds)
                last_ts = time.monotonic()
                rp_final = None
                for path in LEADERSHIP_PATHS:
                    cache_host = final_host or host
                    if _read_cache(cache_host, path) is not None:
                        continue  # already scanned in the cache pass
                    url = f"https://{host}{path}"
                    if not rp.can_fetch(config.USER_AGENT, url):
                        continue
                    if rp_final is not None and not rp_final.can_fetch(config.USER_AGENT, url):
                        continue
                    wait = cfg.per_domain_delay_seconds - (time.monotonic() - last_ts)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    status, html, fu = await _fetch_with_retries(client, url, cfg)
                    last_ts = time.monotonic()
                    if status != 200 or not html:
                        continue
                    fh = domain_of(fu) if fu else None
                    if fh and fh != host and fh != final_host:
                        final_host = fh
                        if cfg.cache_pages:
                            _write_redirect_meta(host, fh)
                        if fh in social:
                            break
                        rp_final = await _fetch_robots(client, fh)
                        if not rp_final.can_fetch(config.USER_AGENT, fu):
                            continue
                    if cfg.cache_pages:
                        _write_cache(final_host or host, path, html)
                    fetched += 1
                    _scan_page(final_host or host, path, html)
                    if _done_searching(_best(candidates)):
                        break

    best = _best(candidates)

    # Email harvesting is the expensive part, so it runs lazily: only when the
    # chosen person still needs an email, or no cascade title matched at all.
    if best is None or best.email is None:
        for page_host, label, html in pages_seen:
            for c in extract_contacts_from_html(html, page_host, label,
                                                firm_legal_name=firm_name):
                harvested.append(c)
                known_emails.add(c.email)

    # --- email resolution: match by name against everything we know ---
    if best is not None and best.email is None and best.name is not None:
        best.email = match_email_by_name(best.name, sorted(known_emails))

    # --- 3) fallback: any contact at all ---
    if best is None:
        best = _fallback_candidate(existing, harvested)

    return FirmPrimaryResult(crd_number=crd, candidate=best, pages_fetched=fetched)


async def _search_all(firms: list[dict], cfg: config.ScraperConfig,
                      cache_only: bool) -> list[FirmPrimaryResult]:
    sem = asyncio.Semaphore(cfg.max_concurrent_domains)

    if cache_only:
        results = []
        for f in atqdm(firms, desc="primary-contact (cache-only)"):
            results.append(await _search_one_firm(f, None, sem, cfg))
        return results

    timeout = httpx.Timeout(cfg.request_timeout_seconds, connect=cfg.request_timeout_seconds)
    headers = {"User-Agent": config.USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    limits = httpx.Limits(max_connections=cfg.max_concurrent_domains * 2,
                          max_keepalive_connections=cfg.max_concurrent_domains)

    async def _run_one(f: dict) -> FirmPrimaryResult:
        try:
            return await _search_one_firm(f, client, sem, cfg)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("primary-contact search failed for CRD %s: %s",
                          f.get("crd_number"), exc)
            return FirmPrimaryResult(crd_number=f.get("crd_number"), candidate=None,
                                     skipped_reason=f"error:{type(exc).__name__}")

    results: list[FirmPrimaryResult] = []
    async with httpx.AsyncClient(headers=headers, timeout=timeout, limits=limits,
                                 http2=False) as client:
        coros = [_run_one(f) for f in firms]
        for fut in atqdm.as_completed(coros, total=len(coros), desc="primary-contact"):
            results.append(await fut)
    return results


# ---------- Entry point ---------------------------------------------------------


def _firms_from_master(master: pd.DataFrame, limit: int | None) -> list[dict]:
    """One dict per firm, in master order, carrying its existing contact rows."""
    firms: list[dict] = []
    seen: set = set()
    for crd, grp in master.groupby("crd_number", sort=False):
        if crd in seen:
            continue
        seen.add(crd)
        first = grp.iloc[0]
        firms.append({
            "crd_number": crd,
            "firm_legal_name": first.get("firm_legal_name"),
            "website": first.get("website"),
            "existing_contacts": grp[["contact_name", "contact_title", "contact_email"]]
                .to_dict("records"),
        })
        if limit is not None and len(firms) >= limit:
            break
    return firms


def run(master_path: Path | None = None, limit: int | None = None,
        cache_only: bool = False, dry_run: bool = False,
        export_xlsx: bool = True,
        scraper_cfg: config.ScraperConfig = config.DEFAULT_SCRAPER) -> None:
    config.ensure_dirs()
    master_path = Path(master_path) if master_path else (
        config.ENRICHED_DIR / "ria_master_20260504.csv")
    master = pd.read_csv(master_path, low_memory=False)

    firms = _firms_from_master(master, limit)
    log.info("Primary-contact search over %d firms (cache_only=%s)", len(firms), cache_only)

    results = asyncio.run(_search_all(firms, scraper_cfg, cache_only))

    rows = []
    by_label: dict[str, int] = {}
    for r in results:
        c = r.candidate
        label = "none" if c is None else (
            CASCADE[c.level][0] if c.level < len(CASCADE) else FALLBACK_LABEL)
        by_label[label] = by_label.get(label, 0) + 1
        rows.append({
            "crd_number": r.crd_number,
            "primary_contact_title": c.title if c else None,
            "primary_contact_name": c.name if c else None,
            "primary_contact_email": c.email if c else None,
        })
    flags = pd.DataFrame(rows)

    n_pages = sum(r.pages_fetched for r in results)
    n_email = int(flags["primary_contact_email"].notna().sum())
    n_name = int(flags["primary_contact_name"].notna().sum())
    print(f"\n=== Primary-contact search over {len(firms):,} firms ===")
    print(f"  pages fetched (network):   {n_pages:,}")
    print(f"  firms with a name:         {n_name:,}")
    print(f"  firms with an email:       {n_email:,}")
    print("  by cascade level:")
    order = [label for label, _ in CASCADE] + [FALLBACK_LABEL, "none"]
    for label in order:
        if label in by_label:
            print(f"    {label:<22} {by_label[label]:>5,}")

    if dry_run:
        with pd.option_context("display.max_rows", None, "display.width", 200,
                               "display.max_colwidth", 60):
            print(flags.to_string(index=False))
        print("\n(dry run — nothing written)")
        return

    # Merge firm-level columns onto every row of the master, after email_source.
    master = master.drop(columns=[c for c in PRIMARY_COLS if c in master.columns])
    master = master.merge(flags, on="crd_number", how="left")
    cols = [c for c in master.columns if c not in PRIMARY_COLS]
    anchor = cols.index("email_source") + 1 if "email_source" in cols else len(cols)
    master = master[cols[:anchor] + PRIMARY_COLS + cols[anchor:]]
    master.to_csv(master_path, index=False)
    print(f"\nWrote {master_path} (added columns: {', '.join(PRIMARY_COLS)})")

    if export_xlsx:
        from src.export_excel import export
        export(csv_path=master_path)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Find each firm's primary finance contact")
    p.add_argument("--master", type=Path, default=None, help="path to the master CSV")
    p.add_argument("--limit", type=int, default=None, help="only the first N firms")
    p.add_argument("--cache-only", action="store_true", help="no network; cached pages only")
    p.add_argument("--dry-run", action="store_true", help="print results, write nothing")
    p.add_argument("--no-excel", action="store_true", help="skip re-exporting the xlsx")
    args = p.parse_args()
    run(master_path=args.master, limit=args.limit, cache_only=args.cache_only,
        dry_run=args.dry_run, export_xlsx=not args.no_excel)
