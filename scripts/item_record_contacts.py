"""Merge email:id pairs into created_contacts.json. Usage:
item_record_contacts.py email1:id1 email2:id2 ..."""
import json
import sys

import config

path = config.FDIC_DATA_DIR / "item_import" / "created_contacts.json"
data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
for arg in sys.argv[1:]:
    email, _, cid = arg.rpartition(":")
    data[email] = int(cid)
path.write_text(json.dumps(data, indent=1), encoding="utf-8")
print(f"recorded {len(sys.argv) - 1}; total contacts = {len(data)}")
