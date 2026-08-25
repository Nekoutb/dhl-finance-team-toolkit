"""A French-language SAP export must map like the English one.

The FBL5N 'AR DETAILS CM' file arrives with French headers when the SAP login
language is French ('Valeur de la devise de la pièce', 'Echéance nette'…) —
this once left amount/dates unmapped, so every dashboard KPI showed zero.
Also covers the AGEING trial balance whose account column is 'Client' and
whose balance column is 'Total Ageing'.
"""
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openpyxl

from app.services import ar_master
from app.tools import ongoing_ctp as ctp


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


tmp = Path(tempfile.mkdtemp(prefix="ctp_french_"))

# --- French AR transaction file (FBL5N export, French login) -----------------
FR_HEADER = ["Société", "Client", "Compte client : nom 1", "Date de pièce",
             "Valeur de la devise de la pièce", "Référence", "Numéro de pièce",
             "Type de pièce", "Gestionnaire", "Echéance nette",
             "Clé de devise de la pièce"]
FR_ROWS = [
    ["CM01", "1004000001", "ACME LOGISTICS", datetime(2026, 5, 1), 1500000,
     "YAOR0001", "90000001", "X4", "Olga", datetime(2026, 5, 15), "XAF"],
    ["CM01", "1004000001", "ACME LOGISTICS", datetime(2026, 8, 1), 800000,
     "YAOR0002", "90000002", "X4", "Olga", datetime(2026, 8, 15), "XAF"],
    ["CM01", "1004000002", "BETA SARL", datetime(2026, 8, 10), -400000,
     "PAY0001", "1400000001", "DZ", "Olga", datetime(2026, 8, 10), "XAF"],
]
tx_path = tmp / "ar_details_fr.xlsx"
wb = openpyxl.Workbook()
ws = wb.active
ws.append(FR_HEADER)
for r in FR_ROWS:
    ws.append(r)
wb.save(tx_path)

result = ctp.analyze(tx_path, as_of=date(2026, 8, 25))
m = result["mapping"]
check("amount mapped (Valeur de la devise…)",
      m.get("amount") == "Valeur de la devise de la pièce")
check("due date mapped (Echéance nette)", m.get("due_date") == "Echéance nette")
check("account mapped (Client)", m.get("account") == "Client")
check("customer name mapped (Compte client : nom 1)",
      m.get("customer") == "Compte client : nom 1")
check("invoice no mapped (Numéro de pièce)",
      m.get("invoice_no") == "Numéro de pièce")
check("currency mapped (Clé de devise…)",
      m.get("currency") == "Clé de devise de la pièce")
check("clerk mapped (Gestionnaire)", m.get("clerk") == "Gestionnaire")
s = result["summary"]
check("amounts read (net 1,900,000)", round(s["total_ar"]) == 1900000)
check("credit line classified", s["credit_count"] == 1)
check("overdue computed from Echéance nette (both overdue invoices)",
      round(s["overdue_total"]) == 2300000)
over60 = [i for i in result["invoices"] if i["days_overdue"] > 60]
check("aging >60d populated (KPIs not zero)",
      len(over60) == 1 and round(over60[0]["amount"]) == 1500000)

# --- AGEING trial balance: Client + Total Ageing + Account Stop/Open ---------
AG_HEADER = ["Collector ID", "Customer Name", "Account Stop/Open",
             "Payment Term", "Client", "Total Ageing", "<= 030 days"]
AG_ROWS = [
    ["01", "ACME LOGISTICS", "", "Z015", "1004000001", 2300000, 800000],
    ["02", "BETA SARL", "X", "Z030", "1004000002", -400000, 0],
]
tb_path = tmp / "ageing.xlsx"
wb = openpyxl.Workbook()
ws = wb.active
ws.append(AG_HEADER)
for r in AG_ROWS:
    ws.append(r)
wb.save(tb_path)

parsed = ar_master.parse_master(tb_path)
mm = parsed["mapping"]
check("ageing: account = Client (not Account Stop/Open)",
      mm.get("account") == "Client")
check("ageing: hold column found", mm.get("hold") == "Account Stop/Open")
check("ageing: balance = Total Ageing", mm.get("balance") == "Total Ageing")
check("ageing: BETA flagged on hold",
      parsed["customers"]["1004000002"]["on_hold"] is True)
check("ageing: ACME open",
      parsed["customers"]["1004000001"]["on_hold"] is False)
check("ageing: balances read",
      parsed["customers"]["1004000001"]["balance"] == 2300000)

tb = ctp.parse_trial_balance(tb_path)
check("TB total from Total Ageing", round(tb["total"]) == 1900000)

print("\nALL CTP FRENCH-EXPORT TESTS PASSED")
