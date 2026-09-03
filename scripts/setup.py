#!/usr/bin/env python3
"""
One-shot setup after `cdk deploy`.

1. Seeds fixture customers into DynamoDB.
2. Uploads the document corpus plus Bedrock metadata sidecars to S3.
3. Creates the S3 vector bucket and index.
4. Creates the Bedrock knowledge base, its service role, and its S3 data source.
5. Runs the ingestion job and records the knowledge base id for the Lambda.

Idempotent: safe to re-run.
"""
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
REGION = "us-east-1"
EMBED_MODEL = "amazon.titan-embed-text-v2:0"
EMBED_DIM = 1024
INDEX_NAME = "ea-policy-index"
KB_NAME = "enterprise-advisor-policy"
ROLE_NAME = "EnterpriseAdvisorKnowledgeBaseRole"

sts = boto3.client("sts", region_name=REGION)
ACCOUNT = sts.get_caller_identity()["Account"]
VECTOR_BUCKET = f"enterprise-advisor-vectors-{ACCOUNT}"

s3 = boto3.client("s3", region_name=REGION)
s3v = boto3.client("s3vectors", region_name=REGION)
iam = boto3.client("iam")
agent = boto3.client("bedrock-agent", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION)


def outputs():
    path = ROOT / ".deploy" / "outputs.json"
    if not path.exists():
        sys.exit("Missing .deploy/outputs.json — run cdk deploy first.")
    return json.loads(path.read_text())["EnterpriseAdvisor"]


def step(msg):
    print(f"\n▸ {msg}")


# ── 1. customers ──────────────────────────────────────────────────────
def seed_customers(table_name):
    step("Seeding fixture customers")
    table = ddb.Table(table_name)
    customers = json.loads((ROOT / "data" / "customers.json").read_text())
    for c in customers:
        item = dict(c)
        item["pk"] = f"CUSTOMER#{c['customer_id']}"
        item["sk"] = "PROFILE"
        table.put_item(Item=item)
        print(f"   {c['customer_id']:10s} {c['name']:14s} {c['geography']:3s} "
              f"entitlements={len(c['entitlements'])} eligible={c['eligible_classifications']}")
    return customers


# ── 2. documents ──────────────────────────────────────────────────────
def seed_holdings(table_name):
    """Accounts, loans, policies, transactions and the hospital network."""
    step("Seeding holdings, transactions and reference data")
    table = ddb.Table(table_name)
    h = json.loads((ROOT / "data" / "holdings.json").read_text())

    def put(customer_id, sk, payload):
        item = json.loads(json.dumps(payload), parse_float=Decimal)
        item["pk"] = f"CUSTOMER#{customer_id}"
        item["sk"] = sk
        table.put_item(Item=item)

    counts = {}
    for a in h["accounts"]:
        put(a["customer_id"], f"ACCT#{a['account_id']}", a)
        counts["accounts"] = counts.get("accounts", 0) + 1
    for l in h["loans"]:
        put(l["customer_id"], f"LOAN#{l['loan_id']}", l)
        counts["loans"] = counts.get("loans", 0) + 1
    for p in h["policies"]:
        put(p["customer_id"], f"POLICY#{p['policy_id']}", p)
        counts["policies"] = counts.get("policies", 0) + 1
    for t in h["transactions"]:
        put(t["customer_id"], f"TXN#{t['date']}#{t['txn_id']}", t)
        counts["transactions"] = counts.get("transactions", 0) + 1

    table.put_item(Item={"pk": "REF", "sk": "HOSPITALS",
                         "value": json.loads(json.dumps(h["network_hospitals"]))})
    counts["network_hospitals"] = len(h["network_hospitals"])
    for k, v in counts.items():
        print(f"   {k:20s} {v}")


