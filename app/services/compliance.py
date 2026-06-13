"""Vendor invoice compliance engine — Cameroon CGI checklist.

Each uploaded invoice is AI-read (scans included) and then checked
deterministically against:

  I.  Conditions de FORME — Art. 150 (5) CGI: the 12 mandatory mentions.
      Exception (Art. 8 bis (2) tiret 1): the formalism is NOT opposable to
      foreign suppliers — form checks are waived (noted) for them.
  II. Conditions de FOND — the substance tests that make the charge
      deductible: supplier on the DGI active-taxpayer file (checked LIVE on
      Fiscalis via the vendor-NIU service), the ≥ FCFA 100,000 no-cash rule,
      tax-haven exposure, plus the judgement items (réalité, rattachement,
      non-exagération, plafonds) raised as review points — they cannot be
      proven from the invoice alone.

Verdict per invoice: PASSED / FAILED (with the failed items, the legal basis
and the remediation action) / plus review items either way. Batches run in a
background thread (4 invoices in parallel) so 20+ PDFs can be ingested at
once; results.json updates as each invoice completes.
"""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from ..config import DATA_DIR, load_config
from . import ai_ocr, dgi

BATCH_DIR = DATA_DIR / "compliance"
MAX_INVOICES = 25
CASH_CEILING = 100_000  # FCFA — Art. 8 bis (1)
_NIU_RE = re.compile(r"\b[PM]\s?\d{12}\s?[A-Z]\b", re.IGNORECASE)

# The 12 mandatory mentions of Art. 150 (5) — (extraction key, label, basis,
# remediation when missing).
FORM_CHECKS = [
    ("supplier_niu", "NIU du fournisseur", "Art. 150 (5) tiret 1",
     "Request a corrected invoice showing the supplier's NIU; verify it on "
     "DGI Fiscalis."),
    ("client_niu", "NIU du client (DHL)", "Art. 150 (5) tiret 1",
     "Ask the supplier to re-issue the invoice with DHL's NIU — share it "
     "with the vendor master data."),
    ("invoice_date", "Date de la facturation", "Art. 150 (5) tiret 2",
     "Request a dated invoice; an undated invoice cannot support the charge "
     "or the VAT deduction."),
    ("supplier_name", "Nom / raison sociale du fournisseur",
     "Art. 150 (5) tiret 2",
     "Request a corrected invoice with the supplier's full legal name."),
    ("supplier_address", "Adresse complète du fournisseur",
     "Art. 150 (5) tiret 2",
     "Request the supplier's complete address on the invoice."),
    ("supplier_rccm", "Numéro RCCM du fournisseur", "Art. 150 (5) tiret 2",
     "Request the supplier's trade-register (RCCM) number on the invoice."),
    ("client_name", "Identité complète du client", "Art. 150 (5) tiret 3",
     "Ask the supplier to invoice DHL's full legal entity name."),
    ("transaction_detail", "Nature, objet et détail de la transaction",
     "Art. 150 (5) tiret 4",
     "Request an itemised invoice describing the goods/services supplied."),
    ("amount_ht", "Prix Hors Taxe (HT)", "Art. 150 (5) tiret 5",
     "Request an invoice showing the pre-tax (HT) amount."),
    ("vat_rate_and_amount", "Taux et montant de la TVA",
     "Art. 150 (5) tiret 6",
     "Request the VAT rate and VAT amount on the invoice (or the "
     "exoneration mention if applicable)."),
    ("amount_ttc", "Montant total TTC dû", "Art. 150 (5) tiret 7",
     "Request the all-taxes-included (TTC) total on the invoice."),
    ("electronic_invoicing_marker",
     "Facture émise dans le système de facturation électronique DGI",
     "Art. 8 bis (2) tiret 2",
     "Confirm the supplier issues through the DGI e-invoicing system; "
     "request the normalised/e-billing version of the invoice."),
]


