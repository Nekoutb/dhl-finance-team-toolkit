"""Generate the ENEO allocation-key sample (replicates the attached Book1:
REPARTITION ENEO — customer -> functions -> % split).

Run: python scripts/make_eno_key_sample.py
"""
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "eno_allocation_key.xlsx"
OUT.parent.mkdir(parents=True, exist_ok=True)

# customer id -> (HT used only to fill the example MONTANT, [(function, pct)])
KEY = {
    "204691644": (1538672, [
        ("2370111005", 0.10), ("2371221005", 0.05), ("2370631005", 0.10),
        ("2371631005", 0.10), ("2371061005", 0.04), ("2371051005", 0.04),
        ("2370821005", 0.06), ("2371031005", 0.04), ("2371041005", 0.04),
        ("2371141005", 0.29), ("2370901005", 0.12), ("2370911005", 0.02)]),
    "200744960": (482120, [
        ("2370701017", 0.35), ("2371631017", 0.30),
        ("2370901017", 0.20), ("2371061017", 0.15)]),
    "200211025": (1236807, [
        ("2370071005", 0.15), ("2370771005", 0.30), ("2370061005", 0.15),
        ("2370701005", 0.15), ("2370041005", 0.15), ("2371051005", 0.10)]),
    "201998424": (69562, [("2370771005", 1.0)]),
    "201824589": (124831, [("2371631011", 1.0)]),
}

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Sheet1"
ws["B1"] = "REPARTITION ENEO"
ws.append(["NUMERO FACTURE", "refclient CMS /CMS Customer", "FONCTIONS",
           "%", "MONTANT"])
total_ht = 0
first = True
for cid, (ht, funcs) in KEY.items():
    total_ht += ht
    for i, (fn, pct) in enumerate(funcs):
        ws.append(["DHL-10-2022" if first and i == 0 else "",
                   cid if i == 0 else "", fn, pct, round(pct * ht, 2)])
    ws.append(["", "", "", 1, ht])   # per-customer subtotal
    first = False
tva = round(total_ht * 0.1925, 2)
ws.append(["TOTAL HT", "", "", "", total_ht])
ws.append(["TVA", "", "", "", tva])
ws.append(["", "TOTAL", "", "", round(total_ht + tva, 2)])
wb.save(OUT)
print(f"Wrote {OUT} ({len(KEY)} customers, TOTAL HT {total_ht:,}, "
      f"TTC {total_ht + tva:,.2f})")
