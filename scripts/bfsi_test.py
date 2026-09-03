#!/usr/bin/env python3
"""
Exercise the BFSI capabilities against the deployed stack.

Four journeys: UPI dispute with compensation, health claim eligibility with
proportionate deduction, loan foreclosure, and periodic KYC. Plus the
entitlement and assurance boundaries around each.
"""
import os
import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

import boto3
import certifi

ROOT = Path(__file__).resolve().parent.parent
OUT = json.loads((ROOT / ".deploy" / "outputs.json").read_text())["EnterpriseAdvisor"]
CUST = {c["customer_id"]: c for c in json.loads((ROOT / "data" / "customers.json").read_text())}
CTX = ssl.create_default_context(cafile=certifi.where())
PASSWORD = os.environ.get("DEMO_PASSWORD")
if not PASSWORD:
    sys.exit("Set DEMO_PASSWORD first — see README, Deploy step 3.")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def post(url, body, headers):
    if not url.lower().startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url!r}")
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=40, context=CTX) as r:  # nosec B310 - https asserted in post()  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:200]}


def sign_in(u):
    st, d = post(f"https://cognito-idp.us-east-1.amazonaws.com/",
                 {"AuthFlow": "USER_PASSWORD_AUTH", "ClientId": OUT["UserPoolClientId"],
                  "AuthParameters": {"USERNAME": u, "PASSWORD": PASSWORD}},
                 {"Content-Type": "application/x-amz-json-1.1",
                  "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth"})
    if st != 200:
        sys.exit(f"sign-in failed for {u}: {d}")
    return d["AuthenticationResult"]["IdToken"]


def new_session(token, username):
    """Session_Record without minting an OpenAI credential."""
    import base64
    import time
    import uuid
    claims = json.loads(base64.urlsafe_b64decode(
        token.split(".")[1] + "=" * (-len(token.split(".")[1]) % 4)))
    t = boto3.resource("dynamodb", region_name="us-east-1").Table(OUT["DataTable"])
    c = t.get_item(Key={"pk": f"CUSTOMER#{username}", "sk": "PROFILE"})["Item"]
    sid = uuid.uuid4().hex
    now = int(time.time())
    t.put_item(Item={
        "pk": f"SESSION#{sid}", "sk": "META", "session_id": sid,
        "customer_id": username, "cognito_sub": claims["sub"],
        "assurance_level": "authenticated", "assurance_expires_at": 0,
        "eligible_classifications": list(c["eligible_classifications"]),
        "accounts": list(c["accounts"]), "geography": c["geography"],
        "turn_count": 0, "created_at": now, "ttl": now + 3600,
    })
    return sid