def _norm_niu(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


# Legal-form / connector words that don't identify the company itself.
_LEGAL_FORMS = {
    "SARL", "SARLU", "SA", "SAS", "SASU", "EURL", "SCS", "SNC", "SCI",
    "LTD", "LIMITED", "PLC", "LLC", "INC", "CORP", "CO", "GMBH", "CIE",
    "ETS", "ETABLISSEMENT", "ETABLISSEMENTS", "GROUP", "GROUPE", "AND", "ET",
}


def _name_tokens(s):
    return [t for t in re.sub(r"[^A-Z0-9]+", " ", str(s or "").upper()).split() if t]


def _name_matches(printed, configured):
    """Is the configured trading name present in the invoice's 'billed to'?

    Lenient by design (per the finance team): the test passes as soon as every
    DISTINCTIVE word of the configured name appears somewhere in the destinator
    — tolerating minor spelling variants (e.g. CAMEROON vs CAMEROUN), ignoring
    spacing, letter case, legal-form suffixes (SARL/SA/LTD…) and the surrounding
    noise of a multi-name 'billed to' block. No need to match the whole string
    or reveal the extra wording around it.
    """
    import difflib

    dest = _name_tokens(printed)
    if not dest:
        return False
    wanted = [t for t in _name_tokens(configured) if t not in _LEGAL_FORMS]
    if not wanted:                         # configured name was only a suffix
        wanted = _name_tokens(configured)
    if not wanted:
        return False
    for w in wanted:
        # Short, highly-distinctive tokens (e.g. "DHL") must match exactly;
        # longer words may differ by a spelling variant.
        found = any(
            t == w or (len(w) >= 4 and
                       difflib.SequenceMatcher(None, w, t).ratio() >= 0.85)
            for t in dest)
        if not found:
            return False
    return True


def evaluate(extraction, niu_status=None, company=None):
    """Apply the checklist to one AI-read invoice.

    extraction: dict from ai_ocr.extract_invoice_compliance.
    niu_status: dgi.verify_niu result for the supplier NIU (or None).
    company: {'niu', 'legal_name'} — OUR identity (set by the admin in
    Settings); when configured, the invoice's client NIU/name must match.
    Returns {verdict, failed, passed, review, foreign, ...}.
    """
    foreign = bool(extraction.get("is_foreign_supplier"))
    failed, passed, review = [], [], []
    company = company or {}

    # ---- Billed-to-us identity (applies to every supplier) -----------------
    our_niu = _norm_niu(company.get("niu"))
    if our_niu:
        printed = (extraction.get("client_niu") or {})
        if printed.get("present") and printed.get("value"):
            if _norm_niu(printed["value"]) == our_niu:
                passed.append({"item": "NIU client = NIU DHL (configuré)",
                               "basis": "Art. 150 (5) tiret 1",
                               "value": printed["value"][:40]})
            else:
                failed.append({
                    "item": "NIU client ≠ NIU DHL",
                    "basis": "Art. 150 (5) tiret 1",
                    "why": f"The invoice shows client NIU "
                           f"“{printed['value']}” but DHL's taxpayer ID on "
                           f"file is “{company.get('niu')}”.",
                    "action": "Ask the supplier to re-issue the invoice "
                              "billed to DHL's correct NIU — share it from "
                              "Settings → Company identity.",
                })
    our_name = (company.get("legal_name") or "").strip()
    if our_name:
        printed = (extraction.get("client_name") or {})
        if printed.get("present") and printed.get("value"):
            if _name_matches(printed["value"], our_name):
                # Trading name found on the invoice — confirm and move on,
                # without echoing the extra wording around it.
                passed.append({"item": f"Facturé à « {our_name} » (raison "
                                       "sociale configurée)",
                               "basis": "Art. 150 (5) tiret 3",
                               "value": ""})
            else:
                failed.append({
                    "item": "Identité client ≠ raison sociale DHL",
                    "basis": "Art. 150 (5) tiret 3",
                    "why": f"The invoice is billed to "
                           f"“{printed['value']}” but our legal name on "
                           f"file is “{our_name}”.",
                    "action": "Ask the supplier to invoice DHL's exact "
                              "legal entity name as configured in Settings.",
                })

    # ---- I. FORM — Art. 150 (5) -------------------------------------------
    if foreign:
        review.append({
            "item": "Fournisseur étranger — formalisme non opposable",
            "basis": "Art. 8 bis (2) tiret 1",
            "note": "The Art. 150 (5) mandatory mentions are waived for "
                    "foreign suppliers (e.g. no Cameroon NIU/RCCM required). "
                    "Form checks were not applied.",
            "action": "Keep proof that the supplier is genuinely foreign "
                      "(contract, foreign address, import documents).",
        })
    else:
        for key, label, basis, action in FORM_CHECKS:
            m = extraction.get(key) or {}
            ok = bool(m.get("present"))
            # VAT line passes if either VAT is shown or an exoneration
            # mention covers it (Art. 150 (5) tiret 8).
            if key == "vat_rate_and_amount" and not ok:
                exo = extraction.get("exoneration_mention") or {}
                if exo.get("present"):
                    passed.append({"item": label + " (exonérée)",
                                   "basis": "Art. 150 (5) tirets 6/8",
                                   "value": exo.get("value", "")})
                    continue
            if ok:
                passed.append({"item": label, "basis": basis,
                               "value": (m.get("value") or "")[:120]})
            else:
                failed.append({"item": label, "basis": basis,
                               "why": "Mention missing or unreadable on the "
                                      "invoice.",
                               "action": action})

        # NIU format sanity (14 chars, P/M + 12 digits + letter)
        niu_val = (extraction.get("supplier_niu") or {}).get("value", "")
        if (extraction.get("supplier_niu") or {}).get("present") and niu_val:
            if not _NIU_RE.search(niu_val.replace(" ", "")):
                review.append({
                    "item": "Format du NIU fournisseur douteux",
                    "basis": "Art. 150 (5) tiret 1",
                    "note": f"Printed NIU “{niu_val}” does not match the "
                            "standard 14-character format (P/M + 12 digits "
                            "+ letter).",
                    "action": "Verify the NIU on DGI Fiscalis and request a "
                              "correction if wrong.",
                })

    # ---- II. SUBSTANCE -----------------------------------------------------
    # 1. Supplier on the active-taxpayer file (live DGI check)
    if not foreign:
        st = (niu_status or {}).get("status")
        if st == "active":
            passed.append({"item": "Fournisseur au fichier des contribuables "
                                   "actifs (vérifié en ligne DGI)",
                           "basis": "Art. 8 bis (2) tiret 4",
                           "value": (niu_status or {}).get(
                               "raison_sociale", "")})
        elif st in ("inactive", "not_found"):
            failed.append({"item": "Fournisseur ABSENT/INACTIF au fichier des "
                                   "contribuables actifs",
                           "basis": "Art. 8 bis (2) tiret 4 — charge non "
                                    "déductible",
                           "why": "DGI Fiscalis returned: "
                                  + ((niu_status or {}).get("message")
                                     or st),
                           "action": "Stop payment until the supplier "
                                     "regularises their tax status; request "
                                     "an up-to-date attestation de "
                                     "conformité fiscale (ACF)."})
        else:
            review.append({"item": "Statut DGI du fournisseur non vérifié",
                           "basis": "Art. 8 bis (2) tiret 4",
                           "note": "The live Fiscalis check could not be "
                                   "completed (no NIU read, or DGI "
                                   "unreachable).",
                           "action": "Check the NIU manually on the Vendor "
                                     "NIU tool / Fiscalis before payment."})

    # 2. ≥ FCFA 100,000 → no cash payment
    ttc = extraction.get("total_ttc_numeric")
    if ttc is not None and ttc >= CASH_CEILING:
        review.append({
            "item": f"Montant ≥ FCFA {CASH_CEILING:,} — paiement en espèces "
                    "interdit",
            "basis": "Art. 8 bis (1) — charge non déductible si payée en "
                     "espèces",
            "note": f"Invoice total read as {ttc:,.0f} "
                    f"{extraction.get('currency') or 'FCFA'}.",
            "action": "Pay by bank transfer, cheque or mobile money and keep "
                      "the proof of payment with the invoice.",
        })

    # 3. Tax-haven exposure (foreign suppliers)
    if foreign:
        review.append({
            "item": "Vérifier l'État du fournisseur (paradis fiscal)",
            "basis": "Art. 8 ter ; Art. 8 bis (3)",
            "note": f"Supplier country read as: "
                    f"{extraction.get('supplier_country') or 'unknown'}.",
            "action": "Confirm the supplier is not domiciled in a "
                      "low-tax/non-cooperative territory; otherwise the "
                      "charge is non-deductible (customs-cleared local "
                      "purchases excepted).",
        })

    # 4. Judgement items — always raised for the reviewer
    review.append({
        "item": "Réalité, rattachement et non-exagération de la charge",
        "basis": "Art. 7, Art. 7-A — réintégration en cas de manquement",
        "note": "These cannot be proven from the invoice alone.",
        "action": "Attach the PO/contract and delivery evidence (réalité); "
                  "book the charge in the right financial year "
                  "(rattachement); confirm the price is at arm's length and "
                  "within the specific caps (frais de siège 2,5 %, "
                  "redevances 2,5 %, commissions 1 %, dons, intérêts "
                  "d'associés…).",
    })

    if extraction.get("readability") == "poor":
        review.append({
            "item": "Scan difficilement lisible",
            "basis": "—",
            "note": "The AI flagged the scan as poor quality; absent "
                    "mentions may simply be unreadable.",
            "action": "Re-scan the original at higher quality and re-run "
                      "the check.",
        })

    return {
        "verdict": "FAILED" if failed else "PASSED",
        "foreign": foreign,
        "failed": failed,
        "passed": passed,
        "review": review,
        "supplier": (extraction.get("supplier_name") or {}).get("value", ""),
        "invoice_no": (extraction.get("invoice_number") or {}).get("value", ""),
        "invoice_date": (extraction.get("invoice_date") or {}).get("value", ""),
        "ttc": ttc,
        "currency": extraction.get("currency") or "",
        "readability": extraction.get("readability") or "",
        # Taxpayer ID read off the invoice + the live Fiscalis verdict.
        "supplier_niu": (extraction.get("supplier_niu") or {}).get("value", ""),
        "dgi_status": ("n/a (foreign)" if foreign
                       else (niu_status or {}).get("status") or "unchecked"),
        "dgi_name": (niu_status or {}).get("raison_sociale", ""),
    }


# --------------------------------------------------------------------------- #
# Batch processing (background thread, 4 invoices in parallel)
# --------------------------------------------------------------------------- #
def _batch_path(token):
    return BATCH_DIR / token


def _results_file(token):
    return _batch_path(token) / "results.json"


_write_lock = threading.Lock()


def _save(token, payload):
    with _write_lock:
        _results_file(token).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _update_invoice(token, idx, updates):
    """Atomic read-modify-write so parallel workers never lose updates."""
    with _write_lock:
        payload = json.loads(_results_file(token).read_text(encoding="utf-8"))
        payload["invoices"][idx].update(updates)
        if all(i["status"] != "pending" for i in payload["invoices"]):
            payload["status"] = "done"
        _results_file(token).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def load_batch(token):
    p = _results_file(token)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def list_recent(limit=8):
    if not BATCH_DIR.exists():
        return []
    out = []
    for d in BATCH_DIR.iterdir():
        b = load_batch(d.name) if d.is_dir() else None
        if b:
            done = sum(1 for i in b["invoices"] if i["status"] != "pending")
            out.append({"token": d.name, "created_at": b.get("created_at", ""),
                        "total": len(b["invoices"]), "done": done,
                        "status": b.get("status", "")})
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out[:limit]


def create_batch(token, filenames):
    _batch_path(token).mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "running",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "invoices": [{"idx": i, "filename": fn, "status": "pending",
                      "result": None, "error": ""}
                     for i, fn in enumerate(filenames)],
    }
    _save(token, payload)
    return payload


