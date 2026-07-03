"""Build Item-import payloads for FDIC banks (Priority 5 + 4).

LEAN companies: website / address / industries / general-inbox email + list only.
CONTACTS = scraper-named people UNION names derived from clean first.last@ emails.

Writes, under data/fdic/item_import/:
  companies_p5.json / companies_p4.json   — lean company create payloads
  contacts_named_p5p4.json                — contacts keyed by cert_number for linkage
Nothing is sent to Item here; this only stages reviewable JSON.
"""
from __future__ import annotations

import json
import re
import pandas as pd

import config

LIST_IDS = {5: 614, 4: 615}          # company priority lists already created in Item
OUT = config.FDIC_DATA_DIR / "item_import"
OUT.mkdir(parents=True, exist_ok=True)

# Truly generic mailbox local-parts — safe to use as a company "general inbox".
LOW_VALUE = {
    "info", "contact", "hello", "admin", "office", "mail", "support",
    "customerservice", "customercare", "service", "help", "inquiries", "inquiry",
    "general", "newaccounts", "loans", "deposits", "marketing", "feedback",
    "main", "frontdesk", "reception", "bank", "banking", "team",
}


def compose_address(r):
    parts = []
    if isinstance(r.get("office_street"), str) and r["office_street"].strip():
        parts.append(r["office_street"].strip())
    city = str(r.get("office_city") or "").strip()
    state = str(r.get("office_state") or "").strip()
    zip_ = str(r.get("office_zip") or "").strip()
    cz = " ".join(x for x in [f"{city}," if city else "", state, zip_] if x).strip()
    if cz:
        parts.append(cz)
    parts.append("United States")
    return ", ".join(parts) if parts else None


def derive_name(email: str):
    """'gary.harris@x.com' -> 'Gary Harris'. Only clean first.last (both >=2 alpha),
    and only if the result reads like a real person (rejects customer.service@,
    member.services@, etc.)."""
    local = email.split("@", 1)[0]
    if "." not in local:
        return None
    parts = local.split(".")
    if len(parts) == 2 and all(p.isalpha() and len(p) >= 2 for p in parts):
        nm = f"{parts[0].title()} {parts[1].title()}"
        return nm if is_real_name(nm) else None
    return None


TITLE_KEYWORDS = {
    "president", "vice", "officer", "manager", "director", "chief", "ceo", "cfo",
    "coo", "cco", "cio", "senior", "assistant", "lender", "banking", "loan",
    "mortgage", "branch", "teller", "cashier", "controller", "treasurer",
    "compliance", "operations", "retail", "commercial", "relationship",
    "executive", "chairman", "founder", "head", "svp", "evp", "vp", "trust",
    "wealth", "lending", "deposit", "accounting", "finance", "credit",
}


def clean_title(t):
    """Return a clean job title, or None if the text is page-chrome / junk."""
    if not isinstance(t, str) or not t.strip():
        return None
    s = re.sub(r"\s+", " ", t).strip()
    s = re.sub(r"\s*\bemail\b\s*$", "", s, flags=re.I).strip()  # trailing "Email"
    if re.search(r"\d", s):           # phone numbers / addresses crept in
        return None
    low = s.lower()
    if "email" in low or "@" in s:    # leftover page chrome
        return None
    if len(s) > 45 or len(s.split()) > 6:   # title + embedded name → too long, drop
        return None
    if not any(k in low for k in TITLE_KEYWORDS):
        return None
    return s


def name_email_consistent(name: str, email: str) -> bool:
    """True iff the email's local part plausibly belongs to this name
    (jane.doe@, jdoe@, j.doe@, janedoe@, doe@, jane@ ...).

    Intentionally broader than src.scrape_primary_contact.match_email_by_name:
    this is a consistency CHECK on a (name, email) pair we already scraped
    together, not a matcher picking one email out of many, so bare-initials and
    bare first/last locals are acceptable here."""
    toks = [re.sub(r"[^a-z]", "", t.lower()) for t in name.split()]
    toks = [t for t in toks if len(t) >= 2]
    if len(toks) < 2:
        return False
    first, last = toks[0], toks[-1]
    local = email.split("@", 1)[0].lower()
    patterns = {
        f"{first}.{last}", f"{first}{last}", f"{first}_{last}", f"{first}-{last}",
        f"{first[0]}{last}", f"{first[0]}.{last}", f"{first}.{last[0]}",
        f"{first}{last[0]}", f"{last}{first[0]}", f"{last}.{first}", f"{last}{first}",
        f"{first[0]}{last[0]}", f"{first[0]}.{last[0]}",   # initials, e.g. mm@ for Mark Marionneaux
        first, last,
    }
    return local in patterns


