"""
Synthetic cashless claim package — India.

Apex Health Services is a fictional Third Party Administrator settling cashless
hospitalisation claims for a retail health insurer. No real insured, hospital,
insurer or TPA data. Every identifier is invented.

The package is deliberately imperfect in three distinct ways, which is what the
copilot has to untangle:

  1. the implant invoice and batch sticker are absent — a hard completeness gap
  2. the pre-authorisation reference is not quoted on the final bill
  3. the room tariff exceeds the policy sub-limit, which triggers a
     proportionate deduction rather than a query

Only the first two are grounds for a query letter. The third is a payable-amount
consequence, and conflating them is the mistake a rushed specialist makes.
"""

CUSTOMER = {
    "organisation": "Apex Health Services",
    "descriptor": "fictional Third Party Administrator, retail health portfolio",
    "persona": "Claims Operations Specialist",
    "regulator_context": "IRDAI cashless framework",
    "note": "All data below is synthetic. No PHI or real policy data is present.",
}

CLAIM = {
    "claim_id": "CLM-48291",
    "claim_type": "cashless",
    "received": "2026-08-14",
    "insured": {
        "member_id": "APX-772041",
        "name": "Synthetic Insured A",
        "date_of_birth": "1961-04-22",
        "age": 65,
        "abha_id": "••-••••-••••-7714",
        "relationship": "self",
    },
    "policy": {
        "policy_no": "AHS/RH/2024/0099231",
        "product": "Apex Arogya Secure — Family Floater",
        "sum_insured": 500000,
        "inception": "2019-06-10",
        "continuous_cover_months": 86,
        "room_rent_sublimit_percent": 1.0,
        "co_pay_percent": 0,
        "declared_ped": ["hypertension"],
        "status": "in force",
    },
    "hospital": {
        "name": "Synthetic Multispeciality Hospital",
        "city": "Bengaluru",
        "network_status": "network",
        "preferred_provider": False,
        "nabh_accredited": True,
        "rohini_id": "0000-0000-0000",
    },
    "treatment": {
        "diagnosis": "Primary osteoarthritis, right knee",
        "icd_10": "M17.11",
        "procedure": "Total knee replacement, right",
        "pcs_code": "0SRC0J9",
        "admission": "2026-08-04",
        "discharge": "2026-08-08",
        "length_of_stay_days": 4,
        "room_category_occupied": "Single private deluxe",
        "room_tariff_per_day": 12000,
        "implant_used": True,
        "implant_description": "Cemented cobalt-chromium total knee prosthesis",
    },
    "billing": {
        "total_billed": 485000,
        "currency": "INR",
        "breakup": {
            "room_and_nursing": 48000,
            "surgeon_and_anaesthetist": 145000,
            "operation_theatre": 62000,
            "implant_prosthesis": 168000,
            "pharmacy_and_consumables": 41000,
            "investigations": 21000,
        },
    },
    "pre_authorisation": {
        "requested_on": "2026-08-03",
        "approved_amount": 420000,
        "reference": "PA-2026-778104",
        "reference_quoted_on_final_bill": False,
    },
    "documents_submitted": [
        "Claim form Part A — insured declaration",
        "Claim form Part B — hospital declaration",
        "Discharge summary signed by treating orthopaedic surgeon",
        "Final consolidated bill with itemised breakup",
        "Pre-operative investigation reports",
    ],
    "documents_absent": [
        "Implant invoice",
        "Implant batch sticker",
    ],
}

POLICY_EXCERPTS = [
    {
        "source_id": "POL-IMP-7.3",
        "document": "Apex Arogya Secure — Claims Documentation Standard",
        "section": "Clause 7.3 — Implants and prostheses",
        "effective_date": "2026-04-01",
        "text": (
            "Where an implant or prosthesis is used, the claim must be supported by the original "
            "implant invoice and the implant batch sticker bearing the lot number. The invoice must "
            "identify the manufacturer, the model, and the price charged. In the absence of both the "
            "invoice and the sticker, the implant component of the bill is not payable and the claim "
            "cannot be settled in full. A line entry for the implant in the consolidated hospital "
            "bill does not substitute for the invoice."
        ),
    },
    {
        "source_id": "POL-RR-4.1",
        "document": "Apex Arogya Secure — Policy Terms",
        "section": "Clause 4.1 — Room rent sub-limit and proportionate deduction",
        "effective_date": "2026-04-01",
        "text": (
            "Eligible room rent is limited to 1 percent of the sum insured per day for a shared or "
            "single private room. Where the insured occupies a room whose tariff exceeds the eligible "
            "limit, all charges that vary with room category, comprising room and nursing, surgeon and "
            "anaesthetist fees, and operation theatre charges, are payable in the proportion that the "
            "eligible tariff bears to the actual tariff. Proportionate deduction does not apply to "
            "implants, pharmacy, consumables or investigations, and does not apply where treatment is "
            "taken at a Preferred Provider Network hospital."
        ),
    },
    {
        "source_id": "POL-PA-5.2",
        "document": "Apex Health Services — Cashless Settlement Procedure",
        "section": "Clause 5.2 — Pre-authorisation reference on the final bill",
        "effective_date": "2026-04-01",
        "text": (
            "For a cashless claim, the pre-authorisation reference number issued by the TPA must be "
            "quoted on the hospital's final consolidated bill. Where the reference is not quoted, the "
            "bill cannot be reconciled to the authorisation and the hospital must be asked to reissue "
            "the bill quoting the reference. The authorisation reference already held on the TPA record "
            "does not remove this requirement."
        ),
    },
    {
        "source_id": "POL-TAT-2.4",
        "document": "Apex Health Services — Service Turnaround Standard",
        "section": "Clause 2.4 — Cashless timelines and queries",
        "effective_date": "2026-04-01",
        "text": (
            "A decision on a cashless pre-authorisation request is communicated to the hospital within "
            "one hour of receipt. Final authorisation on discharge is granted within three hours of the "
            "discharge request. Where a query is raised on a submitted claim, the query must be issued "
            "within 24 hours of receipt of the claim, and the claim must be settled within 15 days of "
            "receipt of the last necessary document."
        ),
    },
    {
        "source_id": "POL-QRY-9.1",
        "document": "Apex Health Services — Provider Communication Standard",
        "section": "Clause 9.1 — Content of a query letter",
        "effective_date": "2026-04-01",
        "text": (
            "A query letter to a hospital must list each specific document or clarification required, "
            "cite the policy or procedure clause creating the requirement, and state the period allowed "
            "for response. A query must not request a document already submitted with the claim, must "
            "not request the insured's entire medical record, and must not restate the deduction "
            "position, which is communicated separately in the settlement advice."
        ),
    },
    {
        "source_id": "POL-MOR-3.6",
        "document": "Apex Arogya Secure — Policy Terms",
        "section": "Clause 3.6 — Moratorium and pre-existing disease",
        "effective_date": "2026-04-01",
        "text": (
            "A pre-existing disease declared at proposal is subject to a waiting period of 36 months of "
            "continuous cover. After 60 months of continuous cover, no claim may be contested on the "
            "ground of non-disclosure or misrepresentation of a pre-existing condition, except where "
            "established fraud is demonstrated. This is the moratorium period."
        ),
    },
]

