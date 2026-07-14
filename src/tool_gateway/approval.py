"""The approval gate as a step, with three genuinely distinct flows.

The old gateway collapsed every non-auto mode into one blocking DB poll that
held the agent's HTTP request open for up to five minutes. Here:

- ``auto``        -> straight through.
- ``interactive`` -> create a ticket and block-await a bounded decision
                     (for a human expected to answer in seconds).
- ``async``       -> create a ticket and raise :class:`PendingApproval`; the
                     request returns a handle at once and execution resumes
                     via :meth:`ToolGateway.resume` on approval. No held
                     connection, survives a client disconnect.
"""

from .domain import (
    ApprovalDenied,
    ApprovalMode,
    Decision,
    PendingApproval,
)
from .pipeline import CallContext, Next
from .ports import TicketStore


def _new_ticket_id(call) -> str:
    # deterministic and readable: the request already carries a unique id
    return f"tkt-{call.request_id}"


def apply_decision(ctx: CallContext, decision: Decision) -> None:
    """Shared by interactive and resume: enforce the verdict and adopt any
    operator edit, leaving a note the agent will see with the result."""
    if not decision.approved:
        raise ApprovalDenied(decision)
    if decision.edited_arguments is not None and decision.edited_arguments != ctx.call.arguments:
        before, after = ctx.call.arguments, decision.edited_arguments
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        ctx.call.arguments = after
        ctx.bag["edit_note"] = (
            f"[note: operator {decision.approver or '?'} edited this call before approval; "
            f"executed with changed fields: {', '.join(changed)}]"
        )


class ApprovalGate:
    def __init__(self, tickets: TicketStore, *, timeout_seconds: float = 300):
        self._tickets = tickets
        self._timeout = timeout_seconds

    async def __call__(self, ctx: CallContext, call_next: Next):
        mode = ctx.grant.approval_mode if ctx.grant else ApprovalMode.AUTO
        if mode is ApprovalMode.AUTO:
            return await call_next(ctx)

        ticket_id = _new_ticket_id(ctx.call)
        await self._tickets.create(ticket_id, ctx.call)
        await ctx.audit.record("gated", ctx.call, approval_mode=mode.value, ticket_id=ticket_id)

        if mode is ApprovalMode.ASYNC:
            raise PendingApproval(ticket_id)

        decision = await self._tickets.await_decision(ticket_id, timeout_seconds=self._timeout)
        await ctx.audit.record("decided", ctx.call, status=decision.status.value, approver=decision.approver)
        apply_decision(ctx, decision)
        return await call_next(ctx)
