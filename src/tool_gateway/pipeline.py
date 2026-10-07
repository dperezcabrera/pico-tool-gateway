"""The composable pipeline: a chain of steps around a tool call.

Each step has the shape ``async (ctx, call_next) -> ToolResult`` — the same
before/after idiom as pico-ioc's AOP interceptors — so a step can act on the
way in (authorize, gate, validate) and on the way out (redact). Audit is a
wrapper applied at build time, not calls sprinkled through the logic.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .domain import GatewayError, Grant, ToolCall, ToolResult
from .ports import AuditLog


@dataclass
class CallContext:
    call: ToolCall
    audit: AuditLog
    grant: Grant | None = None
    can_block: bool = True  # False for callers that must not wait (MCP): any gated call returns a ticket
    bag: dict[str, Any] = field(default_factory=dict)  # steps stash cross-step data here


Next = Callable[[CallContext], Awaitable[ToolResult]]


class Stage(StrEnum):
    BEFORE_APPROVAL = "before_approval"  # first call only: authorize, rate limit, quotas
    AFTER_APPROVAL = "after_approval"  # every execution, including resume: validate, secrets, redact


class Pipeline:
    """Runs ``step.handle_call(ctx, call_next)`` in order; the last step is terminal."""

    def __init__(self, steps: list[Any]):
        self._steps = steps

    async def run(self, ctx: CallContext) -> ToolResult:
        async def dispatch(i: int, ctx: CallContext) -> ToolResult:
            if i >= len(self._steps):
                raise RuntimeError("pipeline reached the end without a terminal step")
            return await self._steps[i].handle_call(ctx, lambda c: dispatch(i + 1, c))

        return await dispatch(0, ctx)


class audited:
    """Wrap a step so its outcome is recorded once, uniformly: an error event
    carrying the exception type on a GatewayError. Keeps the step's place."""

    def __init__(self, step: Any, event: str):
        self._step = step
        self._event = event
        self.stage = getattr(step, "stage", None)
        self.order = getattr(step, "order", 0)

    async def handle_call(self, ctx: CallContext, call_next: Next) -> ToolResult:
        try:
            return await self._step.handle_call(ctx, call_next)
        except GatewayError as exc:
            await ctx.audit.audit_event(f"{self._event}.error", ctx.call, error=type(exc).__name__, detail=str(exc))
            raise
