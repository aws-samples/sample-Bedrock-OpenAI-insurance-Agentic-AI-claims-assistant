#!/usr/bin/env python3
"""
Claims Resolution Copilot — end-to-end test against the deployed stack.

Asserts the workflow (understand → synthesise → separate gaps from deductions →
recommend → draft) and, more importantly, the two governance boundaries:

  * the model recommends, the specialist decides
  * the model never produces money — every rupee figure is arithmetic
"""
import os
import json
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parent.parent
OUT = json.loads((ROOT / ".deploy" / "outputs.json").read_text())["EnterpriseAdvisor"]
CTX = ssl.create_default_context(cafile=certifi.where())
API = OUT["ApiEndpoint"]
PASSWORD = os.environ.get("DEMO_PASSWORD")
if not PASSWORD:
    sys.exit("Set DEMO_PASSWORD first — see README, Deploy step 3.")
PASS, FAIL = [], []

# Same rule the Lambda applies, restated here so the test is independent of it.
MONEY_RE = re.compile(
    r"₹|\brs\.?\b|\binr\b|\brupees?\b|\blakhs?\b|\bcrores?\b"
    r"|\b\d{1,3}(?:,\d{2})+,\d{3}\b|\b\d{1,3}(?:,\d{3})+\b", re.I)

VALID_SOURCES = {
    "POL-IMP-7.3", "POL-RR-4.1", "POL-PA-5.2", "POL-TAT-2.4", "POL-QRY-9.1",
    "POL-MOR-3.6", "DOC-DS-1", "DOC-BILL-1", "DOC-PA-1",
    "HIST-1", "HIST-2", "HIST-3", "CLAIM",
}


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def post(url, body, headers, timeout=150):
    if not url.lower().startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url!r}")
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:  # nosec B310 - https asserted in post()  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:200]}


def sign_in(user):
    st, d = post("https://cognito-idp.us-east-1.amazonaws.com/",
                 {"AuthFlow": "USER_PASSWORD_AUTH", "ClientId": OUT["UserPoolClientId"],
                  "AuthParameters": {"USERNAME": user, "PASSWORD": PASSWORD}},
                 {"Content-Type": "application/x-amz-json-1.1",
                  "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth"})
    if st != 200:
        sys.exit(f"sign-in failed: {d}")
    return d["AuthenticationResult"]["IdToken"]


