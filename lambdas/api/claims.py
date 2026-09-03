"""
Claims Resolution Copilot — the Amazon Bedrock step of the workflow.

Runs an OpenAI model (gpt-5.6-terra) through Amazon Bedrock Converse, with the
review schema declared as a forced tool. Both claims surfaces therefore use
OpenAI models, but reached two different ways, each chosen for what it can do:

    THIS MODULE      OpenAI via Bedrock Converse  ·  structured · no credential
    claims_voice.py  OpenAI Realtime API direct   ·  speech to speech · WebRTC

The review runs through Bedrock because that keeps it inside the AWS boundary and
authorises with the execution role — no stored key. The voice surface calls the
OpenAI Realtime API directly, because that low-latency speech-to-speech API is
not offered through Bedrock.

One consequence worth knowing: OpenAI's Responses API enforced `strict: true`
json_schema server-side, so a conforming object was guaranteed. Bedrock's forced
tool call has no such guarantee. The deterministic gate below therefore now
carries weight it did not have to before — see the "Schema conformance" check,
which validates the object rather than assuming the API already did.

Boundary this module defends:

  PROBABILISTIC   understanding, synthesis, gap detection, recommendation, drafting
  ─────────────
  DETERMINISTIC   schema validation, evidence closure, action allow-list,
                  every rupee figure, human approval, system-of-record write

The model returns a structured recommendation. Everything after that is checked
in code before a specialist ever sees it, and nothing is committed without an
explicit human decision.

Two India-specific rules are enforced here rather than hoped for:

  1. The model never produces money. `deductions_applicable` has no amount field,
     so it cannot state one structurally, and `validate()` rejects a rupee figure
     anywhere in the model's own prose. `settlement_estimate()` below is the only
     source of a payable amount, and it is arithmetic, not inference.

  2. Completeness gaps and deductions are different objects. A missing implant
     invoice is a query to the hospital. A room tariff above the sub-limit is a
     payable-amount consequence and must never appear in a query letter — clause
     9.1 forbids it. Conflating the two is the mistake this gate blocks.
"""
import json
import logging
import os
import re
import time

import bfsi
import claims_data

log = logging.getLogger(__name__)

# OpenAI's gpt-5.6-terra, served by Amazon Bedrock. This is an OpenAI model —
# the same one the original OpenAI-direct implementation used — but reached
# through Bedrock rather than api.openai.com, so the call authorises with the
# Lambda execution role and needs no stored credential.
#
# `us.` prefix: a cross-region inference profile, required because these models
# are not offered ON_DEMAND in a single region.
#
# Measured ~14s on this prompt with 4 evidence citations, comfortably inside the
# API Gateway HTTP API's hard 30-second integration ceiling. Alternatives, set
# via CLAIMS_MODEL:
#   openai.gpt-oss-120b-1:0   OpenAI open-weight, ON_DEMAND (no profile), ~12s
#   us.anthropic.claude-*     also work on the same Converse path
MODEL = os.environ.get("CLAIMS_MODEL", "us.openai.gpt-5.6-terra")

# OpenAI reasoning models on Bedrock reject the `temperature` inference field.
# Detected from the model id rather than hardcoded, so a Claude override still
# gets a temperature and terra does not — the Converse call is otherwise
# identical for both providers.
_MODEL_TAKES_TEMPERATURE = "openai" not in MODEL.lower()

# Read timeout for the Bedrock client. Deliberately longer than the gateway's 30s
# so a slow model produces a clean error in the logs rather than a boto3 retry
# that pays for the same answer twice.
TIMEOUT = 90

STATUSES = ("READY_TO_SETTLE", "QUERY_REQUIRED", "REFER_TO_MEDICAL_TEAM")
ACTIONS = ("SETTLE", "RAISE_QUERY", "REFER_TO_MEDICAL_TEAM")
DEDUCTION_TYPES = ("PROPORTIONATE_ROOM_RENT", "IMPLANT_NOT_EVIDENCED",
                   "NON_PAYABLE_CONSUMABLES", "CO_PAY", "SUB_LIMIT_OTHER")

