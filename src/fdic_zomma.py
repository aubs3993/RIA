"""Score each bank 1-5 as a Zomma outbound target ("Zomma Priority", 5 = best).

This is the FDIC analogue of src/zomma_priority.py. The RIA thesis is ported to
bank fields the same shape, same weights, same 1-5 percentile buckets, same
Fit / Segment / RPA-capable extras, but with bank analogs for the two RIA
signals that don't exist in FDIC data:

  RIA component (weight)            ->  FDIC analog
  ---------------------------------     -----------------------------------------
  service complexity      (0.30)    ->  branch footprint: more offices = more
                                        locations/cores/portals to reconcile =
                                        more manual cross-system data entry.
  size: smaller is better (0.35)    ->  smallness of total assets (ICP band).
  contact reachability    (0.20)    ->  identical (scraped emails + named people).
  ops intensity           (0.15)    ->  thinness per branch: small assets-per-
                                        office = each branch carries more manual
                                        back-office overhead per dollar.

There is no per-institution employee or client count in FDIC data, so the RIA
"clients per employee" density is replaced by assets-per-branch thinness.

Writes "Zomma Priority", "Zomma Fit", "Zomma Segment", "Likely RPA-capable"
onto the FDIC master (bank-level, repeated across each bank's contact rows).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from src.utils import get_logger
# Reuse the RIA scorer's primitives so the bucketing/smallness/contact math is identical.
from src.zomma_priority import _bucket, _log_smallness, contact_scores

log = get_logger("fdic_zomma", config.LOG_DIR / "pipeline.log")

# Tuned 2026-06-15 (the "Balanced" profile): footprint and asset-size are
# anti-correlated for banks (~-0.73 — bigger banks have more branches), so the
# branch-complexity signal is dialed back below the size signal to keep the
# center of mass on smaller "too small for RPA" banks rather than drifting into
# the larger, already-RPA-capable end.
WEIGHTS = {"footprint": 0.30, "size": 0.35, "contact": 0.20, "ops": 0.15}

# Branch count at which the footprint score saturates to 1.0. A bank running
# ~25+ offices is a "fully distributed back office"; a single-branch bank scores 0.
FOOTPRINT_SAT = 25.0

ASSET_LO, ASSET_HI = 50e6, 10e9          # ICP band (dollars); smaller is better
# Assets-per-branch band for the ops/thinness signal. $15M/branch (very thin,
# lots of manual overhead per dollar) -> 1.0; $500M/branch -> 0.0.
PER_BRANCH_LO, PER_BRANCH_HI = 15e6, 500e6

# "Likely RPA-capable" = big + many branches: plausibly already running UiPath.
# Great product fit, but Phase-2 displacement plays for a pre-seed team.
RPA_ASSET_MIN = 5.0e9
RPA_OFFICES_MIN = 40
GOOD_FIT_MIN = 4


def compute(master: pd.DataFrame) -> pd.DataFrame:
    """Return a bank-level frame: cert_number, the four components, composite,
    Zomma Priority/Fit/Segment/RPA-capable — plus inputs, for inspection."""
    firms = contact_scores(master, "cert_number")

    asset = pd.to_numeric(firms["asset_total"], errors="coerce")
    offices = pd.to_numeric(firms["offices"], errors="coerce").fillna(1).clip(lower=1)

    # 1) Branch footprint — more offices = more back-office surface area.
    firms["s_footprint"] = (np.log(offices) / np.log(FOOTPRINT_SAT)).clip(0.0, 1.0)

    # 2) Size fit — smaller assets are better.
    firms["s_size"] = _log_smallness(asset.fillna(ASSET_HI), ASSET_LO, ASSET_HI)

    # 3) Contact richness (s_contact) — computed in contact_scores() above.

    # 4) Ops thinness — small assets-per-branch = more manual overhead per dollar.
    per_branch = (asset / offices).replace([np.inf, -np.inf], np.nan)
    firms["s_ops"] = _log_smallness(per_branch.fillna(PER_BRANCH_HI), PER_BRANCH_LO, PER_BRANCH_HI)

    firms["composite"] = (
        WEIGHTS["footprint"] * firms["s_footprint"]
        + WEIGHTS["size"] * firms["s_size"]
        + WEIGHTS["contact"] * firms["s_contact"]
        + WEIGHTS["ops"] * firms["s_ops"]
    )
    firms["Zomma Priority"] = _bucket(firms["composite"].rank(pct=True)).astype(int)
    # Guard: don't rank a bank we can't even email as a top "call-first" target.
    firms.loc[(firms["Zomma Priority"] == 5) & (firms["n_contacts"] == 0), "Zomma Priority"] = 4

    # ---- Zomma Fit: size-NEUTRAL product fit (drop the size weight) ----------
    denom = WEIGHTS["footprint"] + WEIGHTS["contact"] + WEIGHTS["ops"]
    firms["fit_raw"] = (
        WEIGHTS["footprint"] * firms["s_footprint"]
        + WEIGHTS["contact"] * firms["s_contact"]
        + WEIGHTS["ops"] * firms["s_ops"]
    ) / denom
    firms["Zomma Fit"] = _bucket(firms["fit_raw"].rank(pct=True)).astype(int)

    # ---- Likely RPA-capable: big + many branches (UiPath-displacement) -------
    firms["Likely RPA-capable"] = (asset >= RPA_ASSET_MIN) & (offices >= RPA_OFFICES_MIN)

    # ---- Zomma Segment: go-to-market sequencing ------------------------------
    reachable = firms["n_contacts"] > 0
    if "primary_contact_email" in firms.columns:
        reachable = reachable | firms["primary_contact_email"].notna()
    good_fit = firms["Zomma Fit"] >= GOOD_FIT_MIN
    seg = pd.Series("Low-fit", index=firms.index)
    seg[good_fit & ~firms["Likely RPA-capable"]] = "Beachhead"
    seg[good_fit & firms["Likely RPA-capable"]] = "Expansion (UiPath)"
    seg[~reachable] = "Low-info"   # no email/contact -> can't reach/assess
    firms["Zomma Segment"] = seg
    return firms


def run(master_path) -> None:
    master = pd.read_csv(master_path, low_memory=False)
    firms = compute(master)

    new_cols = ["Zomma Priority", "Zomma Fit", "Zomma Segment", "Likely RPA-capable"]
    master = master.drop(columns=[c for c in new_cols if c in master.columns])
    master = master.merge(firms[["cert_number", *new_cols]], on="cert_number", how="left")
    master["Zomma Priority"] = master["Zomma Priority"].astype("Int64")
    master["Zomma Fit"] = master["Zomma Fit"].astype("Int64")
    master.to_csv(master_path, index=False)

    print(f"=== Zomma scoring over {len(firms):,} banks ===\n")
    pri = firms["Zomma Priority"].value_counts()
    fit = firms["Zomma Fit"].value_counts()
    print("level   Priority(now)   Fit(size-neutral)")
    for p in [5, 4, 3, 2, 1]:
        print(f"  {p}       {int(pri.get(p, 0)):>6}        {int(fit.get(p, 0)):>6}")
    print("\nSegment distribution:")
    for seg, n in firms["Zomma Segment"].value_counts().items():
        print(f"  {seg:<20} {n:>5}")
    print(f"\nLikely RPA-capable (Phase-2 displacement universe): "
          f"{int(firms['Likely RPA-capable'].sum())}")

    top = firms[firms["Zomma Priority"] == 5].sort_values("composite", ascending=False)
    print(f"\n=== Top Zomma-Priority-5 banks: {len(top)} ===")
    for _, r in top.head(15).iterrows():
        print(f"  Pri{r['Zomma Priority']}/Fit{r['Zomma Fit']} | "
              f"{str(r['firm_legal_name'])[:32]:<32} "
              f"${r['asset_total']/1e6:>7,.0f}M  off={int(r['offices']):<4} "
              f"contacts={int(r['n_contacts']):<2} [{r['office_state']}]")
    print(f"\nWrote {new_cols} -> {master_path}")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Score FDIC banks by Zomma Priority")
    p.add_argument("master", nargs="?", default=None, help="path to the FDIC master CSV")
    args = p.parse_args()
    master_path = args.master
    if master_path is None:
        cands = sorted(config.FDIC_ENRICHED_DIR.glob("fdic_targets_*.csv"))
        if not cands:
            raise SystemExit("No FDIC master found — run the scrape first.")
        master_path = cands[-1]
    run(master_path)
