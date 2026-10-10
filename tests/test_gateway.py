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
from tool_gateway.domain import ApprovalDenied, PendingApproval, SchemaInvalid, SecretLeak, ToolNotAllowed, ToolResult
from tool_gateway.gateway import Pending, TicketAlreadyDecided


def build(*, grants=None, secrets=None, leak=None, echo=True, lease=600):
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
        execution_lease_seconds=lease,
    )
    gw._test_upstream = upstream  # expose the fake for assertions
    return gw, grant_resolver, secret_resolver, tickets, audit


def gated_ticket(audit) -> str:
    return next(e["ticket_id"] for e in audit.events if e["event"] == "gated")


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
    await gw.decide(gated_ticket(audit), Decision(DecisionStatus.APPROVED, approver="alice"))
    result = await task
    assert result.content["echo"] == {"title": "ship"}
    assert "approval" not in "".join(audit.actions())  # no error event
    assert "decided" in audit.actions()


async def test_interactive_rejected_raises():
    gw, grants, _s, tickets, audit = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.INTERACTIVE))
    task = asyncio.create_task(gw.call(a_call()))
    await asyncio.sleep(0.05)
    await gw.decide(gated_ticket(audit), Decision(DecisionStatus.REJECTED, approver="bob", reason="nope"))
    with pytest.raises(ApprovalDenied):
        await task


async def test_interactive_timeout_raises():
    gw, grants, *_ = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.INTERACTIVE))
    with pytest.raises(ApprovalDenied):
        await gw.call(a_call())  # nobody decides; 1s timeout


async def test_non_blocking_caller_gets_a_ticket_for_interactive_too():
    # can_block=False (MCP): even an interactive-mode gated call returns a
    # ticket instead of blocking; the caller polls via resume()
    gw, grants, _s, tickets, _a = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.INTERACTIVE))
    outcome = await gw.call(a_call(arguments={"title": "gated"}), can_block=False)
    assert isinstance(outcome, Pending)
    await tickets.decide(outcome.ticket_id, Decision(DecisionStatus.APPROVED, approver="alice"))
    result = await gw.resume(outcome.ticket_id)
    assert result.content["echo"] == {"title": "gated"}


async def test_interactive_edit_is_applied_and_noted():
    gw, grants, _s, tickets, audit = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.INTERACTIVE))
    task = asyncio.create_task(gw.call(a_call(arguments={"title": "typo"})))
    await asyncio.sleep(0.05)
    await gw.decide(
        gated_ticket(audit), Decision(DecisionStatus.APPROVED, approver="alice", edited_arguments={"title": "fixed"})
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


async def test_approved_ticket_executes_once_however_often_it_is_resumed():
    gw, grants, _s, tickets, _a = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.ASYNC))
    pending = await gw.call(a_call(arguments={"title": "once"}))
    await tickets.decide(pending.ticket_id, Decision(DecisionStatus.APPROVED, approver="carol"))
    first = await gw.resume(pending.ticket_id)
    again = await gw.resume(pending.ticket_id)
    assert gw._test_upstream.received == [{"title": "once"}]
    assert again.content == first.content


async def test_concurrent_resumes_execute_once():
    gw, grants, _s, tickets, _a = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.ASYNC))
    pending = await gw.call(a_call())
    await tickets.decide(pending.ticket_id, Decision(DecisionStatus.APPROVED, approver="carol"))
    outcomes = await asyncio.gather(*(gw.resume(pending.ticket_id) for _ in range(3)), return_exceptions=True)
    assert len(gw._test_upstream.received) == 1
    assert all(isinstance(o, (ToolResult, PendingApproval)) for o in outcomes)


async def test_interactive_ticket_is_not_executed_again_by_resume():
    gw, grants, _s, tickets, audit = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.INTERACTIVE))
    task = asyncio.create_task(gw.call(a_call(arguments={"title": "x"})))
    await asyncio.sleep(0.05)
    ticket_id = gated_ticket(audit)
    await gw.decide(ticket_id, Decision(DecisionStatus.APPROVED, approver="alice"))
    await task
    await gw.resume(ticket_id)
    assert len(gw._test_upstream.received) == 1


async def test_ticket_ids_do_not_collide_on_repeated_request_ids():
    gw, grants, _s, tickets, _a = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.ASYNC))
    first = await gw.call(a_call(arguments={"n": 1}))
    second = await gw.call(a_call(arguments={"n": 2}))  # same request_id "r1"
    assert first.ticket_id != second.ticket_id
    assert (await tickets.get(first.ticket_id)).call.arguments == {"n": 1}


async def test_ticket_keeps_the_reference_not_the_materialized_secret():
    secrets = DictSecretResolver({"gh-token": "s3cr3t"})
    gw, grants, _s, tickets, audit = build(secrets=secrets, echo=False)
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.INTERACTIVE))
    task = asyncio.create_task(gw.call(a_call(arguments={"token": "secret://gh-token"})))
    await asyncio.sleep(0.05)
    ticket_id = gated_ticket(audit)
    await gw.decide(ticket_id, Decision(DecisionStatus.APPROVED, approver="alice"))
    await task
    assert (await tickets.get(ticket_id)).call.arguments == {"token": "secret://gh-token"}


