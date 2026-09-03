#!/usr/bin/env python3
"""Create the fixture Cognito users, one per fixture customer. Idempotent."""
import os
import json
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = os.environ.get("DEMO_PASSWORD")
if not PASSWORD:
    sys.exit("Set DEMO_PASSWORD first — see README, Deploy step 3.")

cognito = boto3.client("cognito-idp", region_name="us-east-1")


def main():
    out = json.loads((ROOT / ".deploy" / "outputs.json").read_text())["EnterpriseAdvisor"]
    pool = out["UserPoolId"]
    customers = json.loads((ROOT / "data" / "customers.json").read_text())

    print(f"User pool {pool}\n")
    for c in customers:
        uid = c["customer_id"]
        try:
            cognito.admin_create_user(
                UserPoolId=pool,
                Username=uid,
                UserAttributes=[
                    {"Name": "email", "Value": c["email"]},
                    {"Name": "email_verified", "Value": "true"},
                ],
                MessageAction="SUPPRESS",
            )
            action = "created"
        except ClientError as e:
            if e.response["Error"]["Code"] != "UsernameExistsException":
                raise
            action = "exists"

        cognito.admin_set_user_password(
            UserPoolId=pool, Username=uid, Password=PASSWORD, Permanent=True
        )
        print(f"  {uid:10s} {action:8s} {c['name']:14s} {c['geography']:3s} "
              f"step-up code {c['step_up_code']}")

    # The value is intentionally not echoed — it would land in shell history,
    # CI logs and terminal recordings. Name where it came from instead.
    print("\nAll fixture users share the password from the DEMO_PASSWORD "
          "environment variable.")


if __name__ == "__main__":
    main()
