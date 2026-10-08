"""Declarative policy end to end: loaded from a file at boot, driving the MCP
approval mode per tool, and hot-reloaded by an operator without a restart."""

import json
import sys

import pytest
from pico_ioc import component

from tool_gateway.adapters.memory import EchoUpstream
from tool_gateway.ports import Upstream  # noqa: F401


@component
class _Upstream(EchoUpstream):
    pass


POLICY = {
    "default": "deny",
    "rules": [
        {"tool": "github.get_*", "mode": "auto"},
        {"tool": "github.delete_*", "mode": "async"},
    ],
}


@pytest.fixture
def harness(make_container, make_client, monkeypatch, tmp_path):
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(POLICY), encoding="utf-8")
    config = {
        "fastapi": {"title": "gw"},
        "server_auth": {
            "issuer": "http://gw.local",
            "audience": "gw",
            "auto_create_admin": True,
            "admin_email": "admin@gw.local",
            "admin_password": "secret",
            "admin_role": "operator",
        },
        "auth_client": {"enabled": True, "issuer": "http://gw.local", "audience": "gw"},
        "tool_gateway": {"policy_path": str(policy_file)},
    }
    container = make_container(
        "tool_gateway", "pico_fastapi", "pico_server_auth", "pico_client_auth", sys.modules[__name__], config=config
    )
    client = make_client(container)
    from pico_client_auth.jwks_client import JWKSClient

    jwks = client.get("/api/v1/auth/jwks").json()

    async def _fetch(self):
        self._keys = {k["kid"]: k for k in jwks["keys"]}
        self._fetched_at = float("inf")

    monkeypatch.setattr(JWKSClient, "_fetch_keys", _fetch)
    return client, container, policy_file


def bearer(container, subject, role):
    from pico_server_auth import TokenIssuer

    return {"Authorization": f"Bearer {container.get(TokenIssuer).issue_access_token(subject=subject, role=role)}"}


def call(client, agent, name, **args):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}},
        headers=agent,
    ).json()["result"]


def test_policy_from_file_drives_the_mode(harness):
    client, container, _ = harness
    agent = bearer(container, "agent-1", "agent")
    # get_* -> auto: executes immediately
    assert call(client, agent, "github.get_pr")["isError"] is False
    # delete_* -> async: pending, not executed
    assert call(client, agent, "github.delete_repo")["_meta"]["status"] == "pending_approval"
    # not in policy -> default deny
    assert "not allowed" in str(
        client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "github.create_pr"}},
            headers=agent,
        ).json()["error"]["message"]
    )


def test_operator_hot_reloads_policy(harness):
    client, container, _ = harness
    agent = bearer(container, "agent-1", "agent")
    operator = bearer(container, "admin@gw.local", "operator")

    assert "not allowed" in str(
        client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "slack.post"}},
            headers=agent,
        ).json()["error"]["message"]
    )

    # publish a new ruleset; no restart
    new = {"default": "deny", "rules": [{"tool": "slack.*", "mode": "auto"}]}
    version = client.post("/api/v1/policy", json=new, headers=operator).json()["version"]
    assert call(client, agent, "slack.post")["isError"] is False  # now allowed
    assert client.get("/api/v1/policy", headers=operator).json() == {"version": version, "policy": new}


def test_publish_writes_the_shared_file(harness):
    client, container, policy_file = harness
    operator = bearer(container, "admin@gw.local", "operator")
    new = {"default": "auto", "rules": []}
    client.post("/api/v1/policy", json=new, headers=operator)
    assert json.loads(policy_file.read_text()) == new  # what the other replicas read


def test_an_invalid_policy_is_refused(harness):
    client, container, _ = harness
    operator = bearer(container, "admin@gw.local", "operator")
    r = client.post("/api/v1/policy", json={"rules": [{"tool": "*", "mode": "nonsense"}]}, headers=operator)
    assert r.status_code == 422
    assert client.get("/api/v1/policy", headers=operator).json()["policy"] == POLICY


def test_policy_is_operator_only(harness):
    client, container, _ = harness
    agent = bearer(container, "agent-1", "agent")
    assert client.post("/api/v1/policy", json={"rules": []}, headers=agent).status_code == 403
    assert client.get("/api/v1/policy", headers=agent).status_code == 403
