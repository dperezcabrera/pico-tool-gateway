"""MCP servers as Upstream + ToolCatalog through the official SDK client,
and the whole path: annotations -> policy hints -> approval mode."""

import json

import pytest
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pico_ioc import DictSource, configuration, init

from tool_gateway import Pending, ToolCall, ToolGateway
from tool_gateway.adapters.mcp_upstreams import McpUpstreams
from tool_gateway.adapters.memory import (
    DictSecretResolver,
    ListAuditLog,
    MemoryTicketStore,
    MiniSchemaValidator,
)
from tool_gateway.domain import UpstreamUnavailable
from tool_gateway.policy import DeclarativePolicy
from tool_gateway.ports import ToolCatalog, Upstream

pytestmark = pytest.mark.asyncio


def bank() -> MCPServer:
    server = MCPServer("bank")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def balance(account: str) -> int:
        """Current balance in cents."""
        return 1200

    @server.tool(annotations=ToolAnnotations(destructive_hint=True))
    def wire(account: str, cents: int, ctx: Context) -> dict:
        """Send money out."""
        return {"sent": cents, "by": ctx.request_context.meta["agent_id"]}

    @server.tool()
    def broken() -> str:
        raise RuntimeError("ledger offline")

    return server


def call(tool: str, agent: str = "agent-1", **args) -> ToolCall:
    upstream, _, name = tool.partition(".")
    return ToolCall("r1", agent, upstream, name, args)


async def test_catalog_lists_prefixed_tools_with_their_annotations():
    tools = {t["name"]: t for t in await McpUpstreams({"bank": bank()}).tools_for("agent-1")}
    assert set(tools) == {"bank.balance", "bank.wire", "bank.broken"}
    assert tools["bank.balance"]["description"] == "Current balance in cents."
    assert tools["bank.balance"]["annotations"] == {"readOnlyHint": True}
    assert tools["bank.wire"]["annotations"] == {"destructiveHint": True}
    assert tools["bank.broken"]["annotations"] == {}
    assert tools["bank.wire"]["inputSchema"]["required"] == ["account", "cents"]


async def test_call_tool_runs_the_tool_and_passes_the_verified_agent():
    result = await McpUpstreams({"bank": bank()}).call_tool(call("bank.wire", agent="agent-7", account="a", cents=5))
    assert result.is_error is False
    assert json.loads(result.content) == {"sent": 5, "by": "agent-7"}  # untyped dict: text content


async def test_tool_failure_is_an_error_result():
    result = await McpUpstreams({"bank": bank()}).call_tool(call("bank.broken"))
    assert result.is_error is True
    assert result.content == "Error executing tool broken"  # the server hides internal exception text


async def test_unknown_upstream_is_named():
    with pytest.raises(UpstreamUnavailable, match="no upstream 'crm'"):
        await McpUpstreams({"bank": bank()}).call_tool(call("crm.lookup"))


async def test_unreachable_upstream_fails_the_listing_loudly():
    with pytest.raises(UpstreamUnavailable, match="upstream 'down' did not list its tools"):
        await McpUpstreams({"down": "http://127.0.0.1:9/mcp"}).tools_for("agent-1")


async def test_listing_is_reused_within_the_ttl():
    server = bank()
    upstreams = McpUpstreams({"bank": server}, ttl_seconds=60)
    first = await upstreams.tools_for("agent-1")
    server.add_tool(lambda: "new", name="late")
    assert await upstreams.tools_for("agent-1") == first
    fresh = McpUpstreams({"bank": server}, ttl_seconds=0)
    assert "bank.late" in {t["name"] for t in await fresh.tools_for("agent-1")}


def gateway(upstreams: McpUpstreams) -> ToolGateway:
    policy = DeclarativePolicy(
        rules=[
            {"tool": "*", "hints": {"readOnlyHint": True}, "mode": "auto"},
            {"tool": "*", "hints": {"destructiveHint": True}, "mode": "async"},
        ],
        trust_hints_from=["bank"],
    )
    return ToolGateway(
        grants=policy,
        validator=MiniSchemaValidator(),
        secrets=DictSecretResolver(),
        upstream=upstreams,
        tickets=MemoryTicketStore(),
        audit=ListAuditLog(),
    )


