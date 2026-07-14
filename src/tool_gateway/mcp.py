"""MCP surface: a JSON-RPC ``POST /mcp`` an agent's MCP client connects to.

The agent identity comes from the VERIFIED token (pico-client-auth populates
the SecurityContext), never from the request body — an agent cannot claim to
be another. ``tools/call`` runs through the gateway with ``block_async=True``
so a gated call blocks until decided (MCP is synchronous request/response).
"""

from typing import Any

from pico_client_auth import SecurityContext
from pico_fastapi import controller, post

from .domain import GatewayError, ToolCall
from .gateway import Pending, ToolGateway
from .ports import ToolCatalog


def _error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _result(rid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


@controller(prefix="/mcp", tags=["MCP"])
class McpController:
    def __init__(self, gateway: ToolGateway, catalog: ToolCatalog):
        self._gw = gateway
        self._catalog = catalog

    @post("")
    async def rpc(self, body: dict):
        rid = body.get("id")
        method = body.get("method", "")
        params = body.get("params") or {}
        agent_id = SecurityContext.require().sub  # verified identity, not self-asserted

        if method == "tools/list":
            return _result(rid, {"tools": await self._catalog.tools_for(agent_id)})

        if method == "tools/call":
            full_name = params.get("name", "")
            if "." not in full_name:
                return _error(rid, -32602, f"tool name must be 'upstream.tool', got {full_name!r}")
            upstream_id, _, tool_name = full_name.partition(".")
            call = ToolCall(
                request_id=str(rid),
                agent_id=agent_id,
                upstream_id=upstream_id,
                tool_name=tool_name,
                arguments=params.get("arguments") or {},
            )
            try:
                outcome = await self._gw.call(call, block_async=True)
            except GatewayError as exc:
                return _error(rid, -32001, str(exc))
            # block_async=True means we never get a handle back under MCP
            assert not isinstance(outcome, Pending)
            content = [{"type": "text", "text": note} for note in outcome.notes]
            content.append({"type": "text", "text": str(outcome.content)})
            return _result(rid, {"content": content, "isError": outcome.is_error})

        return _error(rid, -32601, f"method not found: {method}")
