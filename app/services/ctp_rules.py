"""Collection Treatment Plan rules, encoded from the Credit Policy — Express
Specific Conditions v3.3 (2024-02-01).

Each constant cites the policy section it comes from so the logic is auditable
and easy to tune when the user confirms local thresholds.
"""

# --- Payment terms (policy §2.6) ---------------------------------------------
STANDARD_TERM_DAYS = 15   # freight / standard invoices
FISCAL_TERM_DAYS = 7      # duty / VAT / GST / customs charges

# --- Credit limit (policy §2.4 / §2.5) ---------------------------------------
CREDIT_LIMIT_EXPOSURE_BUFFER = 15        # "+15" in the credit-limit formula
CREDIT_LIMIT_OVERRUN_TOLERANCE = 20      # % overrun tolerated within terms

# --- Treatment plans (policy §3.3) -------------------------------------------
PLAN_TELESALES = "telesales_multichannel"
PLAN_FIELD = "field_major"
PLAN_ENTERPRISE = "enterprise"
PLAN_NEW = "new_customer"
DEFAULT_PLAN = PLAN_TELESALES

PLAN_LABELS = {
    PLAN_TELESALES: "Telesales & Multichannel",
    PLAN_FIELD: "Field Sales & Major",
    PLAN_ENTERPRISE: "Enterprise",
    PLAN_NEW: "New Customer",
}

# Free-text segment label -> plan. Aliases drawn from the segmentation pyramid
# (policy §2.2: Enterprise / Relationship / Direct).
_SEGMENT_ALIASES = {
    PLAN_TELESALES: ["telesales", "tls", "multichannel", "multi channel",
                     "multi-channel", "direct", "retail", "cash"],
    PLAN_FIELD: ["field", "major", "relationship"],
    PLAN_ENTERPRISE: ["enterprise", "global", "mnc", "gfps", "national", "csi"],
    PLAN_NEW: ["new customer", "new ", "newly", "new-"],
}

# GCTP timelines: ordered (days_overdue, action, owner). Days are vs the invoice
# due date. Telesales §3.4, Field/Major §3.5, Enterprise §3.6. Where the policy
# shows a base/extended pair (e.g. 30/37) we use the base day.
#
# Overdue / final-demand LETTERS are sent automatically by the system dunning
# procedure (§2.10 e-dunning) — nobody sends letters manually. Pure letter
# steps are therefore marked "Automated dunning" (owner = the system, excluded
# from manual actions due); mixed steps keep only their manual part, with the
# letter noted as handled by dunning.
AUTO_OWNER = "System (e-dunning)"

TIMELINES = {
    PLAN_TELESALES: [
        (5,   "Automated dunning — Overdue Account Letter (sent by system)", AUTO_OWNER),
        (15,  "Automated dunning — Final Demand Letter (sent by system)", AUTO_OWNER),
        (23,  "Call the customer", "Collector"),
        (25,  "Stop credit", "Customer Accounting Supervisor"),
        (30,  "Pass to Collection Agency (final letter sent by system dunning)", "Customer Accounting Supervisor"),
        (90,  "Write-off (100% provision)", "Customer Accounting Supervisor"),
    ],
    PLAN_FIELD: [
        (7,   "Call customer & request payment (overdue letter via system dunning)", "Collector"),
        (21,  "Call customer & inform Sales Executive (final demand via system dunning)", "Collector"),
        (28,  "48h notice of impending Stop Credit to Sales Exec & Manager", "Collector"),
        (30,  "Stop credit", "Customer Accounting Supervisor"),
        (37,  "Call & visit customer if significant balance (after-stop letter via system dunning)", "Collector"),
        (44,  "Pass to agency / solicitor; progress to Court if needed", "Customer Accounting Supervisor"),
        (365, "Write-off", "Customer Accounting Manager"),
    ],
    PLAN_ENTERPRISE: [
        (7,   "Call customer & request payment", "Collector"),
        (21,  "Call & request payment", "Collector"),
        (26,  "Call & request payment; escalate to senior management", "Collector"),
        (31,  "Escalate to Sales Exec / CFO (final demand letter via system dunning)", "Collector"),
        (38,  "Organize meeting(s) with customer", "Customer Accounting Manager"),
        (45,  "Set payment deadline; inform Sales Executive", "Sales Manager"),
        (47,  "Stop credit", "Customer Accounting Supervisor"),
        (54,  "Pass to agency / solicitor; progress to Court if needed", "Customer Accounting Manager"),
        (365, "Write-off", "Customer Accounting Manager"),
    ],
}


