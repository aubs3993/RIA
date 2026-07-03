"""FDIC Stage 3: filter the clean bank table to the bank ICP.

Mirrors `filter_firms` (the RIA stage). Computes a pre-scrape `match_score` in
[0, 1] that favours SMALL banks (the Zomma thesis: too small to afford RPA, so
more manual back-office pain per head). The score is used only to order the
scrape (`--limit` grabs the most on-thesis banks first); the full Zomma Priority
is computed after scraping, once contact reachability is known.

Writes data/fdic/processed/banks_targeted.csv and prints a summary.
"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

import config
from config import BankICP, DEFAULT_BANK_ICP
from src.utils import domain_of, get_logger

log = get_logger("fdic_filter", config.LOG_DIR / "pipeline.log")


def _asset_smallness(asset: float, lo: float, hi: float) -> float:
    """1.0 at the small end of the band, 0.0 at the large end (log scale).

    Smaller banks are the on-thesis target, so the pre-scrape ordering favours
    them. Returns 0 outside the band, NaN if asset is missing.
    """
    if asset is None or (isinstance(asset, float) and math.isnan(asset)):
        return float("nan")
    if asset <= 0 or asset < lo or asset > hi:
        return 0.0
    log_lo, log_hi = math.log(lo), math.log(hi)
    if log_hi == log_lo:
        return 1.0
    return 1.0 - (math.log(asset) - log_lo) / (log_hi - log_lo)


def state_mask(df: pd.DataFrame, states: list[str] | None) -> pd.Series:
    """True where office_state is in `states`; all True when no state filter.

    Shared by all three source filters (RIA/FDIC/NCUA) so the state gate cannot
    drift between them.
    """
    if not states:
        return pd.Series(True, index=df.index)
    states_upper = {s.upper() for s in states}
    return df.get("office_state", pd.Series("", index=df.index)).fillna("").str.upper().isin(states_upper)


def website_mask(df: pd.DataFrame, exclude_no_website: bool) -> pd.Series:
    """True where the firm has a usable website; all True when the ICP does not
    require one. Social-media URLs count as "no website" — they never yield
    advisor emails. Shared by all three source filters (RIA/FDIC/NCUA).
    """
    if not exclude_no_website:
        return pd.Series(True, index=df.index)
    website_series = df.get("website", pd.Series("", index=df.index)).astype("string")
    website_present = website_series.notna() & website_series.str.len().gt(0)
    blocklist = {d.lower() for d in getattr(config, "SOCIAL_URL_BLOCKLIST", [])}
    website_host = website_series.fillna("").map(lambda u: domain_of(u) or "")
    return website_present & ~website_host.isin(blocklist)


def filter_banks(df: pd.DataFrame, icp: BankICP = DEFAULT_BANK_ICP,
                 output_path: Path | None = None) -> pd.DataFrame:
    config.ensure_fdic_dirs()
    output_path = output_path or config.FDIC_TARGETED_CSV
    n_in = len(df)

    f = df.copy()

    asset_ok = pd.to_numeric(f.get("asset_total", pd.Series(pd.NA, index=f.index)),
                             errors="coerce").between(icp.asset_min, icp.asset_max, inclusive="both")

    state_ok = state_mask(f, icp.states)
    website_ok = website_mask(f, icp.exclude_no_website)

    mask = asset_ok & state_ok & website_ok
    out = f[mask].copy()

    out["match_score"] = out["asset_total"].apply(
        lambda v: _asset_smallness(v, icp.asset_min, icp.asset_max)
    ).fillna(0).clip(0, 1).round(4)
    out = out.sort_values("match_score", ascending=False).reset_index(drop=True)

    out.to_csv(output_path, index=False)
    log.info("Wrote %d targeted banks → %s", len(out), output_path)
    _print_summary(n_in, out, icp, output_path)
    return out


def _print_summary(n_in: int, out: pd.DataFrame, icp: BankICP, output_path: Path) -> None:
    print("\n[fdic_filter] summary")
    print(f"  input banks:       {n_in:,}")
    print(f"  passed ICP filter: {len(out):,}")
    print(f"  ICP: asset=[${icp.asset_min:,}–${icp.asset_max:,}]  "
          f"states={icp.states or 'ALL'}  website_required={icp.exclude_no_website}")
    if out.empty:
        print("  (no banks matched — loosen ICP and try again)")
        return

    if "office_state" in out.columns:
        print("\n  top 10 states:")
        for state, n in out["office_state"].fillna("(unknown)").value_counts().head(10).items():
            print(f"    {state:<6} {n:>5}")

    assets = pd.to_numeric(out["asset_total"], errors="coerce").dropna()
    if not assets.empty:
        print("\n  Total assets distribution (USD):")
        for label, q in [("min", assets.min()), ("p25", assets.quantile(0.25)),
                         ("median", assets.median()), ("p75", assets.quantile(0.75)),
                         ("max", assets.max())]:
            print(f"    {label:<6} ${q:,.0f}")

    offices = pd.to_numeric(out["offices"], errors="coerce").dropna()
    if not offices.empty:
        print("\n  Branch (office) count distribution:")
        for label, q in [("min", offices.min()), ("p25", offices.quantile(0.25)),
                         ("median", offices.median()), ("p75", offices.quantile(0.75)),
                         ("max", offices.max())]:
            print(f"    {label:<6} {q:,.0f}")

    print(f"\n  → {output_path}")


if __name__ == "__main__":
    df = pd.read_parquet(config.FDIC_CLEAN_PARQUET)
    filter_banks(df)