# The clause that creates a deduction rather than a query. Citing it as a
# completeness gap is the conflation error.
DEDUCTION_ONLY_CLAUSES = {"POL-RR-4.1"}

INSTRUCTIONS = """You are a claims operations assistant at Apex Health Services, a Third Party
Administrator settling cashless hospitalisation claims in India under the IRDAI framework. A
Claims Operations Specialist has handed you one claim package.

Your job is to produce an operational outcome, not a summary.

Decide whether the claim can be settled as submitted. If it cannot, separate two different things:

  A. COMPLETENESS GAPS — documents or clarifications the hospital must supply. These go in
     `missing_information` and are the content of the query letter.
  B. DEDUCTIONS — policy provisions that reduce the payable amount but require nothing from the
     hospital. These go in `deductions_applicable` and are communicated later in the settlement
     advice, NOT in the query letter.

Conflating the two is the error to avoid. A room tariff above the eligible sub-limit is a
deduction. It is not a query, it is not a missing document, and it must not appear in the letter.

Rules you must follow:

1. Cite only source_id values present in the package. Never invent a source id, a policy clause,
   a claim number, a pre-authorisation reference, or a hospital name.
2. Quote the specific sentence from the source that creates each requirement, verbatim.
3. NEVER state, compute, or estimate any amount of money. Do not write a rupee figure, an INR
   figure, a lakh figure, a comma-grouped number, a payable amount, a deduction amount, or a
   proportion of the bill — not in the summary, not in the rationale, not in a deduction reason,
   and not in the letter. Every settlement figure is calculated by the claims system from the
   billed heads. You may name the head of charge affected and the clause that affects it, and
   nothing more. Restating a clause's own wording inside an `evidence` quote is fine.
4. Do not request a document already listed as submitted in the package.
5. Do not request the insured's entire medical record.
6. Do not settle, reject, or repudiate the claim. You recommend; the specialist decides.
7. The query letter must list each required document, cite the clause requiring it, and state the
   response period drawn from policy. It must NOT mention the room tariff, the room-rent
   sub-limit, or any proportionate deduction — clause 9.1 forbids restating the deduction
   position in a query.
8. Address the letter to the hospital, quote the claim number, and include no clinical detail
   beyond what identifies the admission.
9. Use `QUERY_REQUIRED` with `RAISE_QUERY` when a document is missing. Use `READY_TO_SETTLE` with
   `SETTLE` only when the package is complete. Use `REFER_TO_MEDICAL_TEAM` when the question is
   clinical rather than documentary.

Write for a professional Indian claims audience. Be concise and precise."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "summary", "missing_information", "deductions_applicable",
                 "evidence", "recommended_action", "rationale", "draft_message", "confidence"],
    "properties": {
        "status": {"type": "string", "enum": list(STATUSES)},
        "summary": {
            "type": "string",
            "description": "One or two sentences a specialist can read at a glance. No amounts.",
        },
        "missing_information": {
            "type": "array",
            "description": "Documents or clarifications the hospital must supply. Query letter "
                           "content only. Never a deduction.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["item", "why_required", "source_id"],
                "properties": {
                    "item": {"type": "string"},
                    "why_required": {"type": "string"},
                    "source_id": {"type": "string"},
                },
            },
        },
        "deductions_applicable": {
            "type": "array",
            "description": "Policy provisions that reduce the payable amount. Flag them; the "
                           "claims system computes them. There is deliberately no amount field.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "applies", "basis_source_id", "reason"],
                "properties": {
                    "type": {"type": "string", "enum": list(DEDUCTION_TYPES)},
                    "applies": {"type": "boolean"},
                    "basis_source_id": {"type": "string"},
                    "reason": {
                        "type": "string",
                        "description": "Why the provision is engaged. Name the heads of charge "
                                       "affected. State no figure.",
                    },
                },
            },
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source_id", "reference", "quote"],
                "properties": {
                    "source_id": {"type": "string"},
                    "reference": {"type": "string"},
                    "quote": {"type": "string"},
                },
            },
        },
        "recommended_action": {"type": "string", "enum": list(ACTIONS)},
        "rationale": {"type": "string"},
        "draft_message": {
            "type": "object",
            "additionalProperties": False,
            "required": ["to", "subject", "body"],
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
}




# ══════════════════════════════════════════════════════════════════════
# Deterministic settlement arithmetic — the only place a rupee figure is
# produced. Clause POL-RR-4.1 for the proportionate deduction, POL-IMP-7.3
# for the implant exposure.
# ══════════════════════════════════════════════════════════════════════

# Heads that vary with room category, per clause 4.1. Everything else is
# outside the proportionate deduction.
ROOM_VARIABLE_HEADS = ("room_and_nursing", "surgeon_and_anaesthetist", "operation_theatre")
IMPLANT_HEAD = "implant_prosthesis"

HEAD_LABELS = {
    "room_and_nursing": "Room and nursing",
    "surgeon_and_anaesthetist": "Surgeon and anaesthetist",
    "operation_theatre": "Operation theatre",
    "implant_prosthesis": "Implant and prosthesis",
    "pharmacy_and_consumables": "Pharmacy and consumables",
    "investigations": "Investigations",
}


def rupees(value):
    """₹ with Indian lakh grouping. Reuses the BFSI formatter's grouping."""
    s = bfsi.inr(value).replace("Rs ", "₹")
    return s[:-3] if s.endswith(".00") else s