def is_auto_action(action):
    """True for steps the system dunning runs by itself (no manual control)."""
    return str(action).lower().startswith("automated dunning")
# New customers follow the telesales cadence once overdue, plus pre-due controls
# handled in the analyzer (intro call before due date, weekly exception report).
TIMELINES[PLAN_NEW] = TIMELINES[PLAN_TELESALES]

# Day beyond which AR counts as "past due GCTP" for the §2.5 risk matrix
# ("> 20% of total AR past due GCTP i.e. 28 days for TLS").
GRACE_DAYS = {
    PLAN_TELESALES: 28,
    PLAN_FIELD: 37,
    PLAN_ENTERPRISE: 47,
    PLAN_NEW: 28,
}


def resolve_plan(segment_text):
    if not segment_text:
        return DEFAULT_PLAN
    s = str(segment_text).strip().lower()
    for plan, aliases in _SEGMENT_ALIASES.items():
        if any(alias in s for alias in aliases):
            return plan
    return DEFAULT_PLAN


def term_days_for(doc_type):
    """Fiscal charges are due in 7 days, everything else in 15 (policy §2.6)."""
    if doc_type and any(k in str(doc_type).lower()
                        for k in ("duty", "vat", "gst", "customs", "fiscal", "tax")):
        return FISCAL_TERM_DAYS
    return STANDARD_TERM_DAYS


def is_call_action(action):
    return "call" in action.lower()


# EUR value of one unit of each currency's threshold conversion. XAF/XOF are
# hard-pegged to the EUR so the conversion is exact; extend as needed.
EUR_RATES = {"EUR": 1.0, "XAF": 655.957, "XOF": 655.957}


def call_value_threshold(rank, currency="EUR"):
    """No collection call below this amount, by country rank (policy §3.4).

    Thresholds are defined in EUR; returns the equivalent in ``currency`` when
    a fixed conversion is known, else None (no suppression applied).
    """
    try:
        r = int(rank)
    except (TypeError, ValueError):
        return None
    if r <= 25:
        eur = 1500
    elif r <= 50:
        eur = 1200
    elif r <= 100:
        eur = 750
    else:
        eur = 350
    rate = EUR_RATES.get(str(currency or "EUR").upper().strip() or "EUR")
    return eur * rate if rate else None


def action_due(plan, days_overdue):
    """Return the (day, action, owner) of the latest step reached, or None."""
    timeline = TIMELINES.get(plan, TIMELINES[DEFAULT_PLAN])
    reached = None
    for step in timeline:
        if days_overdue >= step[0]:
            reached = step
        else:
            break
    return reached


def stop_credit_day(plan):
    """Days-overdue at which this plan's 'Stop credit' step triggers."""
    for day, action, _owner in TIMELINES.get(plan, TIMELINES[DEFAULT_PLAN]):
        if "stop credit" in action.lower():
            return day
    return None


def credit_limit_from_billing(annual_billed, payment_term_days=STANDARD_TERM_DAYS):
    """CrLimit = Annual Billed x (Term + 15) / 360 (policy §2.4)."""
    if not annual_billed:
        return None
    return annual_billed * (payment_term_days + CREDIT_LIMIT_EXPOSURE_BUFFER) / 360


def risk_control(ar_beyond_gctp_pct, credit_overrun_pct):
    """Credit-risk matrix (policy §2.5). Returns (key, label)."""
    pct = ar_beyond_gctp_pct or 0
    if pct <= 0:
        return ("ok", "Within terms — no action")
    if pct <= CREDIT_LIMIT_OVERRUN_TOLERANCE:
        return ("watch", "Add to credit watch list")
    # > 20% AR beyond GCTP
    if credit_overrun_pct is not None and credit_overrun_pct > CREDIT_LIMIT_OVERRUN_TOLERANCE:
        return ("stop", "Immediate mitigation — credit stop")
    return ("hold", "Credit hold via GCTP")
