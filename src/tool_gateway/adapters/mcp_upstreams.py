"""MCP servers as the gateway's Upstream and ToolCatalog, through the official
mcp SDK client. A server built with pico-mcp plugs in by URL, annotations
included, so policy ``hints`` see what each tool declares about itself."""

import asyncio
import time
from typing import Any

from mcp import Client

from ..domain import ToolCall, ToolResult, UpstreamUnavailable


class _Session:
    """One open ``Client`` per upstream, shared by concurrent calls.

    The SDK client must be entered and exited in the same task, so a small
    owner task opens it and parks until the session is discarded; callers use
    it from their own tasks. A failure discards the session and the next call
    reconnects. The failed call is not retried: it may have run upstream.
    """

    def __init__(self, target: Any):
        self._target = target
        self._client: Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._lock = asyncio.Lock()

    async def client(self) -> Client:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:  # a session never crosses event loops
            self._client, self._loop, self._lock = None, loop, asyncio.Lock()
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    ready: asyncio.Future = loop.create_future()
                    self._stop = asyncio.Event()
                    loop.create_task(self._own(ready, self._stop))
                    self._client = await ready
        return self._client

    async def _own(self, ready: asyncio.Future, stop: asyncio.Event) -> None:
        try:
            async with Client(self._target) as client:
                ready.set_result(client)
                await stop.wait()
        except BaseException as exc:  # noqa: BLE001
            if not ready.done():
                ready.set_exception(exc)
        finally:
            if self._stop is stop:
                self._client = None

    def discard(self) -> None:
        if self._stop is not None:
            self._stop.set()
        self._client = None


class McpUpstreams:
    """``targets`` maps upstream_id to anything ``mcp.Client`` connects to: a
    streamable HTTP URL in production, an ``MCPServer`` in tests.

    One session per upstream is opened on first use and shared by every call
    (no handshake per call); a failed call drops it and the next one
    reconnects. ``tools_for`` reuses a listing for ``ttl_seconds``.
    """

    def __init__(self, targets: dict[str, Any], *, ttl_seconds: float = 30.0):
        self._targets = dict(targets)
        self._ttl = ttl_seconds
        self._listing: list[dict[str, Any]] = []
        self._listed_at = float("-inf")
        self._sessions = {upstream_id: _Session(target) for upstream_id, target in self._targets.items()}

    def close(self) -> None:
        """Let every open session wind down (the owner tasks exit on their own)."""
        for session in self._sessions.values():
            session.discard()

    async def _use(self, upstream_id: str, operation):
        session = self._sessions[upstream_id]
        try:
            return await operation(await session.client())
        except BaseException:
            session.discard()
            raise

    async def tools_for(self, agent_id: str) -> list[dict[str, Any]]:
        # ponytail: one listing for every agent; the policy decides who may call what
        if time.monotonic() - self._listed_at > self._ttl:
            listing = []
            for upstream_id in self._targets:
                try:
                    tools = (await self._use(upstream_id, lambda c: c.list_tools())).tools
                except Exception as exc:  # noqa: BLE001
                    # fail loud: an empty catalog would hide a dead upstream
                    raise UpstreamUnavailable(f"upstream {upstream_id!r} did not list its tools: {exc}") from exc
                listing += [_spec(upstream_id, t) for t in tools]
            self._listing, self._listed_at = listing, time.monotonic()
        return self._listing

    async def call_tool(self, call: ToolCall) -> ToolResult:
        if call.upstream_id not in self._targets:
            raise UpstreamUnavailable(f"no upstream {call.upstream_id!r}: add it to tool_gateway.upstreams")
        result = await self._use(  # Dispatch reports any failure as UpstreamUnavailable
            call.upstream_id,
            lambda c: c.call_tool(call.tool_name, call.arguments, meta={"agent_id": call.agent_id}),
        )
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
