"""Generate a synthetic Orange Money monthly statement sample.

Mirrors the real "Relevé de vos opérations" export: a metadata block, a failed-
transactions section, then the successful section with a "Solde initial" line.
Exercises the parser's filters — failed ("Echec") and outgoing C2C transfers
must be dropped, leaving only the successful credit collections.

Run: python scripts/make_orange_statement_sample.py
"""
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "orange_statement_sample.xlsx"
OUT.parent.mkdir(parents=True, exist_ok=True)

# Sub-header row: two "N° de Compte" columns (agent col 8, correspondant col 11).
HEADER = ["N°", "Date", "Heure", "Référence", "Service", "Paiement", "Statut",
          "Mode", "N° de Compte", "Wallet", "N° Pseudo", "N° de Compte",
          "Wallet", "Débit", "Crédit", "Compte: 658902134", "Sous-réseau"]


def tx(num, date, time, ref, service, statut, corr, debit="", credit=""):
    r = [""] * 17
    r[0], r[1], r[2], r[3], r[4] = num, date, time, ref, service
    r[5], r[6], r[7], r[8], r[9] = "Transaction", statut, "USSD", "658902134", "Normal"
    r[11], r[12], r[13], r[14], r[15], r[16] = corr, "Normal", debit, credit, 0, 0
    return r


wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Channel User Transaction Report"

for row in [
    ["."],
    ["Relevé de vos opérations"],
    ["Application :", "", "", "Orange Money"],
    ["Réseau :", "", "", "Cameroon"],
    ["Début de Période :", "", "", "01/05/2026"],
    ["Fin de Période :", "", "", "31/05/2026"],
    ["Type de rapport :", "", "", "Rapport mensuel"],
    ["Compte Orange Money :", "", "", "658902134"],
    ["Coordonnées entreprise :", "", "", "DHL EXPRESS - Cameroun, 1220005, Douala"],
    [],
    HEADER,                                            # failed-section header
]:
    ws.append(row)

# A failed B2B attempt (no correspondant, status Echec) — must be excluded.
e = [""] * 17
e[0], e[1], e[2], e[3] = 1, "17/05/2026", "14:26:49", "BM260517.1426.C00598"
e[4], e[5], e[6], e[8], e[9], e[12], e[14] = ("B2B Merchant PAyment", "Transaction",
                                              "Echec", "658902134", "Inconnu",
                                              "Normal", 300000)
ws.append(e)

ws.append(HEADER)                                      # success-section header
# "Solde initial" opening balance — first cell blank, so it is not a tx row.
s = [""] * 17
s[3], s[4], s[8], s[9], s[11], s[14] = ("Transactions réussies", "Wallet Normal",
                                        "658902134", "Normal", "Solde initial",
                                        3054489)
ws.append(s)

SUCCESS = [
    tx(3, "01/05/2026", "16:02:40", "MP260501.1602.D76168", "Merchant Payment",
       "Succès", "699559325", credit=51800),
    tx(4, "02/05/2026", "18:06:15", "MP260502.1806.C94471", "Merchant Payment",
       "Succès", "696578717", credit=86374),
    tx(5, "03/05/2026", "09:22:19", "MP260503.0922.C40028", "Merchant Payment",
       "Succès", "694707993", credit=280000),
    tx(6, "06/05/2026", "13:55:04", "MP260506.1355.B76232", "Merchant Payment",
       "Succès", "699559325", credit=103600),
]
for r in SUCCESS:
    ws.append(r)
# An outgoing C2C transfer (débit, no crédit) — must be excluded.
ws.append(tx(7, "06/05/2026", "08:44:12", "PP260506.0844.D89207", "C2C Transfer",
             "Succès", "694893676", debit=3472663))

wb.save(OUT)
total = sum(r[14] for r in SUCCESS)
print(f"Wrote {OUT}")
print(f"  successful collections: {len(SUCCESS)} = {total:,} XAF "
      f"across 3 correspondants (Echec + C2C excluded)")
