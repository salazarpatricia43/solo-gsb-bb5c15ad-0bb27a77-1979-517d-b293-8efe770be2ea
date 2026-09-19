"""FastAPI application: policy management, adjudication and health checks."""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .counterfactual import CounterfactualSearchError, counterfactuals
from .engine import adjudicate
from .store import store
from .validation import (
    PolicyValidationError,
    validate_counterfactual_request,
    validate_policy,
)

API_PREFIX = "/api/v1"

app = FastAPI(
    title="Explainable Rule Adjudication API",
    version="1.0.0",
    description=(
        "Submit policies built from all/any/not and comparison expressions; "
        "evaluate facts with three-valued (true/false/unknown) logic and "
        "receive priority-based verdicts with per-node derivation traces, "
        "inclusion-minimal missing-field sets, and inclusion-minimal "
        "counterfactual fact edits that force a target conclusion."
    ),
)


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #

@app.exception_handler(PolicyValidationError)
async def policy_validation_handler(
    request: Request, exc: PolicyValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Policy structure validation failed",
            "errors": exc.errors,
        },
    )


@app.exception_handler(CounterfactualSearchError)
async def counterfactual_search_handler(
    request: Request, exc: CounterfactualSearchError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Counterfactual search failed",
            "errors": [str(exc)],
        },
    )


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #

class EvaluateRequest(BaseModel):
    facts: dict[str, Any] = Field(
        default_factory=dict,
        description="Facts keyed by field name. Absent or null fields are unknown.",
    )


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #

@app.get("/health", tags=["health"])
async def health_liveness() -> dict:
    """Liveness probe: the process can serve requests."""
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def health_readiness() -> dict:
    """Readiness probe: storage is reachable."""
    return {"status": "ok", "policies": store.count()}


# --------------------------------------------------------------------------- #
# Policy management
# --------------------------------------------------------------------------- #

@app.post(f"{API_PREFIX}/policies", tags=["policies"], status_code=201)
async def create_policy(request: Request) -> dict:
    payload = await _json_body(request)
    rules = validate_policy(payload)
    policy_id = store.new_id()
    record = store.put(policy_id, {"rules": rules})
    return record


@app.put(f"{API_PREFIX}/policies/{{policy_id}}", tags=["policies"])
async def replace_policy(policy_id: str, request: Request) -> dict:
    payload = await _json_body(request)
    rules = validate_policy(payload)
    record = store.put(policy_id, {"rules": rules})
    return record


@app.get(f"{API_PREFIX}/policies", tags=["policies"])
async def list_policies() -> dict:
    return {"policies": store.list()}


@app.get(f"{API_PREFIX}/policies/{{policy_id}}", tags=["policies"])
async def get_policy(policy_id: str) -> dict:
    record = store.get(policy_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    return record


@app.delete(f"{API_PREFIX}/policies/{{policy_id}}", tags=["policies"])
async def delete_policy(policy_id: str) -> dict:
    if not store.delete(policy_id):
        raise HTTPException(status_code=404, detail="Policy not found")
    return {"id": policy_id, "deleted": True}


# --------------------------------------------------------------------------- #
# Adjudication
# --------------------------------------------------------------------------- #

@app.post(f"{API_PREFIX}/policies/{{policy_id}}/evaluate", tags=["adjudication"])
async def evaluate_policy(policy_id: str, body: Optional[EvaluateRequest] = None) -> dict:
    record = store.get(policy_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    facts = body.facts if body is not None else {}
    verdict = adjudicate(record, facts)
    verdict["policy_id"] = policy_id
    return verdict


@app.post(
    f"{API_PREFIX}/policies/{{policy_id}}/counterfactuals",
    tags=["adjudication"],
)
async def policy_counterfactuals(policy_id: str, request: Request) -> dict:
    """Find inclusion-minimal fact edits that uniquely force a target verdict."""
    record = store.get(policy_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Policy not found")

    payload = await _json_body(request)
    facts, target, candidates, max_changes = validate_counterfactual_request(
        payload
    )

    original_verdict, items = counterfactuals(
        record, facts, target, candidates, max_changes
    )
    return {
        "policy_id": policy_id,
        "target_conclusion": target,
        "original_status": original_verdict["status"],
        "original_conclusion": original_verdict.get("conclusion"),
        "count": len(items),
        "counterfactuals": items,
    }


async def _json_body(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be valid JSON",
        )
