"""Build the bundled CM01 journal-entry template from the team's master file.

Reads the original CM01_PC TEMPLATE workbook and produces
samples/cm01_pc_template.xlsx with:
  - the CM01_* tab's sample data cleared (rows 4+; headers/formats kept),
  - BANK DETAILS cleared below its header row,
  - Sheet1 (sample paste area) cleared,
  - "Guide" and "List of IC Accounts" tabs removed,
  - CHECK + Doc Level Check formulas kept untouched.

Run (one-off, whenever the team's master template changes):
    python scripts/make_cm01_template.py "<path to the master template>"
"""
import sys
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "cm01_pc_template.xlsx"

DEFAULT_SRC = r"C:\Users\UltraBook 3.1\Desktop\CM01_PC TEMPLATE_.23.06.26_PC.xlsx"


def main(src):
    wb = openpyxl.load_workbook(src)          # keep formulas (no data_only)
    main_tab = next(n for n in wb.sheetnames if n.startswith("CM01_"))

    # 1. clear the JE lines (row 4 down) on the CM01 tab — keep formats/headers
    ws = wb[main_tab]
    for row in ws.iter_rows(min_row=4, max_row=ws.max_row):
        for c in row:
            c.value = None

    # 2. clear BANK DETAILS below its header
    bd = next((n for n in wb.sheetnames if n.strip().upper() == "BANK DETAILS"),
              None)
    if bd:
        ws = wb[bd]
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
            for c in row:
                c.value = None

    # 3. clear Sheet1 (sample paste area)
    if "Sheet1" in wb.sheetnames:
        ws = wb["Sheet1"]
        for row in ws.iter_rows():
            for c in row:
                c.value = None

    # 4. drop the tabs the team asked to remove
    for name in ("Guide", "List of IC Accounts"):
        if name in wb.sheetnames:
            del wb[name]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUT)
    print(f"Wrote {OUT}")
    print("  tabs:", openpyxl.load_workbook(OUT).sheetnames)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SRC)
