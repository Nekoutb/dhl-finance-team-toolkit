"""Variance analysis — expense & balance-sheet accounts, current vs prior year.

Inputs: four workbooks (prior-year TB + GL, current-year TB + GL). The trial
balances give the per-account totals; the general ledgers give the journal-
entry descriptions used to explain WHY an account moved. Account class comes
from the OHADA chart (first digit: 1-5 balance sheet, 6 expenses, 7 income);
non-numeric accounts fall back to name keywords.

Output: per-account rows (CY, PY, variance, % change, plain-language comment
naming the driving JE descriptions, and review questions), grouped into
Expense accounts and Balance-sheet accounts, plus an Excel export.
"""
import json
import re
from collections import defaultdict

from ..config import DATA_DIR
from . import excel_reader

OUT_DIR = DATA_DIR / "variance"
FLAT_BAND = 2.0          # ±2% = "relatively flat"
BIG_SWING = 50.0         # > ±50% triggers a completeness question
CONCENTRATION = 0.8      # one driver explaining >80% of the move

_EXPENSE_WORDS = ("expense", "charge", "cost", "fee", "rent", "salar",
                  "achat", "frais", "loyer", "honorair", "amortis", "depreci")


def _num(value):
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(" ", "").replace(" ", "")
    s = s.replace(",", "") if s.count(",") and s.count(".") else s.replace(",", ".")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        n = float(s)
    except ValueError:
        return 0.0
    return -n if neg else n


