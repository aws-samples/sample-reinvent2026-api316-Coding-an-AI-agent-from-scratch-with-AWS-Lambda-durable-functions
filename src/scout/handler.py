"""The Expo Booth Scout — one AI agent in one AWS Lambda durable function.

This is the whole demo (re:Invent API316). It is a single tool-use agent: one
reasoning loop that calls Amazon Bedrock, the model picks a tool, we execute it,
feed the result back, and repeat until the agent finalizes the guide. There is
no external orchestrator — the agent *is* the program.

Durable primitives inside the loop:

* ``context.step`` — every model turn and every tool call is checkpointed, so a
  crash mid-loop replays and resumes at the exact turn it died (zero duplicate
  model calls or tool executions).
* ``context.wait_for_callback`` — the ``request_approval`` tool suspends the
  agent (zero compute) until the attendee approves or rejects.
* ``context.wait_for_condition`` — the ``reserve_swags`` tool reserves swag with an
  external (asynchronous) swag service, then polls it until every swag
  reservation is confirmed. The function suspends between polls, so waiting costs nothing.

The file is ordered for reading top-to-bottom: setup, then ``handler`` (the
loop), then each piece the loop calls in the order it calls them, then small
utilities at the end.
"""

from __future__ import annotations

import os
from typing import Any

from . import agent_tools, llm, state_store, swag_service
from .agent_support import (
    is_approved, json_formatter, poll_swags, publish_failure, publish_final, publish_state,
)
from .swag_service import Booth, SwagReservation
from .durable_sdk import (
    DurableContext,
    Duration,
    StepConfig,
    WaitForCallbackConfig,
    WaitForCallbackContext,
    WaitForConditionConfig,
    WaitForConditionDecision,
    create_retry_strategy,
    durable_execution,
    RetryStrategyConfig,
)

# ---------------------------------------------------------------------------
# Setup: config. (The agent's system prompt lives in llm.py, next to the model call.)
# ---------------------------------------------------------------------------
_MAX_TURNS = int(os.environ.get("SCOUT_AGENT_MAX_TURNS", "16"))
_TOOL_MAX_ATTEMPTS = int(os.environ.get("SCOUT_TOOL_MAX_ATTEMPTS", "4"))
_POLL_MAX_ATTEMPTS = int(os.environ.get("SCOUT_POLL_MAX_ATTEMPTS", "10"))
_POLL_SECONDS = int(os.environ.get("SCOUT_POLL_SECONDS", "5"))
_APPROVAL_TIMEOUT_HOURS = int(os.environ.get("SCOUT_APPROVAL_TIMEOUT_HOURS", "2"))


def _tool_retry():
    """Exponential-backoff retry applied to every tool step.

    A realistic production pattern — external calls can fail transiently — so
    every tool step retries automatically instead of failing the whole agent.
    """
    return create_retry_strategy(
        RetryStrategyConfig(
            max_attempts=_TOOL_MAX_ATTEMPTS,
            initial_delay=Duration.from_seconds(1),
            max_delay=Duration.from_seconds(30),
            backoff_rate=2.0,
        )
    )


def _poll_until_confirmed(reservations: list[SwagReservation], attempts_made: int):
    """Wait strategy for the swag poll — the polling twin of ``_tool_retry``.

    The SDK calls this after each check: stop, or suspend for a while and check
    again. (Hand-written because the Lambda runtime's ``create_wait_strategy``
    returns a decision type its own ``wait_for_condition`` can't read.)
    """
    if all(r["status"] == swag_service.CONFIRMED for r in reservations):
        return WaitForConditionDecision.stop_polling()
    if attempts_made >= _POLL_MAX_ATTEMPTS:
        raise RuntimeError(f"swag reservations not confirmed after {attempts_made} polls")
    return WaitForConditionDecision.continue_waiting(Duration.from_seconds(_POLL_SECONDS))


# ---------------------------------------------------------------------------
# The agent — one durable tool-use loop.
# ---------------------------------------------------------------------------
@durable_execution
def handler(event: dict[str, Any], context: "DurableContext") -> dict[str, Any]:
    """Run the single Expo Booth Scout agent loop.

    Event: ``{"description", "attendee_name"?, "max_booths"?, "_execution_id"?}``.

    The whole body is guarded: any unhandled error publishes a terminal
    ``FAILED`` state and re-raises, so the durable service records the failure
    while the web UI stops polling a run that is never coming back. ``except
    Exception`` is safe — the SDK's suspend signals derive from ``BaseException``,
    so this never interferes with the approval gate suspending.
    """
    execution_id = event.get("_execution_id") or getattr(context, "execution_id", "local-exec")
    try:
        result = _run_agent(event, context, execution_id)
    except Exception as exc:  # noqa: BLE001 - report terminal state, then fail
        publish_failure(context, execution_id, exc)
        raise
    # The loop publishes CONFIRMED/REJECTED via finalize_guide; only the rare
    # "ran out of turns" case needs a terminal state for the UI here.
    if result.get("status") == "INCOMPLETE":
        publish_state(context, execution_id, "incomplete",
                      status="INCOMPLETE", message=result.get("message", ""))
    return result


