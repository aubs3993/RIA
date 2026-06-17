"""Print the next N not-yet-created P5 contacts, with company_id resolved.

Reads contacts_named_p5p4.json (priority==5), maps cert -> company item id via
created_companies.json, skips already-created (created_contacts.json by email),
and skips the 3 RIA-domain banks (company id < first bank id) so bank staff are
never attached to an RIA company. Usage: item_next_contacts.py [N]
"""
import json
import sys

import config

N = int(sys.argv[1]) if len(sys.argv) > 1 else 20
FIRST_BANK_ID = 229083  # anything below this is a pre-existing (RIA) company
OUT = config.FDIC_DATA_DIR / "item_import"

contacts = json.loads((OUT / "contacts_named_p5p4.json").read_text(encoding="utf-8"))
created_co = json.loads((OUT / "created_companies.json").read_text(encoding="utf-8"))
ct_path = OUT / "created_contacts.json"
created_ct = json.loads(ct_path.read_text(encoding="utf-8")) if ct_path.exists() else {}
done = set(created_ct.keys())
# Emails deliberately excluded (non-decision-maker staff at capped big-directory banks).
skip_path = OUT / "skipped_contacts.json"
skipped = set(json.loads(skip_path.read_text(encoding="utf-8"))) if skip_path.exists() else set()
done |= skipped

p5 = [c for c in contacts if c["priority"] == 5]
todo, skipped_ria, no_company = [], 0, 0
for c in p5:
    if c["email"] in done:
        continue
    cid = created_co.get(str(c["cert_number"]))
    if cid is None:
        no_company += 1
        continue
    if cid < FIRST_BANK_ID:
        skipped_ria += 1
        continue
    todo.append({"name": c["name"], "email": c["email"], "role": c["role"], "company_id": cid})

print(f"# P5 contacts: {len(p5)} total | {len(done)} created | {len(todo)} remaining"
      f" | {skipped_ria} skipped(RIA) | {no_company} no-company")
for c in todo[:N]:
    print(json.dumps(c, ensure_ascii=False))
