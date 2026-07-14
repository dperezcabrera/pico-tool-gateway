"""The composable pipeline: a chain of steps around a tool call.

Each step has the shape ``async (ctx, call_next) -> ToolResult`` — the same
before/after idiom as pico-ioc's AOP interceptors — so a step can act on the
way in (authorize, gate, validate) and on the way out (redact). Audit is a
wrapper applied at build time, not calls sprinkled through the logic.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .domain import GatewayError, Grant, ToolCall, ToolResult
from .ports import AuditLog


@dataclass
class CallContext:
    call: ToolCall
    audit: AuditLog
    grant: Grant | None = None
    bag: dict[str, Any] = field(default_factory=dict)  # steps stash cross-step data here


Next = Callable[[CallContext], Awaitable[ToolResult]]
Step = Callable[[CallContext, Next], Awaitable[ToolResult]]


class Pipeline:
    def __init__(self, steps: list[Step]):
        self._steps = steps

    async def run(self, ctx: CallContext) -> ToolResult:
        async def dispatch(i: int, ctx: CallContext) -> ToolResult:
            if i >= len(self._steps):
                raise RuntimeError("pipeline reached the end without a terminal step")
            return await self._steps[i](ctx, lambda c: dispatch(i + 1, c))

        return await dispatch(0, ctx)


def audited(step: Step, event: str) -> Step:
    """Wrap a step so its outcome is recorded once, uniformly: an ok event on
    success, an error event carrying the exception type on a GatewayError."""

    async def wrapper(ctx: CallContext, call_next: Next) -> ToolResult:
        try:
            result = await step(ctx, call_next)
        except GatewayError as exc:
            await ctx.audit.record(f"{event}.error", ctx.call, error=type(exc).__name__, detail=str(exc))
            raise
        return result

    return wrapper
