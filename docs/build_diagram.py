#!/usr/bin/env python3
"""
Render the BFSI Assistant architecture in AWS-icon format.

Outputs into this directory:
  EnterpriseAdvisor-Architecture.png   full architecture with trust zones
  EnterpriseAdvisor-Flow.png           numbered request flow, reference scenario

Requires: diagrams, graphviz (dot on PATH).
"""
from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Fargate, Lambda
from diagrams.aws.database import Dynamodb
from diagrams.aws.integration import SimpleNotificationServiceSns
from diagrams.aws.management import Cloudwatch
from diagrams.aws.network import APIGateway, CloudFront, ElbApplicationLoadBalancer
from diagrams.aws.security import Cognito, KeyManagementService, SecretsManager, WAF
from diagrams.aws.storage import SimpleStorageServiceS3
from diagrams.generic.blank import Blank
from diagrams.onprem.client import Client

HERE = Path(__file__).resolve().parent

NAVY = "#232F3E"
ORANGE = "#FF9900"
BLUE = "#3B48CC"
RED = "#D13212"
GREEN = "#1B660F"
GREY = "#5A6068"

GRAPH = {
    "fontname": "Helvetica",
    "bgcolor": "white",
    "pad": "0.6",
    "dpi": "192",
    "splines": "spline",
}
NODE = {"fontname": "Helvetica", "fontsize": "11"}
EDGE = {"fontname": "Helvetica", "fontsize": "10"}


def architecture() -> None:
    """Full component view, grouped by trust zone."""
    graph = dict(GRAPH, nodesep="1.9", ranksep="0.42", fontsize="26")

    with Diagram(
        "BFSI Assistant — Customer-Facing Grounded AI Advisor",
        filename=str(HERE / "EnterpriseAdvisor-Architecture"),
        outformat="png",
        show=False,
        direction="TB",
        graph_attr=graph,
        node_attr=NODE,
        edge_attr=EDGE,
    ):
        with Cluster("UNTRUSTED", graph_attr={"bgcolor": "#FDECEA", "color": RED, "penwidth": "2"}):
            browser = Client("Customer browser\naudio + text only\nno API key\nno tool execution")

        with Cluster("AWS EDGE + IDENTITY", graph_attr={"bgcolor": "#F2F4F7", "color": GREY}):
            waf = WAF("AWS WAF\nrate limit · abuse")
            cdn = CloudFront("CloudFront + S3\nstatic client")
            cognito = Cognito("Cognito\nMFA required")

        with Cluster("CONTROL PLANE — trusted, Lambda", graph_attr={"bgcolor": "#FFF6E9", "color": ORANGE, "penwidth": "2"}):
            apigw = APIGateway("API Gateway\nHTTP API")
            broker = Lambda("Session_Broker\ntoken validation\nsession binding")
            tools = Lambda("Tool_Broker\n8 MCP tools\npolicy · audit · egress")
            ingest = Lambda("Ingestion_Pipeline\nchunk · embed · index")

        with Cluster("SESSION RELAY — holds the model session", graph_attr={"bgcolor": "#EFE8FB", "color": "#6E40C9", "penwidth": "2"}):
            alb = ElbApplicationLoadBalancer("ALB\nWebSocket")
            relay = Fargate("Session_Relay\nECS Fargate")

        with Cluster("KNOWLEDGE + STATE", graph_attr={"bgcolor": "#EAF6EC", "color": GREEN, "penwidth": "2"}):
            docs = SimpleStorageServiceS3("S3\nDocument_Store")
            vectors = SimpleStorageServiceS3("S3 Vectors\neligibility filter\nINSIDE the query")
            sessions = Dynamodb("DynamoDB\nSessions")
            entdata = Dynamodb("DynamoDB\ncustomers\nrequests")
            audit = Dynamodb("DynamoDB\nAudit_Chain\nhash chained")

        with Cluster("SECURITY + OBSERVABILITY", graph_attr={"bgcolor": "#F2F4F7", "color": GREY}):
            secrets = SecretsManager("Secrets Manager\nOpenAI key")
            kms = KeyManagementService("KMS CMK\nrotation")
            logs = Cloudwatch("CloudWatch\nmetrics · alarms")
            sns = SimpleNotificationServiceSns("SNS")

        with Cluster("OUTSIDE ENTERPRISE BOUNDARY", graph_attr={"bgcolor": "#FDECEA", "color": RED, "penwidth": "2"}):
            openai = Blank("OpenAI Realtime API\ngpt-realtime-2.1\nspeech-to-speech + tools")

        # client, edge, auth
        browser >> Edge(color=GREY, label="HTTPS") >> waf
        waf >> Edge(color=GREY) >> cdn
        browser >> Edge(color=BLUE, style="dashed", label="sign in + MFA") >> cognito

        # session establishment
        waf >> Edge(color=ORANGE, label="POST /session\n(JWT)") >> apigw >> Edge(color=ORANGE) >> broker
        broker >> Edge(color=BLUE, style="dashed", label="JWKS") >> cognito
        broker >> Edge(color=GREEN, label="Session_Record") >> sessions
        broker >> Edge(color=GREY, style="dotted") >> secrets

        # audio path
        browser >> Edge(color="#6E40C9", penwidth="2.4", label="WebSocket audio") >> alb
        alb >> Edge(color="#6E40C9", penwidth="2.4") >> relay
        relay >> Edge(color=RED, penwidth="2.4", label="Realtime WebSocket") >> openai

        # tool path
        relay >> Edge(color=ORANGE, penwidth="2.4", label="tool call\n+ session_id") >> tools
        tools >> Edge(color=GREEN, label="filtered query") >> vectors
        tools >> Edge(color=GREEN) >> entdata
        tools >> Edge(color=GREEN, label="append-only") >> audit

        # ingestion
        docs >> Edge(color=GREEN, style="dashed", label="S3 event") >> ingest
        ingest >> Edge(color=GREEN, style="dashed") >> vectors

        # cross-cutting
        kms >> Edge(color=GREY, style="dotted", label="encrypt at rest") >> sessions
        tools >> Edge(color=GREY, style="dotted") >> logs
        logs >> Edge(color=GREY, style="dotted") >> sns


