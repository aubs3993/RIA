"""Print the next N not-yet-created P5 credit-union contacts, company_id resolved.
Key = email, or 'ceo:<cu_number>' for a phone-only CEO. Usage: [N]"""
import json
import sys

import config

N = int(sys.argv[1]) if len(sys.argv) > 1 else 20
OUT = config.NCUA_DATA_DIR / "item_import"
contacts = json.loads((OUT / "contacts_p5.json").read_text(encoding="utf-8"))
created_co = json.loads((OUT / "created_companies.json").read_text(encoding="utf-8"))
ctpath = OUT / "created_contacts.json"
created_ct = json.loads(ctpath.read_text(encoding="utf-8")) if ctpath.exists() else {}
done = set(created_ct.keys())


def key(c):
    return c["email"] if c["email"] else f"ceo:{c['cu_number']}"


todo, no_co = [], 0
for c in contacts:
    k = key(c)
    if k in done:
        continue
    cid = created_co.get(str(c["cu_number"]))
    if cid is None:
        no_co += 1
        continue
    todo.append({"key": k, "name": c["name"], "email": c["email"], "role": c["role"],
                 "phone": c["phone"], "company_id": cid})
print(f"# P5 CU contacts: {len(contacts)} total | {len(done)} created | {len(todo)} remaining"
      f" | {no_co} no-company")
for c in todo[:N]:
    print(json.dumps(c, ensure_ascii=False))
