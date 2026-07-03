"""Stage 3: filter the clean firm table to the configured ICP.

Computes a `match_score` in [0, 1] favouring AUM near the midpoint of the band
and HNW concentration above the threshold. Writes data/processed/firms_targeted.csv
and prints a summary.
"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

import config
from config import ICP, DEFAULT_ICP
from src.fdic_filter import state_mask, website_mask   # shared ICP gate math
from src.utils import get_logger

log = get_logger("filter_firms", config.LOG_DIR / "pipeline.log")


def _aum_centrality(aum: float, lo: float, hi: float) -> float:
    """1.0 at the geometric midpoint of [lo, hi], decaying toward the edges.

    Geometric (log-space) midpoint is more meaningful for AUM than arithmetic.
    Returns 0 if outside the band, NaN if AUM is missing.
    """
    if aum is None or (isinstance(aum, float) and math.isnan(aum)):
        return float("nan")
    if aum <= 0 or aum < lo or aum > hi:
        return 0.0
    log_lo, log_hi = math.log(lo), math.log(hi)
    log_mid = (log_lo + log_hi) / 2.0
    half_range = (log_hi - log_lo) / 2.0
    if half_range == 0:
        return 1.0
    distance = abs(math.log(aum) - log_mid) / half_range  # 0 at midpoint, 1 at edges
    return max(0.0, 1.0 - distance)


def _hnw_aum_score(hnw_aum: float | None, min_hnw_aum: int) -> float:
    """Log-scale lift over the HNW-AUM threshold. 10x threshold → 1.0."""
    if hnw_aum is None or (isinstance(hnw_aum, float) and math.isnan(hnw_aum)) or hnw_aum <= 0:
        return 0.0
    if min_hnw_aum <= 0:
        return 1.0
    if hnw_aum < min_hnw_aum:
        return 0.0
    return min(1.0, math.log10(hnw_aum / min_hnw_aum) / math.log10(10))


def _hnw_count_score(hnw_clients: float | None, min_hnw_clients: int) -> float:
    """Linear lift; 3x threshold → 1.0."""
    if hnw_clients is None or (isinstance(hnw_clients, float) and math.isnan(hnw_clients)):
        return 0.0
    if min_hnw_clients <= 0:
        return 1.0
    return min(1.0, float(hnw_clients) / (3.0 * min_hnw_clients))


def filter_firms(df: pd.DataFrame, icp: ICP = DEFAULT_ICP, output_path: Path | None = None) -> pd.DataFrame:
    config.ensure_dirs()
    output_path = output_path or config.TARGETED_CSV
    n_in = len(df)

    f = df.copy()

    # Convert masks defensively — missing columns are treated as "not matching"
    # except where the ICP is permissive about it.
    aum_ok = f.get("aum_total", pd.Series(pd.NA, index=f.index)).between(icp.aum_min, icp.aum_max, inclusive="both")
    emp_ok = f.get("employee_count", pd.Series(pd.NA, index=f.index)).between(icp.min_employees, icp.max_employees, inclusive="both")

    # HNW gates: NaN must NOT pass — fillna with 0 then compare ge(threshold).
    hnw_aum = pd.to_numeric(f.get("hnw_aum_dollars", pd.Series(pd.NA, index=f.index)), errors="coerce").fillna(0)
    hnw_cnt = pd.to_numeric(f.get("hnw_clients", pd.Series(pd.NA, index=f.index)), errors="coerce").fillna(0)
    hnw_ok = hnw_aum.ge(icp.min_hnw_aum) & hnw_cnt.ge(icp.min_hnw_clients)

    state_ok = state_mask(f, icp.states)
    website_ok = website_mask(f, icp.exclude_no_website)

    mask = aum_ok & emp_ok & hnw_ok & state_ok & website_ok
    out = f[mask].copy()

    # Score: 50% total-AUM centrality + 35% HNW-AUM lift + 15% HNW-count lift
    aum_score = out["aum_total"].apply(lambda v: _aum_centrality(v, icp.aum_min, icp.aum_max))
    hnw_aum_s = out["hnw_aum_dollars"].apply(lambda v: _hnw_aum_score(v, icp.min_hnw_aum))
    hnw_cnt_s = out["hnw_clients"].apply(lambda v: _hnw_count_score(v, icp.min_hnw_clients))
    out["match_score"] = (
        0.50 * aum_score.fillna(0)
        + 0.35 * hnw_aum_s
        + 0.15 * hnw_cnt_s
    ).clip(0, 1).round(4)

    out = out.sort_values("match_score", ascending=False)

    out.to_csv(output_path, index=False)
    log.info("Wrote %d targeted firms → %s", len(out), output_path)

    _print_summary(n_in, out, icp, output_path)
    return out


def _print_summary(n_in: int, out: pd.DataFrame, icp: ICP, output_path: Path) -> None:
    print("\n[filter_firms] summary")
    print(f"  input firms:       {n_in:,}")
    print(f"  passed ICP filter: {len(out):,}")
    print(f"  ICP: aum=[{icp.aum_min:,}–{icp.aum_max:,}]  emp=[{icp.min_employees}–{icp.max_employees}]  "
          f"hnw_aum>=${icp.min_hnw_aum:,}  hnw_clients>={icp.min_hnw_clients}  states={icp.states or 'ALL'}")

    if out.empty:
        print("  (no firms matched — loosen ICP and try again)")
        return

    if "office_state" in out.columns:
        top_states = out["office_state"].fillna("(unknown)").value_counts().head(10)
        print("\n  top 10 states:")
        for state, n in top_states.items():
            print(f"    {state:<6} {n:>5}")

    if "aum_total" in out.columns:
        aum = out["aum_total"].dropna()
        if not aum.empty:
            print("\n  Total AUM distribution (USD):")
            for label, q in [("min", aum.min()), ("p25", aum.quantile(0.25)),
                             ("median", aum.median()), ("p75", aum.quantile(0.75)),
                             ("max", aum.max())]:
                print(f"    {label:<6} ${q:,.0f}")

    if "hnw_aum_dollars" in out.columns:
        hnw_aum = pd.to_numeric(out["hnw_aum_dollars"], errors="coerce").dropna()
        if not hnw_aum.empty:
            print("\n  HNW AUM distribution (USD):")
            for label, q in [("min", hnw_aum.min()), ("p25", hnw_aum.quantile(0.25)),
                             ("median", hnw_aum.median()), ("p75", hnw_aum.quantile(0.75)),
                             ("max", hnw_aum.max())]:
                print(f"    {label:<6} ${q:,.0f}")

    if "hnw_clients" in out.columns:
        hnw_cnt = pd.to_numeric(out["hnw_clients"], errors="coerce").dropna()
        if not hnw_cnt.empty:
            print("\n  HNW client count distribution:")
            for label, q in [("min", hnw_cnt.min()), ("p25", hnw_cnt.quantile(0.25)),
                             ("median", hnw_cnt.median()), ("p75", hnw_cnt.quantile(0.75)),
                             ("max", hnw_cnt.max())]:
                print(f"    {label:<6} {q:,.0f}")

    print(f"\n  → {output_path}")


if __name__ == "__main__":
    df = pd.read_parquet(config.CLEAN_PARQUET)
    filter_firms(df)
