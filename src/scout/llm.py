"""LLM wrapper around Amazon Bedrock (Converse API).

The agent calls :func:`model_turn` once per reasoning round, inside a durable
``context.step`` (see ``handler.py``), so each model response is checkpointed and
reused on replay — that's what keeps the durable execution deterministic despite
the model being non-deterministic in general.

Configuration (environment variables):
* ``SCOUT_BEDROCK_MODEL_ID`` — model or inference-profile id to invoke.
* ``AWS_REGION`` — region for the Bedrock runtime client (set by Lambda).
"""

from __future__ import annotations

import os
from typing import Any

import boto3

from . import agent_tools

_MODEL_ID = os.environ.get(
    "SCOUT_BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
_REGION = os.environ.get("AWS_REGION", "us-east-1")

# The agent's system prompt. Lives with the model call it configures so the
# handler's loop stays focused on the durable primitives.
_SYSTEM = (
    "You are the Expo Booth Scout, an autonomous agent helping a re:Invent "
    "attendee plan their expo floor visit. Work in this order using your tools:\n"
    "1. Call profile_interests with the attendee's description.\n"
    "2. Call search_catalog with the interests you extracted.\n"
    "3. Optionally call get_booth_details to inspect promising booths (including "
    "the swag they offer).\n"
    "4. Call plan_route with your chosen booths (with their locations) to order them.\n"
    "5. Build a hit list (each stop with its swag and 2-3 talking_points) and call "
    "request_approval to ask the attendee.\n"
    "6. If the attendee approved, call reserve_swags with the approved stops to "
    "reserve each stop's swag, then call finalize_guide with status CONFIRMED and "
    "those stops as the guide. If they rejected it, skip reserve_swags and call "
    "finalize_guide with status REJECTED.\n"
    "Only call finalize_guide once. Keep talking points concrete and tailored to "
    "the attendee's interests. When you mention an AWS service, use its full "
    "official name, e.g. AWS Lambda, AWS Lambda durable functions, AWS Step "
    "Functions, Amazon EventBridge, Amazon Bedrock, Amazon DynamoDB, Amazon Kinesis."
)


def model_turn(messages: list[dict[str, Any]], logger=None) -> dict[str, Any]:
    """One agent turn: send the conversation to the model, return its message.

    Wraps :func:`converse_with_tools` with the agent's system prompt and tool
    config, and returns just the assistant message (tool calls or a final
    answer). The handler runs this inside a ``context.step`` so it's checkpointed.
    Pass the step's logger to get a log line tagged with the step's name — that
    line shows which turns ran in which invocation (replayed turns stay silent).
    """
    response = converse_with_tools(_SYSTEM, messages, agent_tools.tool_config())
    if logger is not None:
        logger.info(f"[agent] model turn: stop_reason={response.get('stopReason')}")
    return response["output"]["message"]


class LLMError(RuntimeError):
    """Raised when a Bedrock call fails. Retryable by the durable step layer."""



def converse_with_tools(
    system_prompt: str,
    messages: list[dict[str, Any]],
    tool_config: dict[str, Any],
) -> dict[str, Any]:
    """One turn of a Converse tool-use conversation.

    Returns the raw Converse response so the caller can inspect ``stopReason``
    and any ``toolUse`` blocks. Called by :func:`model_turn`.
    """
    client = boto3.client("bedrock-runtime", region_name=_REGION)
    try:
        return client.converse(
            modelId=_MODEL_ID,
            system=[{"text": system_prompt}],
            messages=messages,
            toolConfig=tool_config,
            inferenceConfig={"maxTokens": 2048, "temperature": 0},
        )
    except client.exceptions.ResourceNotFoundException as exc:
        raise LLMError(
            f"Bedrock model '{_MODEL_ID}' not usable in region '{_REGION}'. "
            f"Original error: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - normalize to a retryable error
        raise LLMError(f"Bedrock converse (tools) failed: {exc}") from exc


def tool_uses(message: dict[str, Any]) -> list[dict[str, Any]]:
    """The tool calls the model requested in a Converse assistant message."""
    return [c["toolUse"] for c in message.get("content", []) if "toolUse" in c]

