r"""NCUA orchestrator: run the credit-union pipeline end-to-end.

Mirrors fdic_main.py, with one extra stage (2b) because NCUA data carries no
website — sites must be discovered before scraping. Stages:
  1+2. download + parse the latest 5300 Call Report quarter
  2b.  discover credit-union websites (domain-guess + verify)
  3.   filter to the CU ICP                 -> cus_targeted.csv
  4.   scrape websites for emails           (reuses src.scrape_websites)
  5.   primary-contact cascade              (reuses src.scrape_primary_contact)
  6.   Zomma Priority for credit unions      (src.ncua_zomma)

Examples (Windows PowerShell):
    python .\ncua_main.py --limit 50          # smoke test
    python .\ncua_main.py                      # full run (latest quarter)
    python .\ncua_main.py --skip-fetch         # reuse cached clean parquet
    python .\ncua_main.py --state TX
    python .\ncua_main.py --skip-scrape        # stop after Stage 3
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime

import pandas as pd

import config
from src.utils import get_logger
from src import ncua_fetch, ncua_profile, ncua_zomma
from src.ncua_filter import filter_cus
from src.scrape_websites import scrape_websites
from src import scrape_primary_contact

log = get_logger("ncua_main", config.LOG_DIR / "pipeline.log")


def run(skip_fetch: bool, skip_discover: bool, skip_scrape: bool, skip_primary: bool,
        limit: int | None, state: str | None, quarter: str | None) -> int:
    config.ensure_ncua_dirs()
    log.info("NCUA pipeline start (skip_fetch=%s skip_discover=%s skip_scrape=%s limit=%s state=%s)",
             skip_fetch, skip_discover, skip_scrape, limit, state)

    # --- Stage 1+2: fetch + parse
    if skip_fetch:
        if not config.NCUA_CLEAN_PARQUET.exists():
            log.error("--skip-fetch set but %s missing — run a fetch first.", config.NCUA_CLEAN_PARQUET)
            return 1
        clean = pd.read_parquet(config.NCUA_CLEAN_PARQUET)
        log.info("Using cached clean parquet: %d credit unions", len(clean))
    else:
        clean = ncua_fetch.run(quarter=quarter)
        if clean.empty:
            return 1

    icp = config.DEFAULT_CU_ICP
    if state:
        icp = replace(icp, states=[state])

    # --- Stage 2b: pull official Profiles (website + CEO + phone) from NCUA.
    # NCUA's Research API returns these for ~95% of CUs; the name-guess fallback
    # (src.ncua_discover_sites) remains available standalone for the remainder.
    enriched = ncua_profile.enrich(clean, icp, limit=0 if skip_discover else limit)

    # --- Stage 3: filter to ICP (website gate now has data)
    targeted = filter_cus(enriched, icp)
    if targeted.empty:
        print("No credit unions passed the ICP (need discovered websites) — nothing to scrape.")
        return 0

    # --- Stage 4: scrape websites
    if skip_scrape:
        print("\nScrape stage skipped — see cus_targeted.csv for the ICP-filtered credit unions.")
        return 0

    master_path = config.NCUA_ENRICHED_DIR / f"ncua_targets_{datetime.now():%Y%m%d}.csv"
    scrape_websites(
        targeted_csv=config.NCUA_TARGETED_CSV,
        output_path=master_path,
        limit=limit,
    )

    # --- Stage 5: primary-contact cascade, then reconcile the NCUA CEO on top
    if not skip_primary:
        scrape_primary_contact.run(master_path=master_path, limit=limit, export_xlsx=False)
    # Prefer NCUA's official CEO as the primary contact, matched to a scraped email.
    ncua_profile.reconcile_ceo(master_path)

    # --- Stage 6: Zomma Priority
    ncua_zomma.run(master_path)

    print("\n========== NCUA PIPELINE SUMMARY ==========")
    print(f"  clean credit unions: {len(clean):,}")
    print(f"  targeted (post-ICP): {len(targeted):,}")
    print(f"  master:              {master_path}")
    print("===========================================\n")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NCUA credit-union targeting pipeline")
    p.add_argument("--skip-fetch", action="store_true", help="reuse cached clean parquet")
    p.add_argument("--skip-discover", action="store_true", help="reuse cached NCUA Profiles only (no new lookups)")
    p.add_argument("--skip-scrape", action="store_true", help="stop after Stage 3 (filter)")
    p.add_argument("--skip-primary", action="store_true", help="skip the primary-contact cascade")
    p.add_argument("--limit", type=int, default=None, help="cap CUs discovered+scraped (smallest-first)")
    p.add_argument("--state", default=None, help="restrict to one state (e.g. TX)")
    p.add_argument("--quarter", default=None, help="NCUA quarter YYYY-MM (default = latest)")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(run(args.skip_fetch, args.skip_discover, args.skip_scrape, args.skip_primary,
                 args.limit, args.state, args.quarter))
