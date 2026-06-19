"""Parse an Orange Money monthly transaction statement (Relevé de vos
opérations / "Channel User Transaction Report").

The export is not a flat table: rows 0-19 carry a metadata block (Application,
Réseau, Compte, period…), then one or more sections each introduced by a
two-line header (group row + column row) and seeded with a "Transactions
échouées / réussies" title and a "Solde initial" line. Real transactions are
the rows whose first cell is a number (N°).

We read it dynamically (column positions found from the header row, with a
fixed-position fallback) so April/May layouts both parse. For collections we
keep only SUCCESSFUL credits — that excludes failed ("Echec") attempts and
outgoing C2C transfers (which sit on the débit side), exactly the rows the
finance team wants receipts for.
"""
import re
import unicodedata
from pathlib import Path

_FR_MONTHS = ["", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
              "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre"]


def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", str(s or ""))
                   if not unicodedata.combining(c))


def _norm(s):
    """Accent-insensitive, alphanumeric-only, lowercased key for matching."""
    return re.sub(r"[^a-z0-9]", "", _strip_accents(str(s or "")).lower())


def _read_rows(path):
    """Return every cell as a list-of-lists of strings (.xls and .xlsx)."""
    suffix = Path(path).suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append(["" if v is None else _cell(v) for v in row])
        return rows
    # .xls (the native Orange export format)
    import xlrd
    wb = xlrd.open_workbook(path)
    sh = wb.sheet_by_index(0)
    return [[_cell(sh.cell_value(r, c)) for c in range(sh.ncols)]
            for r in range(sh.nrows)]


def _cell(v):
    """xlrd/openpyxl hand back floats for plain integers — drop the .0 so
    account/correspondant numbers stay as written (699559325, not 699559325.0)."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def parse_amount(value):
    if value in (None, ""):
        return 0.0
    s = re.sub(r"[^0-9,.\-]", "", str(value))
    if s.count(",") == 1 and s.count(".") == 0:
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def fmt_xaf(amount):
    """French money format: space thousands separator, no decimals (XAF)."""
    return f"{int(round(amount)):,}".replace(",", " ")


# Column positions in the Orange export (fallback when header text isn't found).
_FALLBACK = {"num": 0, "date": 1, "time": 2, "reference": 3, "service": 4,
             "status": 6, "correspondant": 11, "debit": 13, "credit": 14}

_META_LABELS = {
    "application": "application",
    "reseau": "reseau",
    "compteorangemoney": "account",
    "coordonnees": "company",       # cell stores it truncated ("…entrepris")
    "typederapport": "report_type",
    "debutdeperiode": "period_start",
    "findeperiode": "period_end",
    "generele": "generated",
}


def _find_columns(rows):
    """Locate the column-header row and map the fields we need."""
    for r in rows:
        keys = [_norm(c) for c in r]
        if "reference" in keys and "statut" in keys and "credit" in keys:
            comptes = [i for i, k in enumerate(keys) if k == "ndecompte"]
            cols = {
                "date": _first(keys, "date"),
                "time": _first(keys, "heure"),
                "reference": _first(keys, "reference"),
                "service": _first(keys, "service"),
                "status": _first(keys, "statut"),
                "debit": _first(keys, "debit"),
                "credit": _first(keys, "credit"),
                # Two "N° de Compte" columns: agent (first) and correspondant
                # (last). Use the detected column whenever ANY is present (so a
                # single-column variant reads its real position); only fall back
                # to the fixed index when none is found.
                "correspondant": (comptes[-1] if comptes
                                  else _FALLBACK["correspondant"]),
                "num": 0,
            }
            if all(v is not None for k, v in cols.items() if k != "num"):
                return cols
    return dict(_FALLBACK)


def _first(keys, target):
    for i, k in enumerate(keys):
        if k == target:
            return i
    return None


def _meta(rows):
    meta = {}
    for r in rows:
        if not r:
            continue
        key = _norm(r[0])
        # Prefix match so longer labels ("Coordonnées entreprise du client …")
        # still resolve to the field.
        field = next((f for p, f in _META_LABELS.items() if key.startswith(p)), None)
        if field and field not in meta:
            value = next((c for c in reversed(r) if str(c).strip()), "")
            if value and _norm(value) != key:  # ignore the label echoing itself
                meta[field] = str(value).strip()
    # Display tidy-ups
    if _norm(meta.get("reseau")) in ("cameroon", "cameroun"):
        meta["reseau"] = "Cameroun"
    if meta.get("company"):
        meta["company"] = _clean_company(meta["company"])
    # Some exports give one combined "Période : dd/mm/yyyy - dd/mm/yyyy" cell
    # instead of separate Début/Fin rows — use it so the month label still
    # resolves (_month_label reads the leading date).
    if not meta.get("period_start"):
        for r in rows:
            if r and _norm(r[0]).startswith("periode"):
                meta["period_start"] = next(
                    (c for c in reversed(r) if str(c).strip()), "")
                break
    meta["month_label"] = _month_label(meta.get("period_start", ""))
    return meta


def _clean_company(value):
    """"DHL EXPRESS - Cameroun, 1220005, Douala" -> "DHL EXPRESS Cameroun, Douala":
    drop a bare numeric code segment and the " - " separator for a clean line."""
    parts = [p.strip() for p in str(value or "").split(",")]
    parts = [p for p in parts if p and not re.fullmatch(r"\d+", p)]
    return ", ".join(parts).replace(" - ", " ")


def _month_label(period_start):
    m = re.match(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", str(period_start))
    if m:
        mo, yr = int(m.group(2)), int(m.group(3))
        yr += 2000 if yr < 100 else 0
        if 1 <= mo <= 12:
            return f"{_FR_MONTHS[mo]} {yr}"
    return ""


def _is_number(value):
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", str(value).strip()))


def parse_statement(path):
    """Return {meta, collections, totals, correspondants}.

    ``collections`` = successful credits only (one per receipt), each:
        {num, date, time, reference, service, status, correspondant, credit}
    ``correspondants`` groups those by the correspondant number.
    """
    rows = _read_rows(path)
    cols = _find_columns(rows)
    meta = _meta(rows)

    collections = []
    for r in rows:
        def g(field):
            i = cols.get(field)
            return r[i] if i is not None and i < len(r) else ""
        if not _is_number(g("num")):
            continue                                   # section title / total
        status = g("status")
        credit = parse_amount(g("credit"))
        if _norm(status) != "succes" or credit <= 0:   # drop Echec + C2C débits
            continue
        correspondant = re.sub(r"\.0$", "", str(g("correspondant")).strip())
        if not correspondant:
            continue
        collections.append({
            "num": str(g("num")).split(".")[0],
            "date": str(g("date")).strip(),
            "time": str(g("time")).strip(),
            "reference": str(g("reference")).strip(),
            "service": str(g("service")).strip(),
            "status": "Succès",
            "correspondant": correspondant,
            "credit": credit,
        })

    by_corr = {}
    for tx in collections:
        grp = by_corr.setdefault(tx["correspondant"],
                                 {"correspondant": tx["correspondant"],
                                  "transactions": [], "total": 0.0})
        grp["transactions"].append(tx)
        grp["total"] += tx["credit"]

    totals = {"count": len(collections),
              "credited": sum(t["credit"] for t in collections),
              "correspondant_count": len(by_corr)}
    return {"meta": meta, "collections": collections,
            "correspondants": by_corr, "totals": totals}
