"""Print the next N created CU companies that still need their address backfilled.
Reads addresses from companies_p5.json (kept there when companies were created
without address to dodge the geocoder). Usage: [N]"""
import json
import sys

import config

N = int(sys.argv[1]) if len(sys.argv) > 1 else 20
OUT = config.NCUA_DATA_DIR / "item_import"
comps = json.loads((OUT / "companies_p5.json").read_text(encoding="utf-8"))
created = json.loads((OUT / "created_companies.json").read_text(encoding="utf-8"))
bpath = OUT / "backfilled_addresses.json"
done = set(json.loads(bpath.read_text(encoding="utf-8"))) if bpath.exists() else set()

todo = []
for c in comps:
    cid = created.get(str(c["cu_number"]))
    if cid is None or str(cid) in done:
        continue
    addr = next((f["value"] for f in c["fields"] if f["name"] == "address"), None)
    if not addr:
        continue
    todo.append({"company_id": cid, "address": addr})

print(f"# address backfill: {len(created)} companies | {len(done)} done | {len(todo)} remaining")
for c in todo[:N]:
    print(json.dumps(c, ensure_ascii=False))
