"""The declarative policy engine: rule matching, conditions, first-match,
default, and hot reload."""

import json

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


async def test_publish_swaps_rules():
    p = DeclarativePolicy(default="deny", rules=[])
    assert await p.grant_for(call("github.get_pr")) is None
    await p.publish({"default": "deny", "rules": [{"tool": "github.*", "mode": "auto"}]})
    assert (await p.grant_for(call("github.get_pr"))).approval_mode is ApprovalMode.AUTO


def test_invalid_ruleset_fails_fast():
    with pytest.raises(PolicyError):
        DeclarativePolicy(rules=[{"tool": "*", "mode": "nonsense"}])
    with pytest.raises(PolicyError):
        DeclarativePolicy(rules=[{"tool": "*", "when": [{"arg": "x", "op": "??", "value": 1}], "mode": "auto"}])


HINT_RULES = [
    {"tool": "*", "hints": {"readOnlyHint": True}, "mode": "auto"},
    {"tool": "*", "hints": {"destructiveHint": False, "idempotentHint": True}, "mode": "auto"},
    {"tool": "*", "hints": {"destructiveHint": True}, "mode": "interactive"},
]


def annotated(tool, **hints):
    c = call(tool)
    c.annotations = hints
    return c


async def test_hints_route_by_tool_annotations():
    p = DeclarativePolicy(rules=HINT_RULES, trust_hints_from=["bank"])
    read = await p.grant_for(annotated("bank.balance", readOnlyHint=True))
    retry_safe = await p.grant_for(annotated("bank.tag", destructiveHint=False, idempotentHint=True))
    wire = await p.grant_for(annotated("bank.wire", destructiveHint=True))
    assert read.approval_mode is ApprovalMode.AUTO
    assert retry_safe.approval_mode is ApprovalMode.AUTO
    assert wire.approval_mode is ApprovalMode.INTERACTIVE


async def test_missing_hints_take_the_conservative_spec_defaults():
    p = DeclarativePolicy(rules=HINT_RULES)
    assert (await p.grant_for(annotated("bank.unknown"))).approval_mode is ApprovalMode.INTERACTIVE


async def test_read_only_is_never_destructive():
    # readOnlyHint without destructiveHint: the destructive default must not apply
    p = DeclarativePolicy(
        rules=[{"tool": "*", "hints": {"destructiveHint": True}, "mode": "interactive"}], trust_hints_from=["bank"]
    )
    assert await p.grant_for(annotated("bank.balance", readOnlyHint=True)) is None


def test_invalid_hints_fail_fast():
    with pytest.raises(PolicyError):
        DeclarativePolicy(rules=[{"tool": "*", "hints": {"readonly": True}, "mode": "auto"}])
    with pytest.raises(PolicyError):
        DeclarativePolicy(rules=[{"tool": "*", "hints": {"readOnlyHint": "yes"}, "mode": "auto"}])


async def test_hints_of_an_untrusted_upstream_take_the_conservative_defaults():
    # a server that claims read-only is not believed unless the policy trusts it
    p = DeclarativePolicy(rules=HINT_RULES, trust_hints_from=["bank"])
    liar = await p.grant_for(annotated("rogue.wipe", readOnlyHint=True))
    assert liar.approval_mode is ApprovalMode.INTERACTIVE


async def test_no_upstream_is_trusted_by_default():
    p = DeclarativePolicy(rules=HINT_RULES)
    assert (await p.grant_for(annotated("bank.balance", readOnlyHint=True))).approval_mode is ApprovalMode.INTERACTIVE


async def test_trust_takes_globs_and_reloads_from_the_file(tmp_path):
    import json

    doc = tmp_path / "policy.json"
    doc.write_text(json.dumps({"default": "deny", "trust_hints_from": ["internal-*"], "rules": HINT_RULES}))
    p = DeclarativePolicy(path=str(doc), refresh_seconds=0)
    trusted = await p.grant_for(annotated("internal-crm.lookup", readOnlyHint=True))
    assert trusted.approval_mode is ApprovalMode.AUTO
    doc.write_text(json.dumps({"default": "deny", "rules": HINT_RULES}))
    revoked = await p.grant_for(annotated("internal-crm.lookup", readOnlyHint=True))
    assert revoked.approval_mode is ApprovalMode.INTERACTIVE


