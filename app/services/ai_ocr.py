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
# Cheque reading (scanned cheques received from customers)
# --------------------------------------------------------------------------- #
_CHEQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "cheque_number": {
            "type": "string",
            "description": "The cheque serial number — usually printed at the "
                           "top-right and repeated in the MICR band along the "
                           "bottom edge. Digits only; empty string if "
                           "unreadable.",
        },
        "customer_name": {
            "type": "string",
            "description": "The name of the account holder / drawer who issued "
                           "the cheque (the customer paying us) — printed on "
                           "the cheque, often above the signature or on the "
                           "letterhead. Empty string if unreadable.",
        },
        "amount": {
            "type": ["number", "null"],
            "description": "The cheque amount as a plain number (the figure in "
                           "the amount box; if only the amount in words is "
                           "legible, convert it). null if unreadable.",
        },
        "amount_currency": {
            "type": "string",
            "description": "Currency of the amount (e.g. XAF/FCFA, EUR). Empty "
                           "if not shown.",
        },
        "cheque_date": {
            "type": "string",
            "description": "The date written on the cheque, as ISO yyyy-mm-dd "
                           "when the day/month/year are clear; otherwise the "
                           "date exactly as printed. Empty string if "
                           "unreadable.",
        },
        "drawee_bank": {
            "type": "string",
            "description": "The bank the cheque is drawn on, from the bank name "
                           "printed on the cheque. Empty string if unreadable.",
        },
        "readability": {
            "type": "string", "enum": ["good", "partial", "poor"],
            "description": "How legible the cheque scan is overall.",
        },
    },
    "required": ["cheque_number", "customer_name", "amount", "amount_currency",
                 "cheque_date", "drawee_bank", "readability"],
    "additionalProperties": False,
}

_CHEQUE_PROMPT = (
    "This is a scanned bank cheque received from a customer as payment. The "
    "scan may be rotated, skewed or low quality — read it carefully in "
    "whatever orientation is needed (French or English).\n\n"
    "Extract:\n"
    "- cheque_number: the cheque's serial number. It is normally printed at "
    "the TOP-RIGHT of the cheque and repeated in the MICR band of symbols "
    "along the BOTTOM edge. Report digits only.\n"
    "- customer_name: the name of the account holder / drawer who issued the "
    "cheque — i.e. the customer paying us. It is usually printed near the "
    "signature line (bottom-right) or on the pre-printed account letterhead. "
    "Do NOT report the payee (the 'pay to the order of' line, which is us).\n"
    "- amount: the amount in figures from the amount box. If only the amount "
    "in words is legible, convert it to a number. Amounts may use spaces or "
    "commas as thousand separators (e.g. '1 500 000' = 1500000).\n"
    "- cheque_date: the date written on the cheque (return ISO yyyy-mm-dd when "
    "the day/month/year are clear).\n"
    "- drawee_bank: the bank the cheque is drawn on (the bank's printed name).\n"
    "- amount_currency and overall readability.\n\n"
    "If a field is genuinely unreadable, return an empty string (or null for "
    "the amount) rather than guessing."
)

_IMAGE_MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".gif": "image/gif"}


def media_type_for(filename):
    """Return the API media type for a cheque scan, or None if unsupported."""
    from pathlib import Path
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return "application/pdf"
    return _IMAGE_MEDIA.get(ext)


def extract_cheque(file_bytes, media_type, ai_cfg):
    """Read one scanned cheque → {cheque_number, customer_name, amount, ...}.

    ``media_type`` is 'application/pdf' for a PDF scan, else an image/* type.
    Raises AiNotConfigured (no key) or AiReadError (with a user-safe message).
    """
    api_key = (ai_cfg or {}).get("api_key", "").strip()
    if not api_key:
        raise AiNotConfigured(
            "No Anthropic API key saved — paste it under Settings → "
            "AI document reading.")
    if len(file_bytes) > MAX_PDF_BYTES:
        raise AiReadError("That cheque scan is too large to send for AI "
                          "reading (over 30 MB).")

    b64 = base64.standard_b64encode(file_bytes).decode("ascii")
    if media_type == "application/pdf":
        media_block = {"type": "document",
                       "source": {"type": "base64",
                                  "media_type": "application/pdf", "data": b64}}
    else:
        media_block = {"type": "image",
                       "source": {"type": "base64",
                                  "media_type": media_type, "data": b64}}

    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=180.0)
    model = (ai_cfg or {}).get("model") or "claude-opus-4-8"
    try:
        response = client.messages.create(
            model=model,
            max_tokens=4000,
            thinking={"type": "adaptive"},
            messages=[{
                "role": "user",
                "content": [media_block, {"type": "text", "text": _CHEQUE_PROMPT}],
            }],
            output_config={
                "format": {"type": "json_schema", "schema": _CHEQUE_SCHEMA},
            },
        )
    except anthropic.AuthenticationError:
        raise AiReadError("The Anthropic API key was rejected — check it "
                          "under Settings → AI document reading.")
    except anthropic.RateLimitError:
        raise AiReadError("Rate-limited by the Anthropic API — re-run this "
                          "cheque in a minute.")
    except anthropic.APIStatusError as exc:
        raise AiReadError(f"Anthropic API error ({exc.status_code}).")
    except anthropic.APIConnectionError:
        raise AiReadError("Could not reach the Anthropic API.")

    if response.stop_reason == "max_tokens":
        raise AiReadError("The AI reply was cut off — re-run this cheque.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise AiReadError("The AI reply could not be parsed — re-run this "
                          "cheque.")
    return {
        "cheque_number": "".join(ch for ch in str(data.get("cheque_number") or "")
                                 if ch.isdigit()),
        "customer_name": (data.get("customer_name") or "").strip(),
        "amount": data.get("amount"),
        "amount_currency": (data.get("amount_currency") or "").strip(),
        "cheque_date": (data.get("cheque_date") or "").strip(),
        "drawee_bank": (data.get("drawee_bank") or "").strip(),
        "readability": data.get("readability") or "",
    }