def settlement_estimate():
    """
    What the claim is worth, computed from the billed heads and the policy.

    Two figures matter to the specialist:

      payable_if_complete            the implant invoice arrives, only the
                                     room-rent proportion is deducted
      payable_if_implant_unevidenced the invoice never arrives, so clause 7.3
                                     removes the implant head entirely

    The gap between them is what the query letter is worth, which is the number
    that justifies chasing the hospital.
    """
    claim = claims_data.CLAIM
    pol = claim["policy"]
    treat = claim["treatment"]
    bill = claim["billing"]["breakup"]

    eligible_per_day = round(pol["sum_insured"] * pol["room_rent_sublimit_percent"] / 100)
    actual_per_day = treat["room_tariff_per_day"]
    exceeded = actual_per_day > eligible_per_day
    # A Preferred Provider Network hospital is carved out of clause 4.1.
    carved_out = bool(claim["hospital"].get("preferred_provider"))
    applies = exceeded and not carved_out
    factor = (eligible_per_day / actual_per_day) if applies else 1.0

    heads, variable_billed, variable_payable = [], 0, 0
    for key, billed in bill.items():
        varies = key in ROOM_VARIABLE_HEADS
        payable = round(billed * factor) if varies else billed
        if varies:
            variable_billed += billed
            variable_payable += payable
        heads.append({
            "head": key,
            "label": HEAD_LABELS.get(key, key),
            "billed": billed,
            "varies_with_room": varies,
            "payable": payable,
            "deducted": billed - payable,
            "billed_display": rupees(billed),
            "payable_display": rupees(payable),
        })

    total_billed = claim["billing"]["total_billed"]
    proportionate = variable_billed - variable_payable
    gross = total_billed - proportionate
    co_pay = round(gross * pol["co_pay_percent"] / 100)
    payable_complete = min(gross - co_pay, pol["sum_insured"])

    implant_billed = bill.get(IMPLANT_HEAD, 0)
    implant_evidenced = not any("implant invoice" in d.lower()
                                for d in claim["documents_absent"])
    payable_unevidenced = payable_complete - (0 if implant_evidenced else implant_billed)

    pre_auth = claim["pre_authorisation"]["approved_amount"]

    return {
        "currency": "INR",
        "computed_by": "claims system · deterministic arithmetic, not the model",
        "basis": ["POL-RR-4.1", "POL-IMP-7.3"],
        "total_billed": total_billed,
        "sum_insured": pol["sum_insured"],
        "room": {
            "eligible_per_day": eligible_per_day,
            "actual_per_day": actual_per_day,
            "sublimit_percent": pol["room_rent_sublimit_percent"],
            "exceeded": exceeded,
            "preferred_provider_carve_out": carved_out,
            "deduction_applies": applies,
            "proportion_payable": round(factor, 6),
            "proportion_display": f"{eligible_per_day:,} : {actual_per_day:,}",
        },
        "heads": heads,
        "variable_heads_billed": variable_billed,
        "proportionate_deduction": proportionate,
        "co_pay_percent": pol["co_pay_percent"],
        "co_pay": co_pay,
        "payable_if_complete": payable_complete,
        "implant_at_risk": 0 if implant_evidenced else implant_billed,
        "implant_evidenced": implant_evidenced,
        "payable_if_implant_unevidenced": payable_unevidenced,
        "value_of_the_query": payable_complete - payable_unevidenced,
        "pre_authorisation_approved": pre_auth,
        "within_pre_authorisation": payable_complete <= pre_auth,
        "display": {
            "total_billed": rupees(total_billed),
            "sum_insured": rupees(pol["sum_insured"]),
            "eligible_room_per_day": rupees(eligible_per_day),
            "actual_room_per_day": rupees(actual_per_day),
            "proportionate_deduction": rupees(proportionate),
            "payable_if_complete": rupees(payable_complete),
            "payable_if_implant_unevidenced": rupees(payable_unevidenced),
            "implant_at_risk": rupees(0 if implant_evidenced else implant_billed),
            "value_of_the_query": rupees(payable_complete - payable_unevidenced),
            "pre_authorisation_approved": rupees(pre_auth),
        },
    }


