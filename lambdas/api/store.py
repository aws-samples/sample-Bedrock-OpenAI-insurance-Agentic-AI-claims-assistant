"""Single-table access for customers, sessions, service requests, and config."""
import os
import time
import uuid
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

_ddb = boto3.resource("dynamodb")
_table = _ddb.Table(os.environ["TABLE_DATA"])

SESSION_TTL_SECONDS = 3600


def _get(pk, sk):
    return _table.get_item(Key={"pk": pk, "sk": sk}).get("Item")


def _safe(value):
    """
    DynamoDB rejects Python floats. Convert at the storage boundary so tools can
    work in plain numbers and never have to think about it.
    """
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return value


# ── config ────────────────────────────────────────────────────────────
def get_config(key, default=None):
    item = _get("CONFIG", key)
    return (item or {}).get("value", default)


def put_config(key, value):
    _table.put_item(Item={"pk": "CONFIG", "sk": key, "value": value})


# ── customers ─────────────────────────────────────────────────────────
def get_customer(customer_id):
    return _get(f"CUSTOMER#{customer_id}", "PROFILE")


def put_customer(customer):
    item = dict(customer)
    item["pk"] = f"CUSTOMER#{customer['customer_id']}"
    item["sk"] = "PROFILE"
    _table.put_item(Item=item)


# ── sessions ──────────────────────────────────────────────────────────
def create_session(customer, cognito_sub):
    session_id = uuid.uuid4().hex
    now = int(time.time())
    item = {
        "pk": f"SESSION#{session_id}",
        "sk": "META",
        "session_id": session_id,
        "customer_id": customer["customer_id"],
        "cognito_sub": cognito_sub,
        "assurance_level": "authenticated",
        "assurance_expires_at": 0,
        "eligible_classifications": list(customer.get("eligible_classifications") or []),
        "accounts": list(customer.get("accounts") or []),
        "geography": customer.get("geography", "GLOBAL"),
        "turn_count": 0,
        "created_at": now,
        "ttl": now + SESSION_TTL_SECONDS,
    }
    _table.put_item(Item=item)
    return item


def get_session(session_id):
    return _get(f"SESSION#{session_id}", "META")


def set_assurance(session_id, level, expires_at):
    _table.update_item(
        Key={"pk": f"SESSION#{session_id}", "sk": "META"},
        UpdateExpression="SET assurance_level = :l, assurance_expires_at = :e",
        ExpressionAttributeValues={":l": level, ":e": int(expires_at)},
    )


def set_pending_challenge(session_id, code, expires_at, attempts=0):
    _table.update_item(
        Key={"pk": f"SESSION#{session_id}", "sk": "META"},
        UpdateExpression=(
            "SET challenge_code = :c, challenge_expires_at = :e, challenge_attempts = :a"
        ),
        ExpressionAttributeValues={":c": code, ":e": int(expires_at), ":a": int(attempts)},
    )


def bump_challenge_attempts(session_id, attempts):
    _table.update_item(
        Key={"pk": f"SESSION#{session_id}", "sk": "META"},
        UpdateExpression="SET challenge_attempts = :a",
        ExpressionAttributeValues={":a": int(attempts)},
    )


# ── service requests ──────────────────────────────────────────────────
def find_by_idempotency(customer_id, key):
    item = _get(f"IDEM#{customer_id}", key)
    if not item:
        return None
    return get_request(customer_id, item["request_id"])


def create_request(customer_id, account_id, request_type, details, idempotency_key):
    request_id = f"SR-{uuid.uuid4().hex[:8].upper()}"
    now = int(time.time())
    item = {
        "pk": f"CUSTOMER#{customer_id}",
        "sk": f"REQ#{request_id}",
        "request_id": request_id,
        "customer_id": customer_id,
        "account_id": account_id,
        "request_type": request_type,
        "details": _safe(details or {}),
        "status": "received",
        "created_at": now,
    }
    _table.put_item(Item=item)
    if idempotency_key:
        _table.put_item(
            Item={
                "pk": f"IDEM#{customer_id}",
                "sk": idempotency_key,
                "request_id": request_id,
            }
        )
    return item