def tool(token, sid, name, args=None):
    st, rpc = post(OUT["ApiEndpoint"] + "/mcp",
                   {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": name, "arguments": args or {}}},
                   {"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                    "x-session-id": sid})
    if rpc.get("error"):
        return {"status": "rpc_error", "error": rpc["error"]["message"]}
    return json.loads(rpc["result"]["content"][0]["text"])


def verify(token, sid, cid):
    tool(token, sid, "verify_customer_identity", {})
    return tool(token, sid, "verify_customer_identity",
                {"code": CUST[cid]["step_up_code"]})


def main():
    print("BFSI Assistant — BFSI capability test (India)\n")

    t1, t2, t3, t4 = (sign_in(c) for c in
                      ("cust-1001", "cust-1002", "cust-1003", "cust-1004"))
    s1 = new_session(t1, "cust-1001")
    s2 = new_session(t2, "cust-1002")
    s3 = new_session(t3, "cust-1003")
    s4 = new_session(t4, "cust-1004")

    # ── tool surface ────────────────────────────────────────────────
    print("[0] Tool surface")
    st, rpc = post(OUT["ApiEndpoint"] + "/mcp",
                   {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   {"Content-Type": "application/json", "Authorization": f"Bearer {t1}"})
    names = sorted(t["name"] for t in rpc["result"]["tools"])
    check("15 tools exposed", len(names) == 15, f"{len(names)}")

    # ── holdings ────────────────────────────────────────────────────
    print("\n[1] Holdings")
    h = tool(t1, s1, "get_holdings")["result"]
    check("Priya has account, loan and policy",
          h["accounts"] and h["loans"] and h["policies"],
          f"{h['accounts'][0]['balance_display']} · loan {h['loans'][0]['outstanding_display']} "
          f"· SI {h['policies'][0]['sum_insured_display']}")
    r = tool(t4, s4, "get_holdings")
    check("customer without view_holdings refused",
          r["status"] == "not_permitted", r.get("error", ""))

    # ── journey 1: UPI dispute ──────────────────────────────────────
    print("\n[2] Journey — failed UPI payment")
    txns = tool(t1, s1, "get_transactions", {"only_failed": True})["result"]
    failed = txns["transactions"][0]
    check("failed payment found with UTR",
          failed["status"] == "debited_not_credited",
          f"{failed['amount_display']} to {failed['counterparty']} · UTR {failed['utr']}")

    r = tool(t1, s1, "raise_payment_dispute", {"txn_id": failed["txn_id"]})
    check("dispute blocked at authenticated", r["status"] == "not_permitted",
          r.get("error", ""))

    verify(t1, s1, "cust-1001")
    r = tool(t1, s1, "raise_payment_dispute", {"txn_id": failed["txn_id"]})["result"]
    a = r["assessment"]
    check("TAT breach detected", a["verdict"] == "tat_breached",
          f"due {a['reversal_due_by']}, {a['delay_days']} days late")
    check("compensation computed from the rule",
          a["compensation_accrued"] == a["delay_days"] * 100,
          f"{a['compensation_accrued_display']} at Rs 100/day")
    check("dispute raised", r["dispute_raised"] and r["request_id"].startswith("SR-"),
          r.get("request_id", ""))

    r2 = tool(t1, s1, "raise_payment_dispute", {"txn_id": failed["txn_id"]})["result"]
    check("replay returns the same dispute", r2.get("replayed") is True,
          r2.get("request_id", ""))

    verify(t2, s2, "cust-1002")
    r = tool(t2, s2, "raise_payment_dispute", {"txn_id": failed["txn_id"]})["result"]
    check("another customer's txn gives no existence hint",
          r.get("found") is False, r.get("note", ""))

    # ── journey 2: health claim ─────────────────────────────────────
    print("\n[3] Journey — health claim eligibility")
    r = tool(t1, s1, "check_claim_eligibility",
             {"policy_id": "pol-7781", "treatment": "angioplasty",
              "hospital": "Apollo Hospitals Greams Road"})["result"]
    w = r["waiting_period"]
    check("PED-linked treatment blocked by 36-month wait",
          r["outcome"] == "not_yet_eligible" and w["required_months"] == 36,
          f"{w['months_of_cover']}mo cover, clears {w['clears_on']} ({w['linked_condition']})")

    r = tool(t1, s1, "check_claim_eligibility",
             {"policy_id": "pol-7781", "treatment": "knee replacement",
              "hospital": "Apollo Hospitals Greams Road"})["result"]
    check("specified ailment eligible at 24 months",
          r["outcome"] == "eligible" and r["waiting_period"]["required_months"] == 24,
          f"{r['waiting_period']['months_of_cover']}mo cover")

    r = tool(t3, s3, "check_claim_eligibility",
             {"policy_id": "pol-8842", "treatment": "knee replacement",
              "hospital": "Fortis Bannerghatta", "estimated_amount": 450000,
              "room_rent_per_day": 14000})["result"]
    e = r["estimate"]
    check("proportionate deduction applied", e["proportionate_deduction_applies"],
          f"factor {e['proportionate_factor']} on eligible {e['eligible_room_rent_display']}")
    check("senior co-payment applied", e["co_pay_percent"] == 10.0, e["co_pay_display"])
    check("out-of-pocket computed",
          e["estimated_out_of_pocket"] > 0,
          f"pays {e['estimated_payable_display']}, customer {e['estimated_out_of_pocket_display']}")
    check("cheaper-room guidance offered",
          e["cheaper_room_option"] is not None,
          f"saves {e['cheaper_room_option']['saving_display']} at "
          f"{e['cheaper_room_option']['room_rent_display']}/day")

    r = tool(t3, s3, "check_claim_eligibility",
             {"policy_id": "pol-8842", "treatment": "knee replacement",
              "hospital": "Manipal Hospital Old Airport Road", "estimated_amount": 450000,
              "room_rent_per_day": 14000})["result"]
    check("preferred-provider hospital exempt from deduction",
          r["estimate"]["proportionate_deduction_applies"] is False,
          "PPN exemption honoured")

    print("\n[4] Cashless pre-authorisation")
    r = tool(t3, s3, "initiate_cashless_preauth",
             {"policy_id": "pol-8842", "hospital": "Fortis Bannerghatta",
              "treatment": "knee replacement", "estimated_amount": 450000})
    check("preauth blocked at authenticated", r["status"] == "not_permitted",
          r.get("error", ""))
    verify(t3, s3, "cust-1003")
    r = tool(t3, s3, "initiate_cashless_preauth",
             {"policy_id": "pol-8842", "hospital": "Fortis Bannerghatta",
              "treatment": "knee replacement", "estimated_amount": 450000})["result"]
    check("preauth authorised once verified", r["authorised"] is True,
          f"{r['request_id']} · decision within {r['decision_due_within']}")

    verify(t1, s1, "cust-1001")
    r = tool(t1, s1, "initiate_cashless_preauth",
             {"policy_id": "pol-7781", "hospital": "Apollo Hospitals Greams Road",
              "treatment": "angioplasty"})["result"]
    check("tool refuses unserved waiting period even when verified",
          r["authorised"] is False and r["reason"] == "waiting_period_not_served",
          r.get("explanation", ""))

    # ── journey 3: foreclosure ──────────────────────────────────────
    print("\n[5] Journey — loan foreclosure")
    q = tool(t1, s1, "get_foreclosure_quote", {"loan_id": "loan-4471"})["result"]
    check("floating-rate individual loan has no prepayment charge",
          q["prepayment_charge"] == 0 and q["prepayment_charge_waived"],
          q["charge_reason"])
    check("payoff includes accrued interest",
          q["interest_accrued"] > 0,
          f"{q['outstanding_principal_display']} + {q['interest_accrued_display']} "
          f"= {q['total_payable_display']}")
    check("document release deadline and penalty stated",
          q["document_release_days"] == 30 and q["delay_penalty_per_day"] == 5000,
          f"by {q['document_release_by']}, then {q['delay_penalty_display']}/day")

    q2 = tool(t3, s3, "get_foreclosure_quote", {"loan_id": "loan-5518"})["result"]
    check("fixed-rate loan attracts 2 percent",
          q2["prepayment_charge"] > 0, q2["prepayment_charge_display"])

    r = tool(t2, s2, "get_foreclosure_quote", {"loan_id": "loan-4471"})
    check("customer without loan_servicing refused",
          r["status"] == "not_permitted", r.get("error", ""))

    # ── journey 4: KYC ──────────────────────────────────────────────
    print("\n[6] Journey — periodic KYC")
    k = tool(t1, s1, "get_kyc_status")["result"]
    check("resident low-risk on a 10-year cycle",
          k["cycle_years"] == 10 and k["stage"] == "current",
          f"due {k['due_date']}")
    check("resident may use Video KYC",
          any("Video" in c for c in k["permitted_channels"]))

    k = tool(t2, s2, "get_kyc_status")["result"]
    check("NRI medium-risk overdue on an 8-year cycle",
          k["cycle_years"] == 8 and k["stage"] == "partial_freeze",
          f"{k['overdue_days']} days overdue, full freeze {k['full_freeze_from']}")
    check("Video KYC excluded for non-residents",
          not any("Video" in c for c in k["permitted_channels"]),
          k["channel_note"])
    check("repatriation suspension surfaced",
          "repatriation" in k["summary"].lower())

    # ── eligibility filter on the new corpus ────────────────────────
    print("\n[7] Retrieval boundaries on the India corpus")
    r = tool(t1, s1, "search_policy",
             {"query": "compensation for delayed UPI reversal"})["result"]
    docs = [e["document_id"] for e in r["evidence"]]
    classes = {e["access_classification"] for e in r["evidence"]}
    check("UPI dispute policy retrieved", "POL-UPI-DISPUTE" in docs, ", ".join(docs[:4]))
    check("no internal or restricted document",
          not classes & {"internal", "restricted"}, f"{sorted(classes)}")

    r = tool(t1, s1, "search_policy",
             {"query": "goodwill credit limit fee waiver discretion for a customer"})["result"]
    leaked = [e["document_id"] for e in r["evidence"]
              if e["document_id"] == "PLAYBOOK-DISPUTE-GOODWILL"]
    check("internal goodwill matrix never retrieved", not leaked)

    r = tool(t1, s1, "search_policy",
             {"query": "is my account flagged for suspicious transaction reporting"})["result"]
    leaked = [e["document_id"] for e in r["evidence"] if e["document_id"] == "POL-FIU-STR"]
    check("restricted financial-crime policy never retrieved", not leaked)

    r = tool(t2, s2, "search_policy", {"query": "periodic KYC update requirements"})["result"]
    docs = [e["document_id"] for e in r["evidence"]]
    check("NRI customer gets the non-resident KYC policy",
          "POL-KYC-NRI" in docs, ", ".join(docs[:4]))

    r = tool(t4, s4, "search_policy", {"query": "periodic KYC update requirements"})["result"]
    classes = {e["access_classification"] for e in r["evidence"]}
    check("public-only customer sees public documents only",
          classes == {"public"}, f"{sorted(classes)}")

    print(f"\n{'=' * 62}\npassed {len(PASS)}   failed {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("all BFSI checks passed")


if __name__ == "__main__":
    main()
