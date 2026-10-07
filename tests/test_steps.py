"""The pipeline is assembled from parts: extra steps slot in by stage and
order, and the rate limiter is one more swappable port."""

import sys

import pytest
from pico_ioc import DictSource, component, configuration, init

from tool_gateway import ApprovalMode, Decision, DecisionStatus, Grant, ToolCall, ToolGateway
from tool_gateway.adapters.memory import (
    DictGrantResolver,
    DictSecretResolver,
    EchoUpstream,
    ListAuditLog,
    MemoryTicketStore,
    MiniSchemaValidator,
    WindowRateLimiter,
)
from tool_gateway.domain import GatewayError, RateLimited
from tool_gateway.pipeline import Stage

pytestmark = pytest.mark.asyncio


class Probe:
    """Records what it saw of the call when its turn came."""

    def __init__(self, stage: Stage, order: int, seen: list):
        self.stage, self.order, self._seen = stage, order, seen

    async def handle_call(self, ctx, call_next):
        self._seen.append((self.order, ctx.grant is not None, dict(ctx.call.arguments)))
        return await call_next(ctx)


def build(*steps, mode=ApprovalMode.AUTO, limiter=None):
    grants = DictGrantResolver()
    grants.allow("agent-1", "github.create_pr", Grant(mode))
    grants.allow("agent-2", "github.create_pr", Grant(mode))
    tickets, audit = MemoryTicketStore(), ListAuditLog()
    gw = ToolGateway(
        grants=grants,
        validator=MiniSchemaValidator(),
        secrets=DictSecretResolver({"tok": "s3cr3t"}),
        upstream=EchoUpstream(echo=False),
        tickets=tickets,
        audit=audit,
        rate_limiter=limiter,
        steps=steps,
    )
    return gw, tickets, audit


def a_call(agent="agent-1", **args) -> ToolCall:
    return ToolCall("r", agent, "github", "create_pr", args)


async def test_extra_steps_run_at_their_place_among_the_built_ins():
    seen: list = []
    gw, *_ = build(
        Probe(Stage.BEFORE_APPROVAL, 75, seen),  # before authorize (100): no grant yet
        Probe(Stage.BEFORE_APPROVAL, 150, seen),  # after authorize: grant resolved
        Probe(Stage.AFTER_APPROVAL, 150, seen),  # before materialize (200): still the reference
        Probe(Stage.AFTER_APPROVAL, 250, seen),  # after materialize: the real value
    )
    await gw.call(a_call(token="secret://tok"))
    assert seen == [
        (75, False, {"token": "secret://tok"}),
        (150, True, {"token": "secret://tok"}),
        (150, True, {"token": "secret://tok"}),
        (250, True, {"token": "s3cr3t"}),
    ]


async def test_resume_runs_the_after_steps_only():
    seen: list = []
    gw, tickets, _ = build(
        Probe(Stage.BEFORE_APPROVAL, 75, seen), Probe(Stage.AFTER_APPROVAL, 150, seen), mode=ApprovalMode.ASYNC
    )
    pending = await gw.call(a_call())
    assert [s[0] for s in seen] == [75]
    seen.clear()
    await tickets.decide(pending.ticket_id, Decision(DecisionStatus.APPROVED, approver="ops"))
    await gw.resume(pending.ticket_id)
    assert [s[0] for s in seen] == [150]


async def test_a_step_can_refuse_the_call():
    class Quota:
        stage, order = Stage.BEFORE_APPROVAL, 120

        async def handle_call(self, ctx, call_next):
            raise GatewayError("monthly quota spent")

    gw, _, audit = build(Quota())
    with pytest.raises(GatewayError, match="quota"):
        await gw.call(a_call())
    assert "step.Quota.error" in audit.actions()  # extra steps are audited like the built-ins


async def test_a_step_without_a_stage_is_rejected_at_assembly():
    class Lost:
        stage, order = "whenever", 1

        async def handle_call(self, ctx, call_next):
            return await call_next(ctx)

    with pytest.raises(ValueError, match="stage"):
        build(Lost())


async def test_rate_limit_admits_the_budget_per_agent_and_window():
    now = [0.0]
    gw, *_ = build(limiter=WindowRateLimiter(2, clock=lambda: now[0]))
    await gw.call(a_call())
    await gw.call(a_call())
    with pytest.raises(RateLimited):
        await gw.call(a_call())
    await gw.call(a_call(agent="agent-2"))  # another agent has its own budget
    now[0] = 60.0  # next minute
    await gw.call(a_call())


async def test_rate_limit_zero_admits_everything():
    gw, *_ = build(limiter=WindowRateLimiter(0))
    for _ in range(50):
        await gw.call(a_call())


@component
class _Upstream(EchoUpstream):
    pass


@component
class _AllowAll:
    async def grant_for(self, call: ToolCall) -> Grant:
        return Grant(ApprovalMode.AUTO)


@component
class _Tagger:
    """A step contributed by the app: picked up from the container."""

    stage, order = Stage.AFTER_APPROVAL, 50

    async def handle_call(self, ctx, call_next):
        ctx.call.arguments["tagged"] = True
        return await call_next(ctx)


async def test_the_container_assembles_app_steps_and_the_configured_limit():
    container = init(
        modules=["tool_gateway", sys.modules[__name__]],
        config=configuration(DictSource({"tool_gateway": {"rate_limit_per_minute": 1}})),
    )
    gw = container.get(ToolGateway)
    result = await gw.call(a_call())
    assert result.content["echo"] == {"tagged": True}
    with pytest.raises(RateLimited):
        await gw.call(a_call())
    container.shutdown()
