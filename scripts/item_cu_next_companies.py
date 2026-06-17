"""Print the next N not-yet-created P5 credit-union companies. Usage: [N]"""
import json
import sys

import config

N = int(sys.argv[1]) if len(sys.argv) > 1 else 20
OUT = config.NCUA_DATA_DIR / "item_import"
comps = json.loads((OUT / "companies_p5.json").read_text(encoding="utf-8"))
cpath = OUT / "created_companies.json"
created = json.loads(cpath.read_text(encoding="utf-8")) if cpath.exists() else {}
done = set(created.keys())
todo = [c for c in comps if str(c["cu_number"]) not in done]
print(f"# P5 CU companies: {len(comps)} total | {len(done)} created | {len(todo)} remaining")
for c in todo[:N]:
    # Drop the address field — Item's geocoder is timing out on it. Addresses stay in
    # companies_p5.json and get backfilled in a later pass (item_cu_backfill_addresses).
    c = {**c, "fields": [f for f in c["fields"] if f["name"] != "address"]}
    print(json.dumps(c, ensure_ascii=False))
