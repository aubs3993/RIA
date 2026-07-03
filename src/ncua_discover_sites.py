"""NCUA Stage 2b: discover credit-union websites (NCUA data carries no URL).

Best-effort: for each credit union we generate candidate domains from its name
using common CU conventions (.org first, "fcu"/"cu" suffixes, acronyms) and
verify each over HTTP — a candidate only counts if the page actually loads AND
mentions "credit union" together with a distinctive token from the CU's name
(guards against parked domains and unrelated sites).

Results are cached to data/ncua/processed/discovered_sites.csv so re-runs skip
already-resolved (or already-failed) credit unions. This stage is the NCUA
analogue of having WEBADDR handed to you by the FDIC API; expect a partial
hit-rate, and treat the discovered domain as a lead, not gospel.

Run standalone:
    python -m src.ncua_discover_sites --limit 50      # smoke test, smallest-asset first
    python -m src.ncua_discover_sites                 # all asset-qualified CUs
"""
from __future__ import annotations

import argparse
import asyncio
import re
from dataclasses import dataclass

import httpx
import pandas as pd
from selectolax.parser import HTMLParser
from tqdm.asyncio import tqdm as atqdm

import config
from config import CreditUnionICP, DEFAULT_CU_ICP
from src.scrape_websites import _fetch_robots
from src.utils import domain_of, get_logger

log = get_logger("ncua_discover_sites", config.SCRAPE_LOG)

# Words that are not distinctive enough to identify a specific CU.
_GENERIC = frozenset({
    "credit", "union", "federal", "community", "employees", "employee",
    "association", "financial", "members", "member", "savings", "the", "of",
    "and", "for", "a", "an", "co", "inc", "corp", "company", "first", "national",
    "state", "city", "county", "area", "valley", "fcu", "cu",
})
_TLDS = (".org", ".com", ".coop")
MAX_CANDIDATES = 8


@dataclass
class Discovery:
    cu_number: object
    website: str | None
    method: str | None     # which candidate pattern matched
    status: str            # "found" | "no_match" | "no_candidates"


def _words(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w]


def _significant(name: str) -> list[str]:
    return [w for w in _words(name) if w not in _GENERIC and len(w) >= 3]


def candidate_domains(name: str) -> list[str]:
    """Ordered list of plausible domains for a CU name (best guess first)."""
    raw = _words(name)
    if not raw:
        return []
    has_federal = "federal" in raw
    suffix = "fcu" if has_federal else "cu"

    no_cu = [w for w in raw if w not in {"credit", "union"}]
    no_cu_fed = [w for w in no_cu if w != "federal"]

    cores: list[str] = []
    cores.append("".join(no_cu))             # navyfederal
    cores.append("".join(no_cu_fed))         # navy
    cores.append("".join(no_cu_fed) + suffix)  # navyfcu / abccu
    cores.append("".join(no_cu) + "cu")      # navyfederalcu

    sig = _significant(name)
    acr = "".join(w[0] for w in sig)
    if len(acr) >= 2:
        cores.append(acr + suffix)           # e.g. abcfcu
        cores.append(acr + "cu")
        cores.append(acr)

    seen: set[str] = set()
    domains: list[str] = []
    for c in cores:
        if len(c) < 2 or c in seen:
            continue
        seen.add(c)
        for t in _TLDS:
            domains.append(c + t)
    return domains[:MAX_CANDIDATES]


def _page_confirms(html: str, name: str) -> bool:
    """True if the page looks like THIS credit union's site."""
    if not html:
        return False
    tree = HTMLParser(html)
    text = (tree.body.text(separator=" ", strip=True) if tree.body else tree.text(separator=" ", strip=True)) or ""
    low = text.lower()
    if "credit union" not in low:
        return False
    sig = _significant(name)
    return any(tok in low for tok in sig)


