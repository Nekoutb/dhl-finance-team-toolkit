"""End-to-end self test of the parsing + PDF pipeline (no server needed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.services import excel_reader
from app.tools import orange_cameroun as orange

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "orange_cameroun_sample.xlsx"


def main():
    model = orange.parse_for_review(SAMPLE)
    print("Sheet      :", model["sheet"])
    print("Key column :", model["key_col"])
    print("Header     :", model["header"])
    print("Metadata   :")
    for m in model["metadata"]:
        print("   ", m)
    print(f"Rows ({len(model['rows'])}):")
    for r in model["rows"]:
        print(f"   idx={r['index']} key={r['key']!r} name={r['name']!r} "
              f"amount={r['data'].get('Amount (XAF)')}")

    # Build a PDF for the first transaction, naming the customer.
    cfg = load_config()
    out_pdf = ROOT / "data" / "outputs" / "selftest_receipt.pdf"
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    first = model["rows"][0]["index"]
    result = orange.build_receipt(
        SAMPLE, [first], {first: "ACME Logistics Sarl"}, cfg, out_pdf)
    print("\nPDF built  :", out_pdf, "exists=", out_pdf.exists(),
          "size=", out_pdf.stat().st_size if out_pdf.exists() else 0)
    print("Result     :", result)

    # Re-parse: the name should now auto-fill for the same key.
    model2 = orange.parse_for_review(SAMPLE)
    same_key = [r for r in model2["rows"] if r["index"] == first][0]
    print("Auto-fill  : key", same_key["key"], "->", same_key["name"])
    assert same_key["name"] == "ACME Logistics Sarl", "name not remembered!"
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
