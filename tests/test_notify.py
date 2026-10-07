"""Approvers hear about every gated call: signed webhook, background delivery,
bounded retries, and a ticket that stands whatever happens to the message."""

import hashlib
import hmac
import json

import httpx
import pytest

from tool_gateway import ApprovalMode, Grant, Pending, ToolCall, ToolGateway
from tool_gateway.adapters.memory import (
    DictGrantResolver,
    DictSecretResolver,
    EchoUpstream,
    ListAuditLog,
    MemoryTicketStore,
    MiniSchemaValidator,
)
from tool_gateway.adapters.webhook import WebhookNotifier

pytestmark = pytest.mark.asyncio

URL = "https://approvals.example/hook"


class Receiver:
    """httpx transport that answers with a scripted list of statuses."""

    def __init__(self, *statuses: int | Exception):
        self.statuses = list(statuses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.statuses.pop(0) if self.statuses else 200
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome)


def build(receiver: Receiver | None = None, *, url: str = URL, secret: str = "shh", mode=ApprovalMode.ASYNC):
    audit, tickets = ListAuditLog(), MemoryTicketStore()
    notifier = WebhookNotifier(
        url,
        audit,
        secret=secret,
        backoff_seconds=0,
        transport=httpx.MockTransport(receiver or Receiver()),
    )
    grants = DictGrantResolver()
    grants.allow("agent-1", "bank.wire", Grant(mode))
    gw = ToolGateway(
        grants=grants,
        validator=MiniSchemaValidator(),
        secrets=DictSecretResolver({"key": "plaintext"}),
        upstream=EchoUpstream(),
        tickets=tickets,
        audit=audit,
        notifier=notifier,
    )
    return gw, notifier, tickets, audit


def wire(**args) -> ToolCall:
    return ToolCall("1", "agent-1", "bank", "wire", args, annotations={"destructiveHint": True})


async def test_gated_call_posts_a_signed_event():
    receiver = Receiver()
    gw, notifier, _t, audit = build(receiver)
    pending = await gw.call(wire(cents=5, key="secret://key"), can_block=False)
    await notifier.drain()

    request = receiver.requests[0]
    body = json.loads(request.content)
    assert body == {
        "event": "approval_requested",
        "ticket_id": pending.ticket_id,
        "approval_mode": "async",
        "agent_id": "agent-1",
        "tool": "bank.wire",
        "arguments": {"cents": 5, "key": "secret://key"},  # the reference, never the secret
        "annotations": {"destructiveHint": True},
    }
    expected = "sha256=" + hmac.new(b"shh", request.content, hashlib.sha256).hexdigest()
    assert request.headers["X-Pico-Signature"] == expected
    assert "notified" in audit.actions()


async def test_server_errors_are_retried():
    receiver = Receiver(503, httpx.ConnectError("down"), 200)
    gw, notifier, _t, audit = build(receiver)
    await gw.call(wire(), can_block=False)
    await notifier.drain()
    assert len(receiver.requests) == 3
    assert next(e for e in audit.events if e["event"] == "notified")["attempts"] == 3


async def test_a_rejection_is_not_retried_and_the_ticket_stands():
    receiver = Receiver(403)
    gw, notifier, tickets, audit = build(receiver)
    pending = await gw.call(wire(), can_block=False)
    await notifier.drain()
    assert len(receiver.requests) == 1
    failure = next(e for e in audit.events if e["event"] == "notify.error")
    assert failure["error"] == "HTTP 403"
    assert pending.ticket_id in await tickets.pending()


async def test_exhausted_retries_are_audited():
    receiver = Receiver(500, 500, 500)
    gw, notifier, _t, audit = build(receiver)
    await gw.call(wire(), can_block=False)
    await notifier.drain()
    assert len(receiver.requests) == 3
    assert "notify.error" in audit.actions()


async def test_no_url_means_no_message():
    receiver = Receiver()
    gw, notifier, _t, _a = build(receiver, url="")
    assert isinstance(await gw.call(wire(), can_block=False), Pending)
    await notifier.drain()
    assert receiver.requests == []


async def test_unsigned_without_a_secret():
    receiver = Receiver()
    gw, notifier, _t, _a = build(receiver, secret="")
    await gw.call(wire(), can_block=False)
    await notifier.drain()
    assert "X-Pico-Signature" not in receiver.requests[0].headers


async def test_auto_calls_notify_nobody():
    receiver = Receiver()
    gw, notifier, _t, _a = build(receiver, mode=ApprovalMode.AUTO)
    await gw.call(wire())
    await notifier.drain()
    assert receiver.requests == []


async def test_a_broken_notifier_does_not_lose_the_call():
    class Broken:
        async def notify_approvers(self, ticket_id, call, mode):
            raise RuntimeError("channel misconfigured")

    grants, tickets, audit = DictGrantResolver(), MemoryTicketStore(), ListAuditLog()
    grants.allow("agent-1", "bank.wire", Grant(ApprovalMode.ASYNC))
    gw_broken = ToolGateway(
        grants=grants,
        validator=MiniSchemaValidator(),
        secrets=DictSecretResolver(),
        upstream=EchoUpstream(),
        tickets=tickets,
        audit=audit,
        notifier=Broken(),
    )
    pending = await gw_broken.call(wire(), can_block=False)
    assert isinstance(pending, Pending)
    assert pending.ticket_id in await tickets.pending()
    assert "notify.error" in audit.actions()


async def test_default_wiring_notifies_through_the_configured_webhook():
    from pico_ioc import DictSource, configuration, init

    from tool_gateway.ports import ApproverNotifier

    container = init(
        modules=["tool_gateway"],
        config=configuration(DictSource({"tool_gateway": {"notify_url": URL, "notify_secret": "shh"}})),
    )
    assert isinstance(container.get(ApproverNotifier), WebhookNotifier)
    container.shutdown()
