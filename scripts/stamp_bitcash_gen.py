"""One-off after deploying the fixed-in-time reconciliations (v10.0).

Stamps the CURRENT BIT / Cash AR row store with generation ids. Every
existing sandbox predates generation tracking, so after this stamp they all
read as STALE (their row ids may point into replaced files — exactly the
mix-up being fixed) and show the "re-run matching" banner instead of
resolving wrong rows. Run once, as the data owner:

    sudo -u www-data .venv/bin/python scripts/stamp_bitcash_gen.py
"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools import bitcash  # noqa: E402


def main():
    store = bitcash.rows_store()
    changed = []
    for kind in ("bit", "cash"):
        if store.get(kind) and not store.get(f"gen_{kind}"):
            store[f"gen_{kind}"] = uuid.uuid4().hex[:12]
            changed.append(kind)
    if not changed:
        print("nothing to stamp (no rows on record, or already stamped)")
        return
    with bitcash._lock:
        bitcash._atomic(bitcash.ROWS_PATH,
                        json.dumps(store, ensure_ascii=False))
    print(f"stamped generation for: {', '.join(changed)}")
    print("existing sandboxes will now show the re-run-matching banner "
          "instead of resolving rows from replaced files.")


if __name__ == "__main__":
    main()
