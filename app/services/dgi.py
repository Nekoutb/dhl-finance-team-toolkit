"""Client for the DGI Fiscalis "Vérifier un NIU" service.

The check lives in a tab of http://fiscalis.dgi.cm/modules/Common/Account/Login.aspx
(no login needed): an ASP.NET postback with the NIU in
``TabContainer1$TabPanelVerifyNIU$txtNIU2``. A known NIU fills the
RAISON_SOCIALE / SIGLE / NUMEROCNIRC / ACTIVITEDECLAREE / LIBELLEREGIMEFISCAL /
LIBELLEUNITEGESTION spans; problems appear in ``lblErrMsgNIU``.

Status mapping (tune once real NIUs have been run):
  - record returned                    -> "active"
  - error message mentioning "inactif" -> "inactive"
  - error message / empty record       -> "not_found"
  - network / parsing failure          -> "error"
"""
import re
import time
from datetime import datetime

import httpx

LOGIN_URL = "http://fiscalis.dgi.cm/modules/Common/Account/Login.aspx"
_FIELD_IDS = {
    "raison_sociale": "txtRAISON_SOCIALE",
    "sigle": "txtSIGLE",
    "cni_rc": "txtNUMEROCNIRC",
    "activite": "txtACTIVITEDECLAREE",
    "regime": "txtLIBELLEREGIMEFISCAL",
    "centre": "txtLIBELLEUNITEGESTION",
}
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _hidden(html, name):
    m = re.search(rf'id="{name}" value="([^"]*)"', html)
    return m.group(1) if m else ""


def _span_text(html, span_id):
    m = re.search(
        rf'id="TabContainer1_TabPanelVerifyNIU_{span_id}"[^>]*>(.*?)</span>',
        html, re.DOTALL)
    if not m:
        return ""
    return re.sub(r"<[^>]+>", "", m.group(1)).strip()


def verify_niu(niu, client=None, timeout=30):
    """Look one NIU up on Fiscalis. Returns a result dict (never raises)."""
    niu = str(niu or "").strip()
    result = {
        "niu": niu, "status": "error", "message": "",
        "raison_sociale": "", "sigle": "", "cni_rc": "",
        "activite": "", "regime": "", "centre": "",
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    if not niu:
        result.update(status="not_found", message="No NIU provided")
        return result

    own_client = client is None
    c = client or httpx.Client(timeout=timeout, follow_redirects=True,
                               headers=_HEADERS)
    try:
        page = c.get(LOGIN_URL).text
        data = {
            "__EVENTTARGET": "", "__EVENTARGUMENT": "", "__LASTFOCUS": "",
            "__VIEWSTATE": _hidden(page, "__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": _hidden(page, "__VIEWSTATEGENERATOR"),
            "TabContainer1_ClientState":
                '{"ActiveTabIndex":2,"TabState":[true,true,true]}',
            "TabContainer1$TabPanelVerifyNIU$txtNIU2": niu[:14],
            "TabContainer1$TabPanelVerifyNIU$ibFindContribuable.x": "10",
            "TabContainer1$TabPanelVerifyNIU$ibFindContribuable.y": "10",
        }
        html = c.post(LOGIN_URL, data=data).text

        for field, span_id in _FIELD_IDS.items():
            result[field] = _span_text(html, span_id)
        message = _span_text(html, "lblErrMsgNIU")
        result["message"] = message

        lowered = message.lower()
        if result["raison_sociale"]:
            result["status"] = "inactive" if "inactif" in lowered else "active"
        elif "inactif" in lowered:
            result["status"] = "inactive"
        else:
            result["status"] = "not_found"
            if not message:
                result["message"] = "No taxpayer returned for this NIU"
    except Exception as exc:  # noqa: BLE001 - keep batch runs alive
        result["status"] = "error"
        result["message"] = f"{type(exc).__name__}: {exc}"
    finally:
        if own_client:
            c.close()
    return result


def verify_many(nius, delay=0.4, timeout=30):
    """Verify a list of NIUs over one HTTP session, politely spaced."""
    out = []
    with httpx.Client(timeout=timeout, follow_redirects=True,
                      headers=_HEADERS) as c:
        for n, niu in enumerate(nius):
            if n:
                time.sleep(delay)
            out.append(verify_niu(niu, client=c, timeout=timeout))
    return out
