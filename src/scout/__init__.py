"""AI Expo Booth Scout — one AI agent built on an AWS Lambda durable function.

Session: re:Invent API316 "Coding an AI agent from scratch with AWS Lambda
durable functions".

The Scout is a single tool-using agent (``handler.py``) whose reasoning loop runs
inside one durable function. Its tools live in ``agent_tools.py``; the model runs
via ``llm.py`` (Bedrock Converse). Durable primitives — steps, a human-approval
callback, and external-system condition polling — all appear inside that one loop.
"""

__all__ = ["agent_tools", "catalog", "llm", "state_store"]
