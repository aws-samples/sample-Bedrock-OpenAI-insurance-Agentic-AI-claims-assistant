"""
Retrieval_Service — eligibility-filtered retrieval from a Bedrock Knowledge Base.

The filter is built from the Session_Record and passed to Bedrock, which applies
it during the search. Nothing the model or the client says can widen it.
"""
import os

import boto3

_agent = boto3.client("bedrock-agent-runtime")

MAX_RESULTS = 6


def _filter(session):
    """Deny-by-default eligibility filter."""
    eligible = session.get("eligible_classifications") or []
    geography = session.get("geography") or "GLOBAL"

    clauses = [
        {"in": {"key": "access_classification", "value": list(eligible)}},
        {
            "orAll": [
                {"equals": {"key": "geography", "value": geography}},
                {"equals": {"key": "geography", "value": "GLOBAL"}},
            ]
        },
    ]
    return {"andAll": clauses}


def _citation(result, index):
    md = result.get("metadata") or {}
    return {
        "citation_id": f"C{index}",
        "document_id": md.get("document_id", "unknown"),
        "title": md.get("title", "Untitled"),
        "version": str(md.get("version", "")),
        "effective_date": md.get("effective_date", ""),
        "section_ref": md.get("section_ref", ""),
        "access_classification": md.get("access_classification", ""),
        "geography": md.get("geography", ""),
        "superseded": bool(md.get("superseded", False)),
        "score": result.get("score"),
        "text": (result.get("content") or {}).get("text", ""),
    }


def search(session, query, kb_id, top_k=MAX_RESULTS, document_id=None):
    """Return an Evidence_Set. Current versions rank above superseded ones."""
    flt = _filter(session)
    if document_id:
        flt = {"andAll": [flt, {"equals": {"key": "document_id", "value": document_id}}]}

    resp = _agent.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": max(top_k, MAX_RESULTS),
                "filter": flt,
            }
        },
    )

    evidence = [_citation(r, i + 1) for i, r in enumerate(resp.get("retrievalResults") or [])]

    # Requirement 8.7: current version outranks a superseded one.
    evidence.sort(key=lambda e: (e["superseded"], -(e.get("score") or 0)))
    for i, e in enumerate(evidence[:top_k], start=1):
        e["citation_id"] = f"C{i}"
    return evidence[:top_k]


def detect_conflict(evidence):
    """
    Two live versions of the same document, or a live and a superseded version
    both surfacing, means the answer is not safe to give unqualified.
    """
    by_doc = {}
    for e in evidence:
        by_doc.setdefault(e["document_id"], set()).add(e["version"])
    for doc, versions in by_doc.items():
        if len(versions) > 1:
            return {"document_id": doc, "versions": sorted(versions)}
    return None
