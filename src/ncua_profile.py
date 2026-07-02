"""NCUA Stage 2b (primary): pull each credit union's official Profile from NCUA.

The 5300 Call Report bulk data carries no website or contact, but NCUA's
"Research a Credit Union" tool is backed by a public JSON API that returns the
Profile fields — including the official **website**, the **CEO/manager name**,
and a phone number — for ~95% of credit unions.

    GET https://mapping.ncua.gov/api/CreditUnionDetails/GetCreditUnionDetails/{charter}

This is free, government-sourced, and far better than guessing (which we keep
only as a fallback for the ~5% the API leaves blank, via ncua_discover_sites).
It also gives a named decision-maker up front — for a small credit union the CEO
is the buyer — so after scraping we reconcile that official name onto the
primary-contact column and match it to a scraped email.

Run standalone:
    python -m src.ncua_profile --limit 50     # smoke test (smallest-asset first)
    python -m src.ncua_profile                 # all asset-qualified CUs
"""
from __future__ import annotations

import argparse
import asyncio

import httpx
import pandas as pd
from tqdm.asyncio import tqdm as atqdm

import config
from config import CreditUnionICP, DEFAULT_CU_ICP
from src.utils import get_logger, normalize_url
from src.scrape_primary_contact import match_email_by_name

log = get_logger("ncua_profile", config.SCRAPE_LOG)

DETAIL_URL = "https://mapping.ncua.gov/api/CreditUnionDetails/GetCreditUnionDetails/{}"
PROFILE_CACHE = config.NCUA_PROCESSED_DIR / "ncua_profiles.csv"


def _normalize_record(charter, raw: dict) -> dict:
    """Map the NCUA detail JSON to our profile fields. Pure (no network)."""
    if not isinstance(raw, dict) or raw.get("isError"):
        return {"cu_number": charter, "website": None, "ceo_name": None,
                "phone": None, "status": "error"}
    website = normalize_url((raw.get("creditUnionWebsite") or "").strip() or None)
    ceo = (raw.get("creditUnionCeo") or "").strip() or None
    phone = (raw.get("creditUnionPhone") or "").strip() or None
    return {
        "cu_number": charter,
        "website": website,
        "ceo_name": ceo,
        "phone": phone,
        "status": "ok" if website else "no_website",
    }


async def _fetch_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, charter: int) -> dict:
    async with sem:
        try:
            r = await client.get(DETAIL_URL.format(charter), timeout=15.0,
                                 headers={"Accept": "application/json"})
            if r.status_code != 200:
                return {"cu_number": charter, "website": None, "ceo_name": None,
                        "phone": None, "status": f"http_{r.status_code}"}
            return _normalize_record(charter, r.json())
        except Exception as exc:
            return {"cu_number": charter, "website": None, "ceo_name": None,
                    "phone": None, "status": f"error:{type(exc).__name__}"}


async def _fetch_all(charters: list[int], concurrency: int) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)
    headers = {"User-Agent": config.USER_AGENT}
    out: list[dict] = []
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        coros = [_fetch_one(client, sem, c) for c in charters]
        for fut in atqdm.as_completed(coros, total=len(coros), desc="ncua-profile"):
            out.append(await fut)
    return out


def _load_cache() -> pd.DataFrame:
    if PROFILE_CACHE.exists():
        return pd.read_csv(PROFILE_CACHE)
    return pd.DataFrame(columns=["cu_number", "website", "ceo_name", "phone", "status"])


