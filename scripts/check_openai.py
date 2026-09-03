#!/usr/bin/env python3
"""
Confirm the OpenAI key can actually drive this deployment.

Makes the same two calls the Lambda makes:
  1. GET  /v1/models/gpt-realtime-2.1   is the model visible to this key
  2. POST /v1/realtime/client_secrets   can we mint an ephemeral credential

Key source: $OPENAI_API_KEY if set, otherwise the deployed secret.

Usage:
  python3 scripts/check_openai.py
  OPENAI_API_KEY=sk-... python3 scripts/check_openai.py
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

import certifi

MODEL = "gpt-realtime-2.1"
SECRET_ID = "enterprise-advisor/openai" # nosec B105 - Secrets Manager resource name, not a password
BASE = "https://api.openai.com/v1"
CTX = ssl.create_default_context(cafile=certifi.where())


def get_key():
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key.strip(), "environment"
    import boto3

    raw = boto3.client("secretsmanager", region_name="us-east-1").get_secret_value(
        SecretId=SECRET_ID
    )["SecretString"]
    try:
        return json.loads(raw).get("api_key", raw).strip(), "Secrets Manager"
    except json.JSONDecodeError:
        return raw.strip(), "Secrets Manager"


def call(method, path, key, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {key}"}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:  # nosec B310 - literal https BASE constant  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:300]}


def explain(status, payload):
    err = (payload.get("error") or {})
    msg = err.get("message") or payload.get("raw") or ""
    hints = {
        401: "Key is invalid, revoked, or malformed. If it came from Secrets Manager, "
             "the placeholder is still in place — run the put-secret-value command.",
        403: "Key is valid but not permitted this model. Check the project's model "
             "allowlist, or data-residency restrictions on the org.",
        404: "Model not visible to this key. Usually a project-scoped key without "
             f"{MODEL} allowed.",
        429: "Rate limited or no credit. Free-trial keys cannot use Realtime — the "
             "org needs a positive balance.",
    }
    return msg, hints.get(status, "")


def main():
    try:
        key, source = get_key()
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"could not read a key: {exc}")

    # No part of the key is printed — see set_openai_key.py for the reasoning.
    # Length plus the prefix check diagnoses the realistic failure (a truncated
    # paste, or the CDK placeholder still sitting in the secret).
    print(f"key source: {source}")
    print(f"key shape : {len(key)} chars, expected prefix present: "
          f"{key.startswith('sk-')}")
    if not key.startswith("sk-"):
        print("\n⚠️  This does not look like an OpenAI key. The CDK-generated "
              "placeholder is probably still in the secret.")

    print(f"\n[1] GET /models/{MODEL}")
    status, payload = call("GET", f"/models/{MODEL}", key)
    if status == 200:
        print(f"    OK — model visible, owned by {payload.get('owned_by', 'openai')}")
    else:
        msg, hint = explain(status, payload)
        print(f"    HTTP {status} — {msg}")
        if hint:
            print(f"    → {hint}")

    print("\n[2] POST /realtime/client_secrets")
    status, payload = call("POST", "/realtime/client_secrets", key, {
        "expires_after": {"anchor": "created_at", "seconds": 600},
        "session": {"type": "realtime", "model": MODEL,
                    "instructions": "connectivity check"},
    })
    if status in (200, 201):
        # The ephemeral token is deliberately not printed, not even truncated.
        print(f"    OK — ephemeral token minted, expires_at={payload.get('expires_at')}")
        print("\n✅ This key can drive the deployment.")
        return 0

    msg, hint = explain(status, payload)
    print(f"    HTTP {status} — {msg}")
    if hint:
        print(f"    → {hint}")
    print("\n❌ Voice sessions will fail until this call succeeds.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
