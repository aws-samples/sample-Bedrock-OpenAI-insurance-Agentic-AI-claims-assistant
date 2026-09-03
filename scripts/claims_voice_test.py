#!/usr/bin/env python3
"""
Provider Voice Assistant — end-to-end test against the deployed stack.

Asserts the authority boundary rather than the conversation. The interesting
questions are not "did it answer" but:

  * can this surface reach a settlement outcome?          (it must not)
  * does it ever call the room-rent deduction a document? (it must not)
  * can it request something that is not outstanding?     (it must not)
  * does it agree with the Claims Specialist workflow?    (it must)
  * does the backend ever hand the browser the API key?   (it must not)

Run after scripts/claims_test.py, which covers the Responses API surface.
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
CLAIM = "CLM-48291"
PASS, FAIL = [], []

# An OpenAI project key. Asserted absent from every response the browser sees.
KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{20,}")


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def post(url, body, headers, timeout=60):
    if not url.lower().startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url!r}")
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:  # nosec B310 - https asserted in post()  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            raw = r.read()
            return r.status, json.loads(raw or "{}"), raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or "{}"), raw.decode("utf-8", "replace")
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:200]}, raw.decode("utf-8", "replace")


def sign_in(user):
    st, d, _ = post("https://cognito-idp.us-east-1.amazonaws.com/",
                    {"AuthFlow": "USER_PASSWORD_AUTH", "ClientId": OUT["UserPoolClientId"],
                     "AuthParameters": {"USERNAME": user, "PASSWORD": PASSWORD}},
                    {"Content-Type": "application/x-amz-json-1.1",
                     "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth"})
    if st != 200:
        sys.exit(f"sign-in failed: {d}")
    return d["AuthenticationResult"]["IdToken"]


def main():
    print("Provider Voice Assistant — end-to-end test (Realtime surface)\n")
    tok = sign_in("cust-1001")
    other = sign_in("cust-1003")
    H = {"Content-Type": "application/json", "Authorization": f"Bearer {tok}"}
    H2 = {"Content-Type": "application/json", "Authorization": f"Bearer {other}"}

    def tool(name, args, headers=None, sid=None):
        hdr = dict(headers or H)
        if sid:
            hdr["x-session-id"] = sid
        return post(API + "/claim-voice-tool", {"name": name, "arguments": args}, hdr)

    # ── 1 · authentication ────────────────────────────────────────────
    print("[1] Authentication")
    st, _, _ = post(API + "/claim-voice-session", {}, {"Content-Type": "application/json"})
    check("unauthenticated session refused", st == 401, f"HTTP {st}")
    st, _, _ = post(API + "/claim-voice-tool", {"name": "get_claim_status"},
                    {"Content-Type": "application/json"})
    check("unauthenticated tool call refused", st == 401, f"HTTP {st}")

    # ── 2 · session creation ──────────────────────────────────────────
    print("\n[2] Realtime session")
    st, sess, raw = post(API + "/claim-voice-session", {}, H)
    if st != 200:
        check("session created", False, json.dumps(sess)[:200])
        print(f"\npassed {len(PASS)}   failed {len(FAIL)}")
        sys.exit(1)
    sid = sess["session_id"]
    check("session created", True, f"{sess['model']} · {sid[:12]}…")
    check("ephemeral client secret returned", bool(sess.get("client_secret")),
          f"{len(sess.get('client_secret') or '')} chars")

    # The security property that matters most on this route.
    check("response contains no OpenAI API key", not KEY_RE.search(raw),
          "no sk- token anywhere in the payload")
    check("response contains no secret ARN",
          "OPENAI_SECRET_ARN" not in raw and "secretsmanager" not in raw.lower())
    check("system instructions not leaked to the browser",
          "You are the voice assistant" not in raw and "YOU MUST NOT" not in raw)
    check("authority declared to the UI",
          "approve a claim" in json.dumps(sess.get("authority", {})),
          "may / may_not published")

    # ── 3 · get_claim_status ──────────────────────────────────────────
    print("\n[3] get_claim_status")
    st, d, _ = tool("get_claim_status", {"claim_id": CLAIM}, sid=sid)
    r = d.get("result") or {}
    check("valid claim resolves", d.get("status") == "ok" and r.get("claim_id") == CLAIM,
          f"{r.get('status')} · {r.get('outstanding_items_count')} outstanding")
    check("status is pending while items are outstanding",
          r.get("status") == "PENDING_DOCUMENTS", r.get("status_label", ""))
    # Asserted against the claim package rather than a literal, so the check
    # tests agreement between the two surfaces instead of a hardcoded name.
    _, _pkg, _ = post(API + "/claim-package", {}, H)
    check("provider matches the claim package",
          r.get("provider") == _pkg["claim"]["hospital"]["name"], r.get("provider", ""))
    check("no settlement figure in the status payload",
          not any(k in r for k in ("payable", "settlement_amount", "amount", "total_billed")),
          "status carries no money")

    st, d, _ = tool("get_claim_status", {"claim_id": "CLM-00000"}, sid=sid)
    check("invalid claim returns not_found", d.get("status") == "not_found",
          d.get("error", ""))
    check("not_found leaks no existence detail",
          "CLM-48291" not in json.dumps(d), "no reference to the real claim")

    st, d, _ = tool("get_claim_status", {}, sid=sid)
    check("missing claim_id rejected", d.get("status") == "invalid_request",
          d.get("error", ""))

    # ── 4 · get_missing_documents ─────────────────────────────────────
    print("\n[4] get_missing_documents")
    st, d, _ = tool("get_missing_documents", {"claim_id": CLAIM}, sid=sid)
    docs = (d.get("result") or {}).get("missing_documents") or []
    names = " ".join(x["document"].lower() for x in docs)
    clauses = {x["source_clause"] for x in docs}
    check("three documentary gaps returned", len(docs) == 3, str(len(docs)))
    check("implant invoice listed", "implant invoice" in names)
    check("implant batch sticker listed", "batch sticker" in names)
    check("pre-authorisation reference listed",
          "pre-authorisation reference" in names or "pre-auth" in names)
    check("every gap cites a clause", all(x.get("source_clause") for x in docs),
          ", ".join(sorted(clauses)))

    # The single most important assertion in this file.
    check("room-rent sub-limit NEVER appears as a missing document",
          "POL-RR-4.1" not in clauses
          and not any(t in names for t in ("room", "sub-limit", "sublimit", "tariff")),
          "POL-RR-4.1 is a deduction, not a document")

    # ── 5 · get_claim_review_summary ──────────────────────────────────
    print("\n[5] get_claim_review_summary")
    st, d, _ = tool("get_claim_review_summary", {"claim_id": CLAIM}, sid=sid)
    rev = d.get("result") or {}
    gaps = rev.get("documentary_gaps") or []
    issues = rev.get("policy_issues") or []
    check("gaps and policy issues are separate fields",
          bool(gaps) and bool(issues),
          f"{len(gaps)} gap(s) · {len(issues)} policy issue(s)")
    check("room-rent deduction appears as a policy issue",
          any(i.get("source_clause") == "POL-RR-4.1" and i.get("type") == "deduction"
              for i in issues))
    check("deduction marked as requiring no provider action",
          all(i.get("requires_provider_action") is False for i in issues))
    check("gaps marked as requiring provider action",
          all(g.get("requires_provider_action") is True for g in gaps))
    check("no deduction clause among the documentary gaps",
          all(g.get("source_clause") != "POL-RR-4.1" for g in gaps))
    check("settlement amount deliberately null",
          rev.get("settlement_amount") is None
          and "specialist" in (rev.get("settlement_note") or "").lower())

    # ── 6 · send_document_request ─────────────────────────────────────
    print("\n[6] send_document_request")
    st, d, _ = tool("send_document_request",
                    {"claim_id": CLAIM, "document_ids": ["implant_invoice"],
                     "confirmed": False}, sid=sid)
    check("unconfirmed request refused", d.get("status") == "confirmation_required",
          d.get("error", "")[:70])

    st, d, _ = tool("send_document_request",
                    {"claim_id": CLAIM, "document_ids": ["room_rent_sublimit"],
                     "confirmed": True}, sid=sid)
    check("a non-enum document id is rejected", d.get("status") == "invalid_request",
          "enum-constrained, so the deduction is unrequestable")

    st, d, _ = tool("send_document_request",
                    {"claim_id": CLAIM, "document_ids": ["discharge_summary"],
                     "confirmed": True}, sid=sid)
    check("a document that is not outstanding is rejected",
          d.get("status") in ("invalid_request", "not_outstanding"), d.get("status", ""))

    st, d, _ = tool("send_document_request",
                    {"claim_id": CLAIM,
                     "document_ids": ["implant_invoice", "implant_batch_sticker"],
                     "confirmed": True}, sid=sid)
    sent = d.get("result") or {}
    check("confirmed request for outstanding items succeeds",
          d.get("status") == "ok" and sent.get("status") == "simulated_sent",
          sent.get("audit_reference", ""))
    check("nothing actually sent", "simulated" in (sent.get("note") or "").lower())
    check("audit reference issued",
          (sent.get("audit_reference") or "").startswith("DOCREQ-"))

    # ── 7 · handoff ───────────────────────────────────────────────────
    print("\n[7] handoff_to_claims_specialist")
    st, d, _ = tool("handoff_to_claims_specialist",
                    {"claim_id": CLAIM, "reason": "caller asked for a settlement amount"},
                    sid=sid)
    ho = d.get("result") or {}
    check("handoff recorded", d.get("status") == "ok"
          and ho.get("status") == "handoff_created", ho.get("reference", ""))

    # ── 8 · the authority boundary ────────────────────────────────────
    print("\n[8] Authority boundary")
    for forbidden in ("claim_decision", "approve_claim", "settle_claim",
                      "settlement_estimate"):
        st, d, _ = tool(forbidden, {"claim_id": CLAIM, "decision": "approve"}, sid=sid)
        check(f"cannot invoke · {forbidden}",
              d.get("status") == "not_permitted" and d.get("result") is None,
              "refused server-side")

    st, d, _ = tool("get_claim_status",
                    {"claim_id": CLAIM, "customer_id": "cust-1003"}, sid=sid)
    check("cannot smuggle a customer id through arguments",
          d.get("status") == "invalid_request", d.get("error", "")[:60])

    # The specialist route must still refuse the voice session's identity trick.
    st, d, _ = post(API + "/claim-decision",
                    {"review_id": f"REV-{sid[:8].upper()}", "decision": "approve"}, H)
    check("voice session id is not a claim review id", st == 404,
          "the decision route is unreachable from a voice session")

    # ── 9 · session binding ───────────────────────────────────────────
    print("\n[9] Session binding")
    st, d, _ = tool("get_claim_status", {"claim_id": CLAIM}, headers=H2, sid=sid)
    check("another identity cannot use this session", st == 403, d.get("error", ""))
    st, d, _ = tool("get_claim_status", {"claim_id": CLAIM}, sid="not-a-session")
    check("unknown session refused", st == 400, d.get("error", ""))
    st, d, _ = post(API + "/claim-voice-tool",
                    {"name": "get_claim_status", "arguments": {"claim_id": CLAIM}}, H)
    check("missing session header refused", st == 400, d.get("error", ""))

    # ── 10 · the two surfaces agree ───────────────────────────────────
    print("\n[10] Consistency with the Claims Specialist workflow")
    st, pkg, _ = post(API + "/claim-package", {}, H)
    spec_claim = pkg["claim"]
    st, d, _ = tool("get_claim_status", {"claim_id": CLAIM}, sid=sid)
    voice = d.get("result") or {}
    check("same claim id", voice.get("claim_id") == spec_claim["claim_id"])
    check("same provider", voice.get("provider") == spec_claim["hospital"]["name"])
    check("same procedure", voice.get("procedure") == spec_claim["treatment"]["procedure"])

    st, d, _ = tool("get_missing_documents", {"claim_id": CLAIM}, sid=sid)
    voice_docs = (d.get("result") or {}).get("missing_documents") or []
    voice_names = " ".join(x["document"].lower() for x in voice_docs)
    for absent in spec_claim["documents_absent"]:
        check(f"voice reports the specialist's gap · {absent}",
              absent.lower() in voice_names)
    check("voice reports the unquoted pre-auth reference the specialist sees",
          spec_claim["pre_authorisation"]["reference_quoted_on_final_bill"] is False
          and ("pre-authorisation reference" in voice_names or "pre-auth" in voice_names))
    check("outstanding count matches the specialist's gap count",
          voice.get("outstanding_items_count") == len(voice_docs) == 3,
          f"{voice.get('outstanding_items_count')} vs 3")

    # ── 11 · telemetry ────────────────────────────────────────────────
    print("\n[11] Telemetry")
    st, d, _ = post(API + "/claim-voice-event",
                    {"event": "first_assistant_audio", "session_id": sid,
                     "ms_to_first_audio": 640}, H)
    check("telemetry event accepted", st == 200 and d.get("recorded") is True)
    st, d, _ = post(API + "/claim-voice-event",
                    {"event": "exfiltrate", "session_id": sid}, H)
    check("unrecognised event rejected", st == 400, d.get("error", ""))
    st, d, _ = post(API + "/claim-voice-event",
                    {"event": "session_ended", "session_id": sid}, H2)
    check("cannot report telemetry for someone else's session", st == 403,
          d.get("error", ""))

    print(f"\n{'=' * 62}\npassed {len(PASS)}   failed {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("all provider voice assistant checks passed")


if __name__ == "__main__":
    main()
