# Responsible AI

This sample puts generative AI in front of financial and insurance workflows,
so the guardrails are part of the design rather than an afterthought. This
document states what those guardrails are, where they live in the code, and what
the sample does **not** claim.

## Principle: assist, don't decide

Every AI surface here is advisory. No model can settle a claim, approve a payout,
move money, or change an account. A person makes every consequential decision,
and the architecture — not a prompt instruction — is what keeps it that way.

## Guardrails, and where they live

| Control | Where | What it does |
|---|---|---|
| No authority to act | `claims_voice.py` `REGISTRY` | The voice assistant's tool registry contains no settle/approve/price tool, so the model has no reachable path to one. |
| Money is computed, not generated | `claims.py` `settlement_estimate()` | Every rupee figure comes from deterministic code. A validation check rejects any monetary figure the model introduces in prose. |
| Deterministic gate | `claims.py` `validate()` | 17 named checks (9 blocking) run on the model's output before a specialist sees it: schema conformance, citations resolve to supplied sources, gaps vs. deductions kept separate, no invented amount. |
| Amazon Bedrock Guardrails | `infra/stack.py`, applied in `claims.py` | A managed guardrail screens the claims-review model's input and output: content filters, PII anonymisation, and a denied topic for "financial/insurance advice presented as a final decision." Blocked input/output is surfaced, never silently passed. **Fails closed** — a missing guardrail raises instead of downgrading to an unguarded call. Covers the Bedrock path only; see the section below. |
| Grounded retrieval only | `tools.py`, `retrieval.py` | The advisor answers from retrieved, eligibility-filtered policy documents; it is instructed not to answer from general knowledge, and says so when nothing matches. |
| Human-in-the-loop escalation | `store.py` `create_escalation`, `escalate_to_human` tool | The assistant hands off to a colleague whenever it cannot help, a customer disputes, or a high-risk action is requested. The claims review is only ever a recommendation; a named specialist records the decision. |
| Tamper-evident audit | `audit.py` | Every tool call and decision is written to a hash-chained, append-only log. For any state-changing action the entry is written before the action, which is refused if the write fails. |
| Customer disclosure | `handler.py` `DISCLOSURE`, the `ai-disclaimer` banner on every page | The customer is told they are speaking with an automated assistant, that the guidance is AI-generated and is not financial, legal, or insurance advice, that a colleague should review it **before they act on it**, that it is never the sole basis for a decision on their account or claim, and that they can ask to be transferred at any time. The text is injected into the model's session instructions and rendered in the UI. |

### Where the Bedrock guardrail reaches, and where it cannot

Worth stating plainly, because the two AI surfaces differ and a table row can hide
it. The guardrail covers one of them, and that is a property of the architecture
rather than an oversight:

- **Claims review — covered.** This path runs an OpenAI model *through Amazon
  Bedrock*, so the request and response pass through AWS and the managed guardrail
  screens both. It **fails closed**: if `GUARDRAIL_ID` / `GUARDRAIL_VERSION` are
  absent, `claims.py` raises rather than quietly calling an unguarded model.
  `ALLOW_UNGUARDED_MODEL=1` overrides that for local work against a pre-guardrail
  stack, and logs a warning every time it is used.
- **Voice assistant — not covered, and cannot be.** The browser holds a WebRTC
  media session **directly with OpenAI**. The audio and the model's response never
  traverse AWS, so there is no point at which a Bedrock guardrail could inspect
  them. Bedrock offers no equivalent low-latency speech-to-speech API, which is
  why the path exists at all.

What carries the weight on the voice path instead, all enforced server-side:

- the tool registry has no settle, approve, or price entry, so the capability is
  absent rather than discouraged;
- no tool it can call returns a monetary figure;
- `authorization_policy.json` is deny-by-default — a tool with no rule is
  unreachable;
- identity is re-derived from the JWT and the session record, so a model-supplied
  `customer_id` is refused and audited as an injection attempt;
- every tool call is written to the hash-chained audit log before it takes effect;
- the disclosure is injected into the session instructions and shown on the page.

An adopter who needs content filtering on the model's *words* on this path has to
add it at the model provider, or route the audio through their own service first.
This sample does neither, and does not pretend to.

## Human-in-the-loop is mandatory for anything consequential

The AI never closes the loop on a financial or insurance outcome:

- **Claims** — the model produces a *recommendation* with citations; a claims
  specialist reviews it and records `approve` / `edit` / `reject` under their own
  identity. The payable amount is computed by the claims system, not the model.
- **Voice** — the assistant explains status and requests outstanding documents.
  A request for a settlement decision or an amount is routed to a specialist.
- **Banking advisor** — high-risk actions (closing an account, raising a
  dispute) require step-up identity verification and are gated by a
  deny-by-default authorization policy; anything it cannot handle escalates.

## Fairness and bias

The demo runs on synthetic fixtures, so it makes no fairness claim about a
trained system. For a real deployment, the areas to test before launch:

- **Outcome parity** across protected and vulnerable groups — the fixtures
  already include a senior-citizen persona precisely so this is not overlooked.
  Compare recommendation and escalation rates across cohorts on a labelled set.
- **Retrieval fairness** — confirm the eligibility filter narrows by entitlement
  and geography only, never by a proxy for a protected attribute.
- **Language and accent robustness** — the voice transcription should be
  measured across accents; the design keeps facts in tool results rather than
  the transcript so recognition errors degrade wording, not correctness.
- **Consistency** — the claims review runs at temperature 0 (or the model's
  deterministic default) so the same claim yields the same recommendation.

None of this has been performed here; it is the phase-zero work a production
adopter owns, and it needs a labelled dataset the sample does not ship.

## What this sample does not claim

- **No accuracy or quality metric.** No figure here is a measured model-quality
  result. Producing one requires a labelled set of settled claims scored against
  specialist decisions — out of scope for a demonstration.
- **Not professional advice.** Nothing the system outputs is financial, legal,
  medical, or insurance advice.
- **Synthetic data only.** No real person, provider, insurer, policy, or claim
  is represented anywhere in this repository.
- **Not production-hardened.** See the "Known limitations" section of the README
  (single claim, simulated document requests, no WAF, prompts unversioned, etc.).

## Reporting a concern

For a security or safety issue, please follow
[CONTRIBUTING.md](CONTRIBUTING.md) and report it privately rather than opening a
public issue.
