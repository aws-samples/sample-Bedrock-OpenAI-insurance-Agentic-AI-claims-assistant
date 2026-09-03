#!/usr/bin/env python3
"""
Pre-demo preflight. Run this before standing up in front of anyone.

Checks the things that break a demo and are invisible until they do: a missing
credential, a model name that no longer resolves, a claim that will not load, a
route that was never deployed. Fails loudly and says which one.

    python3 scripts/preflight.py            checks only, ~10 seconds
    python3 scripts/preflight.py --full     also runs all four test suites
"""
import os
import contextlib
import io
import json
import re
import runpy
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

import boto3
import certifi

ROOT = Path(__file__).resolve().parent.parent
CTX = ssl.create_default_context(cafile=certifi.where())
PASSWORD = os.environ.get("DEMO_PASSWORD")
if not PASSWORD:
    sys.exit("Set DEMO_PASSWORD first — see README, Deploy step 3.")
CLAIM = "CLM-48291"
KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{20,}")

OK, BAD = [], []


def check(name, ok, detail=""):
    (OK if ok else BAD).append(name)
    print(f"  {'✓' if ok else '✗'}  {name}" + (f"  — {detail}" if detail else ""))


# Suite names are fixed below; nothing here is caller-supplied.
SUITES = ("smoke_test.py", "bfsi_test.py", "claims_test.py", "claims_voice_test.py")


def _run_suite(script):
    """
    Run one sibling test suite in this interpreter and return (exit_code, output).

    Deliberately not a subprocess. Spawning one would mean building a command
    line, which is the shape static analysis flags as potential command
    injection, and it would also need the right interpreter passed to it. Running
    in-process sidesteps both: there is no command line to construct, and the
    suite necessarily uses the same interpreter and virtual environment as this
    script.

    `runpy.run_path` gives each suite a fresh module namespace, so their
    module-level state cannot leak into each other or into this script. `argv` is
    swapped for the duration because the suites parse their own, and stdout and
    stderr are captured so a failing suite reports through `check()` rather than
    scrolling past. A suite signals failure with `sys.exit`, which surfaces here
    as `SystemExit`; any other exception is reported as a failure rather than
    taking preflight down with it.
    """
    if script not in SUITES:                       # defensive: fixed allowlist
        raise ValueError(f"unknown suite: {script!r}")

    path = ROOT / "scripts" / script
    buf = io.StringIO()
    saved_argv = sys.argv
    code = 0
    try:
        sys.argv = [str(path)]
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            runpy.run_path(str(path), run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except Exception as exc:                        # noqa: BLE001 - report, don't abort
        code = 1
        buf.write(f"\n{type(exc).__name__}: {exc}")
    finally:
        sys.argv = saved_argv
    return code, buf.getvalue()
    return ok


def post(url, body, headers, timeout=60):
    if not url.lower().startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url!r}")
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:  # nosec B310 - https asserted in post()  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            raw = r.read()
            return r.status, json.loads(raw or "{}"), raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or "{}"), raw.decode("utf-8", "replace")
        except json.JSONDecodeError:
            return e.code, {}, raw.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}, ""


