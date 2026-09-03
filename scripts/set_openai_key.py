#!/usr/bin/env python3
"""
Store the OpenAI API key in Secrets Manager.

The key is read from a hidden prompt, so it never appears in the terminal, in
shell history, or in the process list. It is validated against OpenAI before
being stored, and is never written to disk or logged.

Run:
    python3 scripts/set_openai_key.py
"""
import getpass
import json
import ssl
import sys
import urllib.error
import urllib.request

import boto3
import certifi

SECRET_ID = "enterprise-advisor/openai" # nosec B105 - Secrets Manager resource name, not a password
MODEL = "gpt-realtime-2.1"
REGION = "us-east-1"
CTX = ssl.create_default_context(cafile=certifi.where())


def mint_test(key):
    """Make the exact call the Session_Broker makes."""
    req = urllib.request.Request(
        "https://api.openai.com/v1/realtime/client_secrets",
        data=json.dumps({
            "expires_after": {"anchor": "created_at", "seconds": 600},
            "session": {"type": "realtime", "model": MODEL,
                        "instructions": "connectivity check"},
        }).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:  # nosec B310 - literal https endpoint  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:300]}


HINTS = {
    401: "Key is invalid or revoked.",
    403: "Key is valid but blocked from this model — check the project's model "
         "allowlist or org data-residency settings.",
    404: f"{MODEL} is not visible to this key's project.",
    429: "Rate limited or no credit. Realtime needs a positive account balance.",
}


def main():
    print(f"Storing the OpenAI key into secret '{SECRET_ID}' ({REGION}).")
    print("Input is hidden. Nothing is written to disk.\n")

    key = getpass.getpass("OpenAI API key: ").strip()
    if not key:
        sys.exit("no key entered")

    # Never print any part of the key. A fragment is still credential material
    # and it persists in terminal scrollback, shell history and CI logs. The
    # length and the prefix check are enough to diagnose a paste error.
    print(f"\nread {len(key)} chars, expected prefix present: {key.startswith('sk-')}")
    if not key.startswith("sk-"):
        print("warning: does not start with 'sk-' — continuing anyway")

    print(f"\nvalidating against /realtime/client_secrets ...")
    status, payload = mint_test(key)
    if status not in (200, 201):
        msg = (payload.get("error") or {}).get("message") or payload.get("raw", "")
        print(f"  HTTP {status} — {msg}")
        if status in HINTS:
            print(f"  → {HINTS[status]}")
        sys.exit("\nnot stored. fix the key and re-run.")

    # Confirm the mint succeeded without echoing the ephemeral token itself.
    print(f"  OK — ephemeral token minted (expires_at={payload.get('expires_at')})")

    sm = boto3.client("secretsmanager", region_name=REGION)
    sm.put_secret_value(SecretId=SECRET_ID,
                        SecretString=json.dumps({"api_key": key}))
    del key
    print(f"\n✅ stored in {SECRET_ID}")
    print("   no redeploy needed — the Lambda reads it at runtime")
    print("\n   open the site URL from the stack outputs")
    print("   sign in as cust-1001 with your DEMO_PASSWORD")


if __name__ == "__main__":
    main()
