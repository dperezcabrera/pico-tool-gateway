"""MCP surface: a JSON-RPC ``POST /mcp`` an agent's MCP client connects to.

The agent identity comes from the VERIFIED token (pico-client-auth populates
the SecurityContext), never from the request body.

A gated tool does NOT block the agent: ``tools/call`` returns a *pending*
tool_result at once, informing the agent that approval was requested, so it
can tell the user and move on. The agent later polls with the built-in
``gateway.check`` tool to fetch the result once a human decides. MCP stays
synchronous on the wire; the approval is asynchronous for the agent.
"""

from typing import Any

from pico_client_auth import SecurityContext
from pico_fastapi import controller, post

from .domain import ApprovalDenied, DecisionStatus, GatewayError, ToolCall
from .gateway import Pending, ToolGateway, UnknownTicket
from .ports import TicketStore, ToolCatalog

CHECK_TOOL = "gateway.check"

_CHECK_SPEC = {
    "name": CHECK_TOOL,
    "description": "Fetch the result of a tool call that was pending operator approval. "
    "Pass the ticket_id from a pending response.",
    "inputSchema": {"type": "object", "required": ["ticket_id"], "properties": {"ticket_id": {"type": "string"}}},
}


def _error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _result(rid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _text_result(text: str, *, is_error: bool = False, meta: dict | None = None) -> dict:
    out: dict = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if meta:
        out["_meta"] = meta
    return out


def _tool_result(result) -> dict:
    content = [{"type": "text", "text": note} for note in result.notes]
    content.append({"type": "text", "text": str(result.content)})
    return {"content": content, "isError": result.is_error}


def _pending(ticket_id: str) -> dict:
    return _text_result(
        f"This action requires operator approval. Request submitted (ticket {ticket_id}). "
        f"It is pending human review — tell the user, then call the '{CHECK_TOOL}' tool with "
        f'{{"ticket_id": "{ticket_id}"}} to retrieve the result once decided.',
        meta={"status": "pending_approval", "ticket_id": ticket_id},
    )


@controller(prefix="/mcp", tags=["MCP"])
class McpController:
    def __init__(self, gateway: ToolGateway, catalog: ToolCatalog, tickets: TicketStore):
        self._gw = gateway
        self._catalog = catalog
        self._tickets = tickets

    @post("")
    async def rpc(self, body: dict):
        rid = body.get("id")
        method = body.get("method", "")
        params = body.get("params") or {}
        agent_id = SecurityContext.require().sub  # verified identity, not self-asserted

        if method == "tools/list":
            tools = await self._catalog.tools_for(agent_id)
            return _result(rid, {"tools": [*tools, _CHECK_SPEC]})

        if method == "tools/call":
            full_name = params.get("name", "")
            arguments = params.get("arguments") or {}
            if full_name == CHECK_TOOL:
                return await self._check(rid, agent_id, arguments)
            return await self._call(rid, agent_id, full_name, arguments)

        return _error(rid, -32601, f"method not found: {method}")

    async def _call(self, rid, agent_id: str, full_name: str, arguments: dict):
        if "." not in full_name:
            return _error(rid, -32602, f"tool name must be 'upstream.tool', got {full_name!r}")
        upstream_id, _, tool_name = full_name.partition(".")
        call = ToolCall(
            request_id=str(rid), agent_id=agent_id, upstream_id=upstream_id, tool_name=tool_name, arguments=arguments
        )
        try:
            outcome = await self._gw.call(call, can_block=False)  # never hold the agent
        except GatewayError as exc:
            return _error(rid, -32001, str(exc))
        if isinstance(outcome, Pending):
            return _result(rid, _pending(outcome.ticket_id))
        return _result(rid, _tool_result(outcome))

    async def _check(self, rid, agent_id: str, arguments: dict):
        ticket_id = arguments.get("ticket_id", "")
        loaded = await self._tickets.get(ticket_id)
        if loaded is None:
            return _error(rid, -32004, f"no such ticket: {ticket_id}")
        call, decision = loaded
        if call.agent_id != agent_id:  # an agent can only check its own tickets
            return _error(rid, -32004, f"no such ticket: {ticket_id}")
        if decision.status is DecisionStatus.PENDING:
            return _result(rid, _pending(ticket_id))
        try:
            result = await self._gw.resume(ticket_id)
        except ApprovalDenied as exc:
            return _result(
                rid, _text_result(f"Approval denied: {exc}", is_error=True, meta={"status": decision.status.value})
            )
        except (UnknownTicket, GatewayError) as exc:
            return _error(rid, -32001, str(exc))
        return _result(rid, _tool_result(result))
