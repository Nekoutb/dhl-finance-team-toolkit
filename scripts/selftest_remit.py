"""Self-test the remittance portal logic + reconciliation PDF."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.services import remittance_pdf, remittance_store
from app.tools import remittance

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "gl_sample.xlsx"


def main():
    batch = remittance.build_statements_from_gl(SAMPLE, "gl_sample.xlsx")
    print(f"Batch {batch['id']} — {len(batch['customers'])} customers")
    for c in batch["customers"]:
        print(f"   {c['customer_name']:16s} inv={c['invoice_count']} "
              f"pay={c['payment_count']} "
              f"(inv {c['total_invoices']:.0f} / pay {c['total_payments']:.0f}) "
              f"token={c['token'][:8]}")
    assert len(batch["customers"]) == 4

    acme = next(c for c in batch["customers"] if c["customer_name"] == "ACME Logistics")
    stmt = remittance_store.load_statement(acme["token"])
    assert len(stmt["invoices"]) == 2 and len(stmt["payments"]) == 1, stmt

    # Customer allocates payment #0 to invoices #0 and #1 (4700 = 3200 + 1500).
    stmt = remittance.apply_allocation(acme["token"], payment_ids=[0],
                                       invoice_ids=[0, 1],
                                       confirmed_by="J. Doe (ACME)", note="May invoices")
    alloc = stmt["allocation"]
    print("\nAllocation:", alloc["total_payments"], "vs", alloc["total_invoices"],
          "diff", alloc["difference"])
    assert alloc["total_payments"] == 4700 and alloc["total_invoices"] == 4700
    assert alloc["difference"] == 0
    assert stmt["status"] == "allocated"

    # Reload from disk -> persisted (kept in memory for future reference).
    again = remittance_store.load_statement(acme["token"])
    assert again["status"] == "allocated"
    assert len(remittance_store.list_allocations()) >= 1

    out = ROOT / "data" / "outputs" / "selftest_remittance.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    remittance_pdf.build_remittance_pdf(out, stmt,
                                        organization=load_config()["organization"])
    print(f"PDF: {out} exists={out.exists()} size={out.stat().st_size}")
    print("\nALL REMITTANCE CHECKS PASSED")


if __name__ == "__main__":
    main()