# --------------------------------------------------------------------------- #
# Bank statement reading (PDF/scanned statements → transaction lines)
# --------------------------------------------------------------------------- #
_BANK_SCHEMA = {
    "type": "object",
    "properties": {
        "bank_name": {
            "type": "string",
            "description": "The bank's name from the statement letterhead. "
                           "Empty string if not visible.",
        },
        "lines": {
            "type": "array",
            "description": "One entry per transaction line on the statement.",
            "items": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Transaction value/operation date as ISO "
                                       "yyyy-mm-dd when the day/month/year are "
                                       "clear; else as printed. Empty if none.",
                    },
                    "description": {
                        "type": "string",
                        "description": "The full narration / libellé text of the "
                                       "line, exactly as printed.",
                    },
                    "payer": {
                        "type": "string",
                        "description": "The originator/sender of the funds for a "
                                       "credit — the party who paid. Often printed "
                                       "after 'Donneur d'ordre' / 'Donneur "
                                       "d'Ordre'. Empty string if not identifiable.",
                    },
                    "credit": {
                        "type": "number",
                        "description": "Money IN (a credit / cash received) on "
                                       "this line, as a plain number. 0 if none.",
                    },
                    "debit": {
                        "type": "number",
                        "description": "Money OUT (a debit) on this line, as a "
                                       "plain number. 0 if none.",
                    },
                },
                "required": ["date", "description", "payer", "credit", "debit"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["bank_name", "lines"],
    "additionalProperties": False,
}

_BANK_PROMPT = (
    "This is a bank account statement (it may be a scan, multi-page, and in "
    "French or English). Read EVERY transaction line in the running-balance "
    "table.\n\n"
    "For each transaction, extract:\n"
    "- date: the value or operation date (ISO yyyy-mm-dd when clear).\n"
    "- description: the FULL narration / libellé exactly as printed.\n"
    "- payer: for money RECEIVED (a credit), the originator/sender of the "
    "funds — i.e. the customer who paid us. On many statements this is printed "
    "after the label 'Donneur d'ordre' (also 'Donneur d'Ordre', 'DONNEUR "
    "D'ORDRE'). Report just the payer's name/identity; empty string if a line "
    "has no identifiable payer.\n"
    "- credit: the amount of money RECEIVED on the line (cash collection / "
    "incoming), as a plain number; 0 if the line is not a credit.\n"
    "- debit: the amount of money paid OUT on the line, as a plain number; 0 "
    "if the line is not a debit.\n\n"
    "Amounts may use spaces or commas as thousand separators (e.g. "
    "'1 250 000' = 1250000) — report plain numbers. Also report the bank's "
    "name from the letterhead. Do NOT include opening/closing balance summary "
    "rows as transactions."
)


def extract_bank_statement(file_bytes, media_type, ai_cfg):
    """Read a bank statement (PDF or image) → {bank_name, lines:[...]}.

    Each line: {date, description, payer, credit, debit}. Raises AiNotConfigured
    (no key) or AiReadError (with a user-displayable message).
    """
    api_key = (ai_cfg or {}).get("api_key", "").strip()
    if not api_key:
        raise AiNotConfigured(
            "No Anthropic API key saved — paste it under Settings → "
            "AI document reading.")
    if len(file_bytes) > MAX_PDF_BYTES:
        raise AiReadError("That statement is too large to send for AI reading "
                          "(over 30 MB).")

    b64 = base64.standard_b64encode(file_bytes).decode("ascii")
    if media_type == "application/pdf":
        media_block = {"type": "document",
                       "source": {"type": "base64",
                                  "media_type": "application/pdf", "data": b64}}
    else:
        media_block = {"type": "image",
                       "source": {"type": "base64",
                                  "media_type": media_type, "data": b64}}

    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=300.0)
    model = (ai_cfg or {}).get("model") or "claude-opus-4-8"
    try:
        response = client.messages.create(
            model=model,
            max_tokens=32000,
            thinking={"type": "adaptive"},
            messages=[{
                "role": "user",
                "content": [media_block, {"type": "text", "text": _BANK_PROMPT}],
            }],
            output_config={
                "format": {"type": "json_schema", "schema": _BANK_SCHEMA},
            },
        )
    except anthropic.AuthenticationError:
        raise AiReadError("The Anthropic API key was rejected — check it "
                          "under Settings → AI document reading.")
    except anthropic.RateLimitError:
        raise AiReadError("Rate-limited by the Anthropic API — try again in a "
                          "minute.")
    except anthropic.APIStatusError as exc:
        raise AiReadError(f"Anthropic API error ({exc.status_code}).")
    except anthropic.APIConnectionError:
        raise AiReadError("Could not reach the Anthropic API.")

    if response.stop_reason == "max_tokens":
        raise AiReadError("The statement was too long to read in one pass — "
                          "split it into fewer pages and re-upload.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise AiReadError("The AI reply could not be parsed — re-upload the "
                          "statement.")
    lines = []
    for ln in data.get("lines", []):
        lines.append({
            "date": (ln.get("date") or "").strip(),
            "description": (ln.get("description") or "").strip(),
            "payer": (ln.get("payer") or "").strip(),
            "credit": round(float(ln.get("credit") or 0), 2),
            "debit": round(float(ln.get("debit") or 0), 2),
        })
    return {"bank_name": (data.get("bank_name") or "").strip(), "lines": lines}


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