# ══════════════════════════════════════════════════════════════════════
# Model call
# ══════════════════════════════════════════════════════════════════════
def _source_text_index():
    """source_id → searchable text, for verifying quotes."""
    pkg = claims_data.package()
    idx = {}
    for p in pkg["policy_excerpts"]:
        idx[p["source_id"]] = f"{p['document']} {p['section']} {p['text']}"
    for d in pkg["provider_documents"]:
        idx[d["source_id"]] = f"{d['type']} {d['text']}"
    for h in pkg["claim_history"]:
        idx[h["source_id"]] = json.dumps(h)
    idx["CLAIM"] = json.dumps(pkg["claim"])
    return idx


def _normalise(s):
    return " ".join((s or "").lower().split())


WHOLE_RECORD_PHRASES = (
    "entire medical record", "full medical record", "complete medical record",
    "full clinical record", "entire clinical record", "complete chart",
    "entire chart", "full patient record", "all medical records",
    "entire case record", "complete case sheet", "entire case sheet",
)
# Token-level negations immediately before the phrase.
NEGATION_TOKENS = {
    "no", "not", "never", "without", "exclude", "excluding", "excluded",
    "avoid", "avoiding", "dont", "doesnt", "isnt", "arent", "wont", "nor",
}
# Multi-word cues that scope the phrase out.
NEGATION_PHRASES = (
    "rather than", "instead of", "other than", "only the", "limited to",
    "no need for", "do not", "does not", "will not", "not require",
    "not requesting", "not required", "not necessary",
)

# A money figure, in any of the shapes an Indian claims letter would use.
# Bare integers are deliberately NOT matched — "clause 7.3", "24 hours" and
# "36 months" are legitimate and a digit rule would flag all three.
MONEY_RE = re.compile(
    r"₹"
    r"|\brs\.?\b"
    r"|\binr\b"
    r"|\brupees?\b"
    r"|\blakhs?\b"
    r"|\bcrores?\b"
    r"|\b\d{1,3}(?:,\d{2})+,\d{3}\b"      # Indian grouping, 4,85,000
    r"|\b\d{1,3}(?:,\d{3})+\b",           # Western grouping, 485,000
    re.IGNORECASE,
)

# Language that restates the room-rent deduction. Clause 9.1 keeps this out of
# a query letter.
DEDUCTION_CUES = (
    "proportionate", "room rent", "room-rent", "room tariff", "sub-limit",
    "sublimit", "sub limit", "eligible room", "room category charged",
    "deducted", "deduction",
)