def upload_corpus(bucket):
    step(f"Uploading corpus to s3://{bucket}/documents/")
    corpus = json.loads((ROOT / "data" / "corpus.json").read_text())
    for doc in corpus:
        stem = f"{doc['document_id']}--v{doc['version'].replace('.', '_')}"
        key = f"documents/{stem}.txt"
        text = (
            f"{doc['title']} (version {doc['version']}, effective {doc['effective_date']})\n"
            f"{doc['section_ref']}\n\n{doc['body']}"
        )
        s3.put_object(Bucket=bucket, Key=key, Body=text.encode(),
                      ContentType="text/plain")
        meta = {
            "metadataAttributes": {
                "document_id": doc["document_id"],
                "title": doc["title"],
                "version": doc["version"],
                "effective_date": doc["effective_date"],
                "business_domain": doc["business_domain"],
                "geography": doc["geography"],
                "access_classification": doc["access_classification"],
                "superseded": bool(doc["superseded"]),
                "section_ref": doc["section_ref"],
            }
        }
        s3.put_object(Bucket=bucket, Key=f"{key}.metadata.json",
                      Body=json.dumps(meta).encode(),
                      ContentType="application/json")
        flag = "  (not customer-eligible)" if doc["access_classification"] in ("internal", "restricted") else ""
        sup = "  SUPERSEDED" if doc["superseded"] else ""
        print(f"   {doc['access_classification']:10s} {doc['document_id']:26s} v{doc['version']}{sup}{flag}")
    return corpus


# ── 3. vector store ───────────────────────────────────────────────────
def ensure_vector_index():
    step(f"Vector bucket {VECTOR_BUCKET} / index {INDEX_NAME}")
    try:
        s3v.create_vector_bucket(vectorBucketName=VECTOR_BUCKET)
        print("   bucket created")
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("ConflictException", "BucketAlreadyExists"):
            raise
        print("   bucket exists")

    try:
        s3v.create_index(
            vectorBucketName=VECTOR_BUCKET,
            indexName=INDEX_NAME,
            dataType="float32",
            dimension=EMBED_DIM,
            distanceMetric="cosine",
            metadataConfiguration={
                "nonFilterableMetadataKeys": ["AMAZON_BEDROCK_METADATA", "AMAZON_BEDROCK_TEXT"]
            },
        )
        print("   index created")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConflictException":
            raise
        print("   index exists")

    return s3v.get_index(vectorBucketName=VECTOR_BUCKET, indexName=INDEX_NAME)["index"]["indexArn"]


# ── 4. knowledge base ─────────────────────────────────────────────────
def ensure_kb_role(docs_bucket, index_arn):
    step(f"Knowledge base service role {ROLE_NAME}")
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT}},
        }],
    }
    try:
        role = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Bedrock Knowledge Base role for BFSI Assistant",
            Tags=[{"Key": "auto-delete", "Value": "no"},
                  {"Key": "Project", "Value": "EnterpriseAdvisor"}],
        )["Role"]
        print("   role created")
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        role = iam.get_role(RoleName=ROLE_NAME)["Role"]
        iam.update_assume_role_policy(RoleName=ROLE_NAME,
                                      PolicyDocument=json.dumps(trust))
        print("   role exists")

    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["bedrock:InvokeModel"],
             "Resource": [f"arn:aws:bedrock:{REGION}::foundation-model/{EMBED_MODEL}"]},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"],
             "Resource": [f"arn:aws:s3:::{docs_bucket}", f"arn:aws:s3:::{docs_bucket}/*"],
             "Condition": {"StringEquals": {"aws:ResourceAccount": ACCOUNT}}},
            {"Effect": "Allow",
             "Action": ["s3vectors:PutVectors", "s3vectors:GetVectors",
                        "s3vectors:DeleteVectors", "s3vectors:QueryVectors",
                        "s3vectors:GetIndex", "s3vectors:ListVectors"],
             "Resource": [index_arn]},
        ],
    }
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="KnowledgeBaseAccess",
                        PolicyDocument=json.dumps(policy))
    print("   inline policy applied")
    return role["Arn"]


