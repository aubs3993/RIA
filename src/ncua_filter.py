"""NCUA Stage 3: filter the clean credit-union table to the CU ICP.

Mirrors fdic_filter. Runs AFTER site discovery (so the website gate has data to
act on). `match_score` favours small CUs so `--limit` scrapes the most on-thesis
credit unions first.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

import config
from config import CreditUnionICP, DEFAULT_CU_ICP
from src.fdic_filter import _asset_smallness   # identical smallness math
from src.utils import domain_of, get_logger

log = get_logger("ncua_filter", config.LOG_DIR / "pipeline.log")


def filter_cus(df: pd.DataFrame, icp: CreditUnionICP = DEFAULT_CU_ICP,
               output_path: Path | None = None) -> pd.DataFrame:
    config.ensure_ncua_dirs()
    output_path = output_path or config.NCUA_TARGETED_CSV
    n_in = len(df)
    f = df.copy()

    asset_ok = pd.to_numeric(f.get("asset_total", pd.Series(pd.NA, index=f.index)),
                             errors="coerce").between(icp.asset_min, icp.asset_max, inclusive="both")

    if icp.states:
        states_upper = {s.upper() for s in icp.states}
        state_ok = f.get("office_state", pd.Series("", index=f.index)).fillna("").str.upper().isin(states_upper)
    else:
        state_ok = pd.Series(True, index=f.index)

    website_series = f.get("website", pd.Series(pd.NA, index=f.index)).astype("string")
    website_present = website_series.notna() & website_series.str.len().gt(0)
    blocklist = {d.lower() for d in getattr(config, "SOCIAL_URL_BLOCKLIST", [])}
    website_host = website_series.fillna("").map(lambda u: domain_of(u) or "")
    website_present = website_present & ~website_host.isin(blocklist)
    website_ok = website_present if icp.exclude_no_website else pd.Series(True, index=f.index)

    out = f[asset_ok & state_ok & website_ok].copy()
    out["match_score"] = out["asset_total"].apply(
        lambda v: _asset_smallness(v, icp.asset_min, icp.asset_max)
    ).fillna(0).clip(0, 1).round(4)
    out = out.sort_values("match_score", ascending=False).reset_index(drop=True)

    out.to_csv(output_path, index=False)
    log.info("Wrote %d targeted credit unions → %s", len(out), output_path)
    _print_summary(n_in, out, icp, output_path)
    return out


def _print_summary(n_in: int, out: pd.DataFrame, icp: CreditUnionICP, output_path: Path) -> None:
    print("\n[ncua_filter] summary")
    print(f"  input credit unions: {n_in:,}")
    print(f"  passed ICP filter:   {len(out):,}")
    print(f"  ICP: asset=[${icp.asset_min:,}–${icp.asset_max:,}]  "
          f"states={icp.states or 'ALL'}  website_required={icp.exclude_no_website}")
    if out.empty:
        print("  (no credit unions matched — discover more sites or loosen ICP)")
        return
    if "office_state" in out.columns:
        print("\n  top 10 states:")
        for state, n in out["office_state"].fillna("(unknown)").value_counts().head(10).items():
            print(f"    {state:<6} {n:>5}")
    for label, colname, money in [("Total assets", "asset_total", True),
                                  ("Members", "members", False),
                                  ("Employees", "employee_count", False),
                                  ("Branches", "offices", False)]:
        s = pd.to_numeric(out[colname], errors="coerce").dropna() if colname in out.columns else pd.Series([], dtype=float)
        if s.empty:
            continue
        print(f"\n  {label} distribution:")
        for lab, q in [("min", s.min()), ("median", s.median()), ("p75", s.quantile(0.75)), ("max", s.max())]:
            print(f"    {lab:<6} {('$' if money else '') + format(q, ',.0f')}")
    print(f"\n  → {output_path}")


if __name__ == "__main__":
    df = pd.read_parquet(config.NCUA_CLEAN_PARQUET)
    filter_cus(df)
