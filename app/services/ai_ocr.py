"""AI reading of scanned vendor invoices via the Anthropic API.

ENEO invoices arrive as image-only scans (no text layer), so pdfplumber/pypdf
extract nothing — this module sends the PDF itself to Claude, which reads the
scan visually and returns the per-"refclient CMS" HT amounts as validated
JSON (structured outputs), ready to prefill the allocation form. The user
always reviews/edits the amounts before generating the répartition.

The API key is pasted in Settings and stored in config.json (git-ignored).
"""
import base64
import json

MAX_PDF_BYTES = 30 * 1024 * 1024  # API limit is 32MB; keep headroom.

# Structured-output schema: per-customer HT lines + invoice identifiers.
_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_no": {
            "type": "string",
            "description": "Invoice / memoire number printed on the document "
                           "(e.g. 'N° mémoire'). Empty string if not visible.",
        },
        "period": {
            "type": "string",
            "description": "Billing period or invoice month if visible "
                           "(e.g. 'AVRIL 2026'). Empty string if not visible.",
        },
        "lines": {
            "type": "array",
            "description": "One entry per customer reference on the invoice.",
            "items": {
                "type": "object",
                "properties": {
                    "cms_ref": {
                        "type": "string",
                        "description": "The 'refclient CMS' / customer "
                                       "reference number, digits only.",
                    },
                    "ht_amount": {
                        "type": "number",
                        "description": "Amount excluding tax (HT / Hors "
                                       "Taxes) in XAF for that reference.",
                    },
                },
                "required": ["cms_ref", "ht_amount"],
                "additionalProperties": False,
            },
        },
        "total_ht": {
            "type": ["number", "null"],
            "description": "Total HT printed on the invoice, if visible.",
        },
        "total_ttc": {
            "type": ["number", "null"],
            "description": "Total TTC (all taxes included), if visible.",
        },
    },
    "required": ["invoice_no", "period", "lines", "total_ht", "total_ttc"],
    "additionalProperties": False,
}


class AiNotConfigured(Exception):
    """No API key saved yet — point the user at Settings."""


class AiReadError(Exception):
    """The API call failed; message is safe to show to the user."""


def is_configured(cfg):
    return bool((cfg.get("ai") or {}).get("api_key", "").strip())


def _prompt(expected_refs):
    refs = ", ".join(expected_refs) if expected_refs else "(none on file)"
    return (
        "This is a scanned ENEO (Cameroon electricity utility) vendor "
        "invoice. The scan may be rotated or low quality — read it "
        "carefully in whatever orientation is needed.\n\n"
        "Extract, for EVERY customer reference ('refclient CMS' / 'Réf. "
        "client' — a numeric customer account reference) that appears on "
        "the invoice, the amount EXCLUDING tax (HT / Hors Taxes / Montant "
        "HT) in XAF for that reference.\n\n"
        f"Our allocation key has these references on file: {refs}. "
        "The references on the invoice should match these — if a digit "
        "looks ambiguous in the scan, prefer the matching reference from "
        "this list. Still report any reference you see that is NOT in the "
        "list.\n\n"
        "Amounts use spaces or commas as thousand separators (e.g. "
        "'1 538 672' = 1538672). Report plain numbers without separators. "
        "Use the HT (pre-tax) amount per reference, NOT the TTC amount. "
        "Also report the invoice number, the billing period, and the "
        "printed TOTAL HT and TOTAL TTC if visible."
    )


