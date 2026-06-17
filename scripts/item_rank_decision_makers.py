"""Rank a bank's scraped contacts by seniority and print the top-N decision-makers
plus the to-skip remainder. Used to cap big-directory banks at decision-makers only.

Usage: item_rank_decision_makers.py <email-domain> [N=15]

Reads the raw enriched CSV (which keeps the scraped title text the build script
strips), derives a clean name + short title, scores by title keywords, and prints:
  - the top-N as ready-to-create rows (name | title | email | company_id)
  - the remaining emails as a JSON array to append to skipped_contacts.json
"""
import json
import re
import sys

import pandas as pd

import config

dom = sys.argv[1]
N = int(sys.argv[2]) if len(sys.argv) > 2 else 15
OUT = config.FDIC_DATA_DIR / "item_import"

d = pd.read_csv(config.FDIC_ENRICHED_DIR / "fdic_targets_20260615.csv", low_memory=False)
co = json.loads((OUT / "created_companies.json").read_text(encoding="utf-8"))

m = d["contact_email"].astype(str).str.contains("@" + re.escape(dom), case=False, na=False)
g = d[m].copy()
cert = int(g["cert_number"].iloc[0])
cid = co.get(str(cert))
primary = str(g["primary_contact_email"].iloc[0]).strip().lower()

# Seniority scoring by title keyword (higher = more senior). Order matters: we take
# the max matching weight, then add small bonuses for functional ownership roles
# relevant to a back-office automation pitch (ops / finance / IT / compliance).
TIERS = [
    (100, [r"\bceo\b", r"chief executive", r"\bchairman\b", r"\bchairwoman\b", r"\bchair\b", r"\bpresident & ceo\b"]),
    (95,  [r"\bpresident\b"]),
    (90,  [r"chief (operating|financial|credit|banking|risk|lending|technology|information|compliance|administrative|human)", r"\bc[ofretbi]o\b", r"\bcoo\b", r"\bcfo\b", r"\bcio\b", r"\bcto\b", r"\bcco\b", r"\bcro\b", r"\bchro\b"]),
    (80,  [r"\bevp\b", r"executive vice president", r"executive vp"]),
    (70,  [r"\bsvp\b", r"senior vice president", r"senior vp"]),
    (55,  [r"\bdirector\b", r"comptroller", r"controller", r"treasurer"]),
    (50,  [r"\bvice president\b", r"\bvp\b", r"market president"]),
    (30,  [r"\bavp\b", r"assistant vice president"]),
    (20,  [r"\bmanager\b", r"officer", r"administrator", r"supervisor"]),
]
FUNCTION_BONUS = [r"operation", r"compliance", r"\bit\b", r"information", r"technolog",
                  r"finance", r"financ", r"comptroller", r"controller", r"risk", r"bsa",
                  r"human resource", r"\bhr\b", r"credit", r"deposit", r"treasury"]


def clean_name(raw_name, email):
    if isinstance(raw_name, str) and raw_name.strip() and raw_name.strip().lower() != "nan":
        toks = raw_name.split()
        if 2 <= len(toks) <= 4 and all(re.fullmatch(r"[A-Za-z.'\-]+", t) for t in toks):
            return " ".join(toks)
    local = email.split("@", 1)[0]
    parts = re.split(r"[._-]", local)
    parts = [p for p in parts if p.isalpha()]
    if len(parts) >= 2:
        return f"{parts[0].title()} {parts[-1].title()}"
    return local.title()


def short_title(raw, name):
    """Pull a concise title out of the scraped blob (often 'Name Title Name email phone')."""
    if not isinstance(raw, str) or not raw.strip() or raw.strip().lower() == "nan":
        return None
    s = re.sub(r"\s+", " ", raw).strip()
    # drop a leading copy of the name
    if name and s.lower().startswith(name.lower()):
        s = s[len(name):].strip()
    # cut at email / phone / NMLS noise
    s = re.split(r"\b[\w.]+@|\bnmls\b|\d{3}[-.) ]", s, flags=re.I)[0].strip(" -|,")
    # keep only the slash-joined title-ish head (first ~8 words)
    if not s or len(s) > 70:
        s = " ".join(s.split()[:8]) if s else None
    return s or None


def score(title):
    if not title:
        return 0
    low = title.lower()
    base = 0
    for w, pats in TIERS:
        if any(re.search(p, low) for p in pats):
            base = w
            break
    bonus = 3 if any(re.search(p, low) for p in FUNCTION_BONUS) else 0
    return base + bonus


rows = []
seen = set()
for _, r in g.iterrows():
    email = str(r["contact_email"]).strip().lower()
    if email in seen:
        continue
    seen.add(email)
    name = clean_name(r.get("contact_name"), email)
    title = short_title(r.get("contact_title"), name)
    sc = score(title)
    if email == primary:
        sc += 200  # always keep the verified primary contact
    rows.append({"name": name, "title": title, "email": email, "score": sc})

rows.sort(key=lambda x: (-x["score"], x["email"]))
top = rows[:N]
rest = [x["email"] for x in rows[N:]]

print(f"# {dom}  cert={cert} company_id={cid}  total={len(rows)}  keeping top {len(top)}")
print(f"# primary contact: {primary}")
for x in top:
    p = " *PRIMARY" if x["email"] == primary else ""
    print(f'{x["score"]:4} | {x["name"]:26} | {str(x["title"])[:48]:48} | {x["email"]}{p}')
print("\n# SKIP (append to skipped_contacts.json):")
print(json.dumps(rest))