async def _resolve_one(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                       cu_number, name: str) -> Discovery:
    candidates = candidate_domains(name)
    if not candidates:
        return Discovery(cu_number, None, None, "no_candidates")
    async with sem:
        for dom in candidates:
            # These are name-derived guesses, so many candidates belong to
            # unrelated sites — honor each candidate's robots.txt before probing
            # (same politeness guarantee as every other fetcher in the repo).
            rp = await _fetch_robots(client, dom)
            if not rp.can_fetch(config.ROBOTS_UA, f"https://{dom}/"):
                log.debug("robots disallow / on %s — skipping candidate", dom)
                continue
            for scheme in ("https", "http"):
                url = f"{scheme}://{dom}/"
                try:
                    resp = await client.get(url, timeout=8.0, follow_redirects=True)
                except Exception:
                    continue
                if resp.status_code == 200 and _page_confirms(resp.text, name):
                    final = domain_of(str(resp.url)) or dom
                    return Discovery(cu_number, f"http://{final}", dom, "found")
                break  # don't try http if https returned a non-confirming response
    return Discovery(cu_number, None, None, "no_match")


async def _resolve_all(targets: list[tuple], max_concurrent: int) -> list[Discovery]:
    sem = asyncio.Semaphore(max_concurrent)
    headers = {"User-Agent": config.USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    results: list[Discovery] = []
    async with httpx.AsyncClient(headers=headers, http2=False) as client:
        coros = [_resolve_one(client, sem, cu, name) for cu, name in targets]
        for fut in atqdm.as_completed(coros, total=len(coros), desc="discover-sites"):
            results.append(await fut)
    return results


def _load_cache() -> pd.DataFrame:
    if config.NCUA_SITE_CACHE.exists():
        return pd.read_csv(config.NCUA_SITE_CACHE)
    return pd.DataFrame(columns=["cu_number", "website", "method", "status"])


def discover(df: pd.DataFrame, icp: CreditUnionICP = DEFAULT_CU_ICP,
             limit: int | None = None, max_concurrent: int = 12,
             refresh: bool = False) -> pd.DataFrame:
    """Fill `website` for asset/state-qualified CUs. Returns df (copy) with
    websites populated for resolved CUs; caches every attempt."""
    config.ensure_ncua_dirs()
    out = df.copy()

    asset = pd.to_numeric(out["asset_total"], errors="coerce")
    band = asset.between(icp.asset_min, icp.asset_max, inclusive="both")
    if icp.states:
        states = {s.upper() for s in icp.states}
        band &= out["office_state"].astype("string").str.upper().isin(states)
    qualified = out[band].sort_values("asset_total", ascending=True)  # smallest first for smoke tests

    cache = _load_cache()
    done = set() if refresh else set(pd.to_numeric(cache["cu_number"], errors="coerce").dropna().astype("int64"))

    todo = [(int(r.cu_number), str(r.firm_legal_name))
            for r in qualified.itertuples() if int(r.cu_number) not in done]
    if limit is not None:
        todo = todo[:limit]

    log.info("Site discovery: %d qualified, %d cached, %d to resolve",
             len(qualified), len(done), len(todo))
    if todo:
        results = asyncio.run(_resolve_all(todo, max_concurrent))
        new = pd.DataFrame([r.__dict__ for r in results])
        cache = pd.concat([cache, new], ignore_index=True).drop_duplicates("cu_number", keep="last")
        cache.to_csv(config.NCUA_SITE_CACHE, index=False)

    # Merge cached websites back onto the full frame.
    found = cache[cache["website"].notna()][["cu_number", "website"]]
    found = found.assign(cu_number=pd.to_numeric(found["cu_number"], errors="coerce").astype("Int64"))
    site_map = dict(zip(found["cu_number"], found["website"]))
    out["website"] = out["cu_number"].map(site_map).astype("string")

    _print_summary(cache, len(qualified))
    return out


def _print_summary(cache: pd.DataFrame, n_qualified: int) -> None:
    n_found = int((cache["status"] == "found").sum())
    n_attempted = len(cache)
    print("\n[ncua_discover_sites] summary")
    print(f"  asset-qualified CUs:   {n_qualified:,}")
    print(f"  resolution attempts:   {n_attempted:,}")
    print(f"  websites found:        {n_found:,}  "
          f"({n_found / n_attempted:.0%} of attempts)" if n_attempted else "  (none)")
    print(f"  cache → {config.NCUA_SITE_CACHE}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Discover NCUA credit-union websites")
    p.add_argument("--limit", type=int, default=None, help="cap CUs resolved (smallest-asset first)")
    p.add_argument("--refresh", action="store_true", help="ignore cache, re-resolve")
    args = p.parse_args()
    clean = pd.read_parquet(config.NCUA_CLEAN_PARQUET)
    discover(clean, limit=args.limit, refresh=args.refresh)
