"""Control API for the Expo Booth Scout web UI (behind API Gateway HTTP API).

Three routes drive the whole attendee experience:

* ``POST /scout``                     — start a scouting run (async), returns an
                                        ``executionId`` the UI polls on.
* ``GET  /scout/{executionId}``       — read current state (status, hit list,
                                        confirmed guide) from the state store.
* ``POST /scout/{executionId}/decision`` — approve/reject; resumes the suspended
                                        durable function via its callback.

The durable function is invoked with a caller-supplied ``_execution_id`` so this
API and the function agree on the state-store key. Durable functions must be
invoked with a qualified ARN (alias/version); ``SCOUT_FUNCTION_ARN`` supplies it.

Authentication is done by the HTTP API's JWT authorizer (Cognito user pool), so
every request that reaches this handler carries verified token claims. This
handler adds authorization: each run records the caller's ``sub`` as its owner,
and only that user can read it or send its decision. Anyone else gets a 404, so
the API never confirms that someone else's run exists. CORS is configured on
the HTTP API, which overrides any CORS headers returned from here.
"""

from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from typing import Any

import boto3

from . import state_store


def _json_default(value: Any) -> Any:
    """Make DynamoDB values JSON-serializable.

    The DynamoDB resource returns numbers as ``Decimal``; convert them to int
    (when whole) or float so ``json.dumps`` can handle the state item.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

_FUNCTION_ARN = os.environ.get("SCOUT_FUNCTION_ARN", "")
_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Input limits. Every run calls Bedrock, so keep requests small and bounded.
_MAX_DESCRIPTION_CHARS = 2000
_MAX_NAME_CHARS = 100
_MAX_BOOTHS = 8


def _resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, default=_json_default),
    }


def _lambda():
    return boto3.client("lambda", region_name=_REGION)


def _caller_sub(event: dict[str, Any]) -> str:
    """The signed-in user's id, from the claims the JWT authorizer verified."""
    claims = (event.get("requestContext", {}).get("authorizer", {})
              .get("jwt", {}).get("claims", {}))
    return str(claims.get("sub") or "")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway (HTTP API, payload v2) entry point."""
    route_key = event.get("routeKey", "")

    # The authorizer already rejects requests without a valid token. This is a
    # fail-closed backstop in case a route is ever attached without it.
    owner = _caller_sub(event)
    if not owner:
        return _resp(401, {"error": "unauthorized"})

    try:
        if route_key == "POST /scout":
            return _start(event, owner)
        if route_key == "GET /scout/{executionId}":
            return _status(event, owner)
        if route_key == "POST /scout/{executionId}/decision":
            return _decision(event, owner)
    except Exception as exc:  # noqa: BLE001 - surface a clean error to the UI
        return _resp(500, {"error": str(exc)})

    return _resp(404, {"error": f"no route for {route_key}"})


def _owned_item(event: dict[str, Any], owner: str) -> dict[str, Any] | None:
    """Load the run named in the path, only if ``owner`` started it."""
    execution_id = event.get("pathParameters", {}).get("executionId", "")
    if not execution_id:
        return None
    item = state_store.get(execution_id)
    if item is None or item.get("ownerSub") != owner:
        return None
    return item


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _start(event: dict[str, Any], owner: str) -> dict[str, Any]:
    body = _body(event)
    description = str(body.get("description") or "").strip()
    if not description:
        return _resp(400, {"error": "description is required"})
    if len(description) > _MAX_DESCRIPTION_CHARS:
        return _resp(400, {"error": f"description is limited to {_MAX_DESCRIPTION_CHARS} characters"})

    attendee = str(body.get("attendee_name") or "Attendee").strip()[:_MAX_NAME_CHARS] or "Attendee"
    try:
        max_booths = int(body.get("max_booths", 5))
    except (TypeError, ValueError):
        return _resp(400, {"error": "max_booths must be a number"})
    max_booths = min(max(max_booths, 1), _MAX_BOOTHS)

    if not _FUNCTION_ARN:
        return _resp(500, {"error": "SCOUT_FUNCTION_ARN is not configured"})

    execution_id = uuid.uuid4().hex
    payload = {
        "_execution_id": execution_id,
        "description": description,
        "attendee_name": attendee,
        "max_booths": max_booths,
    }

    # Seed the row so the UI's first poll finds a RUNNING state immediately.
    # ownerSub ties the run to the signed-in user; later writes from the durable
    # function merge fields into this row and leave it in place.
    state_store.put(
        execution_id,
        status=state_store.STATUS_RUNNING,
        ownerSub=owner,
        attendee=attendee,
        description=description,
    )

    _lambda().invoke(
        FunctionName=_FUNCTION_ARN,
        InvocationType="Event",  # async; durable execution runs in background
        Payload=json.dumps(payload).encode("utf-8"),
    )
    return _resp(202, {"executionId": execution_id, "status": state_store.STATUS_RUNNING})


def _status(event: dict[str, Any], owner: str) -> dict[str, Any]:
    item = _owned_item(event, owner)
    if item is None:
        return _resp(404, {"error": "unknown executionId"})

    # Never leak the callback id to the browser; the decision route resolves it
    # server-side from the store.
    item.pop("callbackId", None)
    item.pop("ttl", None)
    item.pop("ownerSub", None)
    return _resp(200, item)


def _decision(event: dict[str, Any], owner: str) -> dict[str, Any]:
    body = _body(event)
    approved = body.get("approved") is True

    item = _owned_item(event, owner)
    if item is None:
        return _resp(404, {"error": "unknown executionId"})
    execution_id = item["executionId"]

    callback_id = item.get("callbackId")
    if not callback_id:
        return _resp(409, {"error": "execution is not awaiting approval yet"})

    _lambda().send_durable_execution_callback_success(
        CallbackId=callback_id,
        Result=json.dumps({"approved": approved}).encode("utf-8"),
    )
    return _resp(200, {"executionId": execution_id, "approved": approved})
