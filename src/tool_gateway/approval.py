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

import time
import uuid

from .domain import (
    ApprovalDenied,
    ApprovalMode,
    Decision,
    DecisionStatus,
    GatewayError,
    PendingApproval,
    ToolResult,
)
from .pipeline import CallContext, Next
from .ports import ApproverNotifier, DecisionSignal, TicketStore


def _new_ticket_id() -> str:
    # never derived from the request: an MCP request id is the client's JSON-RPC
    # counter, repeated across calls and agents
    return f"tkt-{uuid.uuid4().hex}"


async def run_once(tickets: TicketStore, ticket_id: str, run) -> ToolResult:
    """Execute an approved ticket at most once and keep the outcome on it, so
    a repeated check or resume returns the stored result instead of calling
    the tool again. A failure is stored too: it is not retried behind the
    operator's back."""
    if not await tickets.claim(ticket_id):
        ticket = await tickets.get(ticket_id)
        if ticket is not None and ticket.result is not None:
            return ticket.result
        raise PendingApproval(ticket_id)  # another caller is executing it right now
    try:
        result = await run()
    except GatewayError as exc:
        result = ToolResult(content=str(exc), is_error=True)
        await tickets.complete(ticket_id, result)
        raise
    await tickets.complete(ticket_id, result)
    return result


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
    def __init__(
        self,
        tickets: TicketStore,
        signal: DecisionSignal,
        *,
        timeout_seconds: float = 300,
        recheck_seconds: float = 5.0,
        notifier: ApproverNotifier | None = None,
    ):
        self._tickets = tickets
        self._signal = signal
        self._timeout = timeout_seconds
        self._recheck = recheck_seconds
        self._notifier = notifier

    async def _await_decision(self, ticket_id: str) -> Decision:
        """Sleep on the signal, reread the ticket on every wake; the periodic
        recheck covers a signal lost between replicas."""
        deadline = time.monotonic() + self._timeout
        while True:
            decision = (await self._tickets.get(ticket_id)).decision
            if decision.status is not DecisionStatus.PENDING:
                return decision
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return Decision(status=DecisionStatus.TIMEOUT)
            await self._signal.wait_for_decision(ticket_id, timeout_seconds=min(remaining, self._recheck))

    async def handle_call(self, ctx: CallContext, call_next: Next):
        mode = ctx.grant.approval_mode if ctx.grant else ApprovalMode.AUTO
        if mode is ApprovalMode.AUTO:
            return await call_next(ctx)

        ticket_id = _new_ticket_id()
        await self._tickets.create(ticket_id, ctx.call)
        await ctx.audit.audit_event("gated", ctx.call, approval_mode=mode.value, ticket_id=ticket_id)
        if self._notifier is not None:
            try:
                await self._notifier.notify_approvers(ticket_id, ctx.call, mode)
            except Exception as exc:  # noqa: BLE001
                # the ticket exists and is listed for operators; a broken channel must not lose the call
                await ctx.audit.audit_event("notify.error", ctx.call, ticket_id=ticket_id, error=str(exc))

        # async always hands back a ticket; interactive blocks only if the
        # caller can wait. A non-blocking caller (MCP) gets a ticket for
        # either mode and drives the wait itself.
        if mode is ApprovalMode.ASYNC or not ctx.can_block:
            raise PendingApproval(ticket_id)

        decision = await self._await_decision(ticket_id)
        if decision.status is DecisionStatus.TIMEOUT and not await self._tickets.decide(ticket_id, decision):
            # an operator decided right at the deadline: their verdict stands
            decision = (await self._tickets.get(ticket_id)).decision
        await ctx.audit.audit_event("decided", ctx.call, status=decision.status.value, approver=decision.approver)
        apply_decision(ctx, decision)
        return await run_once(self._tickets, ticket_id, lambda: call_next(ctx))