PROVIDER_DOCUMENTS = [
    {
        "source_id": "DOC-DS-1",
        "type": "Discharge summary",
        "date": "2026-08-08",
        "author": "Synthetic Consultant Orthopaedic Surgeon",
        "text": (
            "Insured admitted 04 August 2026 for right total knee replacement for primary "
            "osteoarthritis. Cemented cobalt-chromium prosthesis implanted. Intra-operative period "
            "uneventful. Post-operative recovery satisfactory, mobilised with walker from day two. "
            "Discharged 08 August 2026 on oral analgesia with advice for physiotherapy. Signed by the "
            "treating consultant. Room occupied: single private deluxe."
        ),
    },
    {
        "source_id": "DOC-BILL-1",
        "type": "Final consolidated bill",
        "date": "2026-08-08",
        "text": (
            "Total charges INR 4,85,000 for admission 04 to 08 August 2026. Heads billed: room and "
            "nursing 48,000; surgeon and anaesthetist 1,45,000; operation theatre 62,000; implant and "
            "prosthesis 1,68,000; pharmacy and consumables 41,000; investigations 21,000. Room tariff "
            "charged at INR 12,000 per day for four days. The pre-authorisation reference number is not "
            "printed on this bill. No separate implant invoice is enclosed."
        ),
    },
    {
        "source_id": "DOC-PA-1",
        "type": "Pre-authorisation record held by the TPA",
        "date": "2026-08-03",
        "text": (
            "Pre-authorisation PA-2026-778104 issued 03 August 2026 for right total knee replacement, "
            "approved amount INR 4,20,000, valid for admission within seven days. Room category "
            "authorised: as per policy eligibility. Decision communicated to the hospital within the "
            "one-hour standard."
        ),
    },
]

CLAIM_HISTORY = [
    {
        "source_id": "HIST-1",
        "claim_id": "CLM-41902",
        "date_of_service": "2026-02-19",
        "treatment": "Right knee arthroscopy, diagnostic",
        "claim_type": "cashless",
        "billed_amount": 62000,
        "settled_amount": 62000,
        "status": "SETTLED",
        "note": "Same insured, same joint. Implant not used. Settled without query.",
    },
    {
        "source_id": "HIST-2",
        "claim_id": "CLM-39655",
        "date_of_service": "2025-11-02",
        "treatment": "Physiotherapy, day care, 8 sessions",
        "claim_type": "reimbursement",
        "billed_amount": 19500,
        "settled_amount": 19500,
        "status": "SETTLED",
        "note": "Outpatient day care benefit, within limit.",
    },
    {
        "source_id": "HIST-3",
        "claim_id": "CLM-44120",
        "date_of_service": "2026-06-28",
        "treatment": "Pre-operative consultation and investigations",
        "claim_type": "reimbursement",
        "billed_amount": 8400,
        "settled_amount": 8400,
        "status": "SETTLED",
        "note": "Consultation ahead of the knee replacement under review.",
    },
]


def package():
    return {
        "customer": CUSTOMER,
        "claim": CLAIM,
        "policy_excerpts": POLICY_EXCERPTS,
        "provider_documents": PROVIDER_DOCUMENTS,
        "claim_history": CLAIM_HISTORY,
    }


def valid_source_ids():
    ids = {p["source_id"] for p in POLICY_EXCERPTS}
    ids |= {d["source_id"] for d in PROVIDER_DOCUMENTS}
    ids |= {h["source_id"] for h in CLAIM_HISTORY}
    ids.add("CLAIM")
    return ids