def extract_eneo_invoice(pdf_bytes, expected_refs, ai_cfg):
    """Read a scanned ENEO invoice → {invoice_no, period, lines, totals}.

    Raises AiNotConfigured (no key) or AiReadError (anything else, with a
    user-displayable message).
    """
    api_key = (ai_cfg or {}).get("api_key", "").strip()
    if not api_key:
        raise AiNotConfigured(
            "No Anthropic API key saved — paste it under Settings → "
            "AI document reading.")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise AiReadError("That PDF is too large to send for AI reading "
                          "(over 30 MB).")

    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=180.0)
    model = (ai_cfg or {}).get("model") or "claude-opus-4-8"
    try:
        response = client.messages.create(
            model=model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.standard_b64encode(pdf_bytes)
                                          .decode("ascii"),
                        },
                    },
                    {"type": "text", "text": _prompt(expected_refs)},
                ],
            }],
            output_config={
                "format": {"type": "json_schema", "schema": _EXTRACT_SCHEMA},
            },
        )
    except anthropic.AuthenticationError:
        raise AiReadError("The Anthropic API key was rejected — check it "
                          "under Settings → AI document reading.")
    except anthropic.PermissionDeniedError:
        raise AiReadError("The Anthropic API key lacks permission for this "
                          "model — check your console.anthropic.com plan.")
    except anthropic.RateLimitError:
        raise AiReadError("The Anthropic API is rate-limiting us — wait a "
                          "minute and try again.")
    except anthropic.APIStatusError as exc:
        raise AiReadError(f"Anthropic API error ({exc.status_code}) — try "
                          "again shortly.")
    except anthropic.APIConnectionError:
        raise AiReadError("Could not reach the Anthropic API — check the "
                          "server's internet connection.")

    if response.stop_reason == "max_tokens":
        raise AiReadError("The AI reply was cut off before finishing — the "
                          "scan may be unusually long. Try a smaller PDF.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise AiReadError("The AI reply could not be parsed — try again.")

    # Normalise refs to digit strings so they match the allocation key ids.
    lines = {}
    for line in data.get("lines", []):
        ref = "".join(ch for ch in str(line.get("cms_ref", "")) if ch.isdigit())
        if ref:
            lines[ref] = round(float(line.get("ht_amount") or 0), 2)
    return {
        "invoice_no": (data.get("invoice_no") or "").strip(),
        "period": (data.get("period") or "").strip(),
        "lines": lines,
        "total_ht": data.get("total_ht"),
        "total_ttc": data.get("total_ttc"),
    }


# --------------------------------------------------------------------------- #
# Invoice compliance extraction (CGI Art. 150 (5) mentions + substance hints)
# --------------------------------------------------------------------------- #
def _mention():
    return {
        "type": "object",
        "properties": {
            "present": {"type": "boolean"},
            "value": {"type": "string",
                      "description": "The text as printed; empty if absent."},
        },
        "required": ["present", "value"],
        "additionalProperties": False,
    }


_COMPLIANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "supplier_name": _mention(),
        "supplier_niu": _mention(),
        "supplier_address": _mention(),
        "supplier_rccm": _mention(),
        "client_name": _mention(),
        "client_niu": _mention(),
        "invoice_date": _mention(),
        "invoice_number": _mention(),
        "transaction_detail": _mention(),
        "amount_ht": _mention(),
        "vat_rate_and_amount": _mention(),
        "amount_ttc": _mention(),
        "exoneration_mention": _mention(),
        "electronic_invoicing_marker": _mention(),
        "is_foreign_supplier": {
            "type": "boolean",
            "description": "True if the supplier is clearly established "
                           "outside Cameroon (foreign address, no Cameroon "
                           "NIU/RCCM, foreign currency).",
        },
        "supplier_country": {"type": "string"},
        "currency": {"type": "string"},
        "total_ttc_numeric": {
            "type": ["number", "null"],
            "description": "TTC total as a plain number, null if unreadable.",
        },
        "readability": {
            "type": "string", "enum": ["good", "partial", "poor"],
            "description": "How legible the scan is overall.",
        },
    },
    "required": ["supplier_name", "supplier_niu", "supplier_address",
                 "supplier_rccm", "client_name", "client_niu", "invoice_date",
                 "invoice_number", "transaction_detail", "amount_ht",
                 "vat_rate_and_amount", "amount_ttc", "exoneration_mention",
                 "electronic_invoicing_marker", "is_foreign_supplier",
                 "supplier_country", "currency", "total_ttc_numeric",
                 "readability"],
    "additionalProperties": False,
}

