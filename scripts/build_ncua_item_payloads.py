"""Build Item-import payloads for NCUA credit unions (Priority 5).

COMPANIES: website / address / industries / general-inbox email + list.
CONTACTS:  the NCUA-provided CEO (primary, email resolved from the CU's scraped
           addresses) + up to MAX_NONCEO additional cleanly-named scraped/derived
           contacts per credit union.

Reuses the validated name/title filters from the FDIC build script.
Writes, under data/ncua/item_import/:
  companies_p5.json          — lean company create payloads
  contacts_p5.json           — contacts keyed by cu_number for linkage
"""
from __future__ import annotations

import json
import re
import pandas as pd

import config
from build_fdic_item_payloads import (
    is_real_name, name_email_consistent, derive_name, clean_title,
    compose_address, LOW_VALUE,
)

OUT = config.NCUA_DATA_DIR / "item_import"
OUT.mkdir(parents=True, exist_ok=True)

MAX_NONCEO = 5  # cap on non-CEO named contacts per credit union


def clean_person_name(nm):
    if not isinstance(nm, str):
        return None
    nm = re.sub(r"\s+", " ", nm).strip()
    toks = nm.split()
    if not (2 <= len(toks) <= 4):
        return None
    if not all(re.fullmatch(r"[A-Za-z.'\-]+", t) for t in toks):
        return None
    return nm


def resolve_ceo_email(ceo_name, emails, primary_email):
    """CEO email from primary_contact_email, else match the name against the CU's
    scraped emails (flast / f.last / first.last / firstlast / last ...)."""
    if isinstance(primary_email, str) and "@" in primary_email:
        return primary_email.strip().lower()
    toks = [re.sub(r"[^a-z]", "", t.lower()) for t in (ceo_name or "").split()]
    toks = [t for t in toks if len(t) >= 2]
    if len(toks) < 2:
        return None
    first, last = toks[0], toks[-1]
    cands = {
        f"{first}.{last}", f"{first}{last}", f"{first[0]}{last}", f"{first[0]}.{last}",
        f"{last}{first[0]}", f"{first}{last[0]}", f"{last}.{first}", f"{first}.{last[0]}",
    }
    for e in emails:
        if e.split("@", 1)[0].lower() in cands:
            return e
    return None


def main():
    d = pd.read_csv(config.NCUA_ENRICHED_DIR / "ncua_targets_20260615.csv", low_memory=False)
    companies, contacts = [], []
    n_ceo = n_ceo_email = n_nonceo = 0

    for cu, grp in d.groupby("cu_number"):
        r = grp.iloc[0].to_dict()
        if int(r["Zomma Priority"]) != 5:
            continue
        emails = [str(e).strip().lower() for e in grp["contact_email"].dropna()
                  if str(e).strip()]
        emails = list(dict.fromkeys(emails))
        location = f"{r.get('office_city') or ''}, {r.get('office_state') or ''}, United States".strip(", ")
        ph = r.get("ncua_phone")
        phone = None
        if pd.notna(ph):
            try:
                phone = str(int(float(ph)))          # NCUA stores phone as a float -> drop the ".0"
            except (ValueError, TypeError):
                phone = str(ph).strip() or None

        seen = set()
        cu_contacts = []

        # 1) CEO (decision-maker NCUA gives us for every CU).
        ceo = clean_person_name(r.get("ceo_name"))
        if ceo:
            n_ceo += 1
            ce = resolve_ceo_email(r.get("ceo_name"), emails, r.get("primary_contact_email"))
            if ce:
                n_ceo_email += 1
                seen.add(ce)
            cu_contacts.append({"name": ceo, "email": ce or None,
                                "role": "Primary contact - CEO/Manager", "phone": phone})

        # 2) Up to MAX_NONCEO additional cleanly-named contacts.
        cnt = 0
        for _, c in grp.iterrows():
            if cnt >= MAX_NONCEO:
                break
            email = str(c.get("contact_email") or "").strip().lower()
            if not email or email in seen:
                continue
            raw = str(c.get("contact_name") or "").strip()
            if is_real_name(raw) and name_email_consistent(raw, email):
                name = raw
            else:
                name = derive_name(email)
            if not name:
                continue
            seen.add(email)
            cnt += 1
            n_nonceo += 1
            cu_contacts.append({"name": name, "email": email,
                                "role": clean_title(c.get("contact_title")), "phone": None})

        for c in cu_contacts:
            contacts.append({"cu_number": int(cu), "name": c["name"], "email": c["email"],
                             "role": c["role"], "phone": c["phone"], "location": location})

        # company general inbox = a truly generic nameless local-part
        generic = [e for e in emails if e.split("@", 1)[0] in LOW_VALUE and e not in seen]
        fields = []
        if isinstance(r.get("website"), str) and r["website"].strip():
            fields.append({"name": "website_url", "value": r["website"].strip()})
        fields.append({"name": "industries", "value": ["financial services", "credit unions"]})
        addr = compose_address(r)
        if addr:
            fields.append({"name": "address", "value": addr})
        if generic:
            fields.append({"name": "email", "value": generic[0]})
        if phone:
            fields.append({"name": "phone", "value": phone})

        companies.append({"cu_number": int(cu),
                          "name": str(r["firm_legal_name"]).strip(),
                          "fields": fields})

    (OUT / "companies_p5.json").write_text(json.dumps(companies, indent=1), encoding="utf-8")
    (OUT / "contacts_p5.json").write_text(json.dumps(contacts, indent=1), encoding="utf-8")

    print(f"P5 credit unions (companies): {len(companies)}")
    print(f"contacts: {len(contacts)}  (CEOs {n_ceo}, of which {n_ceo_email} with email; "
          f"non-CEO named {n_nonceo})")
    ceo_no_email = n_ceo - n_ceo_email
    print(f"CEOs without a resolved email (phone-only): {ceo_no_email}")
    print("\nsample contacts:")
    for c in contacts[:18]:
        print(f"  {c['name']:24} <{str(c['email'] or '(no email)'):34}> [{c['role'] or ''}]")
    print(f"\nwrote -> {OUT}")


if __name__ == "__main__":
    main()
