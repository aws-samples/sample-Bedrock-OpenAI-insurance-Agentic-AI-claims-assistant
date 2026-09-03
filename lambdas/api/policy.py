"""
Policy_Decision_Point — the single authority for authorization.

Deny-by-default. Inputs are the Session_Record and the deployed policy file only.
Model output and client-supplied claims are never inputs.
"""
import json
import os
import time
from pathlib import Path

_POLICY = json.loads((Path(__file__).parent / "authorization_policy.json").read_text())

VERSION = _POLICY["policy_version"]


class Decision:
    def __init__(self, allowed, reason, required_assurance=None, required_entitlement=None):
        self.allowed = allowed
        self.reason = reason
        self.required_assurance = required_assurance
        self.required_entitlement = required_entitlement

    def as_dict(self):
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "required_assurance": self.required_assurance,
            "required_entitlement": self.required_entitlement,
            "policy_version": VERSION,
        }


def _rank(level):
    return _POLICY["assurance_rank"].get(level or "", 0)


def effective_assurance(session):
    """`verified` decays after the configured validity window."""
    level = session.get("assurance_level", "authenticated")
    if level != "verified":
        return level
    expires = int(session.get("assurance_expires_at", 0) or 0)
    if expires and expires < int(time.time()):
        return "authenticated"
    return "verified"


def rule_for(tool):
    return _POLICY["rules"].get(tool)


def is_state_changing(tool):
    rule = rule_for(tool)
    return bool(rule and rule.get("state_changing"))


def requires_idempotency(tool):
    rule = rule_for(tool)
    return bool(rule and rule.get("idempotency_required"))


def evaluate(tool, args, session, customer):
    """Return a Decision. Anything not explicitly granted is denied."""
    rule = rule_for(tool)
    if rule is None:
        return Decision(False, f"No policy rule grants '{tool}'.")

    assurance = effective_assurance(session)
    entitlements = set(customer.get("entitlements") or [])

    # The action itself may demand a stronger entitlement than the tool.
    needed_entitlement = rule.get("required_entitlement")
    action_map = rule.get("action_entitlements") or {}
    request_type = (args or {}).get("request_type")
    if request_type and request_type in action_map:
        needed_entitlement = action_map[request_type]

    if needed_entitlement and needed_entitlement not in entitlements:
        return Decision(
            False,
            rule.get("reason_if_denied") or "You are not permitted to do this on this account.",
            required_assurance=rule.get("required_assurance"),
            required_entitlement=needed_entitlement,
        )

    required_assurance = rule.get("required_assurance")
    if request_type in _POLICY.get("high_risk_request_types", []):
        required_assurance = "verified"

    if _rank(assurance) < _rank(required_assurance):
        return Decision(
            False,
            f"This needs a {required_assurance} session. Identity verification is required first.",
            required_assurance=required_assurance,
            required_entitlement=needed_entitlement,
        )

    return Decision(True, "Permitted by policy.", required_assurance, needed_entitlement)


def verified_validity_seconds():
    return int(_POLICY.get("verified_validity_seconds", 600))


def scope_constrained(tool):
    rule = rule_for(tool)
    return bool(rule and rule.get("scope_constrained"))
