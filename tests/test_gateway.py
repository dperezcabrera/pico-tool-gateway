"""Every approval flow and every failure path, hermetic (in-memory adapters)."""

import asyncio

import pytest

from tool_gateway import (
    ApprovalMode,
    Decision,
    DecisionStatus,
    Grant,
    ToolCall,
    ToolGateway,
)
from tool_gateway.adapters.memory import (
    DictGrantResolver,
    DictSecretResolver,
    EchoUpstream,
    ListAuditLog,
    MemoryTicketStore,
    MiniSchemaValidator,
)
from tool_gateway.domain import ApprovalDenied, SchemaInvalid, SecretLeak, ToolNotAllowed
from tool_gateway.gateway import Pending


def build(*, grants=None, secrets=None, leak=None, echo=True):
    grant_resolver = grants or DictGrantResolver()
    secret_resolver = secrets or DictSecretResolver()
    audit = ListAuditLog()
    tickets = MemoryTicketStore()
    upstream = EchoUpstream(leak=leak, echo=echo)
    gw = ToolGateway(
        grants=grant_resolver,
        validator=MiniSchemaValidator(),
        secrets=secret_resolver,
        upstream=upstream,
        tickets=tickets,
        audit=audit,
        approval_timeout_seconds=1,
    )
    gw._test_upstream = upstream  # expose the fake for assertions
    return gw, grant_resolver, secret_resolver, tickets, audit


def a_call(**kw):
    base = dict(request_id="r1", agent_id="agent-1", upstream_id="github", tool_name="create_pr", arguments={})
    base.update(kw)
    return ToolCall(**base)


async def test_auto_passes_straight_through():
    gw, grants, *_ = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.AUTO))
    result = await gw.call(a_call(arguments={"title": "x"}))
    assert result.content["echo"] == {"title": "x"}


async def test_unauthorized_is_rejected():
    gw, *_ = build()
    with pytest.raises(ToolNotAllowed):
        await gw.call(a_call())


async def test_interactive_blocks_then_approves():
    gw, grants, _s, tickets, audit = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.INTERACTIVE))

    task = asyncio.create_task(gw.call(a_call(arguments={"title": "ship"})))
    await asyncio.sleep(0.05)  # let it reach the gate and block
    assert not task.done()
    await tickets.decide("tkt-r1", Decision(DecisionStatus.APPROVED, approver="alice"))
    result = await task
    assert result.content["echo"] == {"title": "ship"}
    assert "approval" not in "".join(audit.actions())  # no error event
    assert "decided" in audit.actions()


async def test_interactive_rejected_raises():
    gw, grants, _s, tickets, _a = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.INTERACTIVE))
    task = asyncio.create_task(gw.call(a_call()))
    await asyncio.sleep(0.05)
    await tickets.decide("tkt-r1", Decision(DecisionStatus.REJECTED, approver="bob", reason="nope"))
    with pytest.raises(ApprovalDenied):
        await task


async def test_interactive_timeout_raises():
    gw, grants, *_ = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.INTERACTIVE))
    with pytest.raises(ApprovalDenied):
        await gw.call(a_call())  # nobody decides; 1s timeout


async def test_async_blocks_like_interactive_when_requested():
    # the MCP edge passes block_async=True: an async-gated call blocks until
    # decided instead of returning a Pending handle
    gw, grants, _s, tickets, _a = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.ASYNC))
    task = asyncio.create_task(gw.call(a_call(arguments={"title": "gated"}), block_async=True))
    await asyncio.sleep(0.05)
    assert not task.done()  # blocked, no handle returned
    await tickets.decide("tkt-r1", Decision(DecisionStatus.APPROVED, approver="alice"))
    result = await task
    assert result.content["echo"] == {"title": "gated"}


async def test_interactive_edit_is_applied_and_noted():
    gw, grants, _s, tickets, _a = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.INTERACTIVE))
    task = asyncio.create_task(gw.call(a_call(arguments={"title": "typo"})))
    await asyncio.sleep(0.05)
    await tickets.decide(
        "tkt-r1", Decision(DecisionStatus.APPROVED, approver="alice", edited_arguments={"title": "fixed"})
    )
    result = await task
    assert result.content["echo"] == {"title": "fixed"}  # executed the EDIT
    assert result.notes and "edited" in result.notes[0]


async def test_async_returns_pending_then_resume_executes():
    gw, grants, _s, tickets, audit = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.ASYNC))

    pending = await gw.call(a_call(arguments={"title": "later"}))
    assert isinstance(pending, Pending)  # request returned at once, nothing blocked
    assert "gated" in audit.actions()

    await tickets.decide(pending.ticket_id, Decision(DecisionStatus.APPROVED, approver="carol"))
    result = await gw.resume(pending.ticket_id)
    assert result.content["echo"] == {"title": "later"}
    assert "resumed" in audit.actions()


async def test_async_resume_rejects_when_denied():
    gw, grants, _s, tickets, _a = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.ASYNC))
    pending = await gw.call(a_call())
    await tickets.decide(pending.ticket_id, Decision(DecisionStatus.REJECTED, approver="carol"))
    with pytest.raises(ApprovalDenied):
        await gw.resume(pending.ticket_id)


async def test_schema_validation_rejects_and_accepts():
    gw, grants, _s, _t, _a = build()
    schema = {"type": "object", "required": ["title"], "properties": {"title": {"type": "string"}}}
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.AUTO, input_schema=schema))
    with pytest.raises(SchemaInvalid):
        await gw.call(a_call(arguments={}))  # missing required title
    ok = await gw.call(a_call(arguments={"title": "ok"}))
    assert ok.content["echo"] == {"title": "ok"}


async def test_secret_ref_is_materialized_for_upstream_not_agent():
    gw, grants, secrets, *_ = build(echo=False)  # upstream must not echo the secret back
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.AUTO))
    secrets.define("gh_token", "ghp_realvalue")
    result = await gw.call(a_call(arguments={"token": "secret://gh_token"}))
    assert gw._test_upstream.received[-1]["token"] == "ghp_realvalue"  # upstream got plaintext
    assert "ghp_realvalue" not in str(result.content)  # the agent never sees it


async def test_leak_is_fail_closed():
    gw, grants, secrets, *_ = build(leak="ghp_realvalue")
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.AUTO))
    secrets.define("gh_token", "ghp_realvalue")
    with pytest.raises(SecretLeak):
        await gw.call(a_call(arguments={"token": "secret://gh_token"}))


async def test_audit_trail_is_complete_for_auto():
    gw, grants, _s, _t, audit = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.AUTO))
    await gw.call(a_call(arguments={"title": "x"}))
    assert "authorized" in audit.actions() and "call" in audit.actions()
