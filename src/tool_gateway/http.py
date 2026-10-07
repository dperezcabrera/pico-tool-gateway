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
from .policy import PolicyError
from .ports import GrantResolver, TicketStore


@controller(prefix="/api/v1/tickets", tags=["Tickets"])
class TicketController:
    def __init__(self, gateway: ToolGateway, tickets: TicketStore):
        self._gw = gateway
        self._tickets = tickets

    @requires_role("operator")
    @get("")
    async def pending(self):
        """The approval queue, oldest first: what a lost notification can still be found in."""
        return [
            {
                "ticket_id": ticket_id,
                "agent_id": t.call.agent_id,
                "tool": t.call.full_name,
                "arguments": t.call.arguments,
                "annotations": t.call.annotations,
            }
            for ticket_id, t in (await self._tickets.pending()).items()
        ]

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
    """Hot-reload the authorization policy without a restart. Push a new
    ruleset in the body, or re-read the policy file if none is given."""

    def __init__(self, grants: GrantResolver):
        self._grants = grants

    @requires_role("operator")
    @post("/reload")
    async def reload(self, body: dict | None = None):
        try:
            if body and "rules" in body:
                self._grants.reload(body.get("default", "deny"), body["rules"])
            elif hasattr(self._grants, "reload_from_file"):
                self._grants.reload_from_file()
            else:
                raise HTTPException(400, "no ruleset in body and no policy file configured")
        except PolicyError as exc:
            raise HTTPException(422, str(exc)) from exc
        except AttributeError as exc:  # a non-reloadable GrantResolver was wired
            raise HTTPException(409, "active policy source is not reloadable") from exc
        return {"status": "reloaded"}
