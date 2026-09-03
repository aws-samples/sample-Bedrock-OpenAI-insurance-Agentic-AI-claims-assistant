# Claims Intelligence — two AI surfaces over one insurance claim

A working reference for a question that comes up whenever a contact centre adds
generative AI: **how do you let a model talk to a customer without letting it
decide anything?**

The sample builds two interaction surfaces over a single synthetic health
insurance claim, and keeps the authority boundary in the architecture rather
than in the prompt.

| | Claims Specialist | Provider Voice Assistant |
|---|---|---|
| Interface | Web review workspace | Speech to speech, interruptible |
| Model | OpenAI gpt-5.6-terra **via Amazon Bedrock** | OpenAI Realtime API, direct over WebRTC |
| Job | Assemble evidence, recommend | Explain status, request documents |
| Money | Computes the payable amount in code | **Has no tool that returns a figure** |
| Decides | A named human, on the record | Nothing |

Both surfaces use OpenAI models, reached two different ways on purpose. The
structured review runs an OpenAI model **through Amazon Bedrock**, which keeps it
inside the AWS boundary and authorises with the Lambda execution role — no stored
credential. The voice surface calls the **OpenAI Realtime API directly**, because
that low-latency speech-to-speech API is not offered through Bedrock, so that one
surface does need a key.

Both read the same claim. Neither stores the answer to "what is still
outstanding" — it is derived on every request — so the two surfaces cannot drift
apart and tell a caller different things.

