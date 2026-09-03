"""
BFSI domain calculations, India context.

Deliberately separated from the tools so the arithmetic is unit-testable without
DynamoDB, and so every figure returned to a customer can be traced to a rule in
a cited document rather than to model reasoning.

Nothing here reads identity or decides permission. It computes.
"""
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

# ── helpers ───────────────────────────────────────────────────────────
def _d(value):
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def money(value):
    """Two-decimal rupee figure, as a float for JSON transport."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def inr(value):
    """Indian-format currency string, e.g. Rs 1,40,464.29."""
    neg = value < 0
    whole, frac = f"{abs(float(value)):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{'-' if neg else ''}Rs {whole}.{frac}"


def months_between(start, end):
    s, e = _d(start), _d(end)
    m = (e.year - s.year) * 12 + (e.month - s.month)
    if e.day < s.day:
        m -= 1
    return max(m, 0)


# ══ 1. UPI / IMPS failed transaction ═════════════════════════════════
COMPENSATION_PER_DAY = 100


def upi_dispute_assessment(txn, today=None):
    """
    RBI harmonised turnaround time. Auto-reversal is due by T+1. Beyond that,
    compensation of Rs 100 per day accrues from the day after the deadline.
    """
    today = _d(today or date.today())
    txn_date = _d(txn["date"])
    deadline = txn_date + timedelta(days=1)
    breached = today > deadline and not txn.get("reversed")
    delay_days = max((today - deadline).days, 0) if breached else 0
    compensation = delay_days * COMPENSATION_PER_DAY

    within_window = (today - txn_date).days <= 30
    eligible = (
        txn.get("status") == "debited_not_credited"
        and not txn.get("reversed")
        and within_window
    )

    if txn.get("reversed"):
        verdict = "already_reversed"
        summary = "This transaction has already been reversed, so no compensation arises."
    elif txn.get("status") == "success":
        verdict = "not_failed"
        summary = "This payment succeeded and reached the beneficiary."
    elif not within_window:
        verdict = "outside_dispute_window"
        summary = "The 30-day window to raise a dispute on this transaction has passed."
    elif breached:
        verdict = "tat_breached"
        summary = (
            f"Auto-reversal was due by {deadline:%d %b %Y} and has not happened. "
            f"Compensation of {inr(compensation)} has accrued over {delay_days} day(s)."
        )
    else:
        verdict = "within_tat"
        summary = (
            f"Auto-reversal is still within the permitted window, due by "
            f"{deadline:%d %b %Y}. No compensation is payable yet."
        )

    return {
        "verdict": verdict,
        "eligible_to_dispute": eligible,
        "transaction_date": f"{txn_date:%Y-%m-%d}",
        "reversal_due_by": f"{deadline:%Y-%m-%d}",
        "tat_breached": breached,
        "delay_days": delay_days,
        "compensation_per_day": COMPENSATION_PER_DAY,
        "compensation_accrued": money(compensation),
        "compensation_accrued_display": inr(compensation),
        "amount": money(txn.get("amount", 0)),
        "amount_display": inr(txn.get("amount", 0)),
        "utr": txn.get("utr"),
        "dispute_window_days_left": max(30 - (today - txn_date).days, 0),
        "summary": summary,
        "basis": "POL-UPI-DISPUTE section 3",
    }


# ══ 2. Health claim eligibility ══════════════════════════════════════
INITIAL_WAIT_DAYS = 30
SPECIFIC_AILMENT_MONTHS = 24
PED_MONTHS = 36

# Treatment → (category, linked pre-existing condition or None)
TREATMENTS = {
    "knee replacement": ("specific_ailment", None),
    "joint replacement": ("specific_ailment", None),
    "hip replacement": ("specific_ailment", None),
    "cataract": ("specific_ailment", None),
    "hernia": ("specific_ailment", None),
    "hysterectomy": ("specific_ailment", None),
    "kidney stone": ("specific_ailment", None),
    "gallstone": ("specific_ailment", None),
    "prostate": ("specific_ailment", None),
    "angioplasty": ("ped_linked", "hypertension"),
    "bypass surgery": ("ped_linked", "hypertension"),
    "stent": ("ped_linked", "hypertension"),
    "diabetic foot": ("ped_linked", "diabetes"),
    "retinopathy": ("ped_linked", "diabetes"),
    "dialysis": ("ped_linked", "diabetes"),
    "maternity": ("maternity", None),
    "appendectomy": ("standard", None),
    "dengue": ("standard", None),
    "pneumonia": ("standard", None),
}

# Share of a hospital bill that varies with room category, and is therefore
# exposed to proportionate deduction. Stated openly because it is an assumption.
VARIABLE_SHARE = 0.55


def classify_treatment(treatment):
    t = (treatment or "").strip().lower()
    for key, (category, linked) in TREATMENTS.items():
        if key in t:
            return key, category, linked
    return t, "standard", None


def waiting_period_check(policy, treatment, admission_date, today=None):
    """Which waiting period applies, and has it been served."""
    today = _d(today or date.today())
    admission = _d(admission_date or today)
    inception = _d(policy["inception"])
    months_served = months_between(inception, admission)
    days_served = (admission - inception).days
    matched, category, linked = classify_treatment(treatment)
    declared = [p.lower() for p in (policy.get("declared_ped") or [])]

    # A treatment linked to a declared pre-existing condition takes the PED wait.
    if category == "ped_linked" and linked and any(linked in p for p in declared):
        required, rule = PED_MONTHS, "pre-existing disease (declared)"
    elif category == "specific_ailment":
        required, rule = SPECIFIC_AILMENT_MONTHS, "specified ailment"
    elif category == "maternity":
        required, rule = 24, "maternity"
    else:
        required, rule = 0, "initial 30-day waiting period"

    if required == 0:
        served = days_served >= INITIAL_WAIT_DAYS
        clears_on = inception + timedelta(days=INITIAL_WAIT_DAYS)
    else:
        served = months_served >= required
        y, m = divmod(required, 12)
        clears_on = date(inception.year + y + (inception.month + m - 1) // 12,
                        (inception.month + m - 1) % 12 + 1,
                        min(inception.day, 28))

    return {
        "treatment_matched": matched,
        "rule_applied": rule,
        "required_months": required,
        "required_days": INITIAL_WAIT_DAYS if required == 0 else None,
        "months_of_cover": months_served,
        "served": bool(served),
        "clears_on": f"{clears_on:%Y-%m-%d}",
        "linked_condition": linked if category == "ped_linked" else None,
        "basis": "POL-HEALTH-WAITING section 3",
    }


def hospital_status(hospital_name, hospitals):
    name = (hospital_name or "").strip().lower()
    for h in hospitals or []:
        if name and (name in h["name"].lower() or h["name"].lower() in name):
            return {"matched": h["name"], "network": True,
                    "preferred_provider": bool(h.get("ppn")), "city": h.get("city")}
    return {"matched": hospital_name, "network": False, "preferred_provider": False,
            "city": None}


def claim_estimate(policy, estimated_amount, room_rent_per_day, hospital,
                   senior_citizen=False, variable_share=VARIABLE_SHARE):
    """
    Proportionate deduction and co-payment, per POL-HEALTH-SUBLIMITS.

    Eligible room rent is 1% of sum insured per day. Exceeding it reduces every
    charge that varies with room category, in the ratio eligible/actual. Co-pay
    then applies to the balance. Preferred Provider Network hospitals are exempt
    from proportionate deduction.
    """
    si = float(policy["sum_insured"])
    bill = float(estimated_amount or 0)
    eligible_room = si * 0.01
    icu_room = si * 0.02
    capped = bool(policy.get("room_rent_capped"))
    ppn = bool(hospital.get("preferred_provider"))

    actual_room = float(room_rent_per_day or 0)
    applies = capped and not ppn and actual_room > eligible_room > 0
    factor = (eligible_room / actual_room) if applies else 1.0

    variable = bill * variable_share
    non_variable = bill - variable
    after_proportion = variable * factor + non_variable
    deduction = bill - after_proportion

    co_pay_pct = float(policy.get("co_pay_percent") or 0)
    if senior_citizen and co_pay_pct == 0:
        co_pay_pct = 10.0  # PROD-HEALTH-TERMS clause 6, age 61+
    co_pay = after_proportion * co_pay_pct / 100.0

    payable = max(min(after_proportion - co_pay, si), 0.0)
    out_of_pocket = bill - payable

    # What the same treatment would cost inside the room-rent limit.
    alt = None
    if applies:
        alt_after = bill
        alt_co_pay = alt_after * co_pay_pct / 100.0
        alt_payable = max(min(alt_after - alt_co_pay, si), 0.0)
        alt = {
            "room_rent_per_day": money(eligible_room),
            "room_rent_display": inr(eligible_room),
            "estimated_payable": money(alt_payable),
            "out_of_pocket": money(bill - alt_payable),
            "out_of_pocket_display": inr(bill - alt_payable),
            "saving": money(out_of_pocket - (bill - alt_payable)),
            "saving_display": inr(out_of_pocket - (bill - alt_payable)),
        }

    return {
        "sum_insured": money(si),
        "estimated_bill": money(bill),
        "estimated_bill_display": inr(bill),
        "eligible_room_rent_per_day": money(eligible_room),
        "eligible_room_rent_display": inr(eligible_room),
        "eligible_icu_per_day": money(icu_room),
        "actual_room_rent_per_day": money(actual_room),
        "proportionate_deduction_applies": applies,
        "proportionate_factor": round(factor, 4),
        "variable_share_assumed": variable_share,
        "proportionate_deduction": money(deduction),
        "proportionate_deduction_display": inr(deduction),
        "co_pay_percent": co_pay_pct,
        "co_pay_amount": money(co_pay),
        "co_pay_display": inr(co_pay),
        "estimated_payable": money(payable),
        "estimated_payable_display": inr(payable),
        "estimated_out_of_pocket": money(out_of_pocket),
        "estimated_out_of_pocket_display": inr(out_of_pocket),
        "cheaper_room_option": alt,
        "basis": "POL-HEALTH-SUBLIMITS section 4, PROD-HEALTH-TERMS clause 6",
        "caveat": (
            f"An estimate. It assumes {int(variable_share * 100)}% of the bill varies with "
            "room category; the final split comes from the hospital's itemised bill."
        ),
    }


# ══ 3. Loan foreclosure ══════════════════════════════════════════════
DOC_RELEASE_DAYS = 30
DOC_DELAY_PENALTY_PER_DAY = 5000


def foreclosure_quote(loan, today=None):
    """
    Floating-rate loans to individuals for non-business purposes carry no
    prepayment charge. Fixed-rate loans attract 2% of outstanding principal.
    """
    today = _d(today or date.today())
    principal = float(loan["outstanding_principal"])
    rate = float(loan["rate"])
    last_emi = _d(loan["last_emi_date"])
    days_accrued = max((today - last_emi).days, 0)
    interest = principal * rate / 100.0 * days_accrued / 365.0

    floating = loan.get("rate_type") == "floating"
    business = loan.get("purpose") == "business"
    charge_free = floating and not business
    charge = 0.0 if charge_free else principal * 0.02

    total = principal + interest + charge
    release_by = today + timedelta(days=DOC_RELEASE_DAYS)

    return {
        "loan_id": loan["loan_id"],
        "product": loan.get("product"),
        "rate_type": loan.get("rate_type"),
        "rate_percent": rate,
        "outstanding_principal": money(principal),
        "outstanding_principal_display": inr(principal),
        "interest_accrued_days": days_accrued,
        "interest_accrued": money(interest),
        "interest_accrued_display": inr(interest),
        "prepayment_charge": money(charge),
        "prepayment_charge_display": inr(charge),
        "prepayment_charge_waived": charge_free,
        "charge_reason": (
            "No charge: floating rate, individual borrower, non-business purpose"
            if charge_free else
            f"2% of outstanding principal applies on a {loan.get('rate_type')} rate loan"
        ),
        "total_payable": money(total),
        "total_payable_display": inr(total),
        "quote_valid_for_date": f"{today:%Y-%m-%d}",
        "documents_held": loan.get("original_documents_held") or [],
        "mod_registered": bool(loan.get("mod_registered")),
        "document_release_by": f"{release_by:%Y-%m-%d}",
        "document_release_days": DOC_RELEASE_DAYS,
        "delay_penalty_per_day": DOC_DELAY_PENALTY_PER_DAY,
        "delay_penalty_display": inr(DOC_DELAY_PENALTY_PER_DAY),
        "basis": "POL-LOAN-FORECLOSURE section 5",
        "caveat": "Interest accrues daily, so a quote is only exact for the date shown.",
    }


# ══ 4. Periodic KYC ══════════════════════════════════════════════════
KYC_CYCLE_YEARS = {"low": 10, "medium": 8, "high": 2}
FULL_FREEZE_AFTER_MONTHS = 6


def kyc_status(customer, account=None, today=None):
    today = _d(today or date.today())
    risk = (customer.get("risk_category") or "low").lower()
    years = KYC_CYCLE_YEARS.get(risk, 10)
    last = _d(customer["last_kyc_date"])
    due = date(last.year + years, last.month, min(last.day, 28))
    overdue_days = max((today - due).days, 0)
    full_freeze_on = date(due.year + (due.month + FULL_FREEZE_AFTER_MONTHS - 1) // 12,
                          (due.month + FULL_FREEZE_AFTER_MONTHS - 1) % 12 + 1,
                          min(due.day, 28))

    non_resident = customer.get("residency") == "non_resident"
    if non_resident:
        channels = ["self-attested copies certified by the Indian Embassy or a notary "
                    "in the country of residence", "in person at the home branch while visiting India"]
        channel_note = "Video KYC is not available for non-resident accounts."
        basis = "POL-KYC-NRI section 2"
    else:
        channels = ["Video based Customer Identification Process (Aadhaar holders)",
                    "self-declaration via internet or mobile banking if details are unchanged",
                    "in person at any branch"]
        channel_note = "Video KYC is available because you are a resident Aadhaar holder."
        basis = "POL-KYC-PERIODIC section 4"

    if overdue_days == 0:
        stage, summary = "current", f"KYC is current. Next update due {due:%d %b %Y}."
    elif today < full_freeze_on:
        stage = "partial_freeze"
        summary = (
            f"KYC was due {due:%d %b %Y}, {overdue_days} days ago. The account is under "
            f"partial freeze: credits are allowed, debits are not. Full freeze applies "
            f"from {full_freeze_on:%d %b %Y}."
        )
    else:
        stage = "full_freeze"
        summary = f"KYC has been overdue since {due:%d %b %Y}. The account is under full freeze."

    if non_resident and stage != "current":
        summary += " Outward remittance, including repatriation from the NRO account, is suspended."

    return {
        "risk_category": risk,
        "cycle_years": years,
        "last_kyc_date": f"{last:%Y-%m-%d}",
        "due_date": f"{due:%Y-%m-%d}",
        "overdue_days": overdue_days,
        "stage": stage,
        "freeze_on_record": (account or {}).get("freeze", "none"),
        "full_freeze_from": f"{full_freeze_on:%Y-%m-%d}",
        "permitted_channels": channels,
        "channel_note": channel_note,
        "documents_required": (
            ["valid passport", "valid visa or residence permit", "overseas address proof"]
            if non_resident else
            ["Aadhaar or other officially valid document", "revised address proof if changed"]
        ),
        "summary": summary,
        "basis": basis,
    }