def main():
    print("Claims Resolution Copilot — end-to-end test (India cashless)\n")
    tok = sign_in("cust-1001")
    other = sign_in("cust-1003")
    H = {"Content-Type": "application/json", "Authorization": f"Bearer {tok}"}
    H2 = {"Content-Type": "application/json", "Authorization": f"Bearer {other}"}

    print("[1] Authentication")
    st, _ = post(API + "/claim-analyze", {}, {"Content-Type": "application/json"})
    check("unauthenticated review rejected", st == 401, f"HTTP {st}")
    st, _ = post(API + "/claim-package", {}, {"Content-Type": "application/json"})
    check("unauthenticated package rejected", st == 401, f"HTTP {st}")

    print("\n[2] Claim package")
    st, pkg = post(API + "/claim-package", {}, H)
    claim = pkg["claim"]
    check("package returns the cashless claim under review",
          claim["claim_id"] == "CLM-48291" and claim["claim_type"] == "cashless",
          f"{claim['treatment']['procedure']} · ₹{claim['billing']['total_billed']:,}")
    absent = " ".join(claim["documents_absent"]).lower()
    check("implant documents absent",
          "implant invoice" in absent and "batch sticker" in absent,
          ", ".join(claim["documents_absent"]))
    check("pre-authorisation reference not quoted on the final bill",
          claim["pre_authorisation"]["reference_quoted_on_final_bill"] is False)
    check("room tariff exceeds the sub-limit",
          claim["treatment"]["room_tariff_per_day"]
          > claim["policy"]["sum_insured"] * claim["policy"]["room_rent_sublimit_percent"] / 100,
          f"₹{claim['treatment']['room_tariff_per_day']:,}/day vs ₹5,000 eligible")
    check("six policy and procedure clauses supplied", len(pkg["policy_excerpts"]) == 6,
          str(len(pkg["policy_excerpts"])))
    check("synthetic data declared", "synthetic" in pkg["customer"]["note"].lower())
    check("TPA context declared", "third party administrator"
          in pkg["customer"]["descriptor"].lower())

    print("\n[3] Copilot review")
    st, d = post(API + "/claim-analyze", {}, H)
    if st != 200:
        check("review completed", False, json.dumps(d)[:160])
        print(f"\npassed {len(PASS)}   failed {len(FAIL)}")
        sys.exit(1)
    r, v, m = d["recommendation"], d["validation"], d["meta"]
    check("review completed", True,
          f"{m['model']} · {m['latency_ms']}ms · {m['input_tokens']}in/{m['output_tokens']}out")
    check("status is QUERY_REQUIRED", r["status"] == "QUERY_REQUIRED", r["status"])
    check("action is RAISE_QUERY", r["recommended_action"] == "RAISE_QUERY",
          r["recommended_action"])

    gaps = " ".join(g["item"].lower() for g in r["missing_information"])
    check("found the missing implant invoice", "implant invoice" in gaps,
          f"{len(r['missing_information'])} gap(s)")
    check("found the missing batch sticker",
          "sticker" in gaps or "batch" in gaps)
    check("found the unquoted pre-authorisation reference",
          "pre-auth" in gaps or "preauth" in gaps or "authorisation reference" in gaps
          or "authorization reference" in gaps)
    check("every gap traced to a supplied source",
          all(g["source_id"] in VALID_SOURCES for g in r["missing_information"]),
          ", ".join(g["source_id"] for g in r["missing_information"]))

    print("\n[4] Gaps and deductions kept apart")
    check("room-rent clause not raised as a missing document",
          all(g["source_id"] != "POL-RR-4.1" for g in r["missing_information"]),
          "POL-RR-4.1 is a deduction, not a query")
    engaged = [x for x in r.get("deductions_applicable", []) if x["applies"]]
    check("proportionate room-rent deduction flagged",
          any(x["basis_source_id"] == "POL-RR-4.1" for x in engaged),
          ", ".join(f"{x['type']}({x['basis_source_id']})" for x in engaged) or "none")
    check("no deduction carries an amount",
          all("amount" not in x for x in r.get("deductions_applicable", [])),
          "the schema has no amount field")
    check("every deduction traced to a supplied source",
          all(x["basis_source_id"] in VALID_SOURCES
              for x in r.get("deductions_applicable", [])))

    print("\n[5] The model states no money")
    prose = " ".join(filter(None, [
        r.get("summary"), r.get("rationale"),
        r["draft_message"]["subject"], r["draft_message"]["body"],
        *(g["item"] for g in r["missing_information"]),
        *(g["why_required"] for g in r["missing_information"]),
        *(x["reason"] for x in r.get("deductions_applicable", [])),
    ]))

    # The rule under test is not "no figures" but "no figure the model
    # introduced". Repeating a number the claim already states — the room tariff,
    # the sum insured — describes an input. Producing a payable amount decides an
    # outcome, and that is what must never appear.
    #
    # Computed independently of claims._money_mentions on purpose: a test that
    # calls the implementation it is checking only proves the code agrees with
    # itself. The figures below come from the claim package over the API.
    supplied = set()

    def _collect(node):
        if isinstance(node, dict):
            for v in node.values():
                _collect(v)
        elif isinstance(node, list):
            for v in node:
                _collect(v)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            supplied.add(int(node))
        elif isinstance(node, str):
            for tok in re.findall(r"\d[\d,]*", node):
                if tok.replace(",", ""):
                    supplied.add(int(tok.replace(",", "")))

    _collect(pkg)

    introduced = [tok for tok in re.findall(r"\d[\d,]*", prose)
                  if tok.replace(",", "").isdigit()
                  and int(tok.replace(",", "")) not in supplied
                  and len(tok.replace(",", "")) >= 4]      # ignore clause numbers, dates, counts
    check("the model introduces no figure of its own", not introduced,
          f"introduced: {introduced}" if introduced
          else f"every figure it used is one of the {len(supplied)} the claim supplied")

    # No separate assertion for the settlement amount. It is computed rather than
    # supplied, so it is absent from `supplied`, so the check above already covers
    # a model that states it. Currency words are recorded for the reader only —
    # "INR" on its own asserts nothing.
    currency_words = sorted({h for h in MONEY_RE.findall(prose) if not h[:1].isdigit()})
    print(f"        currency words used: {currency_words or 'none'} — allowed, they "
          f"assert no amount on their own")

    print("\n[6] Settlement estimate computed by code")
    s = d["settlement"]
    check("settlement estimate returned", bool(s),
          f"{s['display']['payable_if_complete']} payable of {s['display']['total_billed']} billed")
    check("eligible room rent is 1% of sum insured per day",
          s["room"]["eligible_per_day"] == 5000, f"₹{s['room']['eligible_per_day']:,}")
    check("proportionate deduction correct",
          s["proportionate_deduction"] == 148750, s["display"]["proportionate_deduction"])
    check("payable if the package completes correct",
          s["payable_if_complete"] == 336250, s["display"]["payable_if_complete"])
    check("payable if the implant stays unevidenced correct",
          s["payable_if_implant_unevidenced"] == 168250,
          s["display"]["payable_if_implant_unevidenced"])
    check("value of the query correct",
          s["value_of_the_query"] == 168000, s["display"]["value_of_the_query"])
    check("only room-variable heads reduced",
          all(h["deducted"] == 0 for h in s["heads"] if not h["varies_with_room"])
          and all(h["deducted"] > 0 for h in s["heads"] if h["varies_with_room"]),
          f"{sum(1 for h in s['heads'] if h['varies_with_room'])} of {len(s['heads'])} heads vary")
    check("estimate is within the pre-authorised amount",
          s["within_pre_authorisation"] is True,
          f"{s['display']['payable_if_complete']} vs {s['display']['pre_authorisation_approved']}")
    check("estimate declares its own provenance",
          "not the model" in s["computed_by"], s["computed_by"])

    print("\n[7] Deterministic gate on the model output")
    check("all validation checks passed", v["passed"] == v["total"],
          f"{v['passed']}/{v['total']}")
    check("no blocking failure", v["blocking"] is False)
    names = {c["check"] for c in v["checks"]}
    for expected in ("Evidence cites only supplied sources",
                     "Quotes appear verbatim in the source",
                     "No amount asserted by the model",
                     "Completeness gaps kept separate from deductions",
                     "Every deduction traced to a source",
                     "Query letter omits the deduction position",
                     "Draft withholds insured identity",
                     "Draft avoids full-record requests"):
        check(f"gate includes · {expected}", expected in names)

    print("\n[8] Query letter")
    body = r["draft_message"]["body"]
    check("letter references the claim", "CLM-48291" in body)
    check("letter states the response period",
          any(t in body.lower() for t in ("15 day", "24 hour", "seven day", "7 day")),
          "drawn from POL-TAT-2.4")
    check("letter withholds insured name and date of birth",
          "Synthetic Insured A" not in body and "1961-04-22" not in body)
    check("letter omits the room-rent deduction position",
          not any(t in body.lower() for t in
                  ("proportionate", "room rent", "room tariff", "sub-limit", "sublimit")),
          "clause 9.1")
    check("letter does not request the whole record",
          not any(p in body.lower() for p in
                  ("please provide the entire medical record", "all medical records for",
                   "complete case sheet is required")))

    print("\n[9] Governance boundary")
    g = d["governance"]
    check("response declares the model may not settle",
          g["model_may_settle"] is False and g["requires_human_decision"] is True)
    check("response declares the model may not state an amount",
          g["model_may_state_an_amount"] is False)
    st, bad = post(API + "/claim-decision",
                   {"review_id": d["review_id"], "decision": "settle"}, H)
    check("only approve, edit or reject are accepted", st == 400, bad.get("error", ""))
    st, bad = post(API + "/claim-decision", {"decision": "approve"}, H)
    check("decision requires a review id", st == 400, bad.get("error", ""))
    st, bad = post(API + "/claim-decision",
                   {"review_id": d["review_id"], "decision": "approve"}, H2)
    check("another specialist cannot decide this review", st == 404, bad.get("error", ""))

    print("\n[10] Human decision")
    st, dec = post(API + "/claim-decision",
                   {"review_id": d["review_id"], "decision": "approve"}, H)
    check("approval recorded", st == 200 and dec["outcome"] == "QUERY_SENT_TO_HOSPITAL",
          f"{dec.get('outcome')} · audit {dec.get('audit_chain')}")
    check("decision attributed to the specialist", dec.get("decided_by") == "cust-1001")

    # A second review, to prove rejection is recorded too. The model call can
    # fail transiently, so report that rather than raising a KeyError on it.
    st, d2 = post(API + "/claim-analyze", {}, H)
    if st != 200 or "review_id" not in d2:
        check("rejection recorded and nothing sent", False,
              f"second review did not complete: HTTP {st} {json.dumps(d2)[:120]}")
    else:
        st, rej = post(API + "/claim-decision",
                       {"review_id": d2["review_id"], "decision": "reject"}, H)
        check("rejection recorded and nothing sent",
              st == 200 and rej.get("outcome") == "DISCARDED", rej.get("outcome", ""))

    print(f"\n{'=' * 62}\npassed {len(PASS)}   failed {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("all claims copilot checks passed")


if __name__ == "__main__":
    main()
