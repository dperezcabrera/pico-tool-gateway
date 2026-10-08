"""The authenticated MCP surface + the operator plane, end to end.

pico-server-auth (embedded) mints the tokens; pico-client-auth validates
them. An agent reaches /mcp with its token; the operator plane needs the
operator role. The agent identity comes from the verified sub, never the body.
"""

import sys

import pytest
from pico_ioc import component  # noqa: E402

from tool_gateway import ApprovalMode, Grant, ToolCall
from tool_gateway.adapters.memory import DictToolCatalog, EchoUpstream
from tool_gateway.ports import GrantResolver, ToolCatalog, Upstream  # noqa: F401  (documented seams)

CONFIG = {
    "fastapi": {"title": "tool-gateway"},
    "server_auth": {
        "issuer": "http://gw.local",
        "audience": "tool-gateway",
        "auto_create_admin": True,
        "admin_email": "admin@gw.local",
        "admin_password": "secret",
        "admin_role": "operator",
    },
    "auth_client": {"enabled": True, "issuer": "http://gw.local", "audience": "tool-gateway"},
    "tool_gateway": {"approval_timeout_seconds": 1},
}


@component
class _Upstream(EchoUpstream):
    pass


@component
class _AllowAll:
    def __init__(self):
        self.mode = ApprovalMode.AUTO
        self.last: ToolCall | None = None

    async def grant_for(self, call: ToolCall) -> Grant:
        self.last = call
        return Grant(self.mode)


@component
class _Catalog(DictToolCatalog):
    def __init__(self):
        super().__init__(
            {
                "agent-1@test": [
                    {
                        "name": "github.create_pr",
                        "description": "open a PR",
                        "inputSchema": {},
                        "annotations": {"readOnlyHint": False, "destructiveHint": False},
                    }
                ]
            }
        )


@pytest.fixture
def harness(make_container, make_client, monkeypatch):
    container = make_container(
        "tool_gateway", "pico_fastapi", "pico_server_auth", "pico_client_auth", sys.modules[__name__], config=CONFIG
    )
    client = make_client(container)

    from pico_client_auth.jwks_client import JWKSClient

    jwks = client.get("/api/v1/auth/jwks").json()

    async def _fetch(self):
        self._keys = {k["kid"]: k for k in jwks["keys"]}
        self._fetched_at = float("inf")

    monkeypatch.setattr(JWKSClient, "_fetch_keys", _fetch)
    return client, container


def token(container, subject: str, role: str) -> dict:
    from pico_server_auth import TokenIssuer

    tok = container.get(TokenIssuer).issue_access_token(subject=subject, role=role)
    return {"Authorization": f"Bearer {tok}"}


def rpc(method, **params):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


def test_mcp_requires_a_token(harness):
    client, _ = harness
    assert client.post("/mcp", json=rpc("tools/list")).status_code == 401


def test_agent_lists_and_calls_a_tool(harness):
    client, container = harness
    agent = token(container, "agent-1@test", "agent")

    listed = client.post("/mcp", json=rpc("tools/list"), headers=agent).json()
    assert listed["result"]["tools"][0]["name"] == "github.create_pr"

    called = client.post(
        "/mcp", json=rpc("tools/call", name="github.create_pr", arguments={"title": "x"}), headers=agent
    ).json()
    assert called["result"]["isError"] is False
    assert "'title': 'x'" in called["result"]["content"][-1]["text"]


def test_identity_comes_from_token_not_body(harness):
    client, container = harness
    # the agent authenticates as agent-1; even if the body tried to spoof,
    # the gateway uses the verified sub. Here agent-2 has no catalog entry.
    agent2 = token(container, "agent-2@test", "agent")
    names = {t["name"] for t in client.post("/mcp", json=rpc("tools/list"), headers=agent2).json()["result"]["tools"]}
    assert names == {"gateway.check"}  # agent-2 has no provisioned tools, only the built-in check


def test_operator_plane_needs_operator_role(harness):
    client, container = harness
    agent = token(container, "agent-1@test", "agent")
    operator = token(container, "admin@gw.local", "operator")

    # an agent token cannot drive the operator plane
    assert client.post("/api/v1/tickets/tkt-x/decide", json={"status": "approved"}, headers=agent).status_code == 403
    # an operator can; an unknown ticket is a 404, not a silent no-op
    assert client.post("/api/v1/tickets/tkt-x/decide", json={"status": "approved"}, headers=operator).status_code == 404


def test_gated_call_returns_pending_not_blocked(harness):
    # a gated tool does NOT block the agent: it returns a pending result at
    # once, informing it to poll gateway.check
    client, container = harness
    agent = token(container, "agent-1@test", "agent")
    container.get(_AllowAll).mode = ApprovalMode.ASYNC

    called = client.post(
        "/mcp", json=rpc("tools/call", name="github.create_pr", arguments={"title": "gated"}), headers=agent
    ).json()
    res = called["result"]
    assert res["isError"] is False
    assert res["_meta"]["status"] == "pending_approval"
    ticket = res["_meta"]["ticket_id"]
    assert "gateway.check" in res["content"][0]["text"]

    # still pending until an operator decides
    checking = client.post(
        "/mcp", json=rpc("tools/call", name="gateway.check", arguments={"ticket_id": ticket}), headers=agent
    ).json()
    assert checking["result"]["_meta"]["status"] == "pending_approval"

    operator = token(container, "admin@gw.local", "operator")
    client.post(f"/api/v1/tickets/{ticket}/decide", json={"status": "approved", "approver": "admin"}, headers=operator)

    # now check returns the real result
    done = client.post(
        "/mcp", json=rpc("tools/call", name="gateway.check", arguments={"ticket_id": ticket}), headers=agent
    ).json()
    assert "'title': 'gated'" in done["result"]["content"][-1]["text"]