def _process_one(token, idx, ai_cfg, company=None):
    pdf = _batch_path(token) / f"{idx:03d}.pdf"
    try:
        extraction = ai_ocr.extract_invoice_compliance(pdf.read_bytes(), ai_cfg)
        niu_status = None
        niu_m = extraction.get("supplier_niu") or {}
        if niu_m.get("present") and not extraction.get("is_foreign_supplier"):
            raw = (niu_m.get("value") or "").replace(" ", "")
            m = _NIU_RE.search(raw)
            if m:
                try:
                    niu_status = dgi.verify_niu(m.group(0).upper())
                except Exception:  # noqa: BLE001 — never kill the batch
                    niu_status = None
        result = evaluate(extraction, niu_status, company=company)
        inv_update = {"status": "done", "result": result, "error": ""}
    except (ai_ocr.AiNotConfigured, ai_ocr.AiReadError) as exc:
        inv_update = {"status": "error", "result": None, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        inv_update = {"status": "error", "result": None,
                      "error": f"{type(exc).__name__}: {exc}"}

    _update_invoice(token, idx, inv_update)


def run_batch_async(token):
    """Process every pending invoice in the batch on a background thread."""
    cfg = load_config()
    ai_cfg = cfg.get("ai")
    company = cfg.get("company") or {}

    def _runner():
        payload = load_batch(token)
        idxs = [i["idx"] for i in payload["invoices"]
                if i["status"] == "pending"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            for idx in idxs:
                pool.submit(_process_one, token, idx, ai_cfg, company)
        payload = load_batch(token)
        payload["status"] = "done"
        _save(token, payload)

    threading.Thread(target=_runner, daemon=True).start()


def build_excel(out_path, payload):
    """Compliance report workbook — one summary sheet + one detail sheet."""
    import xlsxwriter

    wb = xlsxwriter.Workbook(str(out_path))
    head = wb.add_format({"bold": True, "bg_color": "#0b2545",
                          "font_color": "white", "border": 1})
    cell = wb.add_format({"border": 1})
    wrap = wb.add_format({"border": 1, "text_wrap": True, "valign": "top"})
    ok_f = wb.add_format({"border": 1, "bold": True, "font_color": "#157a4a"})
    ko_f = wb.add_format({"border": 1, "bold": True, "font_color": "#bd3727"})

    ws = wb.add_worksheet("Summary")
    ws.set_column(0, 0, 28)
    ws.set_column(1, 3, 16)
    ws.set_column(4, 5, 17)
    ws.set_column(6, 6, 10)
    ws.set_column(7, 8, 50)
    for c, h in enumerate(["File", "Supplier", "Invoice no", "Date",
                           "Taxpayer ID (NIU)", "DGI Fiscalis status",
                           "Verdict", "Failed items (why)",
                           "Remediation / review actions"]):
        ws.write(0, c, h, head)
    r = 1
    for inv in payload["invoices"]:
        ws.write(r, 0, inv["filename"], cell)
        res = inv.get("result")
        if inv["status"] == "error" or not res:
            ws.write(r, 6, "ERROR", ko_f)
            ws.write(r, 7, inv.get("error", ""), wrap)
            r += 1
            continue
        ws.write(r, 1, res["supplier"], cell)
        ws.write(r, 2, res["invoice_no"], cell)
        ws.write(r, 3, res["invoice_date"], cell)
        ws.write(r, 4, res.get("supplier_niu", ""), cell)
        dgi_st = res.get("dgi_status", "")
        ws.write(r, 5, dgi_st.upper(),
                 ok_f if dgi_st == "active"
                 else (ko_f if dgi_st in ("inactive", "not_found") else cell))
        ws.write(r, 6, res["verdict"],
                 ok_f if res["verdict"] == "PASSED" else ko_f)
        ws.write(r, 7, "\n".join(f"• {f['item']} ({f['basis']}) — {f['why']}"
                                 for f in res["failed"]), wrap)
        actions = [f"• {f['action']}" for f in res["failed"]]
        actions += [f"• [review] {v['item']}: {v['action']}"
                    for v in res["review"]]
        ws.write(r, 8, "\n".join(actions), wrap)
        r += 1
    wb.close()
    return out_path