def _full_record_request(body):
    """
    True only when the draft actually asks for the whole record.

    Policy 9.1 forbids requesting the insured's full clinical record. A model
    that writes "no full clinical record is requested" is complying, not
    breaching, so a substring match is not sufficient. Look at the tokens
    immediately before the phrase, where the negation actually sits.
    """
    text = _normalise(body)
    negated = []
    for phrase in WHOLE_RECORD_PHRASES:
        start = 0
        while (i := text.find(phrase, start)) != -1:
            before = text[max(0, i - 60):i]
            tokens = [t.strip(".,;:!?()\"'") for t in before.split()][-4:]
            if (set(tokens) & NEGATION_TOKENS
                    or any(c in before for c in NEGATION_PHRASES)):
                negated.append(phrase)
            else:
                return True, negated          # a genuine request
            start = i + len(phrase)
    return False, negated


def _input_figures():
    """
    Every number that already appears in the claim package handed to the model.

    Used to separate two very different acts. Repeating the room tariff the claim
    states is describing an input. Producing a payable amount is deciding an
    outcome. The original rule could not tell them apart, so it blocked both, and
    a model that helpfully wrote "a deluxe room at INR 12,000 per day" failed the
    review for quoting a fact it had been given.
    """
    figures = set()

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            figures.add(int(node))
        elif isinstance(node, str):
            for token in re.findall(r"\d[\d,]*", node):
                digits = token.replace(",", "")
                if digits:
                    figures.add(int(digits))

    walk(build_input())
    return figures


def _money_mentions(result):
    """
    Money figures the model introduced on its own authority.

    A figure already present in the claim package is not a finding — see
    _input_figures. A figure that is not is exactly what this check exists to
    catch, and the computed payable amount is never in the input, so a model that
    states a settlement figure still fails here.

    `evidence[].quote` is excluded: a verbatim clause quote may legitimately
    contain a figure, and quotes are separately verified against the source.
    """
    known = _input_figures()
    fields = [
        ("summary", result.get("summary")),
        ("rationale", result.get("rationale")),
        ("draft subject", (result.get("draft_message") or {}).get("subject")),
        ("draft body", (result.get("draft_message") or {}).get("body")),
    ]
    for i, m in enumerate(result.get("missing_information") or []):
        fields.append((f"gap {i + 1} item", m.get("item")))
        fields.append((f"gap {i + 1} reason", m.get("why_required")))
    for i, d in enumerate(result.get("deductions_applicable") or []):
        fields.append((f"deduction {i + 1} reason", d.get("reason")))

    hits = []
    for where, text in fields:
        for match in MONEY_RE.finditer(text or ""):
            token = match.group(0).strip()
            digits = token.replace(",", "")
            if digits.isdigit():
                # A number. Only a finding if the model brought it, not the claim.
                if int(digits) in known:
                    continue
                hits.append(f"{where}: {token}")
                continue
            # A bare currency word (₹, INR, lakh). On its own it asserts nothing,
            # so it is only a finding when an unknown figure sits beside it —
            # which the numeric branch above will already have reported.
            window = (text or "")[match.end():match.end() + 24]
            nearby = re.search(r"\d[\d,]*", window)
            if nearby and int(nearby.group(0).replace(",", "")) not in known:
                hits.append(f"{where}: {token} {nearby.group(0)}")
    return hits


def build_input():
    """
    What actually goes to the model. Only the fields needed for the decision —
    name, date of birth and ABHA id are withheld, because completeness does not
    turn on identity.
    """
    pkg = claims_data.package()
    claim = json.loads(json.dumps(pkg["claim"]))
    claim["insured"] = {
        "member_id": claim["insured"]["member_id"],
        "age": claim["insured"]["age"],
        "relationship": claim["insured"]["relationship"],
    }
    return {
        "claim": claim,
        "policy_excerpts": pkg["policy_excerpts"],
        "provider_documents": pkg["provider_documents"],
        "claim_history": pkg["claim_history"],
        "today": os.environ.get("DEMO_TODAY") or time.strftime("%Y-%m-%d"),
    }


_JSON_TYPES = {"string": str, "integer": int, "number": (int, float),
               "boolean": bool, "array": list, "object": dict}


