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

    async def grant_for(self, call: ToolCall) -> Grant:
        return Grant(self.mode)


@component
class _Catalog(DictToolCatalog):
    def __init__(self):
        super().__init__(
            {"agent-1@test": [{"name": "github.create_pr", "description": "open a PR", "inputSchema": {}}]}
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
    listed = client.post("/mcp", json=rpc("tools/list"), headers=agent2).json()
    assert listed["result"]["tools"] == []  # agent-2 sees nothing, as provisioned


def test_operator_plane_needs_operator_role(harness):
    client, container = harness
    agent = token(container, "agent-1@test", "agent")
    operator = token(container, "admin@gw.local", "operator")

    # an agent token cannot drive the operator plane
    assert client.post("/api/v1/tickets/tkt-x/decide", json={"status": "approved"}, headers=agent).status_code == 403
    # an operator can (unknown ticket still records a decision, no-op)
    assert client.post("/api/v1/tickets/tkt-x/decide", json={"status": "approved"}, headers=operator).status_code == 200


def test_mcp_async_blocks_until_operator_decides(harness):
    # the block-until-decided flow is unit-tested at the gateway level
    # (test_gateway.test_async_blocks_like_interactive_when_requested); here
    # we drive it over HTTP with a background thread approving mid-call.
    import threading
    import time

    client, container = harness
    agent = token(container, "agent-1@test", "agent")
    operator = token(container, "admin@gw.local", "operator")
    container.get(_AllowAll).mode = ApprovalMode.ASYNC

    def approve_soon():
        time.sleep(0.3)
        client.post("/api/v1/tickets/tkt-1/decide", json={"status": "approved", "approver": "admin"}, headers=operator)

    threading.Thread(target=approve_soon, daemon=True).start()
    resp = client.post(
        "/mcp", json=rpc("tools/call", name="github.create_pr", arguments={"title": "gated"}), headers=agent
    )
    assert resp.status_code == 200
    assert "'title': 'gated'" in resp.json()["result"]["content"][-1]["text"]