def enrich(df: pd.DataFrame, icp: CreditUnionICP = DEFAULT_CU_ICP,
           limit: int | None = None, concurrency: int = 8,
           refresh: bool = False) -> pd.DataFrame:
    """Fill website/ceo_name/phone for asset/state-qualified CUs from the NCUA
    Profile API. Caches every lookup. Returns df (copy) with the columns added."""
    config.ensure_ncua_dirs()
    out = df.copy()

    asset = pd.to_numeric(out["asset_total"], errors="coerce")
    band = asset.between(icp.asset_min, icp.asset_max, inclusive="both")
    if icp.states:
        states = {s.upper() for s in icp.states}
        band &= out["office_state"].astype("string").str.upper().isin(states)
    qualified = out[band].sort_values("asset_total", ascending=True)

    cache = _load_cache()
    # Only terminal statuses count as done — transient failures (http_5xx/429,
    # timeouts) get retried on the next run; drop_duplicates keep="last" below
    # replaces the stale failure row. "error" = the API said the charter itself
    # is invalid, so refetching it would never help.
    settled = cache[cache["status"].isin(["ok", "no_website", "error"])]
    done = set() if refresh else set(pd.to_numeric(settled["cu_number"], errors="coerce").dropna().astype("int64"))
    todo = [int(c) for c in qualified["cu_number"].tolist() if int(c) not in done]
    if limit is not None:
        todo = todo[:limit]

    log.info("NCUA Profile: %d qualified, %d cached, %d to fetch", len(qualified), len(done), len(todo))
    if todo:
        new = pd.DataFrame(asyncio.run(_fetch_all(todo, concurrency)))
        cache = pd.concat([cache, new], ignore_index=True).drop_duplicates("cu_number", keep="last")
        cache.to_csv(PROFILE_CACHE, index=False)

    cache["cu_number"] = pd.to_numeric(cache["cu_number"], errors="coerce").astype("Int64")
    by_cu = cache.set_index("cu_number")
    out["website"] = out["cu_number"].map(by_cu["website"]).astype("string")
    out["ceo_name"] = out["cu_number"].map(by_cu["ceo_name"]).astype("string")
    out["ncua_phone"] = out["cu_number"].map(by_cu["phone"]).astype("string")

    _print_summary(cache, len(qualified))
    return out


def reconcile_ceo(master_path) -> None:
    """After scraping, prefer the NCUA CEO as the primary contact and match a
    scraped email to that name. Official decision-maker + scraped email."""
    master = pd.read_csv(master_path, low_memory=False)
    if "ceo_name" not in master.columns:
        return
    for col in ("primary_contact_name", "primary_contact_title", "primary_contact_email"):
        if col not in master.columns:
            master[col] = pd.NA

    updated = 0
    matched_n = 0
    for cu, grp in master.groupby("cu_number", sort=False):
        ceo = grp["ceo_name"].dropna().astype(str)
        ceo = ceo.iloc[0].strip() if not ceo.empty and ceo.iloc[0].strip() else None
        if not ceo:
            continue
        emails = [e for e in grp["contact_email"].dropna().astype(str).str.lower().tolist() if "@" in e]
        matched = match_email_by_name(ceo, sorted(set(emails)))
        idx = grp.index
        master.loc[idx, "primary_contact_name"] = ceo
        master.loc[idx, "primary_contact_title"] = "CEO/Manager (NCUA Profile)"
        # Only attach an email we can tie to the CEO *by name* — never relabel a
        # generic inbox (info@, memberservices@) as the CEO's address. When there
        # is no name match the CEO email is left blank; the generic inboxes still
        # live in the per-contact rows for whoever wants them.
        master.loc[idx, "primary_contact_email"] = matched
        matched_n += bool(matched)
        updated += 1

    master.to_csv(master_path, index=False)
    print(f"\n[ncua_profile] reconciled CEO onto {updated:,} credit unions "
          f"({matched_n:,} with a name-matched CEO email; the rest have the CEO "
          f"name + a general inbox in the contact rows)")


def _print_summary(cache: pd.DataFrame, n_qualified: int) -> None:
    ok = cache[cache["status"].isin(["ok", "no_website"])]
    n = len(ok)
    web = int(ok["website"].notna().sum())
    ceo = int(ok["ceo_name"].notna().sum())
    print("\n[ncua_profile] summary")
    print(f"  asset-qualified CUs:   {n_qualified:,}")
    print(f"  profiles fetched:      {len(cache):,}")
    if n:
        print(f"  with website:          {web:,}  ({web / n:.0%})")
        print(f"  with CEO name:         {ceo:,}  ({ceo / n:.0%})")
    print(f"  cache → {PROFILE_CACHE}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Pull NCUA credit-union Profiles (website + CEO)")
    p.add_argument("--limit", type=int, default=None, help="cap CUs fetched (smallest-asset first)")
    p.add_argument("--refresh", action="store_true", help="ignore cache, re-fetch")
    args = p.parse_args()
    clean = pd.read_parquet(config.NCUA_CLEAN_PARQUET)
    enrich(clean, limit=args.limit, refresh=args.refresh)