AUTO_GITHUB = {"default": "deny", "rules": [{"tool": "github.*", "mode": "auto"}]}


async def test_replicas_sharing_a_source_converge_on_a_publish(tmp_path):
    from tool_gateway.policy import FilePolicySource

    path = str(tmp_path / "policy.json")
    a = DeclarativePolicy(source=FilePolicySource(path), refresh_seconds=0)
    b = DeclarativePolicy(source=FilePolicySource(path), refresh_seconds=0)
    assert await b.grant_for(call("github.get_pr")) is None  # no policy yet: deny
    version = await a.publish(AUTO_GITHUB, by="ops")
    assert (await b.grant_for(call("github.get_pr"))).approval_mode is ApprovalMode.AUTO
    assert b.current() == (version, AUTO_GITHUB)


async def test_an_invalid_publish_is_rejected_and_stores_nothing():
    from tool_gateway.policy import MemoryPolicySource

    source = MemoryPolicySource(AUTO_GITHUB)
    p = DeclarativePolicy(source=source)
    with pytest.raises(PolicyError):
        await p.publish({"rules": [{"tool": "*", "mode": "nonsense"}]})
    assert await source.load_policy(None) == ("1", AUTO_GITHUB)


async def test_a_broken_document_keeps_the_last_good_policy(tmp_path):
    doc = tmp_path / "policy.json"
    doc.write_text(json.dumps(AUTO_GITHUB))
    p = DeclarativePolicy(path=str(doc), refresh_seconds=0)
    assert (await p.grant_for(call("github.get_pr"))).approval_mode is ApprovalMode.AUTO
    doc.write_text("{ not json")  # someone edits the file by hand
    assert (await p.grant_for(call("github.get_pr"))).approval_mode is ApprovalMode.AUTO
    doc.write_text(json.dumps({"rules": [{"tool": "*", "mode": "nonsense"}]}))
    assert (await p.grant_for(call("github.get_pr"))).approval_mode is ApprovalMode.AUTO


async def test_the_source_is_asked_at_most_once_per_interval():
    from tool_gateway.policy import MemoryPolicySource

    class Counting(MemoryPolicySource):
        loads = 0

        async def load_policy(self, newer_than):
            Counting.loads += 1
            return await super().load_policy(newer_than)

    p = DeclarativePolicy(source=Counting(AUTO_GITHUB), refresh_seconds=60)
    for _ in range(100):
        await p.grant_for(call("github.get_pr"))
    assert Counting.loads == 1


async def test_may_call_decides_visibility_without_arguments():
    p = DeclarativePolicy(
        rules=[
            {"tool": "payments.charge", "when": [{"arg": "amount_cents", "op": "gt", "value": 10**6}], "deny": True},
            {"tool": "payments.*", "when": [{"arg": "amount_cents", "op": "le", "value": 100}], "mode": "auto"},
            {"tool": "github.get_*", "mode": "auto"},
            {"tool": "github.*", "deny": True},
        ]
    )
    assert await p.may_call(call("payments.charge"))  # a conditional deny may not apply; a conditional allow can
    assert await p.may_call(call("github.get_pr"))
    assert not await p.may_call(call("github.delete_repo"))  # unconditional deny
    assert not await p.may_call(call("slack.post"))  # nothing matches: default deny


async def test_may_call_follows_the_default_and_agent_rules():
    p = DeclarativePolicy(default="auto", rules=[{"tool": "*", "agent": "intern-*", "deny": True}])
    assert await p.may_call(call("x.y", agent="senior"))
    assert not await p.may_call(call("x.y", agent="intern-1"))
