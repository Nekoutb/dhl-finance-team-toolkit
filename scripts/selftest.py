"""End-to-end self test of the Orange statement parsing + receipt-ZIP
pipeline (no server needed) — against the current correspondant model."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import customers
from app.tools import orange_cameroun as orange

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "orange_statement_sample.xlsx"


def main():
    model = orange.parse_for_review(SAMPLE)
    print("Meta       :", model["meta"])
    print("Totals     :", model["totals"])
    print(f"Correspondants ({len(model['correspondants'])}):")
    for r in model["correspondants"]:
        print(f"   {r['correspondant']!r} count={r['count']} "
              f"total={r['total_fmt']} name={r['name']!r}")
    assert model["correspondants"], "no correspondants parsed from the sample!"
    assert model["collections"], "no collections parsed from the sample!"

    # Build the receipts ZIP, naming the first correspondant.
    first = model["correspondants"][0]["correspondant"]
    out_zip = ROOT / "data" / "outputs" / "selftest_receipts.zip"
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    result = orange.build_zip(
        SAMPLE, {first: {"name": "ACME Logistics Sarl", "account": ""}}, out_zip)
    print("\nZIP built  :", out_zip, "exists=", out_zip.exists(),
          "size=", out_zip.stat().st_size if out_zip.exists() else 0)
    print("Result     :", result)
    assert out_zip.exists() and out_zip.stat().st_size > 0, "ZIP not built!"

    # Re-parse: the name should now auto-fill for the same correspondant.
    model2 = orange.parse_for_review(SAMPLE)
    same = [r for r in model2["correspondants"]
            if r["correspondant"] == first][0]
    print("Auto-fill  :", first, "->", same["name"])
    assert same["name"] == "ACME Logistics Sarl", "name not remembered!"

    # Leave no test identity behind in the customer register.
    customers.delete_name(orange.TOOL_SLUG, first)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
