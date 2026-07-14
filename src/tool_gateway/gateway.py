"""ToolGateway: wires the steps into two pipelines and exposes the two
entry points that make async approval real.

- ``call(tool_call)``   runs the full pipeline. Auto/interactive finish
  inline; async returns ``Pending(ticket_id)`` instead of blocking.
- ``resume(ticket_id)`` runs the post-approval pipeline (no gate — the
  decision already exists) once a human approved an async ticket.

Both share the same steps, so the async path can never skip schema
validation, secret materialization or redaction.
"""

from dataclasses import dataclass

from .approval import ApprovalGate, apply_decision
from .domain import DecisionStatus, PendingApproval, ToolCall, ToolNotAllowed, ToolResult
from .pipeline import CallContext, Pipeline, Step, audited
from .ports import (
    AuditLog,
    GrantResolver,
    SchemaValidator,
    SecretResolver,
    TicketStore,
    Upstream,
)
from .steps import Authorize, Dispatch, MaterializeSecrets, Redact, ValidateSchema


@dataclass
class Pending:
    """Returned by ``call`` for an async tool call awaiting approval."""

    ticket_id: str


class UnknownTicket(Exception):
    pass


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
    ):
        self._tickets = tickets
        self._audit = audit
        self._grants = grants
        authorize: Step = audited(Authorize(grants), "authorize")
        gate: Step = audited(ApprovalGate(tickets, timeout_seconds=approval_timeout_seconds), "approval")
        validate: Step = audited(ValidateSchema(validator), "validate")
        materialize: Step = audited(MaterializeSecrets(secrets), "materialize")
        redact: Step = Redact(secrets)
        dispatch: Step = Dispatch(upstream)

        # gate runs before validate so an operator edit is validated too
        self._full = Pipeline([authorize, gate, validate, materialize, redact, dispatch])
        # resume: the decision is already applied by the caller; no gate
        self._post_approval = Pipeline([validate, materialize, redact, dispatch])

    async def call(self, tool_call: ToolCall) -> ToolResult | Pending:
        ctx = CallContext(call=tool_call, audit=self._audit)
        try:
            return await self._full.run(ctx)
        except PendingApproval as pending:
            return Pending(pending.ticket_id)

    async def resume(self, ticket_id: str) -> ToolResult:
        loaded = await self._tickets.get(ticket_id)
        if loaded is None:
            raise UnknownTicket(ticket_id)
        call, decision = loaded
        if decision.status is DecisionStatus.PENDING:
            raise PendingApproval(ticket_id)
        # re-resolve the grant so schema/mode reflect current policy, not a
        # snapshot taken when the ticket was filed
        grant = await self._grants.resolve(call)
        if grant is None:
            raise ToolNotAllowed(f"no longer allowed: {call.full_name}")
        ctx = CallContext(call=call, audit=self._audit, grant=grant)
        apply_decision(ctx, decision)
        await self._audit.record("resumed", call, status=decision.status.value, approver=decision.approver)
        return await self._post_approval.run(ctx)
