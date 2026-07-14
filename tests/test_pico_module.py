"""The gateway works as a pico module in ONE process: init, resolve, call —
no companion services, in-process defaults for every port but Upstream."""

import sys

import pytest

from tool_gateway import ApprovalMode, Grant, ToolCall, ToolGateway
from tool_gateway.adapters.memory import EchoUpstream
from tool_gateway.domain import ToolResult

pytestmark = pytest.mark.asyncio


# A real Upstream and a grant policy: the two things an app provides. A plain
# @component wins over the library's on_missing default via structural match.
from pico_ioc import component  # noqa: E402


@component
class _TestUpstream(EchoUpstream):
    pass


@component
class _AllowAll:
    async def grant_for(self, call: ToolCall) -> Grant:
        return Grant(ApprovalMode.AUTO)


def _boot():
    from pico_ioc import DictSource, configuration, init

    return init(modules=["tool_gateway", sys.modules[__name__]], config=configuration(DictSource({})))


async def test_module_boots_and_resolves_gateway():
    container = _boot()
    gw = container.get(ToolGateway)
    assert isinstance(gw, ToolGateway)
    container.shutdown()


async def test_gateway_runs_a_call_in_one_process():
    container = _boot()
    gw = container.get(ToolGateway)
    result = await gw.call(ToolCall("r1", "agent-1", "github", "create_pr", {"title": "x"}))
    assert isinstance(result, ToolResult)
    assert result.content["echo"] == {"title": "x"}
    container.shutdown()
