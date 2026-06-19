"""The catalogue of finance functions shown on the dashboard.

Two business functions (per the user's process landscape):
  - Order to Cash   : receivables — CtP Portal, remittance & allocation
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
        "name": "CtP Portal",
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
        "name": "Bank Statements — Open AR Appearing on the Bank Statements",
        "category": O2C,
        "icon": "🏦",
        "status": "draft",
        "description": (
            "Upload bank statements. Credited balances become collection "
            "reports: who paid (from the narration text), how much per day, and "
            "each payment linked to the customer's receivables position."
        ),
    },
    {
        "slug": "cheque-processing",
        "name": "Cheque Payment Processing",
        "category": O2C,
        "icon": "💵",
        "status": "draft",
        "description": (
            "Upload scanned cheques received from customers. The app reads each "
            "cheque (number, customer, amount, date) and scans your bank "
            "statements to confirm where each cheque number cleared — the bank, "
            "the amount and the transaction date per statement."
        ),
    },
    # ---- Record to Report ---------------------------------------------------
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
        "category": O2C,
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
        "slug": "variance-analysis",
        "name": "Variance Analysis — CY vs PY",
        "category": R2R,
        "icon": "📊",
        "status": "draft",
        "description": (
            "Upload prior-year and current-year trial balances + general "
            "ledgers. Get expense and balance-sheet accounts compared year on "
            "year — variance, % change, the journal descriptions driving each "
            "move, and the questions to ask."
        ),
    },
    {
        "slug": "invoice-compliance",
        "name": "Vendor Invoice Compliance Engine",
        "category": R2R,
        "icon": "⚖️",
        "status": "draft",
        "description": (
            "Drop in up to 25 vendor invoices (scans included — AI reads "
            "them). Each is checked against the CGI: Art. 150 (5) mandatory "
            "mentions, DGI active-taxpayer status, the no-cash rule and more "
            "— with a pass/fail verdict and the remediation to take."
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


def all_slugs():
    """Every tool slug — the assignable areas for per-user access rights."""
    return [tool["slug"] for tool in TOOLS]


def grouped():
    """Return tools grouped by category, preserving first-seen order."""
    groups = {}
    for tool in TOOLS:
        groups.setdefault(tool["category"], []).append(tool)
    return groups