def _schema_problems(result, schema=None, path="review"):
    """
    Validate the model's object against SCHEMA, returning a list of problems.

    A deliberately small recursive checker rather than a jsonschema dependency:
    the Lambda bundle ships no third-party packages, and the subset of JSON
    Schema this project uses is types, required, enum, and nested
    objects/arrays. Anything beyond that would be a false sense of rigour.
    """
    schema = schema or SCHEMA
    problems = []

    expected = schema.get("type")
    if expected and expected in _JSON_TYPES:
        # bool is a subclass of int in Python, so integer must reject True.
        if expected in ("integer", "number") and isinstance(result, bool):
            return [f"{path}: expected {expected}, got boolean"]
        if not isinstance(result, _JSON_TYPES[expected]):
            return [f"{path}: expected {expected}, got {type(result).__name__}"]

    if isinstance(result, dict):
        for field in schema.get("required", []):
            if field not in result:
                problems.append(f"{path}.{field}: missing")
        if schema.get("additionalProperties") is False:
            extra = set(result) - set(schema.get("properties") or {})
            if extra:
                problems.append(f"{path}: unexpected {sorted(extra)}")
        for field, sub in (schema.get("properties") or {}).items():
            if field in result:
                problems += _schema_problems(result[field], sub, f"{path}.{field}")

    elif isinstance(result, list):
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(result):
                problems += _schema_problems(item, item_schema, f"{path}[{i}]")

    allowed = schema.get("enum")
    if allowed is not None and result not in allowed:
        problems.append(f"{path}: {result!r} not in {allowed}")

    return problems[:6]      # enough to diagnose, not enough to flood the UI


def _tool_config():
    """
    Structured output on Bedrock, obtained by declaring the review schema as a
    tool and forcing the model to call it.

    Bedrock Converse has no equivalent of OpenAI's `strict: true` json_schema —
    there is no server-side guarantee that the emitted object conforms. Forcing
    a single tool via toolChoice is the closest available mechanism: the model
    must answer with a toolUse block shaped by inputSchema, which in practice
    holds. But "in practice" is doing real work in that sentence, so the
    deterministic gate in validate() below is now load-bearing rather than a
    second opinion. Nothing downstream trusts this object until it passes.
    """
    return {
        "tools": [{"toolSpec": {
            "name": "claim_review",
            "description": "Return the structured claim review. Call this exactly once.",
            "inputSchema": {"json": SCHEMA},
        }}],
        "toolChoice": {"tool": {"name": "claim_review"}},
    }