def ensure_kb(role_arn, index_arn):
    step(f"Knowledge base {KB_NAME}")
    for kb in agent.list_knowledge_bases(maxResults=100).get("knowledgeBaseSummaries", []):
        if kb["name"] == KB_NAME:
            print(f"   exists: {kb['knowledgeBaseId']}")
            return kb["knowledgeBaseId"]

    # The role needs a moment to become assumable by Bedrock.
    for attempt in range(10):
        try:
            resp = agent.create_knowledge_base(
                name=KB_NAME,
                description="Published enterprise policy for the BFSI Assistant prototype",
                roleArn=role_arn,
                knowledgeBaseConfiguration={
                    "type": "VECTOR",
                    "vectorKnowledgeBaseConfiguration": {
                        "embeddingModelArn":
                            f"arn:aws:bedrock:{REGION}::foundation-model/{EMBED_MODEL}"
                    },
                },
                storageConfiguration={
                    "type": "S3_VECTORS",
                    "s3VectorsConfiguration": {"indexArn": index_arn},
                },
                tags={"auto-delete": "no", "Project": "EnterpriseAdvisor"},
            )
            kb_id = resp["knowledgeBase"]["knowledgeBaseId"]
            print(f"   created: {kb_id}")
            return kb_id
        except ClientError as e:
            if e.response["Error"]["Code"] in ("ValidationException", "AccessDeniedException") and attempt < 9:
                print(f"   waiting for role propagation ({attempt + 1}/10)")
                time.sleep(6)
                continue
            raise


def ensure_data_source(kb_id, docs_bucket):
    step("S3 data source")
    for ds in agent.list_data_sources(knowledgeBaseId=kb_id, maxResults=100).get("dataSourceSummaries", []):
        print(f"   exists: {ds['dataSourceId']}")
        return ds["dataSourceId"]
    resp = agent.create_data_source(
        knowledgeBaseId=kb_id,
        name="policy-documents",
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": {
                "bucketArn": f"arn:aws:s3:::{docs_bucket}",
                "inclusionPrefixes": ["documents/"],
            },
        },
        vectorIngestionConfiguration={
            "chunkingConfiguration": {
                "chunkingStrategy": "FIXED_SIZE",
                "fixedSizeChunkingConfiguration": {"maxTokens": 300, "overlapPercentage": 20},
            }
        },
    )
    ds_id = resp["dataSource"]["dataSourceId"]
    print(f"   created: {ds_id}")
    return ds_id


def ingest(kb_id, ds_id):
    step("Ingestion job")
    job = agent.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)
    job_id = job["ingestionJob"]["ingestionJobId"]
    while True:
        j = agent.get_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id,
                                    ingestionJobId=job_id)["ingestionJob"]
        status = j["status"]
        if status in ("COMPLETE", "FAILED", "STOPPED"):
            stats = j.get("statistics", {})
            print(f"   {status}  scanned={stats.get('numberOfDocumentsScanned')} "
                  f"indexed={stats.get('numberOfNewDocumentsIndexed')} "
                  f"failed={stats.get('numberOfDocumentsFailed')}")
            if status != "COMPLETE":
                print(json.dumps(j.get("failureReasons", []), indent=2))
            return status
        print(f"   {status} ...")
        time.sleep(10)


def main():
    out = outputs()
    print(f"Account {ACCOUNT} / {REGION}")

    seed_customers(out["DataTable"])
    seed_holdings(out["DataTable"])
    upload_corpus(out["DocumentsBucket"])
    index_arn = ensure_vector_index()
    role_arn = ensure_kb_role(out["DocumentsBucket"], index_arn)
    kb_id = ensure_kb(role_arn, index_arn)
    ds_id = ensure_data_source(kb_id, out["DocumentsBucket"])

    step("Recording knowledge base id for the API")
    ddb.Table(out["DataTable"]).put_item(Item={"pk": "CONFIG", "sk": "KB", "value": kb_id})
    print(f"   CONFIG#KB = {kb_id}")

    status = ingest(kb_id, ds_id)
    print(f"\n{'✅' if status == 'COMPLETE' else '⚠️'} setup finished (ingestion {status})")
    print(f"   knowledge base: {kb_id}")
    print(f"   site:           {out['SiteUrl']}")


if __name__ == "__main__":
    main()
