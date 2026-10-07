"""The persistent store: tickets survive a restart, run once across replicas,
and the audit trail lands in the database."""

import asyncio
import sys

import pytest
from pico_ioc import component
from pico_sqlalchemy import SessionManager
from sqlalchemy import select

from tool_gateway import ApprovalMode, Decision, DecisionStatus, Grant, Pending, ToolCall, ToolGateway
from tool_gateway.adapters.memory import EchoUpstream
from tool_gateway.ports import AuditLog, TicketStore, Upstream
from tool_gateway_sql import SqlAuditLog, SqlTicketStore
from tool_gateway_sql.store import AuditRow

pytestmark = pytest.mark.asyncio


@component
class _Upstream(EchoUpstream):
    pass


@component
class _Policy:
    mode = ApprovalMode.ASYNC

    async def grant_for(self, call: ToolCall) -> Grant:
        return Grant(_Policy.mode)


@pytest.fixture
def boot(make_container, tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'gateway.db'}"

    def _boot():
        return make_container(
            "tool_gateway",
            "tool_gateway_sql",
            "pico_sqlalchemy",
            sys.modules[__name__],
            config={"database": {"url": url}},
        )

    _Policy.mode = ApprovalMode.ASYNC
    return _boot


def a_call(**args) -> ToolCall:
    return ToolCall("r1", "agent-1", "github", "create_pr", args)


async def test_the_sql_adapters_replace_the_memory_defaults(boot):
    container = boot()
    assert isinstance(container.get(TicketStore), SqlTicketStore)
    assert isinstance(container.get(AuditLog), SqlAuditLog)
    # pico-sqlalchemy brings an AOP interceptor; the ports' specific method
    # names keep it from matching Upstream or AuditLog structurally
    assert isinstance(container.get(Upstream), _Upstream)


async def test_a_ticket_survives_a_restart_and_executes_once(boot):
    first = boot()
    pending = await first.get(ToolGateway).call(a_call(title="persist"))
    assert isinstance(pending, Pending)
    await first.get(TicketStore).decide(pending.ticket_id, Decision(DecisionStatus.APPROVED, approver="ops"))
    first.shutdown()

    second = boot()  # a new process on the same database
    gw = second.get(ToolGateway)
    result = await gw.resume(pending.ticket_id)
    again = await gw.resume(pending.ticket_id)
    assert result.content["echo"] == {"title": "persist"}
    assert again.content == result.content
    assert second.get(_Upstream).received == [{"title": "persist"}]


async def test_claim_is_won_once_across_replicas(boot):
    a, b = boot(), boot()
    pending = await a.get(ToolGateway).call(a_call())
    store_a, store_b = a.get(TicketStore), b.get(TicketStore)
    wins = await asyncio.gather(store_a.claim(pending.ticket_id), store_b.claim(pending.ticket_id))
    assert sorted(wins) == [False, True]


async def test_interactive_wait_sees_a_decision_from_another_replica(boot):
    _Policy.mode = ApprovalMode.INTERACTIVE
    a, b = boot(), boot()
    task = asyncio.create_task(a.get(ToolGateway).call(a_call(title="x")))
    for _ in range(50):
        await asyncio.sleep(0.05)
        async with a.get(SessionManager).transaction(read_only=True) as session:
            gated = (await session.execute(select(AuditRow).where(AuditRow.event == "gated"))).scalars().first()
        if gated:
            break
    await b.get(TicketStore).decide(gated.fields["ticket_id"], Decision(DecisionStatus.APPROVED, approver="ops"))
    result = await asyncio.wait_for(task, timeout=5)
    assert result.content["echo"] == {"title": "x"}


async def test_audit_events_are_rows(boot):
    _Policy.mode = ApprovalMode.AUTO
    container = boot()
    await container.get(ToolGateway).call(a_call())
    async with container.get(SessionManager).transaction(read_only=True) as session:
        events = (await session.execute(select(AuditRow.event, AuditRow.agent_id, AuditRow.tool))).all()
    assert ("authorized", "agent-1", "github.create_pr") in [tuple(e) for e in events]


async def test_unknown_ticket_is_none(boot):
    assert await boot().get(TicketStore).get("tkt-nope") is None


async def test_pending_queue_is_oldest_first_and_drops_decided(boot):
    container = boot()
    gw, store = container.get(ToolGateway), container.get(TicketStore)
    first = await gw.call(a_call(n=1))
    second = await gw.call(a_call(n=2))
    assert list(await store.pending()) == [first.ticket_id, second.ticket_id]
    await store.decide(first.ticket_id, Decision(DecisionStatus.REJECTED, approver="ops"))
    assert list(await store.pending()) == [second.ticket_id]