# Words that never appear in a real person's name — they mark page-chrome /
# marketing text the scraper mistakenly grabbed (e.g. "Wow Exceptional",
# "Need Help", "Hours Mailing", "Customer Service").
NAME_STOPWORDS = {
    "wow", "exceptional", "need", "help", "hours", "mailing", "customer",
    "service", "services", "learn", "more", "read", "contact", "us", "welcome",
    "online", "banking", "bank", "login", "log", "search", "home", "about",
    "careers", "career", "location", "locations", "branch", "branches", "hello",
    "thank", "thanks", "you", "your", "get", "started", "open", "account",
    "accounts", "apply", "now", "click", "here", "view", "all", "our", "team",
    "meet", "the", "info", "information", "support", "member", "members",
    "personal", "business", "loans", "loan", "mortgage", "mortgages", "savings",
    "checking", "credit", "debit", "rates", "rate", "today", "please", "call",
    "email", "phone", "website", "site", "page", "menu", "close", "skip", "main",
    "content", "toggle", "navigation", "privacy", "policy", "terms", "copyright",
    "rights", "reserved", "find", "locate", "routing", "number", "fdic",
    "insured", "equal", "housing", "lender", "deposit", "transfer", "pay",
    "bill", "schedule", "appointment", "atm", "atms", "mobile", "app",
    "security", "alert", "alerts", "fraud", "news", "events", "community",
    "foundation", "board", "directors", "leadership", "management", "staff",
    "employment", "jobs", "resources", "faq", "faqs", "sitemap", "feedback",
    "subscribe", "newsletter", "chairperson", "treasurer", "secretary",
    "president", "officer", "director", "manager", "cashier", "teller",
    "eye", "sleepy",  # branch-location "names" the scraper grabbed (e.g. Sleepy Eye)
    # department / function mailboxes that come as first.last form (apex.servicing@)
    "servicing", "lending", "underwriting", "processing", "escrow", "treasury",
    "payroll", "billing", "collections", "dispute", "disputes", "reconciliation",
    "wires", "ach", "operations", "compliance", "marketing", "investor", "relations",
}


def is_real_name(name: str) -> bool:
    """True iff `name` plausibly reads like a real person and not page chrome."""
    if not name:
        return False
    tokens = name.split()
    if not (2 <= len(tokens) <= 4):
        return False
    if any(re.search(r"[^A-Za-z.'\-]", t) for t in tokens):
        return False
    low = [t.lower().strip(".") for t in tokens]
    if any(t in NAME_STOPWORDS for t in low):
        return False
    # First and last token must be real words (>=2 letters); middle can be an initial.
    if len(low[0]) < 2 or len(low[-1]) < 2:
        return False
    return True


