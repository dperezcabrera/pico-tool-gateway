"""Operator HTTP surface (pico-fastapi): recording approval decisions and
resuming approved async tickets. Gated to the ``operator`` role — a second
auth plane, distinct from the agent identity that drives ``/mcp``. Agents
never reach these; humans (or an operator UI) do.
"""

from fastapi import HTTPException
from pico_client_auth import requires_role
from pico_fastapi import controller, post

from .domain import Decision, DecisionStatus, GatewayError
from .gateway import ToolGateway, UnknownTicket
from .ports import TicketStore


@controller(prefix="/api/v1/tickets", tags=["Tickets"])
class TicketController:
    def __init__(self, gateway: ToolGateway, tickets: TicketStore):
        self._gw = gateway
        self._tickets = tickets

    @requires_role("operator")
    @post("/{ticket_id}/decide")
    async def decide(self, ticket_id: str, body: dict):
        decision = Decision(
            status=DecisionStatus(body.get("status", "rejected")),
            approver=body.get("approver", ""),
            reason=body.get("reason", ""),
            edited_arguments=body.get("edited_arguments"),
        )
        await self._tickets.decide(ticket_id, decision)
        return {"status": decision.status.value}

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
