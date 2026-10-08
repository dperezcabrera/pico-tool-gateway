"""Operator HTTP surface (pico-fastapi): recording approval decisions and
resuming approved async tickets. Gated to the ``operator`` role — a second
auth plane, distinct from the agent identity that drives ``/mcp``. Agents
never reach these; humans (or an operator UI) do.
"""

from fastapi import HTTPException
from pico_client_auth import SecurityContext, requires_role
from pico_fastapi import controller, get, post

from .domain import Decision, DecisionStatus, GatewayError
from .gateway import TicketAlreadyDecided, ToolGateway, UnknownTicket
from .policy import DeclarativePolicy, PolicyError
from .ports import GrantResolver, TicketStore

MAX_PAGE = 500


@controller(prefix="/api/v1/tickets", tags=["Tickets"])
class TicketController:
    def __init__(self, gateway: ToolGateway, tickets: TicketStore):
        self._gw = gateway
        self._tickets = tickets

    @requires_role("operator")
    @get("")
    async def pending(
        self, limit: int = 100, after: str | None = None, tool: str | None = None, agent_id: str | None = None
    ):
        """One page of the approval queue, oldest first: what a lost notification
        can still be found in. ``next`` is the cursor for the following page."""
        limit = max(1, min(limit, MAX_PAGE))
        page = await self._tickets.pending(limit=limit, after=after, tool=tool, agent_id=agent_id)
        items = [
            {
                "ticket_id": ticket_id,
                "agent_id": t.call.agent_id,
                "tool": t.call.full_name,
                "arguments": t.call.arguments,
                "annotations": t.call.annotations,
            }
            for ticket_id, t in page.items()
        ]
        return {"items": items, "next": items[-1]["ticket_id"] if len(items) == limit else None}

    @requires_role("operator")
    @post("/{ticket_id}/decide")
    async def decide(self, ticket_id: str, body: dict):
        try:
            decision = Decision(
                status=DecisionStatus(body.get("status", "")),
                approver=SecurityContext.require().sub,  # the verified operator, never a field in the body
                reason=body.get("reason", ""),
                edited_arguments=body.get("edited_arguments"),
            )
            await self._gw.decide(ticket_id, decision)
        except UnknownTicket as exc:
            raise HTTPException(404, "no such ticket") from exc
        except TicketAlreadyDecided as exc:
            raise HTTPException(409, "ticket already decided") from exc
        except ValueError as exc:
            raise HTTPException(422, "status must be 'approved' or 'rejected'") from exc
        return {"status": decision.status.value, "approver": decision.approver}

    @requires_role("operator")
    @post("/{ticket_id}/resume")
    async def resume(self, ticket_id: str):
        try:
            result = await self._gw.resume(ticket_id)
        except UnknownTicket as exc:
            raise HTTPException(404, "no such ticket") from exc
        except GatewayError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"status": "ok", "content": result.content, "is_error": result.is_error, "notes": result.notes}


@controller(prefix="/api/v1/policy", tags=["Policy"])
class PolicyController:
    """Read and publish the declarative policy. A publish is stored in the
    shared PolicySource, so every replica applies it within its refresh
    interval; the replica that took it applies it at once."""

    def __init__(self, grants: GrantResolver):
        self._grants = grants

    def _declarative(self) -> DeclarativePolicy:
        if not isinstance(self._grants, DeclarativePolicy):
            raise HTTPException(409, "the active GrantResolver is not the declarative policy")
        return self._grants

    @requires_role("operator")
    @get("")
    async def current(self):
        policy = self._declarative()
        await policy.refresh()
        version, doc = policy.current()
        return {"version": version, "policy": doc}

    @requires_role("operator")
    @post("")
    async def publish(self, body: dict):
        try:
            version = await self._declarative().publish(body, by=SecurityContext.require().sub)
        except PolicyError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"version": version}
