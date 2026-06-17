"""Print the next N not-yet-created companies for the Item import (resumable).

Reads companies_p{5,4}.json and created_companies.json (cert -> item id), and
prints the next N companies whose cert is not yet created, as compact JSON I can
turn into create_object calls. Usage: item_next_batch.py [tier] [N]
"""
import json
import sys

import config

tier = sys.argv[1] if len(sys.argv) > 1 else "5"
n = int(sys.argv[2]) if len(sys.argv) > 2 else 12
OUT = config.FDIC_DATA_DIR / "item_import"

companies = json.loads((OUT / f"companies_p{tier}.json").read_text(encoding="utf-8"))
created = json.loads((OUT / "created_companies.json").read_text(encoding="utf-8"))
done = {int(k) for k in created}

todo = [c for c in companies if c["cert_number"] not in done]
print(f"# tier P{tier}: {len(companies)} total, {len(companies) - len(todo)} created, {len(todo)} remaining")
batch = todo[:n]
for c in batch:
    print(json.dumps({
        "cert": c["cert_number"],
        "name": c["name"],
        "fields": c["fields"],
        "list_id": c["list_id"],
    }, ensure_ascii=False))