def call_model(_secret_arn=None, reasoning_effort="medium"):
    """
    Run the claim review on Amazon Bedrock.

    The first argument is retained so the handler's call site is unchanged, but
    it is unused: Bedrock authorises through the Lambda execution role, so this
    surface needs no stored credential at all. The OpenAI secret remains in
    Secrets Manager for the Realtime voice surface, which has no Bedrock
    equivalent available.

    `reasoning_effort` maps onto temperature rather than a provider knob. The
    review is meant to be reproducible, so the default is deterministic.
    """
    import boto3
    from botocore.config import Config

    # Read timeout must exceed the model's thinking time or boto3 retries a call
    # that is still running, which doubles the token spend for one answer.
    client = boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "us-east-1")),
        config=Config(read_timeout=TIMEOUT, connect_timeout=10, retries={"max_attempts": 2}),
    )

    # A reproducible review wants temperature 0 where the model accepts one.
    # OpenAI reasoning models on Bedrock do not — they reject the field outright
    # and control determinism internally — so it is omitted for them.
    inference = {"maxTokens": 4096}
    if _MODEL_TAKES_TEMPERATURE:
        inference["temperature"] = {"low": 0.0, "medium": 0.0, "high": 0.2}.get(
            reasoning_effort, 0.0)

    kwargs = dict(
        modelId=MODEL,
        system=[{"text": INSTRUCTIONS}],
        messages=[{"role": "user",
                   "content": [{"text": json.dumps(build_input(), indent=2)}]}],
        toolConfig=_tool_config(),
        inferenceConfig=inference,
    )
    # Responsible-AI guardrail (provisioned in the CDK stack, wired into this
    # function's environment). It screens the model's input and output for
    # disallowed content, anonymises PII, and denies financial or insurance advice
    # presented as a final decision — defence in depth on top of the deterministic
    # gate in validate() and the specialist sign-off.
    #
    # This FAILS CLOSED, deliberately. A high-risk financial review must not
    # silently downgrade to an unguarded model because an environment variable
    # went missing, so an absent guardrail raises rather than proceeding.
    # ALLOW_UNGUARDED_MODEL=1 is the documented escape hatch for local work
    # against a stack built before the guardrail existed; it is logged as a
    # warning every time it is used, so it cannot be the quiet default.
    _gid, _gver = os.environ.get("GUARDRAIL_ID"), os.environ.get("GUARDRAIL_VERSION")
    if _gid and _gver:
        kwargs["guardrailConfig"] = {"guardrailIdentifier": _gid,
                                     "guardrailVersion": _gver}
    elif os.environ.get("ALLOW_UNGUARDED_MODEL") == "1":
        log.warning(
            "ALLOW_UNGUARDED_MODEL=1 and no GUARDRAIL_ID/GUARDRAIL_VERSION: running "
            "the claims review WITHOUT a Bedrock guardrail. Not for any environment "
            "a customer can reach.")
    else:
        raise RuntimeError(
            "refusing to run the claims review without a Bedrock guardrail: deploy the "
            "CDK stack so GUARDRAIL_ID and GUARDRAIL_VERSION are set, or set "
            "ALLOW_UNGUARDED_MODEL=1 to override this deliberately (see "
            "RESPONSIBLE-AI.md)")

    started = time.time()
    response = client.converse(**kwargs)
    latency_ms = int((time.time() - started) * 1000)

    blocks = (response.get("output") or {}).get("message", {}).get("content") or []
    review = next((b["toolUse"]["input"] for b in blocks if "toolUse" in b), None)
    if review is None:
        # Most often stopReason == "max_tokens": the model was cut off mid-object.
        # Surfaced explicitly rather than as a KeyError, because the difference
        # matters when someone is debugging a failed review.
        raise ValueError(
            f"model returned no structured review (stopReason="
            f"{response.get('stopReason')!r})")

    usage = response.get("usage") or {}
    return review, {
        "model": MODEL,
        "latency_ms": latency_ms,
        "input_tokens": usage.get("inputTokens", 0),
        "output_tokens": usage.get("outputTokens", 0),
        # Bedrock reports reasoning tokens only for models that expose extended
        # thinking, and not for this call shape. Reported as 0 rather than
        # omitted, so the response contract to the UI is unchanged.
        "reasoning_tokens": (usage.get("cacheReadInputTokens") or 0) and 0,
        "response_id": response.get("ResponseMetadata", {}).get("RequestId"),
        "stop_reason": response.get("stopReason"),
    }


