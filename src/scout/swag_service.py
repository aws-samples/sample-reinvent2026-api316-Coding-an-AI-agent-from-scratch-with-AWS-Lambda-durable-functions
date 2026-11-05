"""Simulated external swag-reservation service — the thing the agent polls.

Stands in for a real exhibitor swag-desk API that is *asynchronous*: you submit
a swag reservation, it comes back ``PENDING``, and you have to poll until the
exhibitor confirms it. That is exactly the shape ``context.wait_for_condition``
is for.

Every swag reservation turns ``CONFIRMED`` on its ``SCOUT_SWAG_CONFIRM_POLLS``-th
status check (default 2), so the first poll still says ``PENDING`` and the demo
shows at least one wait.

The poll count lives in the state table (key ``<executionId>#swag#<booth>``),
like a real external system keeping its own state. With no table configured it
falls back to an in-process dict, so local tests run without AWS.
"""

import os
from typing import NotRequired, TypedDict

from . import state_store

PENDING = "PENDING"
CONFIRMED = "CONFIRMED"


# Typed shapes for the booth data the agent passes between tools. Type hints
# only: at runtime these are plain dicts, because they arrive as JSON from the
# model's tool input and are checkpointed as JSON by durable steps.
class Booth(TypedDict):
    """One stop on the attendee's route / hit list."""

    order: int
    booth_number: str
    name: str
    swag: NotRequired[str]
    talking_points: NotRequired[list[str]]


class SwagReservation(Booth):
    """A booth's swag reservation; ``status`` is PENDING or CONFIRMED."""

    status: str

_CONFIRM_POLLS = int(os.environ.get("SCOUT_SWAG_CONFIRM_POLLS", "2"))

# Fallback poll counter for local runs (no DynamoDB table).
_LOCAL: dict[str, int] = {}


def reserve(stops: list[Booth]) -> list[SwagReservation]:
    """Submit a swag reservation for each stop. Every one starts PENDING."""
    return [{**stop, "status": PENDING} for stop in stops]


def status(execution_id: str, booth_number: str) -> str:
    """One status check against the exhibitor's swag desk. Each call counts as a poll."""
    key = f"{execution_id}#swag#{booth_number}"
    if state_store.enabled():
        polls = state_store.increment(key, "polls")
    else:
        _LOCAL[key] = _LOCAL.get(key, 0) + 1
        polls = _LOCAL[key]
    return CONFIRMED if polls >= _CONFIRM_POLLS else PENDING
