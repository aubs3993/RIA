"""NCUA Stage 1+2: download a 5300 Call Report quarter and parse it clean.

Unlike FDIC there is no live API — this is a quarterly ZIP of comma-delimited
text files keyed on CU_NUMBER. We:

  * auto-detect the latest published quarter (YYYY-MM ZIP),
  * read the profile (FOICU.txt) and the financial schedules we need
    (FS220.txt: total assets + members; FS220A.txt: employees),
  * count branches per CU from "Credit Union Branch Information.txt",
  * emit a clean parquet whose columns match the shared scraper schema
    (`cu_number` key, `firm_legal_name`, `office_state`, `asset_total`, ...).

`website` is intentionally left blank here — NCUA data carries no URL. Run
src.ncua_profile next (NCUA Profile API, ~95% coverage) to populate it before
scraping; src.ncua_discover_sites is a standalone name-guess fallback for the
remainder.

Run standalone:
    python -m src.ncua_fetch                 # latest quarter (download if needed)
    python -m src.ncua_fetch --quarter 2026-03
    python -m src.ncua_fetch --zip data/ncua/raw/call-report-data-2026-03.zip
"""
from __future__ import annotations

import argparse
import io
import zipfile
from datetime import date, datetime
from pathlib import Path

import httpx
import pandas as pd

import config
from src.utils import get_logger

log = get_logger("ncua_fetch", config.LOG_DIR / "pipeline.log")

PROFILE_FILE = "FOICU.txt"
BRANCH_FILE = "Credit Union Branch Information.txt"
FS_MAIN = "FS220.txt"        # ACCT_010 (assets), ACCT_083 (members)
FS_EMP = "FS220A.txt"        # ACCT_564A/B (employees)


# ---------- download ----------------------------------------------------------


def _candidate_quarters(n: int = 5) -> list[str]:
    """The `n` most recent quarter-end YYYY-MM strings, newest first.

    NCUA posts a quarter a month or two after it closes, so we walk back from the
    most recent quarter-end (03/06/09/12) on or before today and try each until
    one downloads.
    """
    today = date.today()
    pairs: list[tuple[int, int]] = []
    yy = today.year
    while len(pairs) < n + 4:
        for mm in (12, 9, 6, 3):
            if (yy, mm) <= (today.year, today.month):
                pairs.append((yy, mm))
        yy -= 1
    pairs = sorted(set(pairs), reverse=True)[:n]
    return [f"{yy}-{mm:02d}" for yy, mm in pairs]


def download_quarter(quarter: str | None = None) -> Path:
    """Download a quarter's ZIP to data/ncua/raw and return its path. If
    `quarter` (YYYY-MM) is None, try the most recent quarters until one works.
    Reuses an already-downloaded ZIP if present."""
    config.ensure_ncua_dirs()
    quarters = [quarter] if quarter else _candidate_quarters()

    headers = {"User-Agent": config.USER_AGENT}
    with httpx.Client(headers=headers, timeout=120.0, follow_redirects=True) as client:
        for q in quarters:
            dest = config.NCUA_RAW_DIR / f"call-report-data-{q}.zip"
            if dest.exists() and zipfile.is_zipfile(dest):
                log.info("Using cached quarter %s: %s", q, dest)
                return dest
            url = config.NCUA_QUARTERLY_URL.format(ym=q)
            try:
                resp = client.get(url)
            except Exception as exc:
                log.debug("download failed for %s: %s", q, exc)
                continue
            if resp.status_code != 200 or not resp.content[:2] == b"PK":
                log.debug("quarter %s not available (status %s)", q, resp.status_code)
                continue
            dest.write_bytes(resp.content)
            log.info("Downloaded quarter %s (%.1f MB) → %s", q, len(resp.content) / 1e6, dest)
            return dest
    raise RuntimeError(f"Could not download any NCUA quarter from {quarters}")


# ---------- parse -------------------------------------------------------------


def _read(z: zipfile.ZipFile, name: str, usecols=None) -> pd.DataFrame:
    with z.open(name) as f:
        return pd.read_csv(io.TextIOWrapper(f, encoding="latin-1"),
                           dtype=str, usecols=usecols, low_memory=False)


