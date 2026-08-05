"""v11.13 — the operator-statement tick list must never re-arm itself, and the
BIT / Cash AR screens carry about half the words they used to.

Isolated to a temp data dir; the real data/ is never touched.
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.tools import bitcash, iro  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="v1113_"))
config.CONFIG_PATH = _tmp / "config.json"
config.invalidate_config_cache()
iro.IRO_DIR = _tmp / "iro"
iro.IRO_DIR.mkdir(parents=True, exist_ok=True)
iro.UPLOAD_DIR = _tmp / "uploads"
iro.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
iro.DEPOSITS_PATH = _tmp / "deposits.json"
bitcash.ROWS_PATH = _tmp / "rows.json"
bitcash.STORE_PATH = _tmp / "bitcash.json"
bitcash.RECON_DIR = _tmp / "recons"
bitcash.RECON_DIR.mkdir(parents=True, exist_ok=True)

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

main.OUTPUT_DIR = _tmp / "out"
main.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

client = TestClient(main.app)
_fail = 0


def check(label, cond):
    global _fail
    print(("[OK ] " if cond else "[FAIL] ") + label)
    if not cond:
        _fail += 1


OPS = (ROOT / "app" / "templates" / "bitcash" / "operators.html").read_text(
    encoding="utf-8")


def ticked(html):
    """Accounts whose checkbox carries the `checked` attribute, in order.

    Counting the substring "checked" would be vacuous — the page's own script
    contains it — so match the attribute inside each accounts checkbox tag.
    """
    return [m.group(1) for m in re.finditer(
        r'name="accounts" value="(\d+)"[^>]*?\bchecked\b', html, re.S)]


# === 1. Three operators, all with an email saved ============================
ACCTS = ["4003011111", "4003022222", "4003033333"]
bitcash.ROWS_PATH.write_text(json.dumps({
    "bit": [], "bit_header": [], "gen_bit": "", "gen_cash": "g",
    "cash_date_col": "Doc. Date",
    "cash": [{"id": i, "sap_acct": a, "ibs_acct": "4150" + str(i),
              "awb": f"70101010{i}0", "assignment": f"70101010{i}0",
              "reference": f"R{i}", "amount": 9000.0 + i,
              "customer": f"OP{i}", "doc_no": str(i), "date": "01.07.2026"}
             for i, a in enumerate(ACCTS)]}, ensure_ascii=False),
    encoding="utf-8")
for a in ACCTS:
    iro.ensure_token(a)
    client.post("/tools/bit-cash-ar/operators/email",
                data={"account": a, "email": f"{a}@example.com"})

r = client.get("/tools/bit-cash-ar/operators")
check("a fresh visit still pre-ticks operators that have an email",
      r.status_code == 200 and r.text.count('name="accounts"') == 3
      and sorted(ticked(r.text)) == sorted(ACCTS))

# === 2. A send goes ONLY to the ticked accounts =============================
picked = [ACCTS[0]]
r = client.post("/tools/bit-cash-ar/operators/send",
                data={"accounts": picked},
                follow_redirects=False)
check("the send is accepted", r.status_code == 303)
loc = r.headers.get("location", "")
check("exactly the ticked operator was sent to",
      len(iro.load_record(ACCTS[0]).get("sent", [])) == 1
      and not iro.load_record(ACCTS[1]).get("sent")
      and not iro.load_record(ACCTS[2]).get("sent"))

# === 3. THE BUG: the redirect must not re-arm the deselected operators ======
check("the redirect flags that a selection was made", "selmade=1" in loc)
sel = re.search(r"[?&]sel=([^&]*)", loc)
check("nothing is left ticked once it has been sent",
      sel is not None and sel.group(1) == "")

r = client.get(loc.split("?", 1)[1] and
               "/tools/bit-cash-ar/operators?" + loc.split("?", 1)[1])
body = r.text
check("the page that comes back has NO operator ticked",
      r.status_code == 200 and ticked(body) == []
      and body.count('name="accounts"') == 3)
check("but every operator is still listed",
      all(a in body for a in ACCTS))

# a deselection survives an explicit round-trip
r = client.get(f"/tools/bit-cash-ar/operators?selmade=1&sel={ACCTS[2]}")
check("only the account named in sel comes back ticked",
      ticked(r.text) == [ACCTS[2]])

# === 4. A skipped operator stays ticked so it can be retried ================
client.post("/tools/bit-cash-ar/operators/email",
            data={"account": ACCTS[1], "email": ""})
r = client.post("/tools/bit-cash-ar/operators/send",
                data={"accounts": [ACCTS[1], ACCTS[2]]},
                follow_redirects=False)
loc = r.headers.get("location", "")
sel = re.search(r"[?&]sel=([^&]*)", loc)
check("an operator that could not be sent stays ticked, the sent one does not",
      sel is not None and sel.group(1) == ACCTS[1])

# === 4b. EVERY route back to this screen must carry the selection ===========
# Saving an email is the natural next step after a send skips an operator for
# "no email saved" — if that redirect dropped the tick set, the page would come
# back with everyone re-armed and the next send would go to all of them.
KEEP = [
    ("/tools/bit-cash-ar/operators/email",
     {"account": ACCTS[1], "email": "b@example.com"}, "saving an email"),
    ("/tools/bit-cash-ar/operators/regenerate",
     {"account": ACCTS[1]}, "reissuing a secure link"),
    ("/tools/bit-cash-ar/operators/fetch-mail", {}, "fetching mail"),
    ("/tools/bit-cash-ar/operators/deposit/delete",
     {"sha": "nope", "reference": "", "account": "", "at": ""},
     "removing a deposit"),
]
for path, payload, label in KEEP:
    r = client.post(path, data={**payload, "selmade": "1", "sel": ACCTS[1]},
                    follow_redirects=False)
    loc = r.headers.get("location", "")
    got = re.search(r"[?&]sel=([^&]*)", loc)
    check(f"{label} keeps the tick set",
          "selmade=1" in loc and got is not None and got.group(1) == ACCTS[1])
    page = client.get("/tools/bit-cash-ar/operators?" + loc.split("?", 1)[1])
    check(f"{label} comes back with only that operator ticked",
          ticked(page.text) == [ACCTS[1]])

# the refusal path too: "nothing ticked" must not come back as "all ticked"
r = client.post("/tools/bit-cash-ar/operators/send",
                data={"selmade": "1", "sel": ""}, follow_redirects=False)
loc = r.headers.get("location", "")
page = client.get("/tools/bit-cash-ar/operators?" + loc.split("?", 1)[1])
check("a refused empty send comes back empty, not fully ticked",
      ticked(page.text) == [] and "Tick+at+least" in loc.replace("%20", "+"))

# every form on the page mirrors the live selection
check("each form carries a selection mirror",
      OPS.count('name="selmirror"') == 0
      and OPS.count('class="selmirror"') >= 4)
check("the mirrors are kept in step with the ticks",
      "input.opsel" in OPS and "sync()" in OPS)

# === 5. The browser cannot replay a stale tick set ==========================
check("the form and the boxes opt out of browser autofill",
      OPS.count('autocomplete="off"') >= 2)
check("a bfcache restore re-asserts what the server rendered",
      "pageshow" in OPS and "e.persisted" in OPS)
check("there is an all / none control",
      'id="sel-all"' in OPS and 'id="sel-none"' in OPS)

# an empty tick set is still refused outright
r = client.post("/tools/bit-cash-ar/operators/send", data={},
                follow_redirects=False)
check("sending with nothing ticked is refused, not treated as 'all'",
      r.status_code == 303 and "Tick+at+least" in
      r.headers.get("location", "").replace("%20", "+"))

# === 6. The plug panel must describe the journal the pack really writes =====
# build_journal emits a balanced PAIR for the plug — key 40 on the bank G/L and
# key 15 on the plug G/L — both carrying the NEGATED amount. The panel used to
# claim "posting key 40 if amount > 0 else 50"; key 50 is never written at all.
RECON = (ROOT / "app" / "templates" / "bitcash" / "recon.html").read_text(
    encoding="utf-8")
check("the plug panel names both posting keys the journal writes",
      "keys 40 and" in RECON and "15." in RECON and "else 50" not in RECON)
check("and it shows the sign the journal actually carries",
      'format(-view.plug.amount)' in RECON)

# a plug that rounds away must not be stored as a phantom 0.00 line
import app.tools.bitcash as _bc  # noqa: E402
_r = {"token": "feedbead", "status": "open"}
_bc.save_recon(_r)
_bc.set_plug("feedbead", 0.004, "4003026257", "")
check("a plug that rounds to zero clears the plug instead of storing 0.00",
      not (_bc.load_recon("feedbead") or {}).get("plug"))
_bc.set_plug("feedbead", 20000, "4003026257", "")
check("a real plug is still stored",
      (_bc.load_recon("feedbead") or {}).get("plug", {}).get("amount")
      == 20000.0)

# === 7. Decluttering: the wordy originals are gone ==========================
GONE = [
    ("bitcash/operators.html", "They are shared, so that amount is inside"),
    ("operator/statement.html", "attach your evidence and submit"),
    ("operator/statement.html", "you can submit the payment references on"),
    ("operator/statement.html", "filled automatically, cannot be changed"),
    ("bitcash/index.html", "what tips past 60 days by month end"),
    ("bitcash/index.html", "re-reads the file on record"),
    ("bitcash/recon.html", "investigate and select the appropriate line"),
    ("bitcash/recon.html", "clarify the position with the account holder"),
]
for rel, phrase in GONE:
    txt = (ROOT / "app" / "templates" / rel).read_text(encoding="utf-8")
    check(f"{rel}: dropped “{phrase[:38]}…”", phrase not in txt)


def prose_words(text):
    """Words of visible guidance — leads, notes, alerts, drop-zone text.

    Only ONE branch of each {% if %}/{% else %} is counted: that is what a
    reader actually sees on the page.
    """
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", text, flags=re.S)
    t = re.sub(r"\{%-?\s*else\s*-?%\}.*?\{%-?\s*endif\s*-?%\}", "", t,
               flags=re.S)
    n = 0
    for m in re.finditer(
            r'<(p|div|span|small|li)\b[^>]*class="[^"]*'
            r'(lead|note-small|note-inline|alert|hint|filedrop-text)[^"]*"'
            r'[^>]*>(.*?)</\1>', t, re.S):
        inner = re.sub(r"\{[%{].*?[%}]\}", "", m.group(3), flags=re.S)
        n += len(re.sub(r"<[^>]+>", " ", inner).split())
    return n


FILES = ["bitcash/index.html", "bitcash/recon.html", "bitcash/operators.html",
         "operator/statement.html", "operator/done.html"]
before = after = 0
for rel in FILES:
    after += prose_words((ROOT / "app" / "templates" / rel).read_text(
        encoding="utf-8"))
    old = subprocess.run(
        ["git", "show", f"v11.12-baseline:app/templates/{rel}"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8")
    if old.returncode == 0:
        before += prose_words(old.stdout)
if before:
    cut = 100 * (before - after) // before
    print(f"       guidance text: {before} -> {after} words ({cut}% shorter)")
    # v11.15 added the per-mode payment disclosure (recon alert + portal
    # confirm step), spending a few of the freed words on required content.
    check(f"the BIT / Cash AR screens stay well under the v11.12 wordcount "
          f"({cut}%)", cut >= 30)
else:
    print("       (no v11.12-baseline tag — word-count comparison skipped)")

# === 8. The pages still render ==============================================
for path in ["/tools/bit-cash-ar", "/tools/bit-cash-ar/operators"]:
    check(f"{path} still renders", client.get(path).status_code == 200)
tok = iro.ensure_token(ACCTS[0])["token"]
r = client.get(f"/operator/{tok}")
check("the IRO statement still renders", r.status_code == 200)
check("and still says what to do, in one line",
      "Tick what you paid" in r.text and "submit." in r.text)

if _fail:
    print(f"\n{_fail} CHECK(S) FAILED")
    sys.exit(1)
print("\nALL v11.13 TESTS PASSED")
