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
