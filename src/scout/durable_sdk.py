"""Resilient resolution of Durable Execution SDK symbols.

The Lambda runtime provides the SDK, but its module layout and symbol names have
varied across versions (and differ from the published docs). Each symbol is
resolved from a list of known locations so module load tolerates those
differences. Both durable functions (orchestrator + matcher agent) import from
here instead of pinning exact SDK import paths.
"""

from __future__ import annotations


def resolve(*candidates):
    """Return the first importable ``(module, attribute)`` from candidates, or None."""
    for module_name, attr in candidates:
        try:
            module = __import__(module_name, fromlist=[attr])
            return getattr(module, attr)
        except (ImportError, AttributeError):
            continue
    return None


_PKG = "aws_durable_execution_sdk_python"

DurableContext = resolve((_PKG, "DurableContext"), (f"{_PKG}.context", "DurableContext"))
durable_execution = resolve(
    (_PKG, "durable_execution"), (f"{_PKG}.execution", "durable_execution")
)
Duration = resolve((f"{_PKG}.config", "Duration"))
StepConfig = resolve((f"{_PKG}.config", "StepConfig"))
# Plain json.dumps/json.loads, without the {"t": ..., "v": ...} type envelope the
# default ExtendedTypeSerDes writes for dicts and nested lists.
JsonSerDes = resolve((f"{_PKG}.serdes", "JsonSerDes"))
RetryStrategyConfig = resolve((f"{_PKG}.retries", "RetryStrategyConfig"))
create_retry_strategy = resolve((f"{_PKG}.retries", "create_retry_strategy"))
WaitForConditionConfig = resolve(
    (f"{_PKG}.waits", "WaitForConditionConfig"),
    (f"{_PKG}.config", "WaitForConditionConfig"),
)
WaitForConditionDecision = resolve(
    (f"{_PKG}.waits", "WaitForConditionDecision"),
    (f"{_PKG}.config", "WaitForConditionDecision"),
)
WaitForCallbackConfig = resolve(
    (f"{_PKG}.config", "WaitForCallbackConfig"),
    (f"{_PKG}.waits", "WaitForCallbackConfig"),
    (f"{_PKG}.types", "WaitForCallbackConfig"),
)
WaitForCallbackContext = resolve(
    (f"{_PKG}.types", "WaitForCallbackContext"),
    (f"{_PKG}.context", "WaitForCallbackContext"),
)

SDK_AVAILABLE = durable_execution is not None and DurableContext is not None

if not SDK_AVAILABLE:
    # Stand-ins so modules import outside a durable runtime (e.g. syntax checks).
    def durable_execution(fn):  # type: ignore[misc]
        return fn

    class DurableContext:  # type: ignore[no-redef]
        ...
