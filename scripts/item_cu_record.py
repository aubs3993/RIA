"""Merge key:id pairs into a CU checkpoint.
Usage: item_cu_record.py <companies|contacts> key:id key:id ...
(key may itself contain ':' e.g. 'ceo:1234' — split on the LAST ':')"""
import json
import sys

import config

fname = {"companies": "created_companies.json", "contacts": "created_contacts.json",
         "addresses": "backfilled_addresses.json"}[sys.argv[1]]
path = config.NCUA_DATA_DIR / "item_import" / fname
data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
for arg in sys.argv[2:]:
    k, _, v = arg.rpartition(":")
    data[k] = int(v)
path.write_text(json.dumps(data, indent=1), encoding="utf-8")
print(f"recorded {len(sys.argv) - 2}; total = {len(data)}")