async def annotated(upstreams: McpUpstreams, tool: str, **args) -> ToolCall:
    c = call(tool, **args)
    spec = next(t for t in await upstreams.tools_for("agent-1") if t["name"] == tool)
    c.annotations = spec["annotations"]
    return c


async def test_annotations_decide_the_approval_mode_end_to_end():
    upstreams = McpUpstreams({"bank": bank()})
    gw = gateway(upstreams)

    read = await gw.call(await annotated(upstreams, "bank.balance", account="a"), can_block=False)
    assert read.content == {"result": 1200}

    wire = await gw.call(await annotated(upstreams, "bank.wire", account="a", cents=5), can_block=False)
    assert isinstance(wire, Pending)


async def test_default_wiring_reads_upstreams_from_config():
    container = init(
        modules=["tool_gateway"],
        config=configuration(DictSource({"tool_gateway": {"upstreams": {"down": "http://127.0.0.1:9/mcp"}}})),
    )
    with pytest.raises(UpstreamUnavailable, match="upstream 'down'"):
        await container.get(ToolCatalog).tools_for("agent-1")
    container.shutdown()


async def test_default_wiring_without_upstreams_names_the_gap():
    container = init(modules=["tool_gateway"], config=configuration(DictSource({})))
    with pytest.raises(UpstreamUnavailable, match="no upstream 'github'"):
        await container.get(Upstream).call_tool(call("github.create_pr"))
    assert await container.get(ToolCatalog).tools_for("agent-1") == []
    container.shutdown()


class CountingClient:
    """Wraps the SDK client to count sessions and inject one transport failure."""

    opened = 0
    fail_next = False

    def __init__(self, target):
        from mcp import Client

        self._inner = Client(target)

    async def __aenter__(self):
        CountingClient.opened += 1
        self._client = await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)

    async def list_tools(self):
        return await self._client.list_tools()

    async def call_tool(self, *args, **kwargs):
        if CountingClient.fail_next:
            CountingClient.fail_next = False
            raise ConnectionError("connection reset by peer")
        return await self._client.call_tool(*args, **kwargs)


@pytest.fixture
def counting(monkeypatch):
    import tool_gateway.adapters.mcp_upstreams as module

    CountingClient.opened, CountingClient.fail_next = 0, False
    monkeypatch.setattr(module, "Client", CountingClient)
    return CountingClient


async def test_calls_share_one_session_per_upstream(counting):
    import asyncio

    upstreams = McpUpstreams({"bank": bank()})
    await upstreams.tools_for("agent-1")
    results = await asyncio.gather(*(upstreams.call_tool(call("bank.balance", account="a")) for _ in range(10)))
    assert all(r.content == {"result": 1200} for r in results)
    assert counting.opened == 1  # one handshake for the listing and ten concurrent calls
    upstreams.close()


async def test_a_failed_call_drops_the_session_and_the_next_reconnects(counting):
    upstreams = McpUpstreams({"bank": bank()})
    await upstreams.call_tool(call("bank.balance", account="a"))
    counting.fail_next = True
    with pytest.raises(ConnectionError):
        await upstreams.call_tool(call("bank.balance", account="a"))  # not retried: it may have run upstream
    result = await upstreams.call_tool(call("bank.balance", account="a"))
    assert result.content == {"result": 1200}
    assert counting.opened == 2
    upstreams.close()


async def test_a_tool_error_keeps_the_session(counting):
    upstreams = McpUpstreams({"bank": bank()})
    assert (await upstreams.call_tool(call("bank.broken"))).is_error
    await upstreams.call_tool(call("bank.balance", account="a"))
    assert counting.opened == 1
    upstreams.close()