def get_request(customer_id, request_id):
    return _get(f"CUSTOMER#{customer_id}", f"REQ#{request_id}")


def list_requests(customer_id):
    resp = _table.query(
        KeyConditionExpression=Key("pk").eq(f"CUSTOMER#{customer_id}")
        & Key("sk").begins_with("REQ#")
    )
    return resp.get("Items") or []


def create_escalation(customer_id, session_id, reason, summary, context):
    esc_id = f"ESC-{uuid.uuid4().hex[:8].upper()}"
    _table.put_item(
        Item={
            "pk": f"CUSTOMER#{customer_id}",
            "sk": f"ESC#{esc_id}",
            "escalation_id": esc_id,
            "session_id": session_id,
            "reason": reason,
            "summary": summary,
            "context": _safe(context or {}),
            "status": "queued",
            "created_at": int(time.time()),
        }
    )
    return esc_id


# ── token usage ───────────────────────────────────────────────────────
USAGE_FIELDS = (
    "in_text", "in_audio", "in_cached_text", "in_cached_audio",
    "out_text", "out_audio", "total_tokens", "turns",
)


def _add(pk, sk, deltas, extra=None):
    """Atomic counter increment. Creates the item if absent."""
    if not deltas:
        return
    names, values, sets = {}, {}, []
    for i, (k, v) in enumerate(deltas.items()):
        names[f"#f{i}"] = k
        values[f":v{i}"] = int(v)
        sets.append(f"#f{i} :v{i}")
    expr = "ADD " + ", ".join(sets)
    if extra:
        parts = []
        for j, (k, v) in enumerate(extra.items()):
            names[f"#e{j}"] = k
            values[f":e{j}"] = v
            parts.append(f"#e{j} = :e{j}")
        expr += " SET " + ", ".join(parts)
    _table.update_item(
        Key={"pk": pk, "sk": sk},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def record_usage(session_id, customer_id, turn_no, usage):
    """Append a per-turn record and roll it into session and global totals."""
    now = int(time.time())
    item = {
        "pk": f"USAGE#{session_id}",
        "sk": f"TURN#{turn_no:05d}",
        "session_id": session_id,
        "customer_id": customer_id,
        "turn": int(turn_no),
        "ts": now,
        "ttl": now + 60 * 60 * 24 * 30,
    }
    item.update({k: int(usage.get(k, 0) or 0) for k in USAGE_FIELDS if k != "turns"})
    _table.put_item(Item=item)

    deltas = {k: int(usage.get(k, 0) or 0) for k in USAGE_FIELDS if k != "turns"}
    deltas["turns"] = 1
    _add(f"USAGE#{session_id}", "TOTAL", deltas,
         extra={"customer_id": customer_id, "last_ts": now})
    _add("USAGE", "GLOBAL", deltas, extra={"last_ts": now})
    return item


def bump_tool_counter(tool_name, outcome):
    _add("USAGE", "TOOLS", {tool_name: 1, f"outcome_{outcome}": 1, "tool_calls": 1})


def usage_global():
    return _get("USAGE", "GLOBAL") or {}


def usage_tools():
    return _get("USAGE", "TOOLS") or {}


def usage_sessions(limit=25):
    """Recent per-session totals. Small table, so a scan is proportionate here."""
    resp = _table.scan(
        FilterExpression="begins_with(pk, :p) AND sk = :s",
        ExpressionAttributeValues={":p": "USAGE#", ":s": "TOTAL"},
    )
    items = resp.get("Items") or []
    items.sort(key=lambda i: int(i.get("last_ts", 0)), reverse=True)
    return items[:limit]


def usage_turns(session_id, limit=200):
    resp = _table.query(
        KeyConditionExpression=Key("pk").eq(f"USAGE#{session_id}")
        & Key("sk").begins_with("TURN#"),
        Limit=limit,
    )
    return resp.get("Items") or []


# ── holdings, transactions, reference data ────────────────────────────
def _query(pk, prefix):
    resp = _table.query(
        KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(prefix)
    )
    return resp.get("Items") or []


def get_accounts(customer_id):
    return _query(f"CUSTOMER#{customer_id}", "ACCT#")


def get_account(customer_id, account_id):
    return _get(f"CUSTOMER#{customer_id}", f"ACCT#{account_id}")


def get_loans(customer_id):
    return _query(f"CUSTOMER#{customer_id}", "LOAN#")


def get_loan(customer_id, loan_id):
    return _get(f"CUSTOMER#{customer_id}", f"LOAN#{loan_id}")


def get_policies(customer_id):
    return _query(f"CUSTOMER#{customer_id}", "POLICY#")


def get_policy(customer_id, policy_id):
    return _get(f"CUSTOMER#{customer_id}", f"POLICY#{policy_id}")


def get_transactions(customer_id, limit=10):
    items = _query(f"CUSTOMER#{customer_id}", "TXN#")
    items.sort(key=lambda i: i.get("date", ""), reverse=True)
    return items[:limit]


def get_transaction(customer_id, txn_id):
    for t in _query(f"CUSTOMER#{customer_id}", "TXN#"):
        if t.get("txn_id") == txn_id or t.get("utr") == txn_id:
            return t
    return None


def get_hospitals():
    item = _get("REF", "HOSPITALS")
    return (item or {}).get("value") or []


def put_reference(sk, value):
    _table.put_item(Item={"pk": "REF", "sk": sk, "value": value})


def put_holding(customer_id, sk, payload):
    item = dict(payload)
    item["pk"] = f"CUSTOMER#{customer_id}"
    item["sk"] = sk
    _table.put_item(Item=item)


def mark_transaction_disputed(customer_id, txn_sk, dispute_ref):
    _table.update_item(
        Key={"pk": f"CUSTOMER#{customer_id}", "sk": txn_sk},
        UpdateExpression="SET dispute_ref = :r, dispute_raised_at = :t",
        ExpressionAttributeValues={":r": dispute_ref, ":t": int(time.time())},
    )


# ── session trace (server-side source of truth for the operator view) ──
TRACE_TTL_SECONDS = 60 * 60 * 24 * 7


def record_evidence(session_id, request_id, tool_name, evidence):
    """
    Persist the Evidence_Set the retrieval tool actually returned.

    The operator view reads from here rather than from anything the browser
    assembled, so what a reviewer sees is what the server produced.
    """
    if not evidence:
        return
    now = int(time.time())
    _table.put_item(Item=_safe({
        "pk": f"TRACE#{session_id}",
        "sk": f"EV#{now:010d}#{request_id[-8:]}",
        "session_id": session_id,
        "request_id": request_id,
        "tool": tool_name,
        "ts": now,
        "evidence": [
            {k: e.get(k) for k in (
                "citation_id", "document_id", "title", "version", "effective_date",
                "section_ref", "access_classification", "geography", "superseded",
                "score", "text")}
            for e in evidence
        ],
        "ttl": now + TRACE_TTL_SECONDS,
    }))


def get_evidence_trace(session_id, limit=40):
    resp = _table.query(
        KeyConditionExpression=Key("pk").eq(f"TRACE#{session_id}")
        & Key("sk").begins_with("EV#"),
        ScanIndexForward=False,
        Limit=limit,
    )
    return resp.get("Items") or []


def sessions_for_customer(customer_id, limit=15):
    """Recent sessions belonging to one customer, newest first."""
    resp = _table.scan(
        FilterExpression="begins_with(pk, :p) AND sk = :s AND customer_id = :c",
        ExpressionAttributeValues={":p": "SESSION#", ":s": "META", ":c": customer_id},
    )
    items = resp.get("Items") or []
    items.sort(key=lambda i: int(i.get("created_at", 0)), reverse=True)
    return items[:limit]
