"""DynamoDB-backed state store shared by the durable function and the API.

The durable function is asynchronous and *suspends* at the approval gate, so a
browser can't get its progress from a single request/response. Instead the
durable handler publishes its state here at each phase, and the control API
(``api.py``) reads it to drive the UI:

* ``RUNNING``     — execution started, the agent is working
* ``AWAITING_APPROVAL`` — hit list ready, function suspended; ``callback_id`` set
* ``RESERVING``   — attendee approved; swag reserved, polling for confirmation;
  ``reservations`` set
* ``CONFIRMED``   — swag reservations confirmed; ``confirmedGuide`` set
* ``REJECTED``    — terminal, with a ``message``
* ``FAILED``      — terminal; an unhandled error escaped the agent loop

The control API also stores ``ownerSub`` (the Cognito user who started the run)
when it seeds the item, and only returns the item to that user.

One item per execution, keyed by ``executionId``. Writes are last-writer-wins
full puts of a merged item, which is safe under durable replay because each
write is wrapped in a ``context.step`` (checkpointed, skipped on replay) and the
payloads are deterministic.

Configuration: ``SCOUT_STATE_TABLE`` (table name). Falls back to a no-op store
when unset, so the durable logic still runs in environments without the table.
"""

from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from typing import Any

_TABLE_NAME = os.environ.get("SCOUT_STATE_TABLE", "")
# Rows expire after this many seconds (DynamoDB TTL attribute "ttl").
_TTL_SECONDS = int(os.environ.get("SCOUT_STATE_TTL_SECONDS", "86400"))

STATUS_RUNNING = "RUNNING"
STATUS_AWAITING_APPROVAL = "AWAITING_APPROVAL"
# Approved; the agent is reserving swag and polling until it's confirmed.
STATUS_RESERVING = "RESERVING"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_REJECTED = "REJECTED"
# Unhandled error escaped the agent loop. Terminal — published by the handler's
# failure guard so the UI stops polling instead of sitting on RUNNING forever.
STATUS_FAILED = "FAILED"


def enabled() -> bool:
    """True when a state table is configured (i.e. running against AWS)."""
    return bool(_TABLE_NAME)


def _table():
    """Return the DynamoDB Table resource, or ``None`` if not configured."""
    if not _TABLE_NAME:
        return None
    import boto3  # lazy so importing this module needs no AWS deps

    return boto3.resource("dynamodb").Table(_TABLE_NAME)


def _to_dynamo(value: Any) -> Any:
    """Convert a JSON-like value into DynamoDB-safe types (floats -> Decimal)."""
    return json.loads(json.dumps(value), parse_float=Decimal)


def put(execution_id: str, **fields: Any) -> None:
    """Merge ``fields`` into the execution's item via a server-side update.

    Uses ``update_item`` with a SET expression so we never read existing
    ``Decimal`` values back into Python (which caused serialization errors on
    the subsequent write). No-op when the table isn't configured, so the durable
    pipeline still runs without a table.
    """
    table = _table()
    if table is None:
        return

    now = int(time.time())
    fields = {**fields, "updatedAt": now, "ttl": now + _TTL_SECONDS}

    set_parts = []
    names: dict[str, str] = {}
    values: dict[str, Any] = {}
    for i, (key, val) in enumerate(fields.items()):
        set_parts.append(f"#k{i} = :v{i}")
        names[f"#k{i}"] = key
        values[f":v{i}"] = _to_dynamo(val)
    # Set createdAt only if the item is new.
    set_parts.append("createdAt = if_not_exists(createdAt, :now)")
    values[":now"] = now

    table.update_item(
        Key={"executionId": execution_id},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def get(execution_id: str) -> dict[str, Any] | None:
    """Return the stored item for an execution, or ``None``."""
    table = _table()
    if table is None:
        return None
    resp = table.get_item(Key={"executionId": execution_id})
    return resp.get("Item")





def increment(key: str, attribute: str) -> int:
    """Atomically add 1 to a numeric attribute on item ``key`` and return it.

    Backs the simulated swag service's poll counter. Requires a configured table.
    """
    resp = _table().update_item(
        Key={"executionId": key},
        UpdateExpression="SET #ttl = :ttl ADD #attr :one",
        ExpressionAttributeNames={"#ttl": "ttl", "#attr": attribute},
        ExpressionAttributeValues={":ttl": int(time.time()) + _TTL_SECONDS, ":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"][attribute])