def parse_zip(zip_path: Path) -> pd.DataFrame:
    """Read the ZIP into one clean row per credit union."""
    with zipfile.ZipFile(zip_path) as z:
        profile = _read(z, PROFILE_FILE)
        fs_main = _read(z, FS_MAIN, usecols=["CU_NUMBER", config.NCUA_ACCT_ASSETS,
                                             config.NCUA_ACCT_MEMBERS])
        fs_emp = _read(z, FS_EMP, usecols=["CU_NUMBER", config.NCUA_ACCT_FT_EMP,
                                           config.NCUA_ACCT_PT_EMP])
        try:
            branches = _read(z, BRANCH_FILE, usecols=["CU_NUMBER"])
            branch_counts = branches.groupby("CU_NUMBER").size().rename("offices")
        except Exception as exc:
            log.warning("branch file unreadable (%s) — offices will be 1", exc)
            branch_counts = pd.Series(dtype="int64", name="offices")

    df = profile.merge(fs_main, on="CU_NUMBER", how="left").merge(fs_emp, on="CU_NUMBER", how="left")
    df = df.merge(branch_counts, left_on="CU_NUMBER", right_index=True, how="left")
    return _to_clean(df)


def _to_clean(df: pd.DataFrame) -> pd.DataFrame:
    def col(name):
        return df[name] if name in df.columns else pd.Series([None] * len(df), index=df.index)

    def num(name):
        return pd.to_numeric(col(name), errors="coerce")

    ft = num(config.NCUA_ACCT_FT_EMP).fillna(0)
    pt = num(config.NCUA_ACCT_PT_EMP).fillna(0)

    out = pd.DataFrame(index=df.index)
    out["cu_number"] = pd.to_numeric(col("CU_NUMBER"), errors="coerce").astype("Int64")
    out["firm_legal_name"] = col("CU_NAME").astype("string").str.replace(r"\s+", " ", regex=True).str.strip()
    out["website"] = pd.NA   # not in NCUA data — populated by ncua_profile (fallback: ncua_discover_sites)
    out["office_street"] = col("STREET").astype("string").str.strip()
    out["office_city"] = col("CITY").astype("string").str.strip()
    out["office_state"] = col("STATE").astype("string").str.strip()
    out["office_zip"] = col("ZIP_CODE").astype("string").str.strip().str[:5]
    out["charter_state"] = col("CharterState").astype("string").str.strip()
    out["cu_type"] = col("CU_TYPE").astype("string").str.strip()
    out["year_opened"] = pd.to_numeric(col("YEAR_OPENED"), errors="coerce").astype("Int64")
    out["asset_total"] = num(config.NCUA_ACCT_ASSETS)        # already dollars
    out["members"] = num(config.NCUA_ACCT_MEMBERS).astype("Int64")
    out["employee_count"] = (ft + pt).astype("Int64")
    out["offices"] = pd.to_numeric(col("offices"), errors="coerce").fillna(1).clip(lower=1).astype("Int64")
    return out


def run(quarter: str | None = None, zip_path: Path | None = None) -> pd.DataFrame:
    config.ensure_ncua_dirs()
    zp = Path(zip_path) if zip_path else download_quarter(quarter)
    clean = parse_zip(zp)
    clean.to_parquet(config.NCUA_CLEAN_PARQUET, index=False)
    _print_summary(clean, zp)
    return clean


def _print_summary(clean: pd.DataFrame, zp: Path) -> None:
    n = len(clean)
    assets = clean["asset_total"].dropna()
    print("\n[ncua_fetch] summary")
    print(f"  source quarter ZIP:  {zp.name}")
    print(f"  credit unions:       {n:,}")
    print(f"  with members + emp:  {int((clean['members'].notna() & clean['employee_count'].notna()).sum()):,}")
    if not assets.empty:
        print("  total assets (USD):")
        for label, q in [("min", assets.min()), ("p25", assets.quantile(0.25)),
                         ("median", assets.median()), ("p75", assets.quantile(0.75)),
                         ("max", assets.max())]:
            print(f"    {label:<6} ${q:,.0f}")
    print(f"  clean → {config.NCUA_CLEAN_PARQUET}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Fetch + parse an NCUA 5300 Call Report quarter")
    p.add_argument("--quarter", default=None, help="YYYY-MM (e.g. 2026-03); default = latest")
    p.add_argument("--zip", dest="zip_path", default=None, help="parse a local ZIP instead of downloading")
    args = p.parse_args()
    run(quarter=args.quarter, zip_path=args.zip_path)
