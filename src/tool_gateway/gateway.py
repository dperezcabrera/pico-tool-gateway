"""ToolGateway: wires the steps into two pipelines and exposes the two
entry points that make async approval real.

- ``call(tool_call)``   runs the full pipeline. Auto/interactive finish
  inline; async returns ``Pending(ticket_id)`` instead of blocking.
- ``resume(ticket_id)`` runs the post-approval pipeline (no gate — the
  decision already exists) once a human approved an async ticket.

Both share the same steps, so the async path can never skip schema
validation, secret materialization or redaction.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from .approval import ApprovalGate, apply_decision, run_once
from .domain import Decision, DecisionStatus, PendingApproval, ToolCall, ToolNotAllowed, ToolResult
from .pipeline import CallContext, Pipeline, Stage, audited
from .ports import (
    ApproverNotifier,
    AuditLog,
    GatewayStep,
    GrantResolver,
    RateLimiter,
    SchemaValidator,
    SecretResolver,
    TicketStore,
    Upstream,
)
from .steps import Authorize, Dispatch, MaterializeSecrets, RateLimit, Redact, ValidateSchema


@dataclass
class Pending:
    """Returned by ``call`` for an async tool call awaiting approval."""

    ticket_id: str


class UnknownTicket(Exception):
    pass


class TicketAlreadyDecided(Exception):
    """A verdict is final: a decided ticket cannot be decided again."""


class ToolGateway:
    def __init__(
        self,
        *,
        grants: GrantResolver,
        validator: SchemaValidator,
        secrets: SecretResolver,
        upstream: Upstream,
        tickets: TicketStore,
        audit: AuditLog,
        approval_timeout_seconds: float = 300,
        notifier: ApproverNotifier | None = None,
        rate_limiter: RateLimiter | None = None,
        steps: Sequence[GatewayStep] = (),
    ):
        self._tickets = tickets
        self._audit = audit
        self._grants = grants
        builtin = [
            audited(Authorize(grants), "authorize"),
            audited(ValidateSchema(validator), "validate"),
            audited(MaterializeSecrets(secrets), "materialize"),
            Redact(secrets),
        ]
        if rate_limiter is not None:
            builtin.append(audited(RateLimit(rate_limiter), "rate_limit"))
        for step in steps:
            if step.stage not in (Stage.BEFORE_APPROVAL, Stage.AFTER_APPROVAL):
                raise ValueError(f"{type(step).__name__}.stage must be a tool_gateway.pipeline.Stage")
        chain = [*builtin, *(audited(s, f"step.{type(s).__name__}") for s in steps)]
        # sorted() is stable: at equal order a built-in runs before an extra step
        before = sorted((s for s in chain if s.stage is Stage.BEFORE_APPROVAL), key=lambda s: s.order)
        after = sorted((s for s in chain if s.stage is Stage.AFTER_APPROVAL), key=lambda s: s.order)
        gate = audited(ApprovalGate(tickets, timeout_seconds=approval_timeout_seconds, notifier=notifier), "approval")
        dispatch = Dispatch(upstream)

        # after-approval steps follow the gate so an operator edit is validated too
        self._full = Pipeline([*before, gate, *after, dispatch])
        # resume: the decision is already applied by the caller; no gate, no before-approval steps
        self._post_approval = Pipeline([*after, dispatch])

    async def call(self, tool_call: ToolCall, *, can_block: bool = True) -> ToolResult | Pending:
        """Run the full pipeline. ``can_block=False`` (non-blocking callers
        like MCP) returns a ``Pending`` handle for any gated call instead of
        waiting; the caller drives the wait itself."""
        ctx = CallContext(call=tool_call, audit=self._audit, can_block=can_block)
        try:
            return await self._full.run(ctx)
        except PendingApproval as pending:
            return Pending(pending.ticket_id)

    async def decide(self, ticket_id: str, decision: Decision) -> None:
        """Record an operator verdict on a pending ticket, once, and audit it.
        ``decision.approver`` must be the verified operator, not a claim."""
        if decision.status not in (DecisionStatus.APPROVED, DecisionStatus.REJECTED):
            raise ValueError(f"an operator can approve or reject, not {decision.status.value!r}")
        ticket = await self._tickets.get(ticket_id)
        if ticket is None:
            raise UnknownTicket(ticket_id)
        if not await self._tickets.decide(ticket_id, decision):
            raise TicketAlreadyDecided(ticket_id)
        await self._audit.audit_event(
            "decision",
            ticket.call,
            ticket_id=ticket_id,
            status=decision.status.value,
            approver=decision.approver,
            reason=decision.reason,
            edited=decision.edited_arguments is not None,
        )

    async def resume(self, ticket_id: str) -> ToolResult:
        ticket = await self._tickets.get(ticket_id)
        if ticket is None:
            raise UnknownTicket(ticket_id)
        if ticket.result is not None:
            return ticket.result  # already executed: never twice
        call, decision = ticket.call, ticket.decision
        if decision.status is DecisionStatus.PENDING:
            raise PendingApproval(ticket_id)
        # re-resolve the grant so schema/mode reflect current policy, not a
        # snapshot taken when the ticket was filed
        grant = await self._grants.grant_for(call)
        if grant is None:
            raise ToolNotAllowed(f"no longer allowed: {call.full_name}")
        ctx = CallContext(call=call, audit=self._audit, grant=grant)
        apply_decision(ctx, decision)
        await self._audit.audit_event("resumed", call, status=decision.status.value, approver=decision.approver)
        return await run_once(self._tickets, ticket_id, lambda: self._post_approval.run(ctx))