# ══════════════════════════════════════════════════════════════════════
# The gate
# ══════════════════════════════════════════════════════════════════════
def validate(result):
    """
    Deterministic gate. The model's output is untrusted until this passes.

    Returns (checks, blocking) where checks is a list of named results the UI
    displays, and blocking is True when the recommendation must not be shown as
    actionable. Structured-field checks block. Prose heuristics inform.
    """
    checks = []
    valid_ids = claims_data.valid_source_ids()
    idx = _source_text_index()
    claim = claims_data.CLAIM

    def add(name, ok, detail="", blocking=False):
        checks.append({"check": name, "ok": bool(ok), "detail": detail,
                       "blocking": bool(blocking and not ok)})

    # Under OpenAI's Responses API this check could be a formality, because
    # `strict: true` json_schema was enforced server-side and a non-conforming
    # object could not arrive. Bedrock's forced tool call carries no such
    # guarantee, so the object is now actually inspected here. This is the check
    # that stops a truncated or malformed review reaching a specialist.
    schema_problems = _schema_problems(result)
    add("Schema conformance", not schema_problems,
        "; ".join(schema_problems) if schema_problems
        else f"{len(SCHEMA['required'])} required fields present, types correct",
        blocking=True)

    add("Status in allow-list", result.get("status") in STATUSES,
        result.get("status", "—"), blocking=True)
    add("Action in allow-list", result.get("recommended_action") in ACTIONS,
        result.get("recommended_action", "—"), blocking=True)

    ev = result.get("evidence") or []
    unknown = [e.get("source_id") for e in ev if e.get("source_id") not in valid_ids]
    add("Evidence cites only supplied sources", not unknown,
        f"unknown: {unknown}" if unknown else f"{len(ev)} citation(s) resolved", blocking=True)

    bad_quotes = [e.get("source_id") for e in ev
                  if e.get("source_id") in idx
                  and _normalise(e.get("quote"))[:90] not in _normalise(idx[e["source_id"]])]
    add("Quotes appear verbatim in the source", not bad_quotes,
        f"not found in: {bad_quotes}" if bad_quotes else "all quotes matched", blocking=True)

    miss = result.get("missing_information") or []
    miss_unknown = [m.get("source_id") for m in miss if m.get("source_id") not in valid_ids]
    add("Every gap traced to a source", not miss_unknown,
        f"unknown: {miss_unknown}" if miss_unknown else f"{len(miss)} gap(s) traced",
        blocking=True)

    ded = result.get("deductions_applicable") or []
    ded_unknown = [d.get("basis_source_id") for d in ded
                   if d.get("basis_source_id") not in valid_ids]
    add("Every deduction traced to a source", not ded_unknown,
        f"unknown: {ded_unknown}" if ded_unknown
        else f"{sum(1 for d in ded if d.get('applies'))} of {len(ded)} deduction(s) engaged",
        blocking=True)

    # ── the money rule ────────────────────────────────────────────────
    money = _money_mentions(result)
    add("No amount asserted by the model", not money,
        "; ".join(money[:4]) if money
        else "every rupee figure comes from the claims system", blocking=True)

    # ── gaps and deductions are different objects ─────────────────────
    conflated = [m.get("source_id") for m in miss
                 if m.get("source_id") in DEDUCTION_ONLY_CLAUSES]
    add("Completeness gaps kept separate from deductions", not conflated,
        f"{conflated} is a deduction clause, not a missing document" if conflated
        else "no deduction clause raised as a query", blocking=True)

    # ── advisory checks ───────────────────────────────────────────────
    supplied = _normalise(" ".join(claim["documents_submitted"]))
    redundant = [m["item"] for m in miss
                 if m.get("item") and _normalise(m["item"]) in supplied]
    add("No request for already-supplied documents", not redundant,
        f"redundant: {redundant}" if redundant else "none redundant")

    absent = [d.lower() for d in claim["documents_absent"]]
    gap_text = _normalise(" ".join(m.get("item", "") for m in miss))
    found_absent = [d for d in absent if d in gap_text]
    add("Absent documents identified", len(found_absent) == len(absent),
        f"{len(found_absent)} of {len(absent)}: {', '.join(found_absent) or 'none'}")

    room_flagged = any(d.get("applies") and d.get("basis_source_id") == "POL-RR-4.1"
                       for d in ded)
    add("Room-rent deduction identified", room_flagged,
        "flagged for settlement advice under POL-RR-4.1" if room_flagged
        else "clause 4.1 deduction not flagged")

    body = ((result.get("draft_message") or {}).get("body") or "")
    add("Draft references the correct claim", claim["claim_id"] in body, claim["claim_id"])

    leaked = [c for c in DEDUCTION_CUES if c in _normalise(body)]
    add("Query letter omits the deduction position", not leaked,
        f"clause 9.1 keeps this out of a query: {leaked}" if leaked
        else "deduction reserved for the settlement advice")

    over_broad, negated = _full_record_request(body)
    add("Draft avoids full-record requests", not over_broad,
        f"phrase used as a prohibition, not a request: {negated}" if negated and not over_broad
        else ("policy 9.1 prohibits requesting the whole record" if over_broad
              else "no whole-record language"))

    add("Draft withholds insured identity",
        claim["insured"]["name"].lower() not in body.lower()
        and claim["insured"]["date_of_birth"] not in body,
        "no name or date of birth in the draft")

    # The model must not have settled the claim.
    add("No settlement asserted by the model",
        result.get("status") != "READY_TO_SETTLE"
        or result.get("recommended_action") == "SETTLE",
        "recommendation only; approval is a separate human step")

    blocking = any(c["blocking"] for c in checks)
    return checks, blocking
