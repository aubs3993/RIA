"""Sanity tests for the FDIC bank pipeline's pure (no-network) functions:

1. fdic_fetch.to_clean: raw FDIC fields map to the shared schema, ASSET/DEP are
   converted from $thousands to dollars, and junk websites are dropped.
2. fdic_filter.filter_banks: the ICP asset band + website gate work, and
   match_score orders smallest-first.
3. fdic_zomma.compute: branch footprint, smallness, reachability flow into the
   1-5 buckets and the reachability guard caps unreachable banks at 4.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fdic_fetch import to_clean  # noqa: E402
from src.fdic_filter import filter_banks  # noqa: E402
from src import fdic_zomma  # noqa: E402
from config import BankICP  # noqa: E402


def test_to_clean_maps_and_converts_units():
    raw = pd.DataFrame([
        {"CERT": 5510, "NAME": "Frost  Bank", "WEBADDR": "www.frostbank.com",
         "ADDRESS": "100 W Houston", "CITY": "San Antonio", "STALP": "TX",
         "STNAME": "Texas", "ZIP": "78205", "ASSET": 52769892, "DEP": 43235075,
         "OFFICES": 219, "BKCLASS": "SM", "ESTYMD": "01/01/1900",
         "ROA": 1.1, "ROE": 12.0, "NETINC": 500000},
        {"CERT": 999, "NAME": "No Site Bank", "WEBADDR": None,
         "ADDRESS": "1 Main", "CITY": "Nowhere", "STALP": "KS", "STNAME": "Kansas",
         "ZIP": "00000", "ASSET": 100000, "DEP": 80000, "OFFICES": 1,
         "BKCLASS": "NM", "ESTYMD": "01/01/2000", "ROA": 0.1, "ROE": 1.0, "NETINC": 10},
    ])
    clean = to_clean(raw)

    frost = clean[clean["cert_number"] == 5510].iloc[0]
    # Double space collapsed, website normalized, $thousands -> dollars.
    assert frost["firm_legal_name"] == "Frost Bank"
    assert frost["website"] == "http://www.frostbank.com"
    assert frost["asset_total"] == 52769892 * 1000
    assert frost["deposits"] == 43235075 * 1000
    assert int(frost["offices"]) == 219

    nosite = clean[clean["cert_number"] == 999].iloc[0]
    assert pd.isna(nosite["website"])   # None website stays missing


def _bank(cert, name, asset, offices, state="TX", site=True):
    return {
        "cert_number": cert, "firm_legal_name": name,
        "website": f"http://{name.lower().replace(' ', '')}.com" if site else None,
        "office_state": state, "asset_total": asset, "deposits": asset * 0.8,
        "offices": offices,
    }


def test_filter_banks_applies_band_and_orders_smallest_first(tmp_path):
    df = pd.DataFrame([
        _bank(1, "Tiny Bank", 10_000_000, 1),        # below band -> out
        _bank(2, "Small Bank", 80_000_000, 2),       # in band
        _bank(3, "Mid Bank", 2_000_000_000, 20),     # in band
        _bank(4, "Huge Bank", 50_000_000_000, 500),  # above band -> out
        _bank(5, "No Site Bank", 100_000_000, 3, site=False),  # no website -> out
    ])
    icp = BankICP(asset_min=50_000_000, asset_max=10_000_000_000)
    out = filter_banks(df, icp, output_path=tmp_path / "targeted.csv")

    assert set(out["cert_number"]) == {2, 3}            # only in-band, with website
    # match_score favours the smaller bank, so it sorts first.
    assert out.iloc[0]["cert_number"] == 2
    assert out.iloc[0]["match_score"] >= out.iloc[1]["match_score"]


def test_zomma_buckets_and_reachability_guard():
    # 40 banks spanning a range of footprint/size so percentile buckets populate.
    rows = []
    for i in range(40):
        asset = 50_000_000 * (1 + i)          # increasing assets
        offices = 1 + (i % 12)                 # varying branch footprint
        has_email = i >= 5                     # first 5 banks are unreachable
        rows.append({
            "cert_number": 1000 + i,
            "firm_legal_name": f"Bank {i}",
            "asset_total": asset,
            "offices": offices,
            "contact_name": "Jane Doe" if has_email else np.nan,
            "contact_email": f"jane@bank{i}.com" if has_email else np.nan,
        })
    master = pd.DataFrame(rows)
    firms = fdic_zomma.compute(master)

    # Buckets are in 1..5 and the size-neutral Fit column exists.
    assert set(firms["Zomma Priority"].unique()) <= {1, 2, 3, 4, 5}
    assert "Zomma Fit" in firms.columns

    # Reachability guard: a bank with zero contacts is never Priority 5.
    unreachable = firms[firms["n_contacts"] == 0]
    assert (unreachable["Zomma Priority"] < 5).all()
    # Unreachable banks land in the Low-info segment.
    assert (unreachable["Zomma Segment"] == "Low-info").all()
