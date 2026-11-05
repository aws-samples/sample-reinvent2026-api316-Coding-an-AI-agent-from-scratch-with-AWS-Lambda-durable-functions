"""Support helpers for the agent handler — web-UI state plumbing and SDK compat.

These are intentionally kept out of ``handler.py`` so that file reads as a clean
example of the durable primitives. Nothing here is core to the durable-functions
story:

* ``publish_state`` / ``publish_failure`` — write per-execution state to the
  store so the web UI can render progress. Not the point of the demo.
* ``publish_final`` — the ``finalize_guide`` tool: map the model's status to the
  store, publish the terminal state, build the return value.
* ``poll_swags`` — the body of one swag-reservation poll (the ``check`` passed
  to ``wait_for_condition``). Business logic + a UI write, no durable primitive.
* ``is_approved`` — normalize a callback payload into a boolean.
* ``json_formatter`` — checkpoint step results as plain JSON instead of the SDK's
  ``{"t", "v"}`` type envelope.
"""

from __future__ import annotations

import json
from typing import Any

from . import state_store, swag_service
from .durable_sdk import JsonSerDes
from .swag_service import SwagReservation


def json_formatter():
    """Checkpoint step results as plain JSON (no ``{"t", "v"}`` type envelope).

    Every step result in the agent is already JSON-safe (Bedrock messages and
    tool results), so the SDK's type-preserving default is not needed. Returns
    None (SDK default) if the runtime's SDK has no ``JsonSerDes``.
    """
    return JsonSerDes() if JsonSerDes else None


def publish_state(context, execution_id: str, name: str, **fields) -> None:
    """Publish execution state to the store inside a checkpointed step."""
    context.step(
        lambda ctx: (state_store.put(execution_id, **fields) or {"published": name}),
        name=f"publish_state_{name}",
    )


def publish_final(context, execution_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Handle ``finalize_guide``: publish the terminal state and return the result.

    ``payload`` is the model's tool input: ``status`` (CONFIRMED/REJECTED), plus
    optional ``guide`` and ``message``. The store write goes through
    :func:`publish_state`, so it is checkpointed and skipped on replay.
    """
    status = payload.get("status", "CONFIRMED")
    guide = payload.get("guide", [])
    message = payload.get("message", "")

    store_status = {
        "CONFIRMED": state_store.STATUS_CONFIRMED,
        "REJECTED": state_store.STATUS_REJECTED,
    }.get(status, state_store.STATUS_CONFIRMED)

    fields: dict[str, Any] = {"status": store_status}
    if guide:
        fields["confirmedGuide"] = guide
    if message:
        fields["message"] = message
    publish_state(context, execution_id, "finalized", **fields)

    context.logger.info(f"[agent] finalized: status={status}, {len(guide)} guide items")
    result: dict[str, Any] = {"status": status}
    if guide:
        result["confirmed_guide"] = guide
    if message:
        result["message"] = message
    return result


def publish_failure(context, execution_id: str, exc: Exception) -> None:
    """Best-effort terminal ``FAILED`` state so the UI stops polling forever.

    A *direct* store write (not :func:`publish_state`): this runs while an
    exception unwinds, and opening a new durable operation there can itself fail.
    The write is a last-writer-wins merge, so repeating it across retries is
    harmless. Both the log and the write are guarded so reporting a failure never
    masks the original one.
    """
    message = f"{type(exc).__name__}: {exc}"[:500]
    try:
        context.logger.error(f"[agent] execution failed: {message}")
    except Exception:  # noqa: BLE001 - logging must not shadow the real error
        pass
    try:
        state_store.put(execution_id, status=state_store.STATUS_FAILED, message=message)
    except Exception:  # noqa: BLE001 - store may be unset or unreachable
        pass


def poll_swags(execution_id: str, reservations: list[SwagReservation],
               logger=None) -> list[SwagReservation]:
    """One swag-reservation poll — the ``check`` behind ``wait_for_condition``.

    Asks the swag service about every swag reservation not yet confirmed, logs
    the result, and pushes it to the UI. Returns the updated list, which the SDK
    checkpoints and passes into the next poll. A direct store write (not
    :func:`publish_state`): this already runs inside a durable operation.
    """
    updated = [
        r if r["status"] == swag_service.CONFIRMED
        else {**r, "status": swag_service.status(execution_id, r["booth_number"])}
        for r in reservations
    ]
    if logger is not None:
        logger.info("[agent] swag poll: " +
                    ", ".join(f"{r['booth_number']}={r['status']}" for r in updated))
    state_store.put(execution_id, status=state_store.STATUS_RESERVING, reservations=updated)
    return updated


def is_approved(approval: Any) -> bool:
    """Normalize a callback payload into an approve/reject boolean.

    The API sends the callback result as a JSON string like ``{"approved": true}``,
    which the SDK hands back as a string. Parse it, then read ``approved``.
    """
    if isinstance(approval, str):
        try:
            approval = json.loads(approval)
        except ValueError:
            return approval.strip().lower() in ("approved", "yes", "true")
    if isinstance(approval, dict):
        return bool(approval.get("approved"))
    return bool(approval)
