#!/usr/bin/env python3
"""
Inventory every AWS resource provisioned for BFSI Assistant.

Covers both halves: the CloudFormation stack (CDK) and the resources created
outside it by scripts/setup.py, because CloudFormation does not yet support the
S3 Vectors storage type for Bedrock knowledge bases.

Also checks the `auto-delete: no` tag the account janitor requires.
"""
import json
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
STACK = "EnterpriseAdvisor"
ROOT = Path(__file__).resolve().parent.parent

cfn = boto3.client("cloudformation", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
s3v = boto3.client("s3vectors", region_name=REGION)
ddb = boto3.client("dynamodb", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
agent = boto3.client("bedrock-agent", region_name=REGION)
cognito = boto3.client("cognito-idp", region_name=REGION)
iam = boto3.client("iam")
sm = boto3.client("secretsmanager", region_name=REGION)
sts = boto3.client("sts", region_name=REGION)

ACCOUNT = sts.get_caller_identity()["Account"]
BILLABLE_IDLE = set()


def head(t):
    print(f"\n{'─' * 74}\n{t}\n{'─' * 74}")


def outputs():
    o = cfn.describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]
    return {x["OutputKey"]: x["OutputValue"] for x in o}


def stack_resources():
    head("1. CloudFormation stack  ·  provisioned by AWS CDK")
    st = cfn.describe_stacks(StackName=STACK)["Stacks"][0]
    print(f"   stack     {STACK}")
    print(f"   status    {st['StackStatus']}")
    print(f"   created   {st['CreationTime']:%Y-%m-%d %H:%M}")

    pages = cfn.get_paginator("list_stack_resources").paginate(StackName=STACK)
    rows = []
    for page in pages:
        for r in page["StackResourceSummaries"]:
            rows.append((r["ResourceType"], r.get("PhysicalResourceId", "")[:58],
                         r["ResourceStatus"]))
    rows.sort()
    print(f"\n   {len(rows)} resources\n")
    print(f"   {'TYPE':44s} {'PHYSICAL ID':58s}")
    for t, pid, status in rows:
        flag = "" if status.endswith("COMPLETE") else f"  ⚠️ {status}"
        print(f"   {t:44s} {pid:58s}{flag}")
    return rows


def outside_stack(out):
    head("2. Created outside CloudFormation  ·  provisioned by scripts/setup.py")
    print("   CloudFormation has no S3 Vectors storage type for Bedrock knowledge")
    print("   bases yet, so these four are created with boto3.\n")

    vb = f"enterprise-advisor-vectors-{ACCOUNT}"
    try:
        b = s3v.get_vector_bucket(vectorBucketName=vb)["vectorBucket"]
        print(f"   S3 vector bucket      {vb}")
        print(f"                         created {b['creationTime']:%Y-%m-%d %H:%M}")
    except ClientError as e:
        print(f"   S3 vector bucket      MISSING ({e.response['Error']['Code']})")

    try:
        ix = s3v.get_index(vectorBucketName=vb, indexName="ea-policy-index")["index"]
        print(f"   S3 vector index       ea-policy-index")
        print(f"                         dim={ix['dimension']} metric={ix['distanceMetric']} "
              f"type={ix['dataType']}")
    except ClientError as e:
        print(f"   S3 vector index       MISSING ({e.response['Error']['Code']})")

    kb_id = None
    for kb in agent.list_knowledge_bases(maxResults=100).get("knowledgeBaseSummaries", []):
        if kb["name"] == "enterprise-advisor-policy":
            kb_id = kb["knowledgeBaseId"]
    if kb_id:
        d = agent.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
        emb = d["knowledgeBaseConfiguration"]["vectorKnowledgeBaseConfiguration"]["embeddingModelArn"]
        print(f"   Bedrock knowledge base {kb_id}  ({d['status']})")
        print(f"                         embedding {emb.split('/')[-1]}")
        print(f"                         storage   {d['storageConfiguration']['type']}")
        for ds in agent.list_data_sources(knowledgeBaseId=kb_id,
                                          maxResults=10).get("dataSourceSummaries", []):
            print(f"   Bedrock data source   {ds['dataSourceId']}  ({ds['status']})")
            jobs = agent.list_ingestion_jobs(knowledgeBaseId=kb_id,
                                             dataSourceId=ds["dataSourceId"],
                                             maxResults=1).get("ingestionJobSummaries", [])
            if jobs:
                st = jobs[0]["statistics"]
                print(f"                         last ingest {jobs[0]['status']}  "
                      f"scanned={st.get('numberOfDocumentsScanned')} "
                      f"indexed={st.get('numberOfNewDocumentsIndexed')} "
                      f"failed={st.get('numberOfDocumentsFailed')}")
    else:
        print("   Bedrock knowledge base MISSING")

    try:
        r = iam.get_role(RoleName="EnterpriseAdvisorKnowledgeBaseRole")["Role"]
        pols = iam.list_role_policies(RoleName=r["RoleName"])["PolicyNames"]
        print(f"   IAM role              {r['RoleName']}")
        print(f"                         inline policies: {', '.join(pols)}")
    except ClientError:
        print("   IAM role              MISSING")
    return kb_id


def contents(out):
    head("3. What is inside the stores")

    docs = out["DocumentsBucket"]
    objs = s3.list_objects_v2(Bucket=docs, Prefix="documents/").get("Contents", [])
    txt = [o for o in objs if o["Key"].endswith(".txt")]
    meta = [o for o in objs if o["Key"].endswith(".metadata.json")]
    print(f"   S3 documents          {len(txt)} documents + {len(meta)} metadata sidecars")

    corpus = json.loads((ROOT / "data" / "corpus.json").read_text())
    by_class = {}
    for d in corpus:
        by_class.setdefault(d["access_classification"], []).append(d["document_id"])
    for k in ("public", "customer", "internal", "restricted"):
        ids = by_class.get(k, [])
        elig = "eligible" if k in ("public", "customer") else "NEVER eligible"
        print(f"       {k:11s} {len(ids)}  ({elig})")

    for label, key in (("DynamoDB data", "DataTable"), ("DynamoDB audit", "AuditTable")):
        t = ddb.describe_table(TableName=out[key])["Table"]
        print(f"   {label:21s} {t['TableName']}")
        print(f"                         items≈{t['ItemCount']}  billing="
              f"{t['BillingModeSummary']['BillingMode']}  status={t['TableStatus']}")

    users = cognito.list_users(UserPoolId=out["UserPoolId"], Limit=20)["Users"]
    print(f"   Cognito users         {len(users)}: "
          f"{', '.join(sorted(u['Username'] for u in users))}")

    v = sm.describe_secret(SecretId="enterprise-advisor/openai")
    print(f"   Secret versions       {len(v['VersionIdsToStages'])} "
          f"(last changed {v['LastChangedDate']:%Y-%m-%d %H:%M})")


def tags(out):
    head("4. Janitor tag check  ·  auto-delete: no")
    ok, missing, skipped = [], [], []

    def check(label, fetch):
        try:
            t = fetch()
            (ok if t.get("auto-delete") == "no" else missing).append(label)
        except Exception as e:  # noqa: BLE001
            skipped.append(f"{label} ({type(e).__name__})")

    fn = out["ApiFunctionName"]
    check(f"Lambda {fn[:34]}", lambda: lam.list_tags(
        Resource=lam.get_function(FunctionName=fn)["Configuration"]["FunctionArn"])["Tags"])
    for key in ("DataTable", "AuditTable"):
        arn = ddb.describe_table(TableName=out[key])["Table"]["TableArn"]
        check(f"DynamoDB {out[key][:30]}",
              lambda a=arn: {x["Key"]: x["Value"]
                             for x in ddb.list_tags_of_resource(ResourceArn=a)["Tags"]})
    for key in ("DocumentsBucket", "SiteBucket"):
        check(f"S3 {out[key][:34]}",
              lambda b=out[key]: {x["Key"]: x["Value"]
                                  for x in s3.get_bucket_tagging(Bucket=b)["TagSet"]})
    check("Secret enterprise-advisor/openai",
          lambda: {x["Key"]: x["Value"]
                   for x in sm.describe_secret(SecretId="enterprise-advisor/openai")["Tags"]})
    check("IAM role KnowledgeBaseRole",
          lambda: {x["Key"]: x["Value"] for x in iam.list_role_tags(
              RoleName="EnterpriseAdvisorKnowledgeBaseRole")["Tags"]})

    for x in ok:
        print(f"   tagged      {x}")
    for x in missing:
        print(f"   ⚠️ MISSING  {x}")
    for x in skipped:
        print(f"   no tag api  {x}")
    return missing


def cost(out):
    head("5. Cost shape")
    print("   Idle cost is effectively zero. Nothing here is provisioned by the hour.\n")
    for line in [
        ("Lambda",              "per request, 512 MB, ~1 s per tool call"),
        ("API Gateway HTTP API", "per request"),
        ("DynamoDB",            "on-demand, both tables"),
        ("S3 + S3 Vectors",     "storage only, 12 documents"),
        ("Bedrock KB",          "per embedding at ingest, per Retrieve at query"),
        ("Cognito",             "free below the monthly active user tier"),
        ("CloudFront",          "per request, no minimum"),
        ("Secrets Manager",     "~$0.40 per month, the one fixed line"),
        ("KMS",                 "AWS-managed keys, no charge"),
        ("OpenAI",              "billed by OpenAI, not AWS, per audio minute"),
    ]:
        print(f"   {line[0]:22s} {line[1]}")
    print("\n   No NAT gateway, no load balancer, no container, no OpenSearch cluster,")
    print("   no provisioned capacity. That was the point of the WebRTC decision.")


def main():
    out = outputs()
    print(f"BFSI Assistant — provisioned inventory")
    print(f"account {ACCOUNT}  ·  region {REGION}")
    stack_resources()
    outside_stack(out)
    contents(out)
    missing = tags(out)
    cost(out)

    head("Endpoints")
    for k in ("SiteUrl", "ApiEndpoint", "UserPoolId", "UserPoolClientId"):
        print(f"   {k:18s} {out[k]}")

    print(f"\n{'─' * 74}")
    print("teardown:  cd infra && npx -y aws-cdk@2.1136.0 destroy --app '../.venv/bin/python app.py'")
    print("           then delete the vector bucket, knowledge base, and IAM role from setup.py")
    print("note:      the audit table has RemovalPolicy.RETAIN and survives a stack destroy")
    if missing:
        print(f"\n⚠️  {len(missing)} resource(s) missing the auto-delete tag")


if __name__ == "__main__":
    main()