def test_agent_cannot_check_another_agents_ticket(harness):
    client, container = harness
    agent1 = token(container, "agent-1@test", "agent")
    agent2 = token(container, "agent-2@test", "agent")
    container.get(_AllowAll).mode = ApprovalMode.ASYNC
    called = client.post("/mcp", json=rpc("tools/call", name="github.create_pr", arguments={}), headers=agent1).json()
    ticket = called["result"]["_meta"]["ticket_id"]
    other = client.post(
        "/mcp", json=rpc("tools/call", name="gateway.check", arguments={"ticket_id": ticket}), headers=agent2
    ).json()
    assert other["error"]["code"] == -32004  # not your ticket


def test_check_tool_is_listed(harness):
    client, container = harness
    agent = token(container, "agent-1@test", "agent")
    names = {t["name"] for t in client.post("/mcp", json=rpc("tools/list"), headers=agent).json()["result"]["tools"]}
    assert "gateway.check" in names


def test_call_carries_the_catalog_annotations_to_the_policy(harness):
    client, container = harness
    agent = token(container, "agent-1@test", "agent")
    client.post("/mcp", json=rpc("tools/call", name="github.create_pr", arguments={}), headers=agent)
    assert container.get(_AllowAll).last.annotations == {"readOnlyHint": False, "destructiveHint": False}


def test_operator_lists_the_pending_queue(harness):
    client, container = harness
    agent = token(container, "agent-1@test", "agent")
    operator = token(container, "admin@gw.local", "operator")
    container.get(_AllowAll).mode = ApprovalMode.ASYNC
    called = client.post(
        "/mcp", json=rpc("tools/call", name="github.create_pr", arguments={"title": "q"}), headers=agent
    ).json()
    ticket = called["result"]["_meta"]["ticket_id"]

    assert client.get("/api/v1/tickets", headers=agent).status_code == 403
    queue = client.get("/api/v1/tickets", headers=operator).json()["items"]
    assert queue == [
        {
            "ticket_id": ticket,
            "agent_id": "agent-1@test",
            "tool": "github.create_pr",
            "arguments": {"title": "q"},
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        }
    ]
    client.post(f"/api/v1/tickets/{ticket}/decide", json={"status": "rejected"}, headers=operator)
    assert client.get("/api/v1/tickets", headers=operator).json() == {"items": [], "next": None}


def test_the_queue_pages_with_a_cursor(harness):
    client, container = harness
    agent = token(container, "agent-1@test", "agent")
    operator = token(container, "admin@gw.local", "operator")
    tickets = [_gated(client, container, agent, n=i) for i in range(5)]
    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"after": cursor} if cursor else {})}
        page = client.get("/api/v1/tickets", params=params, headers=operator).json()
        seen += [item["ticket_id"] for item in page["items"]]
        cursor = page["next"]
        if cursor is None:
            break
    assert seen == tickets
    assert client.get("/api/v1/tickets", params={"limit": 10_000}, headers=operator).status_code == 200


def _gated(client, container, agent, **args) -> str:
    container.get(_AllowAll).mode = ApprovalMode.ASYNC
    called = client.post("/mcp", json=rpc("tools/call", name="github.create_pr", arguments=args), headers=agent).json()
    return called["result"]["_meta"]["ticket_id"]


def test_the_approver_is_the_verified_operator_not_the_body(harness):
    client, container = harness
    agent = token(container, "agent-1@test", "agent")
    operator = token(container, "admin@gw.local", "operator")
    ticket = _gated(client, container, agent)
    decided = client.post(
        f"/api/v1/tickets/{ticket}/decide", json={"status": "approved", "approver": "the-cfo"}, headers=operator
    ).json()
    assert decided["approver"] == "admin@gw.local"


def test_a_verdict_is_final(harness):
    client, container = harness
    agent = token(container, "agent-1@test", "agent")
    operator = token(container, "admin@gw.local", "operator")
    ticket = _gated(client, container, agent, title="no")
    assert (
        client.post(f"/api/v1/tickets/{ticket}/decide", json={"status": "rejected"}, headers=operator).status_code
        == 200
    )
    again = client.post(f"/api/v1/tickets/{ticket}/decide", json={"status": "approved"}, headers=operator)
    assert again.status_code == 409
    check = client.post(
        "/mcp", json=rpc("tools/call", name="gateway.check", arguments={"ticket_id": ticket}), headers=agent
    )
    assert check.json()["result"]["isError"] is True  # still rejected, never executed
    assert container.get(_Upstream).received == []


def test_an_operator_can_only_approve_or_reject(harness):
    client, container = harness
    agent = token(container, "agent-1@test", "agent")
    operator = token(container, "admin@gw.local", "operator")
    ticket = _gated(client, container, agent)
    for status in ("pending", "timeout", "", "maybe"):
        r = client.post(f"/api/v1/tickets/{ticket}/decide", json={"status": status}, headers=operator)
        assert r.status_code == 422, status
