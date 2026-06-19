# Finance Team Toolkit

An internal web platform (FastAPI, local-hosted, **no login**) where the DHL
Cameroon finance team runs its recurring tasks: upload a file, the tool
processes it per the agreed policy/procedure, and returns the result on
screen, as Excel/PDF, or by email. Liquid-glass UI with light/dark mode and a
command palette (`Ctrl+K`; `g`-shortcuts per tool).

## Run it

| Action | How |
|---|---|
| Start / restart | Double-click **`Start Finance Toolkit.bat`** (kills any stuck instance first) |
| Open | **http://127.0.0.1:8801** (the footer shows the running version) |
| Stop | **`Stop Finance Toolkit.bat`** |
| Auto-start | Starts silently at Windows logon (Startup-folder script) |

Dependencies: `pip install -r requirements.txt` (Python 3.13). Port 8000 is
used by another app on this machine — the toolkit lives on **8801**.

## Tools

### Order to Cash

- **🧭 CtP Portal** — upload the **AR transaction file** (invoice
  level; totals lines auto-excluded), optional **customer master file(s)**
  (segment → treatment plan, balances, credit-hold position; several files
  merge) and an optional **AR trial balance** (first analysis: transaction
  total agreed to the TB). Produces the receivables control dashboard
  (aging %, unapplied cash %, credit-hold projections, editable small-balance
  threshold, bank-statement matching), per-invoice controls on the Credit
  Policy v3.3 timelines (letters handled by e-dunning), credit-hold
  exceptions vs the register/master, and a multi-sheet Excel report.
- **🔗 Remittance & Allocation Portal** — upload the daily customer GL; every
  customer gets a private link to view their statement (oldest-first, customer
  references), tick the payments they made and the invoices settled, and
  confirm with obligatory contact details. The reconciliation PDF goes to the
  customer and to the configured finance recipients, and stays on file.
- **🏦 Bank Statements — Collection Activity** — upload up to 10 statements at
  once (Excel or PDF); each bank is identified from its letterhead. Credited
  balances become collection reports: who paid (matched from narration text),
  daily collections, unmatched credits, and each payment linked to the
  customer's AR position (coverage %, remaining, risk status, hold state).

### Record to Report

- **📱 Mobile Money (Orange & MTN) → PDF** — upload the provider report
  (Excel or PDF), select one transaction or any group, extract a receipt that
  carries the document's own header/logo; file names start with the
  transaction reference (e.g. `C00598_…`). Third-party names and account
  numbers are remembered and auto-filled. Send by email directly.
- **📄 Orange Money → PDF** — the original single-provider variant.
- **🛡️ Vendor Tax Status (NIU) Verification** — paste vendors or upload the
  Excel vendor list (name, NIU, tax clearance certificate, issue date, email).
  Each NIU is checked live on **DGI Fiscalis** ("Vérifier un NIU");
  certificates are valid **3 months** from issue. The report applies the
  payment control — **OK TO PAY only when the certificate is valid and the
  NIU is ACTIVE** — and one click emails non-compliant vendors that overdue
  invoices will not be paid until the situation is regularised.

## Settings

- **⚙️ /settings** — SMTP (host/port/TLS/credentials + test send). Until SMTP
  is enabled, every email feature produces ready-to-send `.eml` files instead.
- Remittance portal has its own settings page (finance recipient emails) and
  a customer-contacts register; the Mobile Money tool has a default-recipient
  setting.

## Project layout

```
app/
  main.py               routes (one section per tool)
  config.py             config.json loading + APP_VERSION + save_user_config
  services/             shared engines: excel_reader (.xlsx/.xlsm/.xls),
                        pdf_table_reader, header_assets, pdf_writer,
                        ctp_rules (Credit Policy v3.3), ctp_dashboard,
                        ar_master, bank_statement, dgi (Fiscalis client),
                        vendors, holds, customers, emailer, stores…
  tools/                one module per tool + registry.py (dashboard catalogue)
  templates/ static/    UI (liquid-glass design system, app.js)
scripts/                sample generators, test suites (httptest_*.py),
                        export_static.py (offline HTML preview -> preview/)
samples/                synthetic demo files for every tool
data/                   runtime data — git-ignored, stays on this machine
```

## Tests & preview

```powershell
python scripts/httptest.py            # Orange flow      (+ _remit, _bank,
python scripts/httptest_ctp.py        # CtP flow            _vendor,
python scripts/httptest_smtp.py       # settings            _xls selftest)
python scripts/export_static.py       # 24-page offline HTML preview
```

Suites that touch email run under `scripts/testutil.smtp_guard()` so they can
never send real mail; suites snapshot/restore any store they touch.

## Repo hygiene

`config.json` (SMTP credentials), the whole `data/` directory (real finance
data: uploads, analyses, statements, registers, contacts) and generated
`preview/` pages are **git-ignored** — the repository contains code, synthetic
samples and tests only.

## Adding a tool

1. Add an entry in `app/tools/registry.py` (pick the business function).
2. Create `app/tools/<tool>.py` reusing the `services/`.
3. Add routes in `app/main.py`, templates under `app/templates/<tool>/`,
   a sample generator + `scripts/httptest_<tool>.py`, and an exporter entry.
