"""The catalogue of finance functions shown on the dashboard.

Two business functions (per the user's process landscape):
  - Order to Cash   : receivables — CtP Portal, remittance & allocation,
                      bank statements, cheques, Orange Money receipts
  - Record to Report: conversions — invoice allocation, variance analysis

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
        "name": "Electronic Cheque Register",
        "category": O2C,
        "icon": "💵",
        "status": "draft",
        "description": (
            "Upload scanned cheques received from customers. Each cheque is "
            "read (cheque number, client, issuing bank, amount, date) into a "
            "permanent register, and every cheque number is checked against "
            "the bank statements on file — a green tick when it appears (with "
            "the bank, date and amount credited), a red cross when not."
        ),
    },
    {
        "slug": "account-stop",
        "name": "Account Stop — Payments on Stopped Accounts",
        "category": O2C,
        "icon": "⛔",
        "status": "draft",
        "description": (
            "Every account currently on credit stop — account number, name, "
            "total AR and overdue balance — with the payment traces found for "
            "it across the bank statements, the cheque register and Orange "
            "Money, so a stopped account that has paid is spotted at once."
        ),
    },
    {
        "slug": "quick-statement",
        "name": "Quick Account Statement",
        "category": O2C,
        "icon": "🧾",
        "status": "draft",
        "description": (
            "Generate a customer account statement in one click, for customer "
            "requests — built from the open items already on file in the "
            "latest CtP analysis (no new upload needed)."
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
        "slug": "bit-cash-ar",
        "name": "BIT & Cash AR",
        "category": R2R,
        "icon": "🏧",
        "status": "draft",
        "description": (
            "Upload the Bank In Transit (BIT) and Cash Account Receivables "
            "(Cash AR) files — open items are counted per day and plotted. "
            "Reconcile customer payment statements against both (invoices in "
            "the Cash AR, the total paid in the BIT), approve each sandbox, "
            "and generate the CM01 journal-entry file."
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