def flow() -> None:
    """Numbered request flow for the reference scenario."""
    graph = dict(GRAPH, nodesep="0.5", ranksep="1.3", fontsize="24")

    with Diagram(
        'Request Flow — "Close my account while travelling internationally"',
        filename=str(HERE / "EnterpriseAdvisor-Flow"),
        outformat="png",
        show=False,
        direction="LR",
        graph_attr=graph,
        node_attr=NODE,
        edge_attr=EDGE,
    ):
        browser = Client("Customer\nspeaks question")
        cognito = Cognito("Cognito\nMFA")
        broker = Lambda("Session_Broker")
        relay = Fargate("Session_Relay")
        openai = Blank("gpt-realtime-2.1")
        tools = Lambda("Tool_Broker\npolicy + egress")
        vectors = SimpleStorageServiceS3("S3 Vectors")
        entdata = Dynamodb("Enterprise data")
        audit = Dynamodb("Audit_Chain")

        browser >> Edge(color=BLUE, label="1  sign in + MFA") >> cognito
        cognito >> Edge(color=BLUE, label="2  validated JWT") >> broker
        broker >> Edge(color=ORANGE, label="3  Session_Record\nassurance = authenticated") >> relay
        browser >> Edge(color="#6E40C9", penwidth="2.2", label="4  audio") >> relay
        relay >> Edge(color=RED, penwidth="2.2", label="5  audio stream") >> openai
        openai >> Edge(color=ORANGE, label="6  search_policy") >> relay
        relay >> Edge(color=ORANGE, penwidth="2.2", label="7  broker call") >> tools
        tools >> Edge(color=GREEN, label="8  query filtered by\neligibility + geography") >> vectors
        vectors >> Edge(color=GREEN, label="9  eligible chunks only\n+ Citation_IDs") >> tools
        tools >> Edge(color=GREEN, label="10  entitlement lookup") >> entdata
        tools >> Edge(color=GREEN, label="11  decision recorded") >> audit
        tools >> Edge(color=ORANGE, label="12  evidence as DATA\nnot instruction") >> relay
        relay >> Edge(color=RED, label="13  grounded turn") >> openai
        openai >> Edge(color="#6E40C9", penwidth="2.2", label="14  spoken answer + citations\nverification_required") >> browser


if __name__ == "__main__":
    architecture()
    flow()
    for name in ("EnterpriseAdvisor-Architecture.png", "EnterpriseAdvisor-Flow.png"):
        p = HERE / name
        print(f"{'OK ' if p.exists() else 'MISSING '}{p}")
