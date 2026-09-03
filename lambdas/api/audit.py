"""
Audit_Chain — append-only, hash-chained record of what the system did.

One chain per session. Each entry digests its predecessor, so removing or
editing an entry is detectable. The Lambda role is granted PutItem and Query
only, never UpdateItem or DeleteItem.
"""
import hashlib
import json
import os
import time
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

_ddb = boto3.resource("dynamodb")
_table = _ddb.Table(os.environ["TABLE_AUDIT"])

GENESIS = "0" * 64


class AuditWriteError(RuntimeError):
    """Raised when the chain cannot be extended. State changes must fail closed."""


def _plain(value):
    """DynamoDB returns Decimal. Normalise so a digest computed on write matches one
    computed on read."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    return value


def _canonical(entry):
    return json.dumps(_plain(entry), sort_keys=True, separators=(",", ":"))


def _last(session_id):
    resp = _table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("session_id").eq(session_id),
        ScanIndexForward=False,
        Limit=1,
    )
    items = resp.get("Items") or []
    if not items:
        return 0, GENESIS
    last = items[0]
    return int(last["seq"]), last["digest"]


def append(session_id, *, request_id, customer_id, action, decision, reason,
           policy_version=None, result_status=None, extra=None):
    """Append one entry. Retries once on a sequence collision."""
    for _ in range(3):
        seq, prev = _last(session_id)
        entry = {
            "session_id": session_id,
            "seq": seq + 1,
            "prev_digest": prev,
            "ts": int(time.time()),
            "request_id": request_id,
            "customer_id": customer_id,
            "action": action,
            "decision": decision,
            "reason": reason,
            "policy_version": policy_version,
            "result_status": result_status,
        }
        if extra:
            entry.update(extra)
        entry["digest"] = hashlib.sha256(_canonical(entry).encode()).hexdigest()
        try:
            _table.put_item(
                Item=entry,
                ConditionExpression="attribute_not_exists(session_id) AND attribute_not_exists(seq)",
            )
            return entry
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise AuditWriteError(str(exc)) from exc
    raise AuditWriteError("could not extend audit chain")


def verify(session_id):
    """Recompute the chain and report the first break, if any."""
    resp = _table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("session_id").eq(session_id),
        ScanIndexForward=True,
    )
    prev = GENESIS
    expected_seq = 1
    for item in resp.get("Items") or []:
        entry = {k: v for k, v in item.items() if k != "digest"}
        if int(item["seq"]) != expected_seq:
            return {"ok": False, "break_at": int(item["seq"]), "why": "sequence gap"}
        if item["prev_digest"] != prev:
            return {"ok": False, "break_at": int(item["seq"]), "why": "prev_digest mismatch"}
        recomputed = hashlib.sha256(_canonical(entry).encode()).hexdigest()
        if recomputed != item["digest"]:
            return {"ok": False, "break_at": int(item["seq"]), "why": "digest mismatch"}
        prev = item["digest"]
        expected_seq += 1
    return {"ok": True, "entries": expected_seq - 1}


def session_context(session_id, max_entries=200):
    """
    Rebuild what happened in this session, for a human handoff.

    The audit chain already records cited_documents, decisions and outcomes per
    tool call, so it is the authoritative source — no extra state to maintain.
    """
    resp = _table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("session_id").eq(session_id),
        ScanIndexForward=True,
        Limit=max_entries,
    )
    docs, history, denials = [], [], []
    for item in resp.get("Items") or []:
        action = item.get("action", "")
        for d in item.get("cited_documents") or []:
            if d and d not in docs:
                docs.append(d)
        if action.startswith("tools/"):
            history.append({
                "tool": action.split("/", 1)[1],
                "decision": item.get("decision"),
                "status": item.get("result_status"),
                "reason": item.get("reason"),
                "ts": int(item.get("ts", 0)),
            })
        if item.get("decision") == "deny":
            denials.append({
                "action": action,
                "reason": item.get("reason"),
                "ts": int(item.get("ts", 0)),
            })
    return {
        "documents_cited": docs,
        "tool_history": history,
        "denials": denials,
        "audit_entries": len(resp.get("Items") or []),
    }
