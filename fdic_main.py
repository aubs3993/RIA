r"""FDIC orchestrator: run the bank pipeline end-to-end.

Mirrors main.py (the RIA orchestrator). Stages:
  1+2. fetch + clean active FDIC-insured institutions (BankFind API)
  3.   filter to the bank ICP  -> banks_targeted.csv
  4.   scrape bank websites for emails        (reuses src.scrape_websites)
  5.   find each bank's primary finance contact (reuses src.scrape_primary_contact)
  6.   score Zomma Priority for banks          (src.fdic_zomma)

Examples (Windows PowerShell):
    python .\fdic_main.py
    python .\fdic_main.py --skip-fetch
    python .\fdic_main.py --skip-fetch --limit 25
    python .\fdic_main.py --state TX
    python .\fdic_main.py --skip-scrape          # stop after Stage 3
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pandas as pd

import config
from src.utils import get_logger
from src import fdic_fetch, fdic_zomma
from src.fdic_filter import filter_banks
from src.scrape_websites import scrape_websites
from src import scrape_primary_contact

log = get_logger("fdic_main", config.LOG_DIR / "pipeline.log")


def run(skip_fetch: bool, skip_scrape: bool, skip_primary: bool,
        limit: int | None, state: str | None) -> int:
    config.ensure_fdic_dirs()
    log.info("FDIC pipeline start (skip_fetch=%s skip_scrape=%s skip_primary=%s limit=%s state=%s)",
             skip_fetch, skip_scrape, skip_primary, limit, state)

    # --- Stage 1+2: fetch + clean
    if skip_fetch:
        if not config.FDIC_CLEAN_PARQUET.exists():
            log.error("--skip-fetch set but %s missing — run a fetch first.", config.FDIC_CLEAN_PARQUET)
            return 1
        clean = pd.read_parquet(config.FDIC_CLEAN_PARQUET)
        log.info("Using cached clean parquet: %d banks", len(clean))
    else:
        clean = fdic_fetch.run(state=state)
        if clean.empty:
            return 1

    # --- Stage 3: filter to ICP
    icp = config.DEFAULT_BANK_ICP
    if state:
        icp = replace(icp, states=[state])
    targeted = filter_banks(clean, icp)
    if targeted.empty:
        print("No banks passed the ICP — nothing to scrape.")
        return 0

    # --- Stage 4: scrape websites for emails
    if skip_scrape:
        log.info("Skipping scrape stage")
        print("\nScrape stage skipped — see banks_targeted.csv for the ICP-filtered banks.")
        return 0

    master_path = config.FDIC_ENRICHED_DIR / f"fdic_targets_{datetime.now():%Y%m%d}.csv"
    scrape_websites(
        targeted_csv=config.FDIC_TARGETED_CSV,
        output_path=master_path,
        limit=limit,
    )

    # --- Stage 5: primary-contact cascade
    if not skip_primary:
        scrape_primary_contact.run(
            master_path=master_path,
            limit=limit,
            export_xlsx=False,   # the RIA xlsx exporter is RIA-schema specific
        )

    # --- Stage 6: Zomma Priority
    fdic_zomma.run(master_path)

    print("\n========== FDIC PIPELINE SUMMARY ==========")
    print(f"  clean banks:        {len(clean):,}")
    print(f"  targeted (post-ICP):{len(targeted):,}")
    print(f"  master:             {master_path}")
    print("===========================================\n")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="FDIC bank targeting pipeline")
    p.add_argument("--skip-fetch", action="store_true", help="reuse cached clean parquet")
    p.add_argument("--skip-scrape", action="store_true", help="stop after Stage 3 (filter)")
    p.add_argument("--skip-primary", action="store_true", help="skip the primary-contact cascade")
    p.add_argument("--limit", type=int, default=None, help="cap banks scraped (smallest-first)")
    p.add_argument("--state", default=None, help="restrict to one state (e.g. TX)")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(run(args.skip_fetch, args.skip_scrape, args.skip_primary, args.limit, args.state))