async def test_decisions_are_audited_with_the_operator():
    gw, grants, _s, _t, audit = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.ASYNC))
    pending = await gw.call(a_call())
    await gw.decide(pending.ticket_id, Decision(DecisionStatus.APPROVED, approver="carol", reason="ok"))
    event = next(e for e in audit.events if e["event"] == "decision")
    assert (event["approver"], event["status"], event["reason"]) == ("carol", "approved", "ok")


async def test_interactive_timeout_is_recorded_so_a_late_approval_cannot_run_it():
    gw, grants, _s, tickets, audit = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.INTERACTIVE))
    with pytest.raises(ApprovalDenied):
        await gw.call(a_call())  # nobody decides within the 1s timeout
    ticket_id = gated_ticket(audit)
    assert (await tickets.get(ticket_id)).decision.status is DecisionStatus.TIMEOUT
    with pytest.raises(TicketAlreadyDecided):
        await gw.decide(ticket_id, Decision(DecisionStatus.APPROVED, approver="late"))
    assert gw._test_upstream.received == []


async def test_a_verdict_right_at_the_deadline_stands():
    class LastSecond(MemoryTicketStore):
        async def decide(self, ticket_id, decision):
            if decision.status is DecisionStatus.TIMEOUT:
                # the operator's approval lands just before the waiter records its timeout
                await super().decide(ticket_id, Decision(DecisionStatus.APPROVED, approver="alice"))
            return await super().decide(ticket_id, decision)

    grants = DictGrantResolver()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.INTERACTIVE))
    upstream = EchoUpstream()
    gw = ToolGateway(
        grants=grants,
        validator=MiniSchemaValidator(),
        secrets=DictSecretResolver(),
        upstream=upstream,
        tickets=LastSecond(),
        audit=ListAuditLog(),
        approval_timeout_seconds=0.1,
    )
    result = await gw.call(a_call(arguments={"title": "just in time"}))
    assert result.content["echo"] == {"title": "just in time"}


async def test_the_signal_wakes_the_waiter_at_once():
    gw, grants, _s, _t, audit = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.INTERACTIVE))
    task = asyncio.create_task(gw.call(a_call()))
    await asyncio.sleep(0.05)
    started = asyncio.get_running_loop().time()
    await gw.decide(gated_ticket(audit), Decision(DecisionStatus.APPROVED, approver="alice"))
    await task
    assert asyncio.get_running_loop().time() - started < 0.5  # not the 5 s recheck, not the 1 s timeout


async def test_a_signal_sent_before_the_wait_is_not_lost():
    from tool_gateway.adapters.memory import MemoryDecisionSignal

    signal = MemoryDecisionSignal()
    await signal.signal_decision("tkt-1")
    await asyncio.wait_for(signal.wait_for_decision("tkt-1", timeout_seconds=5), timeout=0.5)


async def test_memory_queue_pages_and_filters():
    gw, grants, _s, tickets, _a = build()
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.ASYNC))
    grants.allow("agent-1", "github.delete_repo", Grant(ApprovalMode.ASYNC))
    created = [await gw.call(a_call(tool_name=name)) for name in ("create_pr", "delete_repo", "create_pr")]
    ids = [p.ticket_id for p in created]
    first = await tickets.pending(limit=2)
    assert list(first) == ids[:2]
    assert list(await tickets.pending(after=ids[1])) == ids[2:]
    assert list(await tickets.pending(tool="github.delete_*")) == [ids[1]]


async def _approved_ticket_whose_executor_died(gw, grants, tickets):
    grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.ASYNC))
    pending = await gw.call(a_call(arguments={"amount": 100}))
    await gw.decide(pending.ticket_id, Decision(DecisionStatus.APPROVED, approver="carol"))
    assert await tickets.claim(pending.ticket_id)  # an executor took it, then its process died
    return pending.ticket_id


async def test_a_dead_executor_is_closed_as_interrupted_never_rerun():
    gw, grants, _s, tickets, _a = build(lease=0)
    ticket_id = await _approved_ticket_whose_executor_died(gw, grants, tickets)
    result = await gw.resume(ticket_id)
    assert result.is_error and "outcome is unknown" in result.content
    assert gw._test_upstream.received == []  # the call may have taken effect: not run again
    assert (await gw.resume(ticket_id)).content == result.content  # stored, stable


async def test_a_claim_within_its_lease_is_still_running():
    gw, grants, _s, tickets, _a = build(lease=600)
    ticket_id = await _approved_ticket_whose_executor_died(gw, grants, tickets)
    with pytest.raises(PendingApproval):
        await gw.resume(ticket_id)
