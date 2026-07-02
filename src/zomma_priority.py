"""Score each firm 1-5 as a Zomma outbound target ("Zomma Priority", 5 = best).

Rationale (from the Notion Zomma profile + RPA-incumbent customer base):
Zomma automates cross-system back-office data entry for financial-services
firms. RPA incumbents (UiPath/Blue Prism/AA) prove the ROI — but only at huge
banks/insurers/asset managers. Zomma's wedge is that SAME pain at firms too
small to afford RPA. The pilot customer (Pre-Planning Solutions) is small,
multi-service (insurance/estate/funeral/AUM), with 2,000+ clients and tiny
staff drowning in manual cross-portal entry.

So the best targets look like: many back-office services (more portals = more
pain), smaller (can't afford RPA / more pain per head), reachable (we have
contacts), and operationally dense (lots of clients per employee).

Composite weights -> percentile buckets 1-5. Writes one "Zomma Priority"
column onto the master (firm-level, repeated across each firm's contact rows).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config

# Back-office service relevance to Zomma's automation thesis (higher = more
# cross-system data-entry pain, and/or closer to the pilot customer profile).
SERVICE_WEIGHTS = {
    "svc_insurance": 3.0,         # carrier portals = the canonical RPA/Zomma use case
    "svc_tax": 2.0,               # prep/filing data entry
    "svc_estate_planning": 2.0,   # document-heavy; matches pilot
    "svc_accounting": 2.0,        # bookkeeping / repeated data entry
    "svc_401k": 2.0,              # plan-admin portals
    "svc_brokerage": 1.5,         # custodian / securities ops
    "svc_funeral": 1.5,           # matches pilot's unusual multi-service profile
    "svc_retirement_planning": 1.0,
    "svc_family_office": 1.0,
    "svc_alternatives": 1.0,
}
# A firm offering insurance+tax+estate+accounting+401k (=11) is a "fully
# multi-service back office" -> service score saturates at 1.0.
SERVICE_SATURATION = 11.0

WEIGHTS = {"service": 0.40, "size": 0.25, "contact": 0.20, "ops": 0.15}

AUM_LO, AUM_HI = 250e6, 5e9     # ICP band
EMP_LO, EMP_HI = 5, 200

# "Likely RPA-capable" = big + complex + staffed enough to plausibly run UiPath
# today. These are the Phase-2 / displacement plays: great product fit, but a
# pre-seed team should win references first before chasing them.
RPA_AUM_MIN = 2.0e9
RPA_EMP_MIN = 40
RPA_SVC_MIN = 3
GOOD_FIT_MIN = 4   # Zomma Fit >= this counts as a strong product fit

# Composite percentile -> priority bucket. 5 = top ~10%.
def _bucket(pct: pd.Series) -> pd.Series:
    pr = pd.Series(1, index=pct.index)
    pr[pct >= 0.15] = 2
    pr[pct >= 0.40] = 3
    pr[pct >= 0.70] = 4
    pr[pct >= 0.90] = 5
    return pr


def _log_smallness(x, lo, hi):
    """1.0 when x==lo (small), 0.0 when x==hi (large), log scale, clipped."""
    x = np.clip(x.astype(float), lo, hi)
    return 1.0 - (np.log(x) - np.log(lo)) / (np.log(hi) - np.log(lo))


def compute(master: pd.DataFrame) -> pd.DataFrame:
    """Return a firm-level frame: crd_number, the four components, composite,
    Zomma Priority — plus the inputs, for inspection."""
    svc_cols = list(SERVICE_WEIGHTS)
    has_email = master["contact_email"].notna() & (master["contact_email"].astype(str) != "")

    g = master.groupby("crd_number")
    firms = g.first().reset_index()
    firms["n_contacts"] = g.apply(lambda d: int(has_email.loc[d.index].sum())).values
    firms["n_named"] = g.apply(
        lambda d: int((d["contact_name"].notna() & has_email.loc[d.index]).sum())
    ).values

    # 1) Service complexity (blank/unreadable services -> 0, i.e. not a known
    #    multi-service target).
    wsum = sum(firms[c].fillna(0).astype(float) * w for c, w in SERVICE_WEIGHTS.items())
    firms["s_service"] = (wsum / SERVICE_SATURATION).clip(upper=1.0)

    # 2) Size fit — smaller is better (60% AUM, 40% headcount).
    size_aum = _log_smallness(firms["aum_total"], AUM_LO, AUM_HI)
    size_emp = _log_smallness(firms["employee_count"].clip(lower=1), EMP_LO, EMP_HI)
    firms["s_size"] = 0.6 * size_aum + 0.4 * size_emp

    # 3) Contact richness — more, and named, contacts = more reachable.
    firms["s_contact"] = (0.7 * np.minimum(firms["n_contacts"], 5) / 5
                          + 0.3 * np.minimum(firms["n_named"], 3) / 3)

    # 4) Operational intensity — clients per employee (the pilot's pain signal).
    clients = firms["individual_clients"].fillna(0) + firms["hnw_clients"].fillna(0)
    volume = np.maximum(clients, firms["total_accounts"].fillna(0))
    density = volume / firms["employee_count"].clip(lower=1)
    firms["s_ops"] = density.rank(pct=True)

    firms["composite"] = (
        WEIGHTS["service"] * firms["s_service"]
        + WEIGHTS["size"] * firms["s_size"]
        + WEIGHTS["contact"] * firms["s_contact"]
        + WEIGHTS["ops"] * firms["s_ops"]
    )

    firms["Zomma Priority"] = _bucket(firms["composite"].rank(pct=True)).astype(int)
    # Guard: don't rank a firm we can't even email as a top "call-first" target.
    firms.loc[(firms["Zomma Priority"] == 5) & (firms["n_contacts"] == 0), "Zomma Priority"] = 4

    # ---- Zomma Fit: size-NEUTRAL product fit (drop the size weight) ----------
    # A big firm with the same service/ops/contact profile as a small one is an
    # equally good *product* fit — it's just a later go-to-market target.
    denom = WEIGHTS["service"] + WEIGHTS["contact"] + WEIGHTS["ops"]
    firms["fit_raw"] = (
        WEIGHTS["service"] * firms["s_service"]
        + WEIGHTS["contact"] * firms["s_contact"]
        + WEIGHTS["ops"] * firms["s_ops"]
    ) / denom
    firms["Zomma Fit"] = _bucket(firms["fit_raw"].rank(pct=True)).astype(int)

    # ---- Likely RPA-capable: big + complex + staffed (UiPath-displacement) ----
    svc_count = firms[svc_cols].sum(axis=1)  # NaN (blank) counts as 0
    firms["Likely RPA-capable"] = (
        (firms["aum_total"] >= RPA_AUM_MIN)
        & (firms["employee_count"] >= RPA_EMP_MIN)
        & (svc_count >= RPA_SVC_MIN)
    )

    # ---- Zomma Segment: go-to-market sequencing -------------------------------
    readable = firms[svc_cols].notna().any(axis=1)   # did we read their site at all
    good_fit = firms["Zomma Fit"] >= GOOD_FIT_MIN
    seg = pd.Series("Low-fit", index=firms.index)
    seg[good_fit & ~firms["Likely RPA-capable"]] = "Beachhead"
    seg[good_fit & firms["Likely RPA-capable"]] = "Expansion (UiPath)"
    seg[~readable] = "Low-info"                       # unreadable site -> can't assess/reach
    firms["Zomma Segment"] = seg
    return firms


def run(master_path=None) -> None:
    master_path = master_path or config.latest_ria_master()
    master = pd.read_csv(master_path)

    firms = compute(master)

    new_cols = ["Zomma Priority", "Zomma Fit", "Zomma Segment", "Likely RPA-capable"]
    master = master.drop(columns=[c for c in new_cols if c in master.columns])
    master = master.merge(firms[["crd_number", *new_cols]], on="crd_number", how="left")
    master["Zomma Priority"] = master["Zomma Priority"].astype("Int64")
    master["Zomma Fit"] = master["Zomma Fit"].astype("Int64")
    master.to_csv(master_path, index=False)

    svc_cols = list(SERVICE_WEIGHTS)
    print(f"=== Zomma scoring over {len(firms):,} firms ===\n")
    pri = firms["Zomma Priority"].value_counts()
    fit = firms["Zomma Fit"].value_counts()
    print("level   Priority(now)   Fit(size-neutral)")
    for p in [5, 4, 3, 2, 1]:
        print(f"  {p}       {int(pri.get(p,0)):>6}        {int(fit.get(p,0)):>6}")
    print("\nSegment distribution:")
    for seg, n in firms["Zomma Segment"].value_counts().items():
        print(f"  {seg:<20} {n:>5}")
    print(f"\nLikely RPA-capable (Phase-2 displacement universe): {int(firms['Likely RPA-capable'].sum())}")

    exp = firms[firms["Zomma Segment"] == "Expansion (UiPath)"].sort_values(
        "fit_raw", ascending=False)
    print(f"\n=== Expansion (UiPath-displacement) targets: {len(exp)} firms ===")
    for _, r in exp.head(15).iterrows():
        services = ", ".join(c.replace("svc_", "") for c in svc_cols if r.get(c) == 1)
        print(f"  Fit{r['Zomma Fit']}/Pri{r['Zomma Priority']} | {str(r['firm_legal_name'])[:30]:<30} "
              f"${r['aum_total']/1e9:.1f}B emp={int(r['employee_count']):<3} contacts={int(r['n_contacts']):<2} [{services[:48]}]")
    print(f"\nWrote {new_cols} -> {master_path}")


if __name__ == "__main__":
    run()
