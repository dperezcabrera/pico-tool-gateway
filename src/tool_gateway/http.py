"""HTTP edge (pico-fastapi): a thin controller over ToolGateway. Three
endpoints mirror the gateway's own surface — call, decide, resume — and
nothing more; all the logic already lives in the pipeline."""

from fastapi import HTTPException
from pico_fastapi import controller, post

from .domain import Decision, DecisionStatus, GatewayError, ToolCall
from .gateway import Pending, ToolGateway, UnknownTicket
from .ports import TicketStore


def _result(r) -> dict:
    return {"status": "ok", "content": r.content, "is_error": r.is_error, "notes": r.notes}


@controller(prefix="/api/v1/tools", tags=["Tools"])
class ToolController:
    def __init__(self, gateway: ToolGateway):
        self._gw = gateway

    @post("/call")
    async def call(self, body: dict):
        call = ToolCall(
            request_id=body["request_id"],
            agent_id=body["agent_id"],
            upstream_id=body["upstream_id"],
            tool_name=body["tool_name"],
            arguments=body.get("arguments") or {},
        )
        try:
            outcome = await self._gw.call(call)
        except GatewayError as exc:
            raise HTTPException(422, str(exc)) from exc
        if isinstance(outcome, Pending):
            return {"status": "pending", "ticket_id": outcome.ticket_id}
        return _result(outcome)


@controller(prefix="/api/v1/tickets", tags=["Tickets"])
class TicketController:
    """decide records the human verdict (unblocking an interactive call);
    resume executes an approved async ticket."""

    def __init__(self, gateway: ToolGateway, tickets: TicketStore):
        self._gw = gateway
        self._tickets = tickets

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

    @post("/{ticket_id}/resume")
    async def resume(self, ticket_id: str):
        try:
            result = await self._gw.resume(ticket_id)
        except UnknownTicket as exc:
            raise HTTPException(404, "no such ticket") from exc
        except GatewayError as exc:
            raise HTTPException(422, str(exc)) from exc
        return _result(result)