def _run_agent(event: dict[str, Any], context: "DurableContext", execution_id: str) -> dict[str, Any]:
    attendee = event.get("attendee_name", "Attendee")
    description = event.get("description", "")
    max_booths = int(event.get("max_booths", 5))

    messages: list[dict[str, Any]] = [{
        "role": "user",
        "content": [{"text": (
            f"Attendee: {attendee}\n"
            f"Description: {description}\n"
            f"Aim for about {max_booths} booths on the hit list."
        )}],
    }]

    for reasoning_round in range(1, _MAX_TURNS + 1):
        # 1) Ask the model what to do next — a checkpointed model turn.
        assistant_msg = context.step(
            lambda ctx, m=list(messages): llm.model_turn(m, ctx.logger),
            name=f"model_turn_{reasoning_round}",
            config=StepConfig(serdes=json_formatter()),
        )
        messages.append(assistant_msg)

        tool_uses = llm.tool_uses(assistant_msg)
        if not tool_uses:
            break  # model answered without a tool; nothing to execute

        # 2) If the model finalized, that's the terminal step — return the guide.
        finalize = next((tool_use for tool_use in tool_uses if tool_use["name"] == agent_tools.FINALIZE), None)
        if finalize is not None:
            return publish_final(context, execution_id, finalize.get("input") or {})

        # 3) Otherwise run each requested tool and feed the results back.
        #    Name each op "<round>_<index>" so the console groups tools by the
        #    reasoning round that requested them, and names stay unique.
        tool_result_content = []
        for tool_index, tool_use in enumerate(tool_uses):
            seq = f"{reasoning_round}_{tool_index}"
            result = _run_tool(context, execution_id, tool_use["name"], tool_use.get("input") or {}, seq)
            tool_result_content.append(
                {"toolResult": {"toolUseId": tool_use["toolUseId"], "content": [{"json": result}]}}
            )
        messages.append({"role": "user", "content": tool_result_content})

    # Loop exhausted without finalize.
    return {"attendee": attendee, "status": "INCOMPLETE",
            "message": "Agent did not finalize within the reasoning-round budget."}


# ---------------------------------------------------------------------------
# What the loop calls, in the order it calls them.
# ---------------------------------------------------------------------------
def _run_tool(context: "DurableContext", execution_id: str, name: str, args: dict[str, Any], seq: str) -> dict[str, Any]:
    """Dispatch one tool call to the right durable primitive."""
    # Every other tool is a checkpointed step that auto-retries with backoff.
    return context.step(
        lambda ctx, n=name, a=args: agent_tools.run_data_tool(n, a, ctx.logger),
        name=f"tool_{name}_{seq}",
    )


def _request_approval(context: "DurableContext", execution_id: str, hit_list: list[Booth]) -> dict[str, Any]:
    """The human-in-the-loop tool: suspend until the attendee approves/rejects."""
    def submit_approval(callback_id: str, cb_ctx: "WaitForCallbackContext") -> None:
        # Publish the hit list + callback id so the web UI can render the plan
        # and POST the approve/reject decision back through that callback id.
        context.logger.info(f"[agent] awaiting approval. callback_id={callback_id}")
        state_store.put(
            execution_id,
            status=state_store.STATUS_AWAITING_APPROVAL,
            hitList=hit_list,
            callbackId=callback_id,
        )

    approval = context.wait_for_callback(
        submitter=submit_approval,
        name="attendee_approval_gate",
        # The attendee has _APPROVAL_TIMEOUT_HOURS (default 2) to answer.
        config=WaitForCallbackConfig(timeout=Duration.from_hours(_APPROVAL_TIMEOUT_HOURS)),
    )
    return {"approved": is_approved(approval)}


def _reserve_swags(context: "DurableContext", execution_id: str, stops: list[Booth]) -> dict[str, Any]:
    """External polling: reserve the swag, then poll until the exhibitors confirm."""
    # 1) Submit the swag reservations — a side effect on an external system, so it's a
    #    checkpointed step. On replay it is NOT re-submitted.
    submitted = context.step(
        lambda ctx: swag_service.reserve(stops),
        name="swag_submit",
        config=StepConfig(retry_strategy=_tool_retry(), serdes=json_formatter()),
    )

    # 2) Poll until every swag reservation is CONFIRMED. Each check is checkpointed;
    #    between checks the function suspends — no compute while we wait.
    #    `check` takes the last state and returns the new one (each is checkpointed).
    confirmed = context.wait_for_condition(
        check=lambda reservations, check_ctx: poll_swags(
            execution_id, reservations, check_ctx.logger),
        config=WaitForConditionConfig(wait_strategy=_poll_until_confirmed, initial_state=submitted),
        name="swags_confirmed",
    )
    return {"reservations": confirmed}

