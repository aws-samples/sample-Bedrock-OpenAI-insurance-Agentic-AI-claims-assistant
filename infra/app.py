#!/usr/bin/env python3
"""CDK app for the BFSI Assistant prototype."""
import os

import aws_cdk as cdk

from stack import EnterpriseAdvisorStack

app = cdk.App()

EnterpriseAdvisorStack(
    app,
    "EnterpriseAdvisor",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
    description="BFSI Assistant — customer-facing grounded AI advisor (prototype)",
)

# Every taggable resource opts out of the account janitor.
cdk.Tags.of(app).add("auto-delete", "no")
cdk.Tags.of(app).add("Project", "EnterpriseAdvisor")

app.synth()
