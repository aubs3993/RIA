"""Sanity tests for the NCUA credit-union pipeline's pure (no-network) functions:

1. ncua_fetch._to_clean: raw FOICU/FS220 fields map to the shared schema,
   employees sum full-time + part-time, assets pass through as dollars.
2. ncua_discover_sites.candidate_domains: name -> plausible domains, with the
   "fcu" suffix for federal CUs and the right ordering.
3. ncua_zomma.compute: members-per-employee density flows into the score and the
   reachability guard caps unreachable CUs at 4.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ncua_fetch import _to_clean  # noqa: E402
from src.ncua_discover_sites import candidate_domains  # noqa: E402
from src.ncua_profile import _normalize_record, reconcile_ceo  # noqa: E402
from src import ncua_zomma  # noqa: E402


def test_to_clean_maps_and_sums_employees():
    raw = pd.DataFrame([{
        "CU_NUMBER": "60448", "CU_NAME": "NAVY  FEDERAL CREDIT UNION",
        "STREET": "820 Follin Ln", "CITY": "Vienna", "STATE": "VA",
        "ZIP_CODE": "22180-1234", "CharterState": "0", "CU_TYPE": "1",
        "YEAR_OPENED": "1933", "ACCT_010": "203558954708", "ACCT_083": "15350733",
        "ACCT_564A": "20000", "ACCT_564B": "5234", "offices": "350",
    }])
    clean = _to_clean(raw)
    r = clean.iloc[0]
    assert r["firm_legal_name"] == "NAVY FEDERAL CREDIT UNION"   # double space collapsed
    assert r["office_zip"] == "22180"                            # ZIP truncated to 5
    assert r["asset_total"] == 203558954708                      # dollars, no scaling
    assert int(r["members"]) == 15350733
    assert int(r["employee_count"]) == 25234                     # 20000 + 5234
    assert pd.isna(r["website"])                                 # never populated here


def test_candidate_domains_patterns():
    navy = candidate_domains("NAVY FEDERAL CREDIT UNION")
    assert navy[0] == "navyfederal.org"          # best guess first, .org leads
    # A federal CU should produce an "fcu" acronym/suffix candidate somewhere.
    abc = candidate_domains("ABC FEDERAL CREDIT UNION")
    assert any("fcu" in d for d in abc)
    # A non-federal CU uses the "cu" suffix instead.
    coop = candidate_domains("RIVERSIDE CREDIT UNION")
    assert any(d.startswith("riversidecu.") for d in coop)
    assert candidate_domains("") == []


def test_profile_normalize_record():
    ok = _normalize_record(5536, {
        "creditUnionWebsite": "WWW.NAVYFCU.ORG", "creditUnionCeo": "Dietrich Kuhlmann ",
        "creditUnionPhone": "8888426328", "isError": False,
    })
    assert ok["website"] == "http://www.navyfcu.org"   # normalized (scheme + lowercased)
    assert ok["ceo_name"] == "Dietrich Kuhlmann"        # trimmed
    assert ok["status"] == "ok"

    # Blank website -> kept as a profile (CEO may still be useful) but flagged.
    no_site = _normalize_record(1, {"creditUnionWebsite": "", "creditUnionCeo": "Jane Roe"})
    assert no_site["website"] is None and no_site["status"] == "no_website"
    assert no_site["ceo_name"] == "Jane Roe"

    # Error payload -> empty profile.
    err = _normalize_record(2, {"isError": True, "errorMessage": "not found"})
    assert err["website"] is None and err["status"] == "error"


def test_reconcile_ceo_only_keeps_name_matched_email(tmp_path):
    # CU 1: a scraped email matches the CEO by name (joshua@ ~ Joshua Poole).
    # CU 2: only a generic inbox was scraped — it must NOT be relabeled as the CEO's.
    master = pd.DataFrame([
        {"cu_number": 1, "firm_legal_name": "BRECO", "ceo_name": "Joshua W Poole",
         "contact_name": None, "contact_email": "info@brecofcu.com"},
        {"cu_number": 1, "firm_legal_name": "BRECO", "ceo_name": "Joshua W Poole",
         "contact_name": "Joshua Poole", "contact_email": "joshua@brecofcu.com"},
        {"cu_number": 2, "firm_legal_name": "ACME CU", "ceo_name": "Jane Roe",
         "contact_name": None, "contact_email": "memberservices@acmecu.org"},
    ])
    p = tmp_path / "master.csv"
    master.to_csv(p, index=False)
    reconcile_ceo(p)
    out = pd.read_csv(p).groupby("cu_number").first()

    assert out.loc[1, "primary_contact_name"] == "Joshua W Poole"
    assert out.loc[1, "primary_contact_email"] == "joshua@brecofcu.com"   # name-matched
    assert out.loc[2, "primary_contact_name"] == "Jane Roe"               # CEO name still set
    assert pd.isna(out.loc[2, "primary_contact_email"])                   # generic inbox NOT claimed as CEO


def test_zomma_density_and_reachability_guard():
    rows = []
    for i in range(40):
        has_email = i >= 5
        rows.append({
            "cu_number": 2000 + i,
            "firm_legal_name": f"CU {i}",
            "office_state": "TX",
            "asset_total": 50_000_000 * (1 + i),
            "offices": 1 + (i % 10),
            "members": 1000 * (1 + i),
            "employee_count": 5 + (i % 7),
            "contact_name": "Jane Doe" if has_email else np.nan,
            "contact_email": f"jane@cu{i}.org" if has_email else np.nan,
        })
    master = pd.DataFrame(rows)
    firms = ncua_zomma.compute(master)

    # members_per_emp computed and the buckets are valid.
    assert "members_per_emp" in firms.columns
    assert set(firms["Zomma Priority"].unique()) <= {1, 2, 3, 4, 5}
    # Reachability guard: zero-contact CUs never reach Priority 5 and are Low-info.
    unreachable = firms[firms["n_contacts"] == 0]
    assert (unreachable["Zomma Priority"] < 5).all()
    assert (unreachable["Zomma Segment"] == "Low-info").all()
