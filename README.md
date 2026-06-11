# Finance Team Toolkit

An internal web app (FastAPI) where the finance team runs file-handling tasks —
one tool per function — with **no login**. The team uploads a file, the tool
processes it per the agreed procedure, and returns/sends the result.

This is a **draft**. The first live tool is the Orange Cameroun Mobile Money
converter; the others are placeholders awaiting your detailed procedures.

## Run it

```powershell
# from this folder
./run.ps1            # serves on http://127.0.0.1:8801 and opens the browser
./run.ps1 9000       # or pick another port
```

(Port 8000 is used by another app on this machine, so the default is **8801**.
Manual alternative: `python -m uvicorn app.main:app --port 8801 --reload`.)

## Live tool — Orange Cameroun: Mobile Money → PDF

1. **Dashboard → Orange Cameroun** and upload the Excel export (`.xlsx`).
2. The tool auto-detects the table header and lists every transaction line.
3. Tick **1 (or 2)** transaction line(s).
4. Confirm or type the **customer name**. It's remembered against that
   account/number and **auto-fills next time** the same key appears.
5. Click **Generate & email**:
   - If SMTP is configured (`config.json`), it emails the PDF directly.
   - Otherwise it produces a ready-to-send **`.eml`** (opens in Outlook) plus a
     **PDF** download.

Saved names: **Dashboard → Orange Cameroun → "View saved customer names"**.

## Configuration

Copy `config.example.json` to `config.json` and edit. `config.json` is
git-ignored (it can hold the SMTP password). Key fields:

| Field | Purpose |
|-------|---------|
| `orange_cameroun.default_recipient` | Pre-filled "Email to" address |
| `orange_cameroun.company_label` / `document_title` | PDF heading text |
| `orange_cameroun.email_subject_template` / `email_body_template` | Use `{customer}` placeholder |
| `smtp.enabled` + host/port/username/password/from_address | Direct email send |

## Project layout

```
app/
  main.py                 FastAPI routes
  config.py               config.json loading + defaults
  services/
    excel_reader.py       header auto-detection + row parsing
    pdf_writer.py         reportlab PDF receipt
    emailer.py            SMTP send + .eml builder
    customers.py          remembered customer names (data/customers.json)
  tools/
    registry.py           dashboard catalogue (add tools here)
    orange_cameroun.py    Orange Cameroun pipeline
  templates/  static/     UI
data/                     uploads, generated PDFs, customers.json (git-ignored)
```

## Adding a new tool

1. Add an entry to `app/tools/registry.py`.
2. Create `app/tools/<your_tool>.py` for the logic (reuse the `services/`).
3. Add routes in `app/main.py` and templates under `app/templates/`.

## Notes / to refine once you share specifics

- **Sample file**: the parser auto-detects layout, but a real Orange Cameroun
  export lets me lock the column mapping (amount, date, reference, customer key)
  and match the receipt to your required format.
- **Customer key column**: currently auto-guessed (phone/account-like column).
  Tune `KEY_CANDIDATES` in `excel_reader.py` once the file is known.
- **Email**: confirm the destination address and whether to send via SMTP
  (Office 365 etc.) or keep the `.eml` workflow.
```
