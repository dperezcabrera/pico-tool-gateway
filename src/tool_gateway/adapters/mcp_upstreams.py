"""MCP servers as the gateway's Upstream and ToolCatalog, through the official
mcp SDK client. A server built with pico-mcp plugs in by URL, annotations
included, so policy ``hints`` see what each tool declares about itself."""

import time
from typing import Any

from mcp import Client

from ..domain import ToolCall, ToolResult, UpstreamUnavailable


class McpUpstreams:
    """``targets`` maps upstream_id to anything ``mcp.Client`` connects to: a
    streamable HTTP URL in production, an ``MCPServer`` in tests.

    Each operation opens its own connection: no session state to lose when an
    upstream restarts. ``tools_for`` reuses a listing for ``ttl_seconds``.
    """

    def __init__(self, targets: dict[str, Any], *, ttl_seconds: float = 30.0):
        self._targets = dict(targets)
        self._ttl = ttl_seconds
        self._listing: list[dict[str, Any]] = []
        self._listed_at = float("-inf")

    async def tools_for(self, agent_id: str) -> list[dict[str, Any]]:
        # ponytail: one listing for every agent; the policy decides who may call what
        if time.monotonic() - self._listed_at > self._ttl:
            listing = []
            for upstream_id, target in self._targets.items():
                try:
                    async with Client(target) as client:
                        tools = (await client.list_tools()).tools
                except Exception as exc:  # noqa: BLE001
                    # fail loud: an empty catalog would hide a dead upstream
                    raise UpstreamUnavailable(f"upstream {upstream_id!r} did not list its tools: {exc}") from exc
                listing += [_spec(upstream_id, t) for t in tools]
            self._listing, self._listed_at = listing, time.monotonic()
        return self._listing

    async def call_tool(self, call: ToolCall) -> ToolResult:
        target = self._targets.get(call.upstream_id)
        if target is None:
            raise UpstreamUnavailable(f"no upstream {call.upstream_id!r}: add it to tool_gateway.upstreams")
        async with Client(target) as client:  # Dispatch reports any failure as UpstreamUnavailable
            result = await client.call_tool(call.tool_name, call.arguments, meta={"agent_id": call.agent_id})
        return ToolResult(content=_content(result), is_error=bool(result.is_error))


def _spec(upstream_id: str, tool) -> dict[str, Any]:
    return {
        "name": f"{upstream_id}.{tool.name}",
        "description": tool.description or "",
        "inputSchema": tool.input_schema,
        "annotations": tool.annotations.model_dump(by_alias=True, exclude_none=True) if tool.annotations else {},
    }


def _content(result) -> Any:
    if result.structured_content is not None:
        return result.structured_content
    return "\n".join(getattr(block, "text", "") for block in result.content)
