"""Ports: the seams fleet (or anyone) plugs real infrastructure into.

Each is a Protocol so an adapter needs no base class. The domain and the
pipeline depend only on these — never on a vault, a DB or an MCP transport.
"""

from typing import Any, Protocol, runtime_checkable

from .domain import Decision, Grant, ToolCall, ToolResult


@runtime_checkable
class GrantResolver(Protocol):
    """Authorize a call and resolve its approval mode + schema.
    Returns None when the agent may not run the tool."""

    async def resolve(self, call: ToolCall) -> Grant | None: ...


@runtime_checkable
class SchemaValidator(Protocol):
    """Validate arguments against a JSON-Schema-shaped dict.
    Returns a list of human-readable errors (empty = valid).
    This is the piece worth reusing beyond tool calls."""

    def validate(self, arguments: dict[str, Any], schema: dict[str, Any] | None) -> list[str]: ...


@runtime_checkable
class SecretResolver(Protocol):
    """Materialize ``secret://ref`` placeholders before dispatch (the agent
    never sees plaintext) and redact the response fail-closed afterwards."""

    async def materialize(
        self, arguments: dict[str, Any], *, upstream_id: str, agent_id: str
    ) -> tuple[dict[str, Any], list[str]]: ...

    def redact(self, result: ToolResult, *, upstream_id: str) -> ToolResult: ...


@runtime_checkable
class Upstream(Protocol):
    """The actual tool executor (an MCP session, an HTTP client, ...)."""

    async def call(self, upstream_id: str, tool_name: str, arguments: dict[str, Any]) -> ToolResult: ...


@runtime_checkable
class TicketStore(Protocol):
    """Durable home for pending approvals. ``await_decision`` is how the
    interactive mode blocks; the async mode never calls it."""

    async def create(self, ticket_id: str, call: ToolCall) -> None: ...

    async def get(self, ticket_id: str) -> tuple[ToolCall, Decision] | None: ...

    async def decide(self, ticket_id: str, decision: Decision) -> None: ...

    async def await_decision(self, ticket_id: str, *, timeout_seconds: float) -> Decision: ...


@runtime_checkable
class AuditLog(Protocol):
    """One sink for the whole flow; the pipeline wraps steps with it so
    audit is cross-cutting, not interleaved with logic."""

    async def record(self, event: str, call: ToolCall, **fields: Any) -> None: ...