def main():
    full = "--full" in sys.argv
    print("Preflight — Claims Intelligence demo\n")

    # ── 1 · deployment outputs ────────────────────────────────────────
    print("[1] Deployment")
    outputs = ROOT / ".deploy" / "outputs.json"
    if not check("stack outputs present", outputs.exists(), str(outputs)):
        sys.exit("cannot continue without .deploy/outputs.json — deploy first")
    out = json.loads(outputs.read_text())["EnterpriseAdvisor"]
    api = out["ApiEndpoint"]
    check("API endpoint known", bool(api), api)
    check("site URL known", bool(out.get("SiteUrl")), out.get("SiteUrl", ""))

    # ── 2 · credentials, server-side only ─────────────────────────────
    print("\n[2] Credentials")
    try:
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        raw = sm.get_secret_value(SecretId=out["OpenAISecretArn"])["SecretString"]
        try:
            key = json.loads(raw).get("api_key", raw)
        except json.JSONDecodeError:
            key = raw
        key = (key or "").strip()
        check("OpenAI credential present in Secrets Manager",
              key.startswith("sk-") and len(key) > 40, f"{key[:7]}… {len(key)} chars")
    except Exception as exc:  # noqa: BLE001
        check("OpenAI credential present in Secrets Manager", False, str(exc)[:90])

    # ── 3 · Lambda configuration ──────────────────────────────────────
    print("\n[3] Model configuration")
    try:
        lam = boto3.client("lambda", region_name="us-east-1")
        env = lam.get_function_configuration(
            FunctionName=out["ApiFunctionName"])["Environment"]["Variables"]
        realtime = env.get("OPENAI_REALTIME_MODEL")
        check("OPENAI_REALTIME_MODEL configured", bool(realtime), realtime or "unset")
        claims_model = env.get("CLAIMS_MODEL", "")
        check("CLAIMS_MODEL configured (Bedrock Converse surface)",
              bool(claims_model), claims_model or "unset")
        # A Bedrock model that needs a cross-region inference profile fails at
        # call time without the `us.` prefix, with an error that names the
        # foundation model and reads like a model-access problem instead.
        check("claims model is a cross-region inference profile id",
              claims_model.startswith(("us.", "eu.", "apac.")),
              "prefix present" if claims_model.startswith(("us.", "eu.", "apac."))
              else f"{claims_model} has no region prefix")
        check("no API key in Lambda environment",
              not any(KEY_RE.search(str(v)) for v in env.values()),
              "credential is read from Secrets Manager at call time")
    except Exception as exc:  # noqa: BLE001
        check("Lambda configuration readable", False, str(exc)[:90])
        realtime = None

    # ── 4 · the configured model actually resolves ────────────────────
    if realtime:
        try:
            req = urllib.request.Request(
                f"https://api.openai.com/v1/models/{realtime}",
                headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=15, context=CTX) as r:  # nosec B310 - literal https endpoint  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
                body = json.loads(r.read())
            check(f"model resolves upstream · {realtime}",
                  body.get("id") == realtime, body.get("id", ""))
        except urllib.error.HTTPError as e:
            check(f"model resolves upstream · {realtime}", False,
                  f"HTTP {e.code} — check the model name")
        except Exception as exc:  # noqa: BLE001
            check(f"model resolves upstream · {realtime}", False, str(exc)[:70])

    # ── 4b · the Bedrock model the claims review depends on ───────────
    # Checked by listing rather than by invoking: a real converse call on this
    # prompt costs ~4,700 input tokens, and preflight is meant to be cheap enough
    # to run before every demo.
    if claims_model:
        try:
            br = boto3.client("bedrock", region_name="us-east-1")
            profiles = {p["inferenceProfileId"]
                        for p in br.list_inference_profiles(maxResults=200)
                                    .get("inferenceProfileSummaries", [])
                        if p.get("status") == "ACTIVE"}
            check(f"claims model available · {claims_model}",
                  claims_model in profiles,
                  "ACTIVE inference profile" if claims_model in profiles
                  else "not an ACTIVE profile in this account/region")
        except Exception as exc:  # noqa: BLE001
            check(f"claims model available · {claims_model}", False, str(exc)[:80])

    # ── 5 · sign-in ───────────────────────────────────────────────────
    print("\n[4] Identity")
    st, d, _ = post("https://cognito-idp.us-east-1.amazonaws.com/",
                    {"AuthFlow": "USER_PASSWORD_AUTH", "ClientId": out["UserPoolClientId"],
                     "AuthParameters": {"USERNAME": "cust-1001", "PASSWORD": PASSWORD}},
                    {"Content-Type": "application/x-amz-json-1.1",
                     "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth"})
    if not check("demo sign-in works", st == 200, f"HTTP {st}"):
        sys.exit("sign-in failed — nothing else can be checked")
    tok = d["AuthenticationResult"]["IdToken"]
    H = {"Content-Type": "application/json", "Authorization": f"Bearer {tok}"}

    # ── 6 · both surfaces respond ─────────────────────────────────────
    print("\n[5] Claims Specialist surface (Responses API)")
    st, pkg, _ = post(api + "/claim-package", {}, H)
    check("claim package loads", st == 200 and pkg.get("claim", {}).get("claim_id") == CLAIM,
          f"HTTP {st}")
    spec_absent = set(pkg.get("claim", {}).get("documents_absent") or [])
    check("demo claim carries its two absent documents", len(spec_absent) == 2,
          ", ".join(sorted(spec_absent)))

    print("\n[6] Provider Voice Assistant surface (Realtime API)")
    st, sess, raw = post(api + "/claim-voice-session", {}, H)
    ok_sess = check("realtime session mints", st == 200 and bool(sess.get("client_secret")),
                    f"HTTP {st} · {sess.get('model', '')}")
    check("no API key returned to the browser", not KEY_RE.search(raw))
    check("no system instructions returned to the browser",
          "YOU MUST NOT" not in raw)

    if ok_sess:
        sid = sess["session_id"]
        hdr = {**H, "x-session-id": sid}
        for name, args in (
            ("get_claim_status", {"claim_id": CLAIM}),
            ("get_missing_documents", {"claim_id": CLAIM}),
            ("get_claim_review_summary", {"claim_id": CLAIM}),
        ):
            st, d, _ = post(api + "/claim-voice-tool",
                            {"name": name, "arguments": args}, hdr)
            check(f"tool responds · {name}",
                  st == 200 and d.get("status") == "ok",
                  f"{d.get('latency_ms', '?')} ms")

        st, d, _ = post(api + "/claim-voice-tool",
                        {"name": "get_missing_documents", "arguments": {"claim_id": CLAIM}},
                        hdr)
        docs = (d.get("result") or {}).get("missing_documents") or []
        clauses = {x.get("source_clause") for x in docs}
        check("three outstanding items", len(docs) == 3, str(len(docs)))
        check("room-rent deduction absent from the document list",
              "POL-RR-4.1" not in clauses, ", ".join(sorted(c for c in clauses if c)))

        names = " ".join(x["document"].lower() for x in docs)
        check("both surfaces agree on the outstanding documents",
              all(a.lower() in names for a in spec_absent),
              "voice list contains every gap the specialist sees")

        st, d, _ = post(api + "/claim-voice-tool",
                        {"name": "settle_claim", "arguments": {"claim_id": CLAIM}}, hdr)
        check("settlement tool unreachable from voice",
              d.get("status") == "not_permitted", "refused server-side")

    # ── 7 · the pages are served ──────────────────────────────────────
    print("\n[7] Web")
    site = out.get("SiteUrl", "")
    if site and not site.lower().startswith("https://"):
        raise ValueError(f"refusing non-https site URL: {site!r}")
    for path, needle in (("/healthcare/", "Claims Resolution Copilot"),
                         ("/healthcare/realtime/", "Realtime Claim Assistant")):
        try:
            with urllib.request.urlopen(site + path, timeout=20, context=CTX) as r:  # nosec B310 - https asserted at site assignment  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
                html = r.read().decode("utf-8", "replace")
            ok = needle in html
            check(f"page served · {path}", ok, "" if ok else "unexpected content")
            check(f"page carries no API key · {path}", not KEY_RE.search(html))
            check(f"surface nav present · {path}", 'class="surfaces"' in html)
        except Exception as exc:  # noqa: BLE001
            check(f"page served · {path}", False, str(exc)[:70])

    # ── 8 · optionally the full suites ────────────────────────────────
    if full:
        print("\n[8] Test suites")
        for script in SUITES:
            code, out = _run_suite(script)
            tail = [ln for ln in out.strip().splitlines() if "passed" in ln]
            check(f"suite · {script}", code == 0,
                  tail[-1].strip() if tail else f"exit {code}")
    else:
        print("\n  (run with --full to also execute all four test suites)")

    print(f"\n{'=' * 62}\n{len(OK)} ok   {len(BAD)} failed")
    if BAD:
        for b in BAD:
            print(f"  FAILED: {b}")
        sys.exit(1)
    print("preflight clean — safe to demo")


if __name__ == "__main__":
    main()