_COMPLIANCE_PROMPT = (
    "You are reading a vendor invoice (possibly a scan — it may be rotated or "
    "low quality; read carefully in any orientation, French or English).\n\n"
    "Report, for EACH item below, whether it is PRESENT on the invoice and "
    "the exact text as printed:\n"
    "- supplier_name: the supplier's name / raison sociale\n"
    "- supplier_niu: the supplier's Numéro d'Identifiant Unique (NIU) — a "
    "14-character Cameroon taxpayer number like 'M012345678901A' or "
    "'P...' (also labelled 'NIU', 'N° contribuable')\n"
    "- supplier_address: the supplier's full address\n"
    "- supplier_rccm: the supplier's trade-register number (RCCM / RC / "
    "Registre du Commerce)\n"
    "- client_name: the customer's full identity (name/raison sociale)\n"
    "- client_niu: the CUSTOMER's NIU\n"
    "- invoice_date: the invoice date\n"
    "- invoice_number: the invoice number\n"
    "- transaction_detail: the nature/object/detail of the goods or services\n"
    "- amount_ht: the price excluding tax (HT)\n"
    "- vat_rate_and_amount: the VAT rate AND the VAT amount\n"
    "- amount_ttc: the total all-taxes-included (TTC) due\n"
    "- exoneration_mention: any 'Exonérée' or 'Prise en charge État' "
    "mention per product\n"
    "- electronic_invoicing_marker: any sign the invoice was issued through "
    "the tax administration's e-invoicing system (QR code, DGI/e-billing "
    "mention, 'facture normalisée', verification code)\n\n"
    "Also report is_foreign_supplier, supplier_country, currency, the TTC "
    "total as a plain number, and overall scan readability."
)


def extract_invoice_compliance(pdf_bytes, ai_cfg):
    """Read one vendor invoice for the compliance checklist.

    Returns the structured mention dict. Raises AiNotConfigured/AiReadError.
    """
    api_key = (ai_cfg or {}).get("api_key", "").strip()
    if not api_key:
        raise AiNotConfigured(
            "No Anthropic API key saved — paste it under Settings → "
            "AI document reading.")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise AiReadError("PDF too large to send for AI reading (over 30 MB).")

    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=240.0)
    model = (ai_cfg or {}).get("model") or "claude-opus-4-8"
    try:
        response = client.messages.create(
            model=model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.standard_b64encode(pdf_bytes)
                                          .decode("ascii"),
                        },
                    },
                    {"type": "text", "text": _COMPLIANCE_PROMPT},
                ],
            }],
            output_config={
                "format": {"type": "json_schema",
                           "schema": _COMPLIANCE_SCHEMA},
            },
        )
    except anthropic.AuthenticationError:
        raise AiReadError("The Anthropic API key was rejected — check it "
                          "under Settings → AI document reading.")
    except anthropic.RateLimitError:
        raise AiReadError("Rate-limited by the Anthropic API — re-run this "
                          "invoice in a minute.")
    except anthropic.APIStatusError as exc:
        raise AiReadError(f"Anthropic API error ({exc.status_code}).")
    except anthropic.APIConnectionError:
        raise AiReadError("Could not reach the Anthropic API.")

    if response.stop_reason == "max_tokens":
        raise AiReadError("The AI reply was cut off — try a smaller PDF.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise AiReadError("The AI reply could not be parsed — re-run this "
                          "invoice.")


def test_key(ai_cfg):
    """Cheap round-trip to confirm the saved key works. Raises AiReadError."""
    api_key = (ai_cfg or {}).get("api_key", "").strip()
    if not api_key:
        raise AiNotConfigured("No API key saved yet.")
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=30.0)
    try:
        client.messages.create(
            model=(ai_cfg or {}).get("model") or "claude-opus-4-8",
            max_tokens=16,
            messages=[{"role": "user", "content": "Reply with OK."}],
        )
    except anthropic.AuthenticationError:
        raise AiReadError("Key rejected (authentication failed).")
    except anthropic.APIError as exc:
        raise AiReadError(f"{type(exc).__name__}: {exc}")
