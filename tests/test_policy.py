"""The declarative policy engine: rule matching, conditions, first-match,
default, and hot reload."""

import pytest

from tool_gateway import ApprovalMode, ToolCall
from tool_gateway.policy import DeclarativePolicy, PolicyError

pytestmark = pytest.mark.asyncio


def call(tool="github.create_pr", agent="agent-1", **args):
    upstream, _, name = tool.partition(".")
    return ToolCall(request_id="r", agent_id=agent, upstream_id=upstream, tool_name=name, arguments=args)


async def test_default_deny():
    p = DeclarativePolicy(default="deny", rules=[])
    assert await p.grant_for(call()) is None


async def test_default_mode_when_no_rule_matches():
    p = DeclarativePolicy(default="auto", rules=[{"tool": "slack.*", "mode": "async"}])
    assert (await p.grant_for(call("github.x"))).approval_mode is ApprovalMode.AUTO


async def test_tool_glob_and_mode():
    p = DeclarativePolicy(
        rules=[{"tool": "github.get_*", "mode": "auto"}, {"tool": "*.delete_*", "mode": "interactive"}]
    )
    assert (await p.grant_for(call("github.get_pr"))).approval_mode is ApprovalMode.AUTO
    assert (await p.grant_for(call("github.delete_repo"))).approval_mode is ApprovalMode.INTERACTIVE
    assert await p.grant_for(call("github.create_pr")) is None  # no match, default deny


async def test_agent_glob():
    p = DeclarativePolicy(rules=[{"tool": "*", "agent": "trusted-*", "mode": "auto"}])
    assert (await p.grant_for(call(agent="trusted-bot"))).approval_mode is ApprovalMode.AUTO
    assert await p.grant_for(call(agent="random")) is None


async def test_arg_condition_threshold():
    p = DeclarativePolicy(
        rules=[
            {"tool": "payments.charge", "when": [{"arg": "amount_cents", "op": "le", "value": 10000}], "mode": "auto"},
            {"tool": "payments.charge", "mode": "interactive"},
        ]
    )
    small = await p.grant_for(call("payments.charge", amount_cents=5000))
    big = await p.grant_for(call("payments.charge", amount_cents=50000))
    assert small.approval_mode is ApprovalMode.AUTO
    assert big.approval_mode is ApprovalMode.INTERACTIVE


async def test_first_match_wins():
    p = DeclarativePolicy(
        rules=[{"tool": "github.*", "mode": "auto"}, {"tool": "github.delete_repo", "mode": "interactive"}]
    )
    # the broad auto rule comes first, so it wins even for delete
    assert (await p.grant_for(call("github.delete_repo"))).approval_mode is ApprovalMode.AUTO


async def test_explicit_deny_rule():
    p = DeclarativePolicy(default="auto", rules=[{"tool": "prod.*", "deny": True}])
    assert await p.grant_for(call("prod.wipe")) is None
    assert (await p.grant_for(call("dev.build"))).approval_mode is ApprovalMode.AUTO


async def test_missing_arg_fails_condition_closed():
    p = DeclarativePolicy(rules=[{"tool": "*", "when": [{"arg": "amount", "op": "lt", "value": 100}], "mode": "auto"}])
    assert await p.grant_for(call("x.y")) is None  # no 'amount' -> condition false -> no match


async def test_hot_reload_swaps_rules():
    p = DeclarativePolicy(default="deny", rules=[])
    assert await p.grant_for(call("github.get_pr")) is None
    p.reload(default="deny", rules=[{"tool": "github.*", "mode": "auto"}])
    assert (await p.grant_for(call("github.get_pr"))).approval_mode is ApprovalMode.AUTO


def test_invalid_ruleset_fails_fast():
    with pytest.raises(PolicyError):
        DeclarativePolicy(rules=[{"tool": "*", "mode": "nonsense"}])
    with pytest.raises(PolicyError):
        DeclarativePolicy(rules=[{"tool": "*", "when": [{"arg": "x", "op": "??", "value": 1}], "mode": "auto"}])