def _norm_acct(value):
    s = str(value if value is not None else "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _find_col(header, *frags, exclude=()):
    low = [str(h).lower() for h in header]
    for i, h in enumerate(low):
        if any(f in h for f in frags) and not any(x in h for x in exclude):
            return header[i]
    return None


def classify(account, name=""):
    """'expense' | 'income' | 'balance' from the account class / name."""
    digits = re.sub(r"\D", "", str(account))
    if digits:
        first = digits[0]
        if first == "6":
            return "expense"
        if first == "7":
            return "income"
        if first in "12345":
            return "balance"
    low = str(name).lower()
    if any(w in low for w in _EXPENSE_WORDS):
        return "expense"
    return "balance"


def parse_tb(path):
    """Trial balance → {account: {"name": str, "balance": float}}."""
    parsed = excel_reader.read_transactions(path)
    header = parsed["header"]
    c_acct = _find_col(header, "account", "compte", "acct") or (header[0] if header else None)
    c_name = _find_col(header, "name", "libell", "intitul", "description",
                       exclude=("account no", "account num"))
    c_bal = _find_col(header, "closing", "balance", "solde", "ytd", "amount", "montant")
    c_deb = _find_col(header, "debit", "débit")
    c_cred = _find_col(header, "credit", "crédit")

    out = {}
    for row in parsed["rows"]:
        d = row["data"]
        acct = _norm_acct(d.get(c_acct)) if c_acct else ""
        if not acct or "total" in acct.lower():
            continue
        name = str(d.get(c_name) or "").strip() if c_name else ""
        if "total" in name.lower() and not re.search(r"\d", acct):
            continue
        if c_bal and d.get(c_bal) not in (None, ""):
            bal = _num(d.get(c_bal))
        elif c_deb or c_cred:
            bal = _num(d.get(c_deb)) - _num(d.get(c_cred))
        else:
            continue
        if acct in out:
            out[acct]["balance"] += bal
        else:
            out[acct] = {"name": name, "balance": bal}
    return out


def parse_gl(path):
    """General ledger → [{account, name, description, amount}]."""
    parsed = excel_reader.read_transactions(path)
    header = parsed["header"]
    c_acct = _find_col(header, "account", "compte", "acct") or (header[0] if header else None)
    c_name = _find_col(header, "account name", "libellé compte", "acct name")
    c_desc = _find_col(header, "description", "narration", "text", "libell",
                       "memo", "détail", "detail", "designation", "désignation",
                       exclude=("account",))
    c_amt = _find_col(header, "amount", "montant", "value", "valeur",
                      exclude=("vat", "tva", "tax"))
    c_deb = _find_col(header, "debit", "débit")
    c_cred = _find_col(header, "credit", "crédit")

    rows = []
    for row in parsed["rows"]:
        d = row["data"]
        acct = _norm_acct(d.get(c_acct)) if c_acct else ""
        if not acct or "total" in acct.lower():
            continue
        if c_amt and d.get(c_amt) not in (None, ""):
            amt = _num(d.get(c_amt))
        elif c_deb or c_cred:
            amt = _num(d.get(c_deb)) - _num(d.get(c_cred))
        else:
            continue
        rows.append({
            "account": acct,
            "name": str(d.get(c_name) or "").strip() if c_name else "",
            "description": (str(d.get(c_desc) or "").strip()
                            if c_desc else ""),
            "amount": amt,
        })
    return rows


def _desc_totals(gl_rows):
    """{account: {description: net amount}}."""
    out = defaultdict(lambda: defaultdict(float))
    for r in gl_rows:
        desc = r["description"] or "(no description)"
        out[r["account"]][desc] += r["amount"]
    return out


def _fmt(n):
    return f"{n:,.0f}"


def _drivers(cy_desc, py_desc, variance):
    """Top JE descriptions whose CY-vs-PY delta moves WITH the variance."""
    deltas = {}
    for desc in set(cy_desc) | set(py_desc):
        deltas[desc] = cy_desc.get(desc, 0.0) - py_desc.get(desc, 0.0)
    same_sign = [(d, v) for d, v in deltas.items()
                 if v and (v > 0) == (variance > 0)]
    same_sign.sort(key=lambda t: abs(t[1]), reverse=True)
    return same_sign[:2], deltas


def _comment_and_questions(acct, name, group, cy, py, variance, pct,
                           cy_desc, py_desc):
    questions = []
    label = name or f"account {acct}"

    if pct is not None and abs(pct) <= FLAT_BAND:
        return ("Amount remained relatively flat year on year "
                "(±2% variance)."), questions

    if py == 0 and cy != 0:
        top = sorted(cy_desc.items(), key=lambda t: abs(t[1]), reverse=True)[:2]
        what = " and ".join(f"“{d}” (XAF {_fmt(v)})" for d, v in top) \
            or "current-year activity"
        questions.append(f"This is a new line vs prior year — was {label} "
                         "budgeted and approved, and is it expected to recur?")
        return f"New this year — the balance is driven by {what}.", questions

    if cy == 0 and py != 0:
        questions.append(f"{label.capitalize()} has dropped to nil — has the "
                         "service genuinely stopped, or is a current-year "
                         "accrual/invoice missing (completeness)?")
        return ("The decline is explained by no current-year activity on this "
                "account (prior year held XAF " + _fmt(py) + ")."), questions

    top, _ = _drivers(cy_desc, py_desc, variance)
    if top:
        what = " and ".join(f"“{d}” (XAF {_fmt(v):s})" for d, v in top)
        comment = (f"The increase is driven by {what}." if variance > 0
                   else f"The decline is explained by {what}.")
        if abs(top[0][1]) >= CONCENTRATION * abs(variance):
            questions.append(f"A single item — “{top[0][0]}” — explains most "
                             "of this movement. Is it a one-off, and was it "
                             "approved at the right level?")
    else:
        comment = (("The increase " if variance > 0 else "The decline ")
                   + "comes from many small movements — no single journal "
                     "description stands out in the GL detail.")
        questions.append(f"No dominant driver found for {label} — review the "
                         "GL line by line to confirm the movement is valid.")

    if pct is not None and abs(pct) > BIG_SWING:
        questions.append(f"The swing exceeds {BIG_SWING:.0f}% — check cut-off "
                         "(late invoices/accruals) and that prior-year "
                         "comparatives are stated on the same basis.")
    if group == "balance" and pct is not None and pct > 25:
        questions.append("For this balance-sheet account: is the growing "
                         "balance ageing? Confirm recoverability/validity and "
                         "whether a provision is needed.")
    if group == "expense" and variance > 0 and pct is not None and pct > 20:
        questions.append("Was this cost increase in budget? If not, who "
                         "approved the overrun and is corrective action "
                         "needed for the rest of the year?")
    return comment, questions


def build_analysis(py_tb, py_gl, cy_tb, cy_gl):
    """Compare CY vs PY per account; comments from GL JE descriptions."""
    cy_by_desc = _desc_totals(cy_gl)
    py_by_desc = _desc_totals(py_gl)

    groups = {"expense": [], "balance": []}
    accounts = sorted(set(py_tb) | set(cy_tb))
    for acct in accounts:
        name = (cy_tb.get(acct, {}).get("name")
                or py_tb.get(acct, {}).get("name") or "")
        group = classify(acct, name)
        if group == "income":
            continue  # scope: expense + balance sheet
        cy = round(cy_tb.get(acct, {}).get("balance", 0.0), 2)
        py = round(py_tb.get(acct, {}).get("balance", 0.0), 2)
        variance = round(cy - py, 2)
        pct = round(variance / abs(py) * 100, 1) if py else None
        comment, questions = _comment_and_questions(
            acct, name, group, cy, py, variance, pct,
            cy_by_desc.get(acct, {}), py_by_desc.get(acct, {}))
        groups[group].append({
            "account": acct, "name": name, "cy": cy, "py": py,
            "variance": variance, "pct": pct,
            "comment": comment, "questions": questions,
        })

    def _tot(rows, key):
        return round(sum(r[key] for r in rows), 2)

    summary = {}
    for g, rows in groups.items():
        tot_cy, tot_py = _tot(rows, "cy"), _tot(rows, "py")
        summary[g] = {
            "cy": tot_cy, "py": tot_py,
            "variance": round(tot_cy - tot_py, 2),
            "pct": (round((tot_cy - tot_py) / abs(tot_py) * 100, 1)
                    if tot_py else None),
            "count": len(rows),
        }
    return {"expense": groups["expense"], "balance": groups["balance"],
            "summary": summary}


def save_result(token, result, meta):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "result": result}
    (OUT_DIR / f"{token}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def load_result(token):
    p = OUT_DIR / f"{token}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def list_recent(limit=8):
    if not OUT_DIR.exists():
        return []
    out = []
    for p in OUT_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({"token": p.stem,
                    "created_at": d.get("meta", {}).get("created_at", ""),
                    "label": d.get("meta", {}).get("label", "")})
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out[:limit]


def build_excel(out_path, result, label=""):
    import xlsxwriter

    wb = xlsxwriter.Workbook(str(out_path))
    title_f = wb.add_format({"bold": True, "font_size": 13})
    head = wb.add_format({"bold": True, "bg_color": "#0b2545",
                          "font_color": "white", "border": 1,
                          "text_wrap": True})
    cell = wb.add_format({"border": 1})
    wrap = wb.add_format({"border": 1, "text_wrap": True, "valign": "top"})
    money = wb.add_format({"border": 1, "num_format": "#,##0"})
    pctf = wb.add_format({"border": 1, "num_format": "0.0%"})
    boldmoney = wb.add_format({"border": 1, "num_format": "#,##0",
                               "bold": True})

    for group, sheet_name in (("expense", "Expense accounts"),
                              ("balance", "Balance sheet accounts")):
        ws = wb.add_worksheet(sheet_name)
        ws.set_column(0, 0, 12)
        ws.set_column(1, 1, 34)
        ws.set_column(2, 4, 15)
        ws.set_column(5, 5, 9)
        ws.set_column(6, 6, 60)
        ws.set_column(7, 7, 60)
        ws.write(0, 0, f"Variance analysis — {sheet_name}"
                       + (f" — {label}" if label else ""), title_f)
        for c, h in enumerate(["Account", "Account name", "Current year",
                               "Prior year", "Variance", "% change",
                               "Comment", "Questions to consider"]):
            ws.write(1, c, h, head)
        r = 2
        for row in result[group]:
            ws.write(r, 0, row["account"], cell)
            ws.write(r, 1, row["name"], cell)
            ws.write_number(r, 2, row["cy"], money)
            ws.write_number(r, 3, row["py"], money)
            ws.write_number(r, 4, row["variance"], money)
            if row["pct"] is None:
                ws.write(r, 5, "new", cell)
            else:
                ws.write_number(r, 5, row["pct"] / 100, pctf)
            ws.write(r, 6, row["comment"], wrap)
            ws.write(r, 7, "\n".join(f"• {q}" for q in row["questions"]), wrap)
            r += 1
        s = result["summary"][group]
        ws.write(r, 1, "TOTAL", boldmoney)
        ws.write_number(r, 2, s["cy"], boldmoney)
        ws.write_number(r, 3, s["py"], boldmoney)
        ws.write_number(r, 4, s["variance"], boldmoney)
        if s["pct"] is not None:
            ws.write_number(r, 5, s["pct"] / 100, pctf)
    wb.close()
    return out_path
