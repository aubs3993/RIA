"""Score each credit union 1-5 as a Zomma outbound target ("Zomma Priority").

The NCUA analogue of src/zomma_priority.py. Credit unions report both members
and employees, so this is the CLOSEST of the three sources to the original RIA
model — the ops signal is genuine members-per-employee density, not a proxy:

  RIA component (weight)            ->  NCUA analog
  ---------------------------------     -----------------------------------------
  service complexity      (0.40)    ->  branch footprint (offices): more branches
                                        = more cores/portals to reconcile.
  size: smaller is better (0.25)    ->  smallness of total assets (ICP band).
  contact reachability    (0.20)    ->  identical (scraped emails + named people).
  ops intensity           (0.15)    ->  members per employee (the RIA "clients per
                                        employee" density, exactly).

Writes "Zomma Priority", "Zomma Fit", "Zomma Segment", "Likely RPA-capable"
onto the NCUA master (CU-level, repeated across each CU's contact rows).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from src.utils import get_logger
from src.zomma_priority import _bucket, _log_smallness

log = get_logger("ncua_zomma", config.LOG_DIR / "pipeline.log")

WEIGHTS = {"footprint": 0.40, "size": 0.25, "contact": 0.20, "ops": 0.15}

FOOTPRINT_SAT = 25.0                  # branch count at which footprint saturates
ASSET_LO, ASSET_HI = 50e6, 5e9        # ICP band (dollars); smaller is better

# "Likely RPA-capable" = big + staffed + many branches: plausibly already running
# UiPath. Great product fit, Phase-2 displacement plays.
RPA_ASSET_MIN = 2.0e9
RPA_EMP_MIN = 100
RPA_OFFICES_MIN = 15
GOOD_FIT_MIN = 4


def compute(master: pd.DataFrame) -> pd.DataFrame:
    has_email = master["contact_email"].notna() & (master["contact_email"].astype(str) != "")

    g = master.groupby("cu_number")
    firms = g.first().reset_index()
    firms["n_contacts"] = g.apply(lambda d: int(has_email.loc[d.index].sum())).values
    firms["n_named"] = g.apply(
        lambda d: int((d["contact_name"].notna() & has_email.loc[d.index]).sum())
    ).values

    asset = pd.to_numeric(firms["asset_total"], errors="coerce")
    offices = pd.to_numeric(firms["offices"], errors="coerce").fillna(1).clip(lower=1)
    members = pd.to_numeric(firms["members"], errors="coerce").fillna(0)
    employees = pd.to_numeric(firms["employee_count"], errors="coerce").fillna(0).clip(lower=1)

    # 1) Branch footprint — more offices = more back-office surface area.
    firms["s_footprint"] = (np.log(offices) / np.log(FOOTPRINT_SAT)).clip(0.0, 1.0)
    # 2) Size fit — smaller assets are better.
    firms["s_size"] = _log_smallness(asset.fillna(ASSET_HI), ASSET_LO, ASSET_HI)
    # 3) Contact richness — more, and named, contacts = more reachable.
    firms["s_contact"] = (0.7 * np.minimum(firms["n_contacts"], 5) / 5
                          + 0.3 * np.minimum(firms["n_named"], 3) / 3)
    # 4) Ops intensity — members per employee (the pilot's manual-entry pain signal).
    firms["members_per_emp"] = members / employees
    firms["s_ops"] = firms["members_per_emp"].rank(pct=True)

    firms["composite"] = (
        WEIGHTS["footprint"] * firms["s_footprint"]
        + WEIGHTS["size"] * firms["s_size"]
        + WEIGHTS["contact"] * firms["s_contact"]
        + WEIGHTS["ops"] * firms["s_ops"]
    )
    firms["Zomma Priority"] = _bucket(firms["composite"].rank(pct=True)).astype(int)
    firms.loc[(firms["Zomma Priority"] == 5) & (firms["n_contacts"] == 0), "Zomma Priority"] = 4

    # ---- Zomma Fit: size-NEUTRAL product fit ---------------------------------
    denom = WEIGHTS["footprint"] + WEIGHTS["contact"] + WEIGHTS["ops"]
    firms["fit_raw"] = (
        WEIGHTS["footprint"] * firms["s_footprint"]
        + WEIGHTS["contact"] * firms["s_contact"]
        + WEIGHTS["ops"] * firms["s_ops"]
    ) / denom
    firms["Zomma Fit"] = _bucket(firms["fit_raw"].rank(pct=True)).astype(int)

    # ---- Likely RPA-capable --------------------------------------------------
    firms["Likely RPA-capable"] = (
        (asset >= RPA_ASSET_MIN)
        & (employees >= RPA_EMP_MIN)
        & (offices >= RPA_OFFICES_MIN)
    )

    # ---- Zomma Segment -------------------------------------------------------
    reachable = firms["n_contacts"] > 0
    if "primary_contact_email" in firms.columns:
        reachable = reachable | firms["primary_contact_email"].notna()
    good_fit = firms["Zomma Fit"] >= GOOD_FIT_MIN
    seg = pd.Series("Low-fit", index=firms.index)
    seg[good_fit & ~firms["Likely RPA-capable"]] = "Beachhead"
    seg[good_fit & firms["Likely RPA-capable"]] = "Expansion (UiPath)"
    seg[~reachable] = "Low-info"
    firms["Zomma Segment"] = seg
    return firms


def run(master_path) -> None:
    master = pd.read_csv(master_path, low_memory=False)
    firms = compute(master)

    new_cols = ["Zomma Priority", "Zomma Fit", "Zomma Segment", "Likely RPA-capable"]
    master = master.drop(columns=[c for c in new_cols if c in master.columns])
    master = master.merge(firms[["cu_number", *new_cols]], on="cu_number", how="left")
    master["Zomma Priority"] = master["Zomma Priority"].astype("Int64")
    master["Zomma Fit"] = master["Zomma Fit"].astype("Int64")
    master.to_csv(master_path, index=False)

    print(f"=== Zomma scoring over {len(firms):,} credit unions ===\n")
    pri = firms["Zomma Priority"].value_counts()
    fit = firms["Zomma Fit"].value_counts()
    print("level   Priority(now)   Fit(size-neutral)")
    for p in [5, 4, 3, 2, 1]:
        print(f"  {p}       {int(pri.get(p, 0)):>6}        {int(fit.get(p, 0)):>6}")
    print("\nSegment distribution:")
    for seg, n in firms["Zomma Segment"].value_counts().items():
        print(f"  {seg:<20} {n:>5}")
    print(f"\nLikely RPA-capable: {int(firms['Likely RPA-capable'].sum())}")

    top = firms[firms["Zomma Priority"] == 5].sort_values("composite", ascending=False)
    print(f"\n=== Top Zomma-Priority-5 credit unions: {len(top)} ===")
    for _, r in top.head(15).iterrows():
        print(f"  Pri{r['Zomma Priority']}/Fit{r['Zomma Fit']} | "
              f"{str(r['firm_legal_name'])[:30]:<30} "
              f"${r['asset_total']/1e6:>6,.0f}M off={int(r['offices']):<3} "
              f"mem/emp={r['members_per_emp']:>5.0f} contacts={int(r['n_contacts']):<2} [{r['office_state']}]")
    print(f"\nWrote {new_cols} -> {master_path}")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Score NCUA credit unions by Zomma Priority")
    p.add_argument("master", nargs="?", default=None, help="path to the NCUA master CSV")
    args = p.parse_args()
    master_path = args.master
    if master_path is None:
        cands = sorted(config.NCUA_ENRICHED_DIR.glob("ncua_targets_*.csv"))
        if not cands:
            raise SystemExit("No NCUA master found — run the scrape first.")
        master_path = cands[-1]
    run(master_path)