> **This is a demonstration, not a product.** All data is synthetic. Read
> [Known limitations](#known-limitations) before drawing conclusions about
> production readiness.

> **Responsible AI.** This is generative AI in front of financial and insurance
> workflows, so the guardrails are part of the design. **[RESPONSIBLE-AI.md](RESPONSIBLE-AI.md)**
> documents them and where each one lives in the code: the assist-don't-decide
> boundary, the Amazon Bedrock guardrail on the claims-review path (which fails
> closed without one), mandatory human-in-the-loop escalation, the customer
> disclosure, the fairness and bias testing a production adopter owes before
> launch, and what this sample explicitly does **not** claim. Read it before
> adapting any of this for real customers.

## The idea worth borrowing

The voice assistant cannot settle a claim. Not because it is instructed not to —
because its tool registry has no settlement entry:

```python
REGISTRY = {
    "get_claim_status":              get_claim_status,
    "get_missing_documents":         get_missing_documents,
    "get_claim_review_summary":      get_claim_review_summary,
    "send_document_request":         send_document_request,
    "handoff_to_claims_specialist":  handoff_to_claims_specialist,
}
```

A dictionary cannot dispatch a key it does not hold. No tool on this surface
returns a monetary amount, so when a caller asks "how much will I get?" there is
nothing for the model to read out. Asking politely in a prompt is a mitigation;
removing the capability is a control.

A second example, from the same file. The claim has three outstanding items and
one policy deduction. Conflating them is the operational error the demo exists to
prevent — a hospital that is told a *deduction* is a *missing document* sends
paperwork nobody wanted and the clock restarts. So the deduction clause is
excluded from the requestable set structurally, and `send_document_request`
re-checks every id against what the claims system currently reports outstanding.

## Architecture

```
CONTROL PLANE   browser ──HTTPS──► API Gateway ──► Lambda ──► DynamoDB
                                                          ├─► Secrets Manager
                                                          └─► OpenAI
                every fact, every consequence, every audit entry

MEDIA PLANE     browser ◄──WebRTC──► OpenAI Realtime
                speech only · never enters AWS · no relay, no container, no NAT
```

The browser holds the media session directly, using a short-lived credential the
Lambda mints from Secrets Manager. The long-lived OpenAI key never leaves AWS.
Because there is no audio relay, the voice surface needs no VPC, no NAT gateway
and no container — it is three additional routes on a Lambda that already exists.

The trade is that the browser relays tool calls. So the Lambda treats every tool
argument as untrusted, and re-derives identity from the JWT and the server-side
session record rather than from anything the model said.

**Components**

- **Amazon Cognito** — user pool, JWT authorizer on all 12 routes
- **Amazon API Gateway** — HTTP API
- **AWS Lambda** — one Python 3.13 function, all 12 routes
- **Amazon DynamoDB** — application state, plus a hash-chained audit table
- **AWS Secrets Manager** — the OpenAI credential
- **Amazon S3 + CloudFront** — the static client
- **Amazon Bedrock Knowledge Base** — retrieval for the banking advisor surface
- **Amazon CloudWatch** — structured logs, tool latency, voice telemetry

Regenerate the architecture diagram with `python3 docs/build_diagram.py`
(requires `graphviz` on your PATH and the `diagrams` package).

## Prerequisites

- An AWS account you can deploy into, and credentials configured
- Python 3.11 or later, and Node.js 18 or later (for the CDK CLI)
- **An OpenAI API key with access to the Realtime and Responses APIs.** OpenAI
  is not an AWS service; you supply your own key and pay OpenAI directly.
- `graphviz` only if you want to regenerate diagrams

## Deploy

```bash
# 1 · dependencies
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r infra/requirements.txt

# 2 · infrastructure
cd infra
npx aws-cdk@2 bootstrap          # first time in this account and region
npx aws-cdk@2 deploy --outputs-file ../.deploy/outputs.json
cd ..

# 3 · a password for the synthetic demo users
#     Choose your own. It is never written to the repository.
export DEMO_PASSWORD='<choose-a-strong-password>'

# 4 · seed fixtures, create the users, provision the knowledge base
python3 scripts/setup.py
python3 scripts/create_users.py

# 5 · your OpenAI key, straight into Secrets Manager
python3 scripts/set_openai_key.py       # prompts; never echoes, never stored locally

# 6 · publish the static client
python3 scripts/deploy_web.py
```

The stack outputs a CloudFront URL. Sign in with any fixture user — `cust-1001`
through `cust-1004` — and the password from step 3.

| Path | Surface |
|---|---|
| `/` | Banking voice advisor (Realtime + Bedrock retrieval) |
| `/healthcare` | Claims specialist review workspace |
| `/healthcare/realtime` | Provider voice assistant |
| `/ops` | Usage and tool telemetry |
| `/token` | JWT inspector |

## Verify

```bash
python3 scripts/preflight.py            # ~30 checks, about 15 seconds
python3 scripts/preflight.py --full     # plus all four suites, about 3 minutes
```

The suites run against **live deployed infrastructure**, not mocks. They assert
behaviour worth asserting rather than coverage for its own sake:

| Suite | What it proves |
|---|---|
| `smoke_test.py` | Deployment, identity and routing are wired |
| `bfsi_test.py` | Retrieval eligibility filtering, step-up, policy engine |
| `claims_test.py` | The 17-check gate; the deduction is never called a document |
| `claims_voice_test.py` | No settlement path; no credential reaches the browser |

Two examples of the kind of assertion these make:

```python
# The room-rent sub-limit is a deduction. Calling it a missing document is the
# error this whole sample exists to prevent.
assert "POL-RR-4.1" not in {g["source_id"] for g in missing_information}

# The browser must never receive the long-lived OpenAI credential.
assert not re.search(r"sk-[A-Za-z0-9_-]{20,}", session_response_body)
```

## Cost

Running this incurs charges on both AWS and OpenAI.

The AWS side is small and mostly usage-based: Lambda, DynamoDB on-demand, API
Gateway, CloudFront and Secrets Manager together are cents per day at demo
volumes. **The Bedrock Knowledge Base and its vector index are the part that
costs money while idle**, so tear down when you are done.

The OpenAI side depends on conversation length and is billed by OpenAI at their
published Realtime and Responses rates. Realtime audio is materially more
expensive per minute than text. Set a spend limit on your OpenAI account before
demonstrating this to a room.

## Clean up

```bash
cd infra && npx aws-cdk@2 destroy && cd ..
python3 scripts/setup.py --delete-kb     # the Bedrock KB is not managed by CDK
```

The Bedrock Knowledge Base and its S3 Vectors index are created outside
CloudFormation, because the S3 Vectors storage type is not yet covered by it. So
`destroy` alone leaves them behind, still billing. Delete them explicitly.

## Security notes

Worth understanding before you adapt this.

- **The OpenAI key lives only in Secrets Manager**, is read at call time, and is
  never returned to the browser. A test asserts this on every session response.
- **The browser receives a short-lived credential** scoped to one Realtime
  session, not the account key.
- **Tool arguments are untrusted.** Identity comes from the JWT and the session
  record. If a model supplies a `customer_id`, that is treated as an attack: the
  call is denied and an audit entry is written.
- **Sessions are bound to an identity.** Presenting another user's session id
  returns 403.
- **The audit table is append-only by IAM** — the Lambda role has no
  `UpdateItem` or `DeleteItem` on it. Entries are hash-chained, and for any
  state-changing tool the entry is written *before* the change, with the change
  refused if that write fails.
- **`authorization_policy.json` is deny-by-default.** A tool with no rule is
  unreachable rather than open.
- **No WAF is deployed**, and API Gateway throttling is left at stage defaults.
  Add both before exposing anything like this publicly.

Found a security issue? Please follow [CONTRIBUTING.md](CONTRIBUTING.md) and
report it privately rather than opening an issue.

## Known limitations

Stated plainly, because the gap between a demonstration and a product is where
most of the work lives.

- **One hard-coded claim.** The resolver matches a single claim id; anything else
  returns `not_found`. A real deployment needs a claim repository and tenant
  scoping. The resolver is the single function that changes.
- **`send_document_request` is simulated.** The audit entry is real; no message
  leaves the system. The dispatch point is marked in the code.
- **No idempotency on the voice tool route.** A duplicated confirmed request
  would write two audit entries. The banking broker already has the pattern to
  copy.
- **Prompts are code, not configuration.** The three system prompts are Python
  constants, so changing wording is a deployment. They also carry no version and
  are not recorded in the audit trail, so you cannot tell from an audit entry
  which prompt produced an interaction. For a regulated deployment, fix this
  first.
- **Transcription is best-effort.** Clinical terms and Indian English provider
  names will mis-transcribe. The model recovers because tools carry the facts,
  but the transcript will look imperfect.
- **The transcript lags the voice by design.** It is committed once the audio for
  a turn has finished playing, because every approach that predicted speech
  progress rendered text before it was spoken. See the comment block above
  `sayCommit` in `web/healthcare/realtime/realtime.js`.
- **All three surfaces share one Lambda and one IAM role**, so the role is the
  union of what all three need — including `bedrock:Retrieve`, which the claims
  surfaces never use. Split them in production.
- **No accuracy claim is made.** Producing one needs a labelled set of settled
  claims scored against specialist decisions. That work has not been done here,
  and no figure in this repository should be read as a quality measurement.

## Repository layout

```
infra/            CDK application — one stack, all AWS resources
lambdas/api/      The Lambda bundle, 10 modules
  handler.py        routing, auth, all 12 route handlers
  claims_voice.py   voice instructions, 5 tools, derived claim status
  claims.py         claim review, the 17-check gate, settlement arithmetic
  tools.py          15 tools for the banking advisor
  policy.py         authorization decisions
  audit.py          hash-chained audit log
  store.py          all DynamoDB access
  retrieval.py      eligibility-filtered Bedrock retrieval
  claims_data.py    the synthetic claim package
  bfsi.py           banking domain logic
scripts/          setup, deployment and the four test suites
web/              static client, no framework and no build step
data/             synthetic fixtures
docs/             solution overview, demo walkthrough, diagram generator
```

## License

MIT-0. See [LICENSE](LICENSE).