def main():
    d = pd.read_csv(config.FDIC_ENRICHED_DIR / "fdic_targets_20260615.csv", low_memory=False)
    d["has_email"] = d["contact_email"].notna() & (d["contact_email"].astype(str).str.strip() != "")
    d["has_name"] = d["contact_name"].notna() & (d["contact_name"].astype(str).str.strip() != "")

    companies = {5: [], 4: []}
    contacts = []
    n_scraped = n_derived = n_rejected = 0
    rejected_samples = []

    for cert, grp in d.groupby("cert_number"):
        pri = int(grp.iloc[0]["Zomma Priority"])
        if pri not in (5, 4):
            continue
        r = grp.iloc[0].to_dict()
        location = f"{r.get('office_city') or ''}, {r.get('office_state') or ''}, United States".strip(", ")
        primary_email = str(r.get("primary_contact_email") or "").strip().lower()

        seen = set()
        cu_contacts = []

        # 1) Scraper-extracted names. Trust a scraped name only when it (a) reads
        #    like a real person AND (b) matches the email local part, OR it is the
        #    verified primary contact. Otherwise fall back to deriving from the
        #    email; if that fails too, drop it (a junk/mis-paired name is worse
        #    than no contact).
        for _, c in grp[grp["has_email"] & grp["has_name"]].iterrows():
            email = str(c["contact_email"]).strip().lower()
            if email in seen:
                continue
            raw = str(c["contact_name"]).strip()
            is_primary = bool(primary_email) and email == primary_email
            name = None
            # Require the name to match the email even for primary contacts — the
            # cascade sometimes returns a place/slogan ("Eustis Cambridge",
            # "Built Together") that is_real_name can't catch but the email won't match.
            if is_real_name(raw) and name_email_consistent(raw, email):
                name = raw
                n_scraped += 1
            else:
                derived = derive_name(email)
                if derived:
                    name = derived
                    n_derived += 1
            if not name:
                n_rejected += 1
                if len(rejected_samples) < 25:
                    rejected_samples.append(f"{raw}  <{email}>")
                continue
            seen.add(email)
            raw_title = c.get("contact_title")
            if isinstance(raw_title, str) and raw_title.strip().lower().startswith(name.lower()):
                raw_title = raw_title.strip()[len(name):]
            role = clean_title(raw_title)
            if is_primary:
                role = f"Primary contact{(' - ' + role) if role else ''}"
            cu_contacts.append({"name": name, "email": email, "role": role})

        # 2) Names derived from clean first.last@ emails that had no scraped name.
        nameless = grp[grp["has_email"] & ~grp["has_name"]]["contact_email"].dropna().astype(str).str.lower()
        nameless = list(dict.fromkeys(nameless))
        for email in nameless:
            if email in seen:
                continue
            nm = derive_name(email)
            if nm:
                seen.add(email)
                role = "Primary contact" if email == primary_email and primary_email else None
                cu_contacts.append({"name": nm, "email": email, "role": role})
                n_derived += 1

        for c in cu_contacts:
            contacts.append({"cert_number": int(cert), "priority": pri,
                             "name": c["name"], "email": c["email"], "role": c["role"],
                             "location": location})

        # General inbox = a truly generic nameless local-part.
        generic = [e for e in nameless if e.split("@", 1)[0] in LOW_VALUE and e not in seen]
        general = generic[0] if generic else None

        fields = []
        if isinstance(r.get("website"), str) and r["website"].strip():
            fields.append({"name": "website_url", "value": r["website"].strip()})
        fields.append({"name": "industries", "value": ["financial services", "banking"]})
        addr = compose_address(r)
        if addr:
            fields.append({"name": "address", "value": addr})
        if general:
            fields.append({"name": "email", "value": general})

        companies[pri].append({
            "cert_number": int(cert),
            "name": str(r["firm_legal_name"]).strip(),
            "fields": fields,
            "list_id": LIST_IDS[pri],
        })

    (OUT / "companies_p5.json").write_text(json.dumps(companies[5], indent=1), encoding="utf-8")
    (OUT / "companies_p4.json").write_text(json.dumps(companies[4], indent=1), encoding="utf-8")
    (OUT / "contacts_named_p5p4.json").write_text(json.dumps(contacts, indent=1), encoding="utf-8")

    print(f"companies P5: {len(companies[5])} | P4: {len(companies[4])}")
    print(f"contacts total: {len(contacts)}  (scraper-named {n_scraped} + derived {n_derived})")
    print(f"rejected junk names: {n_rejected}")
    for p in (5, 4):
        print(f"  P{p} contacts: {sum(1 for c in contacts if c['priority'] == p)}")
    print("\nsample REJECTED names (should all look like junk):")
    for s in rejected_samples:
        print(f"  - {s}")
    print("\nsample KEPT names (should all look like real people):")
    for c in contacts[:25]:
        print(f"  - {c['name']:28} <{c['email']}>  [{c['role'] or ''}]")
    print(f"\nwrote → {OUT}")


if __name__ == "__main__":
    main()
