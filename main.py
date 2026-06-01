r"""Orchestrator: run the four pipeline stages end-to-end.

Examples (Windows PowerShell):
    python .\main.py
    python .\main.py --skip-download
    python .\main.py --skip-scrape
    python .\main.py --limit 50
    python .\main.py --skip-download --limit 25
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

import config
from src.utils import get_logger
from src.download_adv import download_latest
from src.parse_adv import parse_adv
from src.filter_firms import filter_firms
from src.scrape_websites import scrape_websites

log = get_logger("main", config.LOG_DIR / "pipeline.log")


def _latest_data_file() -> Path | None:
    files = [p for p in config.RAW_DIR.iterdir() if p.suffix.lower() in (".xlsx", ".csv")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def run(skip_download: bool, skip_scrape: bool, limit: int | None) -> int:
    config.ensure_dirs()
    log.info("Pipeline start (skip_download=%s skip_scrape=%s limit=%s)",
             skip_download, skip_scrape, limit)

    # --- Stage 1
    if skip_download:
        data_path = _latest_data_file()
        if data_path is None:
            log.error("--skip-download set but no xlsx/csv in %s", config.RAW_DIR)
            return 1
        log.info("Using cached data file: %s", data_path)
        downloaded_rows = None
    else:
        result = download_latest()
        data_path = result.data_path
        downloaded_rows = result.row_count

    # --- Stage 2 (idempotent: re-parses whatever data file is on disk)
    df = parse_adv(data_path)

    # --- Stage 3
    targeted = filter_firms(df, config.DEFAULT_ICP)

    # --- Stage 4
    if skip_scrape:
        log.info("Skipping scrape stage")
        scraped_path = None
        scraped_df = pd.DataFrame()
    else:
        scraped_path = scrape_websites(limit=limit)
        scraped_df = pd.read_csv(scraped_path) if scraped_path.exists() else pd.DataFrame()

    print("\n========== PIPELINE SUMMARY ==========")
    print(f"  downloaded firms (source rows): {downloaded_rows if downloaded_rows is not None else '(cached)'}")
    print(f"  parsed firms (clean parquet):   {len(df):,}")
    print(f"  targeted firms (post-ICP):      {len(targeted):,}")
    if not skip_scrape:
        attempted = limit if limit is not None else len(targeted)
        print(f"  firms scraped (attempted):      {attempted:,}")
        if not scraped_df.empty:
            has_email = scraped_df["contact_email"].notna() & (scraped_df["contact_email"] != "")
            email_rows = scraped_df[has_email]
            print(f"  total output rows:              {len(scraped_df):,}")
            print(f"  rows with an email:             {len(email_rows):,}")
            print(f"  firms kept w/o email:           {(~has_email).sum():,}")
            print("  contacts per source:")
            for src, n in email_rows["email_source"].value_counts().items():
                print(f"    {src:<16} {n:>5}")
            print(f"  output: {scraped_path}")
        else:
            print("  (no rows produced)")
    else:
        print("  scrape stage skipped — see firms_targeted.csv for ICP-filtered firms")
    print("======================================\n")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="RIA targeting pipeline")
    p.add_argument("--skip-download", action="store_true", help="reuse latest xlsx in data/raw")
    p.add_argument("--skip-scrape", action="store_true", help="stop after Stage 3 (filter)")
    p.add_argument("--limit", type=int, default=None, help="cap the number of firms scraped")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(run(args.skip_download, args.skip_scrape, args.limit))
