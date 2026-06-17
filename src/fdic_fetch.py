"""FDIC Stage 1+2: pull active FDIC-insured institutions and clean them.

Unlike the SEC Form ADV flow there is nothing to download-and-unzip and no HTML
to scrape here: the FDIC BankFind Suite exposes a clean public JSON API. We page
through every active institution, write the raw rows for reproducibility, and
emit a clean parquet whose column names match the shared scraper schema
(`firm_legal_name`, `website`, `office_state`, ...) so the website and
primary-contact scrapers can be reused verbatim.

Run standalone:
    python -m src.fdic_fetch                  # all active institutions
    python -m src.fdic_fetch --state TX       # one state
    python -m src.fdic_fetch --limit 200      # cap (smoke test)
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd

import config
from src.utils import get_logger, normalize_url

log = get_logger("fdic_fetch", config.LOG_DIR / "pipeline.log")

INSTITUTIONS_URL = f"{config.FDIC_API_BASE}/institutions"
PAGE_SIZE = 1000               # API allows up to 10k; 1k pages are gentle + safe
PER_PAGE_DELAY_SECONDS = 0.5   # politeness between pages


def fetch_institutions(
    active_only: bool = True,
    state: str | None = None,
    limit: int | None = None,
    page_size: int = PAGE_SIZE,
) -> pd.DataFrame:
    """Page through the institutions endpoint and return one row per bank (raw
    FDIC field names). `state` is a two-letter code (STALP); `limit` caps the
    total rows pulled (for smoke tests)."""
    clauses = []
    if active_only:
        clauses.append("ACTIVE:1")
    if state:
        clauses.append(f"STALP:{state.upper()}")
    filters = " AND ".join(clauses) if clauses else None

    headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    rows: list[dict] = []
    offset = 0
    total = None

    with httpx.Client(headers=headers, timeout=30.0) as client:
        while True:
            params = {
                "fields": ",".join(config.FDIC_INSTITUTION_FIELDS),
                "limit": page_size,
                "offset": offset,
                "sort_by": "CERT",
                "sort_order": "ASC",
            }
            if filters:
                params["filters"] = filters

            resp = client.get(INSTITUTIONS_URL, params=params, follow_redirects=True)
            resp.raise_for_status()
            payload = resp.json()

            if total is None:
                total = payload.get("meta", {}).get("total", 0)
                log.info("FDIC institutions matching %s: %s", filters or "(all)", f"{total:,}")

            chunk = [item["data"] for item in payload.get("data", [])]
            if not chunk:
                break
            rows.extend(chunk)
            log.info("  fetched %d / %s", len(rows), f"{total:,}")

            offset += page_size
            if (limit is not None and len(rows) >= limit) or offset >= total:
                break
            time.sleep(PER_PAGE_DELAY_SECONDS)

    if limit is not None:
        rows = rows[:limit]
    df = pd.DataFrame(rows)
    log.info("Pulled %d institution rows", len(df))
    return df


def to_clean(raw: pd.DataFrame) -> pd.DataFrame:
    """Map raw FDIC fields to the shared clean schema.

    `cert_number` is the stable per-bank key (the scrapers recognize it).
    ASSET/DEP arrive in $thousands and are converted to dollars here so that
    `asset_total`/`deposits` read like RIA's `aum_total` (plain dollars).
    """
    if raw.empty:
        return raw

    def col(name):
        return raw[name] if name in raw.columns else pd.Series([None] * len(raw), index=raw.index)

    out = pd.DataFrame(index=raw.index)
    out["cert_number"] = pd.to_numeric(col("CERT"), errors="coerce").astype("Int64")
    # Collapse the double spaces FDIC occasionally embeds ("Frost  Bank").
    out["firm_legal_name"] = col("NAME").astype("string").str.replace(r"\s+", " ", regex=True).str.strip()
    out["website"] = col("WEBADDR").map(normalize_url)
    out["office_street"] = col("ADDRESS").astype("string").str.strip()
    out["office_city"] = col("CITY").astype("string").str.strip()
    out["office_state"] = col("STALP").astype("string").str.strip()
    out["office_state_name"] = col("STNAME").astype("string").str.strip()
    out["office_zip"] = col("ZIP").astype("string").str.strip()
    out["asset_total"] = pd.to_numeric(col("ASSET"), errors="coerce") * 1000
    out["deposits"] = pd.to_numeric(col("DEP"), errors="coerce") * 1000
    out["offices"] = pd.to_numeric(col("OFFICES"), errors="coerce").astype("Int64")
    out["bank_class"] = col("BKCLASS").astype("string").str.strip()
    out["established"] = col("ESTYMD").astype("string").str.strip()
    out["roa"] = pd.to_numeric(col("ROA"), errors="coerce")
    out["roe"] = pd.to_numeric(col("ROE"), errors="coerce")
    out["net_income"] = pd.to_numeric(col("NETINC"), errors="coerce") * 1000
    return out


def run(
    active_only: bool = True,
    state: str | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """Fetch → clean → persist raw JSON-ish CSV + clean parquet. Returns clean df."""
    config.ensure_fdic_dirs()

    raw = fetch_institutions(active_only=active_only, state=state, limit=limit)
    if raw.empty:
        log.warning("No institutions returned — nothing written.")
        return raw

    stamp = datetime.now().strftime("%Y%m%d")
    raw_path = config.FDIC_RAW_DIR / f"institutions_{stamp}.csv"
    raw.to_csv(raw_path, index=False)

    clean = to_clean(raw)
    clean.to_parquet(config.FDIC_CLEAN_PARQUET, index=False)

    _print_summary(clean, raw_path)
    return clean


def _print_summary(clean: pd.DataFrame, raw_path: Path) -> None:
    n = len(clean)
    has_site = clean["website"].notna().sum()
    print("\n[fdic_fetch] summary")
    print(f"  institutions pulled:   {n:,}")
    print(f"  with a usable website: {has_site:,}  ({has_site / n:.0%})")
    assets = clean["asset_total"].dropna()
    if not assets.empty:
        print("  total assets (USD):")
        for label, q in [("min", assets.min()), ("p25", assets.quantile(0.25)),
                         ("median", assets.median()), ("p75", assets.quantile(0.75)),
                         ("max", assets.max())]:
            print(f"    {label:<6} ${q:,.0f}")
    print(f"  raw   → {raw_path}")
    print(f"  clean → {config.FDIC_CLEAN_PARQUET}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Fetch FDIC-insured institutions")
    p.add_argument("--state", default=None, help="two-letter state filter (e.g. TX)")
    p.add_argument("--limit", type=int, default=None, help="cap rows pulled (smoke test)")
    p.add_argument("--include-inactive", action="store_true", help="include inactive institutions")
    args = p.parse_args()
    run(active_only=not args.include_inactive, state=args.state, limit=args.limit)
