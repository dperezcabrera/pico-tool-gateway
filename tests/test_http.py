"""The HTTP edge over pico-fastapi, driven end to end through the gateway.
An app wires a real Upstream and a grant policy; the rest is the module."""

import sys

from pico_fastapi import controller, get
from pico_ioc import component

from tool_gateway import ApprovalMode, Grant, ToolCall
from tool_gateway.adapters.memory import EchoUpstream
from tool_gateway.ports import GrantResolver, Upstream  # noqa: F401  (documented seams)


@component
class _Upstream(EchoUpstream):
    pass


@component
class _Grants:
    def __init__(self):
        self.mode = ApprovalMode.AUTO

    async def resolve(self, call: ToolCall) -> Grant:
        return Grant(self.mode)


@controller(prefix="/mode")
class _ModeSwitch:
    """Test helper: flip the approval mode at runtime to exercise async."""

    def __init__(self, grants: _Grants):
        self._grants = grants

    @get("/{mode}")
    async def set_mode(self, mode: str):
        self._grants.mode = ApprovalMode(mode)
        return {"mode": mode}


def _client(make_container, make_client):
    container = make_container(
        "tool_gateway", "pico_fastapi", sys.modules[__name__], config={"fastapi": {"title": "gw"}}
    )
    return make_client(container)


def _body(**kw):
    base = dict(request_id="r1", agent_id="agent-1", upstream_id="github", tool_name="create_pr", arguments={})
    base.update(kw)
    return base


def test_auto_call_over_http(make_container, make_client):
    client = _client(make_container, make_client)
    r = client.post("/api/v1/tools/call", json=_body(arguments={"title": "x"}))
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "content": {"tool": "create_pr", "echo": {"title": "x"}},
        "is_error": False,
        "notes": [],
    }


def test_async_call_returns_pending_then_decide_and_resume(make_container, make_client):
    client = _client(make_container, make_client)
    client.get("/mode/async")

    pending = client.post("/api/v1/tools/call", json=_body(arguments={"title": "later"})).json()
    assert pending["status"] == "pending" and pending["ticket_id"]

    decided = client.post(
        f"/api/v1/tickets/{pending['ticket_id']}/decide", json={"status": "approved", "approver": "alice"}
    )
    assert decided.json() == {"status": "approved"}

    resumed = client.post(f"/api/v1/tickets/{pending['ticket_id']}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["content"]["echo"] == {"title": "later"}


def test_async_rejected_resume_is_422(make_container, make_client):
    client = _client(make_container, make_client)
    client.get("/mode/async")
    pending = client.post("/api/v1/tools/call", json=_body()).json()
    client.post(f"/api/v1/tickets/{pending['ticket_id']}/decide", json={"status": "rejected"})
    assert client.post(f"/api/v1/tickets/{pending['ticket_id']}/resume").status_code == 422
