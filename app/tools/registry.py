"""The catalogue of finance functions shown on the dashboard.

Two business functions (per the user's process landscape):
  - Order to Cash   : receivables — CtP monitoring, remittance & allocation
  - Record to Report: conversions & compliance — Mobile Money / Orange to PDF,
                      vendor NIU verification

Add a dict to surface a new tool. ``status: "live"``/"draft" link to a working
page; ``"coming_soon"`` are placeholders awaiting the user's procedure.
List order = display order on the landing page.
"""

O2C = "Order to Cash"
R2R = "Record to Report"

TOOLS = [
    # ---- Order to Cash ------------------------------------------------------
    {
        "slug": "ongoing-ctp-monitoring",
        "name": "Ongoing CtP Monitoring",
        "category": O2C,
        "icon": "🧭",
        "status": "draft",
        "description": (
            "Collections Treatment Plan compliance. Feed the AR breakdown at "
            "invoice level (+ the customer master file); get the receivables "
            "dashboard, the controls per invoice and customer, and credit-hold "
            "exceptions."
        ),
    },
    {
        "slug": "remittance-portal",
        "name": "Remittance & Allocation Portal",
        "category": O2C,
        "icon": "🔗",
        "status": "draft",
        "description": (
            "Upload the daily customer general ledgers. Each customer gets a "
            "unique link to view their statement, match payments to invoices, and "
            "confirm. A reconciliation PDF goes to the customer and to finance, "
            "and is kept on file."
        ),
    },
    {
        "slug": "bank-statements",
        "name": "Bank Statements — Collection Activity",
        "category": O2C,
        "icon": "🏦",
        "status": "draft",
        "description": (
            "Upload bank statements. Credited balances become collection "
            "reports: who paid (from the narration text), how much per day, and "
            "each payment linked to the customer's receivables position."
        ),
    },
    # ---- Record to Report ---------------------------------------------------
    {
        "slug": "momo",
        "name": "Mobile Money Payments (Orange & MTN) → PDF",
        "category": R2R,
        "icon": "📱",
        "status": "draft",
        "description": (
            "Upload the provider's mobile-money report (Excel or PDF). Pick one "
            "transaction or a whole group and extract a PDF with the report "
            "header + the selected lines. Third-party names are remembered and "
            "auto-filled next time."
        ),
    },
    {
        "slug": "vendor-invoice-allocation",
        "name": "Vendor Invoice Allocation",
        "category": R2R,
        "icon": "🧮",
        "status": "draft",
        "description": (
            "MTN, Orange and ENEO. Upload the vendor invoice, enter the amounts, "
            "and the platform builds the cost-allocation sheet (e.g. REPARTITION "
            "ENEO) ready to view and download as Excel."
        ),
    },
    {
        "slug": "orange-cameroun",
        "name": "Orange Money → PDF",
        "category": R2R,
        "icon": "📄",
        "status": "live",
        "description": (
            "Extract one transaction or a whole group from the Orange Cameroun "
            "Excel export into a PDF receipt — document header & logo included, "
            "customer names remembered — and email it."
        ),
    },
    {
        "slug": "vendor-niu",
        "name": "Vendor Tax Status (NIU) Verification",
        "category": R2R,
        "icon": "🛡️",
        "status": "draft",
        "description": (
            "Paste vendors (name, taxpayer ID/NIU, tax clearance certificate). "
            "The app checks each NIU on the DGI Fiscalis platform, records the "
            "taxpayer's status, and exports the table to Excel."
        ),
    },
    {
        "slug": "excel-to-pdf-generic",
        "name": "General Excel → PDF Converter",
        "category": R2R,
        "icon": "🧾",
        "status": "coming_soon",
        "description": (
            "Convert other approved Excel files to PDF. "
            "Awaiting your detailed procedure."
        ),
    },
]


def by_slug(slug):
    for tool in TOOLS:
        if tool["slug"] == slug:
            return tool
    return None


def grouped():
    """Return tools grouped by category, preserving first-seen order."""
    groups = {}
    for tool in TOOLS:
        groups.setdefault(tool["category"], []).append(tool)
    return groups
