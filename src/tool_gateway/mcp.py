"""MCP surface: ``/mcp`` served by the official SDK, so any MCP client connects
(Claude Desktop, Cursor, ``mcp.Client``, a bare JSON-RPC POST).

Stateless with JSON responses: every request stands alone and carries its own
token, so replicas need no session affinity and the identity always comes from
the request being served. The agent identity is the VERIFIED token
(pico-client-auth's middleware fills the SecurityContext), never the body.

A gated tool does NOT block the agent: ``tools/call`` returns a *pending*
result at once, informing the agent that approval was requested, so it can
tell the user and move on. The agent later polls with the built-in
``gateway.check`` tool to fetch the result once a human decides.
"""

import asyncio
import json

import mcp_types as types
from fastapi import FastAPI
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from pico_client_auth import SecurityContext
from pico_ioc import cleanup, component
from starlette.routing import Route

from .domain import ApprovalDenied, DecisionStatus, GatewayError, PendingApproval, ToolCall, ToolResult
from .gateway import Pending, ToolGateway, UnknownTicket
from .ports import TicketStore, ToolCatalog

CHECK_TOOL = "gateway.check"

_CHECK_TOOL = types.Tool(
    name=CHECK_TOOL,
    description="Fetch the result of a tool call that was pending operator approval. "
    "Pass the ticket_id from a pending response.",
    input_schema={"type": "object", "required": ["ticket_id"], "properties": {"ticket_id": {"type": "string"}}},
    annotations=types.ToolAnnotations(read_only_hint=True),
)

# JSON-RPC error codes, unchanged from the hand-rolled edge this replaces
_INVALID_PARAMS, _GATEWAY_ERROR, _NO_TICKET = -32602, -32001, -32004


def _text(text: str, *, is_error: bool = False, meta: dict | None = None) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error, meta=meta)


def _tool_result(result: ToolResult) -> types.CallToolResult:
    content = result.content if isinstance(result.content, str) else json.dumps(result.content, default=str)
    blocks = [types.TextContent(type="text", text=note) for note in result.notes]
    blocks.append(types.TextContent(type="text", text=content))
    return types.CallToolResult(content=blocks, is_error=result.is_error)


def _pending(ticket_id: str) -> types.CallToolResult:
    return _text(
        f"This action requires operator approval. Request submitted (ticket {ticket_id}). "
        f"It is pending human review — tell the user, then call the '{CHECK_TOOL}' tool with "
        f'{{"ticket_id": "{ticket_id}"}} to retrieve the result once decided.',
        meta={"status": "pending_approval", "ticket_id": ticket_id},
    )


def _tool(spec: dict) -> types.Tool:
    hints = spec.get("annotations") or {}
    return types.Tool(
        name=spec["name"],
        description=spec.get("description") or "",
        input_schema=spec.get("inputSchema") or {"type": "object"},
        annotations=types.ToolAnnotations.model_validate(hints) if hints else None,
    )


class _Asgi:
    """Starlette routes a function or method as request/response; an object is
    routed as a raw ASGI app, which is what the SDK handler needs."""

    def __init__(self, handler):
        self._handler = handler

    async def __call__(self, scope, receive, send) -> None:
        await self._handler(scope, receive, send)


@component
class McpEdge:
    """Registers ``/mcp`` on the FastAPI app (a pico-fastapi configurer).

    The SDK's request handling needs its session manager running; pico-fastapi
    owns the app lifespan, so a small owner task starts the manager on the
    first request and keeps it running until the container shuts down.
    """

    priority = 0

    def __init__(self, gateway: ToolGateway, catalog: ToolCatalog, tickets: TicketStore):
        self._gw = gateway
        self._catalog = catalog
        self._tickets = tickets
        server = Server("pico-tool-gateway", on_list_tools=self._list_tools, on_call_tool=self._call_tool)
        self._manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready: asyncio.Future | None = None
        self._stop: asyncio.Event | None = None

    def configure_app(self, app: FastAPI) -> None:
        # an exact route, not a mount: a mount redirects /mcp to /mcp/ and plain
        # JSON-RPC clients do not follow redirects
        app.router.routes.append(Route("/mcp", endpoint=_Asgi(self._asgi), methods=["GET", "POST", "DELETE"]))

    async def _asgi(self, scope, receive, send) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:  # the manager is bound to the loop it started on
            self._loop, self._ready, self._stop = loop, loop.create_future(), asyncio.Event()
            loop.create_task(self._own(self._ready, self._stop))
        await self._ready
        await self._manager.handle_request(scope, receive, send)

    async def _own(self, ready: asyncio.Future, stop: asyncio.Event) -> None:
        try:
            async with self._manager.run():
                ready.set_result(None)
                await stop.wait()
        except BaseException as exc:  # noqa: BLE001
            if not ready.done():
                ready.set_exception(exc)

    @cleanup
    def _shutdown(self) -> None:
        if self._stop is not None:
            self._stop.set()

    async def _list_tools(self, ctx, params) -> types.ListToolsResult:
        agent_id = SecurityContext.require().sub
        specs = await self._catalog.tools_for(agent_id)
        return types.ListToolsResult(tools=[*(_tool(s) for s in specs), _CHECK_TOOL])

    async def _call_tool(self, ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        agent_id = SecurityContext.require().sub  # verified identity, not self-asserted
        arguments = params.arguments or {}
        if params.name == CHECK_TOOL:
            return await self._check(agent_id, arguments)
        return await self._call(str(ctx.request_id), agent_id, params.name, arguments)

    async def _call(self, request_id: str, agent_id: str, full_name: str, arguments: dict) -> types.CallToolResult:
        if "." not in full_name:
            raise MCPError(_INVALID_PARAMS, f"tool name must be 'upstream.tool', got {full_name!r}")
        upstream_id, _, tool_name = full_name.partition(".")
        spec = next((t for t in await self._catalog.tools_for(agent_id) if t.get("name") == full_name), {})
        call = ToolCall(
            request_id=request_id,
            agent_id=agent_id,
            upstream_id=upstream_id,
            tool_name=tool_name,
            arguments=arguments,
            annotations=spec.get("annotations") or {},
        )
        try:
            outcome = await self._gw.call(call, can_block=False)  # never hold the agent
        except GatewayError as exc:
            raise MCPError(_GATEWAY_ERROR, str(exc)) from exc
        if isinstance(outcome, Pending):
            return _pending(outcome.ticket_id)
        return _tool_result(outcome)

    async def _check(self, agent_id: str, arguments: dict) -> types.CallToolResult:
        ticket_id = arguments.get("ticket_id", "")
        ticket = await self._tickets.get(ticket_id)
        if ticket is None or ticket.call.agent_id != agent_id:  # an agent can only check its own tickets
            raise MCPError(_NO_TICKET, f"no such ticket: {ticket_id}")
        decision = ticket.decision
        if decision.status is DecisionStatus.PENDING:
            return _pending(ticket_id)
        try:
            result = await self._gw.resume(ticket_id)
        except PendingApproval:  # approved, and another check is executing it right now
            return _pending(ticket_id)
        except ApprovalDenied as exc:
            return _text(f"Approval denied: {exc}", is_error=True, meta={"status": decision.status.value})
        except (UnknownTicket, GatewayError) as exc:
            raise MCPError(_GATEWAY_ERROR, str(exc)) from exc
        return _tool_result(result)
