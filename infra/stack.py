"""
BFSI Assistant infrastructure.

Seven services: Cognito, API Gateway, Lambda, DynamoDB, Secrets Manager,
S3 + CloudFront. The Bedrock knowledge base and its S3 Vectors index are
provisioned by scripts/setup_kb.py because CloudFormation does not yet cover
the S3 Vectors storage type.
"""
import os
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secrets
from constructs import Construct

ROOT = Path(__file__).resolve().parent.parent


class EnterpriseAdvisorStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs):
        super().__init__(scope, cid, **kwargs)

        # ── documents ────────────────────────────────────────────────
        docs_bucket = s3.Bucket(
            self, "Documents",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # ── state ────────────────────────────────────────────────────
        data_table = ddb.Table(
            self, "Data",
            partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            encryption=ddb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )

        audit_table = ddb.Table(
            self, "Audit",
            partition_key=ddb.Attribute(name="session_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="seq", type=ddb.AttributeType.NUMBER),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ── OpenAI credential ────────────────────────────────────────
        openai_secret = secrets.Secret(
            self, "OpenAIKey",
            secret_name="enterprise-advisor/openai", # nosec B106 - Secrets Manager resource name, not a secret value
            description="OpenAI API key used only to mint ephemeral Realtime credentials",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── identity ─────────────────────────────────────────────────
        user_pool = cognito.UserPool(
            self, "Customers",
            user_pool_name="enterprise-advisor-customers",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(username=True, email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,
        )

        user_pool_client = user_pool.add_client(
            "WebClient",
            user_pool_client_name="enterprise-advisor-web",
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            prevent_user_existence_errors=True,
        )

        # ── API function ─────────────────────────────────────────────
        api_log_group = logs.LogGroup(
            self, "ApiLogs",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── Responsible-AI guardrail ─────────────────────────────────
        # Applied on every claims-review model call (see claims.py). A defence
        # in depth alongside the deterministic 17-check gate and human decision:
        # it blocks the model from issuing financial/insurance advice as fact,
        # and denies a small set of high-risk topics, before output reaches a
        # specialist. Denied input/output is surfaced, never silently passed.
        guardrail = bedrock.CfnGuardrail(
            self, "ClaimsGuardrail",
            name=f"{cid}-claims-review",
            description="Responsible-AI guardrail for the AI claims review.",
            blocked_input_messaging=(
                "This request can\u2019t be processed by the automated review. "
                "A claims specialist will take it from here."),
            blocked_outputs_messaging=(
                "The automated review can\u2019t provide that. A claims specialist "
                "will decide and respond."),
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type=t, input_strength="HIGH", output_strength="HIGH")
                    for t in ("HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT")
                ] + [
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="PROMPT_ATTACK", input_strength="HIGH",
                        output_strength="NONE"),
                ],
            ),
            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(
                topics_config=[
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        name="DefinitiveFinancialOrInsuranceAdvice",
                        definition=(
                            "Presenting a settlement amount, payout, or coverage "
                            "determination as a final decision the customer can act "
                            "on, rather than a recommendation for specialist review."),
                        type="DENY",
                        examples=[
                            "Your claim will be settled for 3,36,250 rupees.",
                            "You are approved — we will pay the full amount.",
                            "This is definitely covered, you can proceed.",
                        ]),
                ],
            ),
            sensitive_information_policy_config=(
                bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                    pii_entities_config=[
                        bedrock.CfnGuardrail.PiiEntityConfigProperty(
                            type=t, action="ANONYMIZE")
                        for t in ("EMAIL", "PHONE", "CREDIT_DEBIT_CARD_NUMBER")
                    ],
                )
            ),
        )
        guardrail_version = bedrock.CfnGuardrailVersion(
            self, "ClaimsGuardrailVersion",
            guardrail_identifier=guardrail.attr_guardrail_id,
        )

        api_fn = lambda_.Function(
            self, "Api",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(str(ROOT / "lambdas" / "api")),
            # The claims review on Sonnet 4.5 measured ~30s, which the previous
            # 30s timeout would have cut off exactly at the finish line. Raised
            # with headroom rather than tuned to the measurement.
            timeout=Duration.seconds(120),
            memory_size=512,
            log_group=api_log_group,
            environment={
                "TABLE_DATA": data_table.table_name,
                "TABLE_AUDIT": audit_table.table_name,
                # Still required: the OpenAI credential serves the Realtime voice
                # surface only. The claims review runs on Bedrock and uses the
                # execution role, so it needs no stored credential.
                "OPENAI_SECRET_ARN": openai_secret.secret_arn,
                "REALTIME_MODEL": "gpt-realtime-2.1",
                # Claims Specialist — an OpenAI model (gpt-5.6-terra) served by
                # Amazon Bedrock. Cross-region inference profile, so the `us.`
                # prefix is required: it is not offered ON_DEMAND in one region.
                "CLAIMS_MODEL": os.environ.get(
                    "CLAIMS_MODEL", "us.openai.gpt-5.6-terra"),
                "BEDROCK_REGION": os.environ.get("BEDROCK_REGION", "us-east-1"),
                # Responsible-AI guardrail applied on every claims-review call.
                "GUARDRAIL_ID": guardrail.attr_guardrail_id,
                "GUARDRAIL_VERSION": guardrail_version.attr_version,
                # Provider Voice Assistant. Separate from REALTIME_MODEL so the
                # two Realtime surfaces can be moved independently.
                "OPENAI_REALTIME_MODEL": os.environ.get(
                    "OPENAI_REALTIME_MODEL", "gpt-realtime-2.1"),
                "OPENAI_REALTIME_VOICE": os.environ.get(
                    "OPENAI_REALTIME_VOICE", "alloy"),
                "REALTIME_TTL_SECONDS": "600",
                "EPHEMERAL_TTL_SECONDS": "600",
            },
        )

        data_table.grant_read_write_data(api_fn)
        openai_secret.grant_read(api_fn)

        # Apply the responsible-AI guardrail on every claims-review model call.
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:ApplyGuardrail"],
            resources=[guardrail.attr_guardrail_arn],
        ))

        # Audit is append-only for the application: no UpdateItem, no DeleteItem.
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem", "dynamodb:Query", "dynamodb:GetItem"],
            resources=[audit_table.table_arn],
        ))

        # Retrieval against whichever knowledge base setup_kb.py creates.
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:Retrieve"],
            resources=[f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*"],
        ))

        # ── Bedrock Converse, for the claims review ──────────────────
        # A cross-region inference profile needs TWO grants, which is the usual
        # trip-up: the profile itself, and the underlying foundation model in
        # every region the profile may route to. Granting only the profile fails
        # at call time with AccessDeniedException naming the foundation model,
        # which reads as though the model were not enabled.
        CONVERSE_REGIONS = ["us-east-1", "us-east-2", "us-west-2"]
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[
                f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                *[f"arn:aws:bedrock:{r}::foundation-model/*" for r in CONVERSE_REGIONS],
            ],
        ))

        # ── HTTP API ─────────────────────────────────────────────────
        jwt_auth = authorizers.HttpJwtAuthorizer(
            "CognitoJwt",
            f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}",
            jwt_audience=[user_pool_client.user_pool_client_id],
        )

        http_api = apigw.HttpApi(
            self, "HttpApi",
            api_name="enterprise-advisor",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigw.CorsHttpMethod.POST, apigw.CorsHttpMethod.OPTIONS],
                allow_headers=["content-type", "authorization", "x-session-id",
                               "x-idempotency-key"],
                max_age=Duration.hours(1),
            ),
        )

        integration = integrations.HttpLambdaIntegration("ApiIntegration", api_fn)
        for path in ("/session", "/mcp", "/audit-verify", "/usage", "/usage-summary",
                     "/session-trace",
                     # Claims Specialist — Responses API
                     "/claim-package", "/claim-analyze", "/claim-decision",
                     # Provider Voice Assistant — Realtime API
                     "/claim-voice-session", "/claim-voice-tool", "/claim-voice-event"):
            http_api.add_routes(
                path=path,
                methods=[apigw.HttpMethod.POST],
                integration=integration,
                authorizer=jwt_auth,
            )

        # Prototype stand-in for WAF rate limiting.
        stage = http_api.default_stage.node.default_child
        stage.default_route_settings = {
            "throttlingBurstLimit": 20,
            "throttlingRateLimit": 10,
        }

        # ── web client ───────────────────────────────────────────────
        site_bucket = s3.Bucket(
            self, "Site",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # Resolve extensionless paths such as /token to /token/index.html.
        rewrite_fn = cloudfront.Function(
            self, "UriRewrite",
            code=cloudfront.FunctionCode.from_inline(
                "function handler(event) {\n"
                "  var request = event.request;\n"
                "  var uri = request.uri;\n"
                "  if (uri.endsWith('/')) {\n"
                "    request.uri = uri + 'index.html';\n"
                "  } else if (!uri.split('/').pop().includes('.')) {\n"
                "    request.uri = uri + '/index.html';\n"
                "  }\n"
                "  return request;\n"
                "}\n"
            ),
            runtime=cloudfront.FunctionRuntime.JS_2_0,
            comment="Append index.html to directory-style paths",
        )

        distribution = cloudfront.Distribution(
            self, "Cdn",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                function_associations=[cloudfront.FunctionAssociation(
                    function=rewrite_fn,
                    event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                )],
            ),
            comment="BFSI Assistant client",
        )

        # ── outputs ──────────────────────────────────────────────────
        CfnOutput(self, "ApiEndpoint", value=http_api.api_endpoint)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=user_pool_client.user_pool_client_id)
        CfnOutput(self, "DataTable", value=data_table.table_name)
        CfnOutput(self, "AuditTable", value=audit_table.table_name)
        CfnOutput(self, "DocumentsBucket", value=docs_bucket.bucket_name)
        CfnOutput(self, "SiteBucket", value=site_bucket.bucket_name)
        CfnOutput(self, "SiteUrl", value=f"https://{distribution.distribution_domain_name}")
        CfnOutput(self, "OpenAISecretArn", value=openai_secret.secret_arn)
        CfnOutput(self, "ApiFunctionName", value=api_fn.function_name)
