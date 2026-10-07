"""Ports: the seams fleet (or anyone) plugs real infrastructure into.

pico-ioc matches these protocols by method names, so each name is specific
(``call_tool``, ``audit_event``): a generic one like ``invoke`` would match
every AOP interceptor in the container.

Each is a Protocol so an adapter needs no base class. The domain and the
pipeline depend only on these — never on a vault, a DB or an MCP transport.
"""

from typing import Any, Protocol, runtime_checkable

from .domain import Decision, Grant, Ticket, ToolCall, ToolResult


@runtime_checkable
class GrantResolver(Protocol):
    """Authorize a call and resolve its approval mode + schema.
    Returns None when the agent may not run the tool."""

    async def grant_for(self, call: ToolCall) -> Grant | None: ...


@runtime_checkable
class SchemaValidator(Protocol):
    """Validate arguments against a JSON-Schema-shaped dict.
    Returns a list of human-readable errors (empty = valid).
    This is the piece worth reusing beyond tool calls."""

    def check_args(self, arguments: dict[str, Any], schema: dict[str, Any] | None) -> list[str]: ...


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
    """The actual tool executor (an MCP session, an HTTP client, ...). Gets the
    whole call: arguments already materialized, ``agent_id`` already verified."""

    async def call_tool(self, call: ToolCall) -> ToolResult: ...


@runtime_checkable
class TicketStore(Protocol):
    """Durable home for gated calls. ``await_decision`` is how the
    interactive mode blocks; the async mode never calls it.

    ``create`` keeps a snapshot of the call: later changes to the object (the
    pipeline materializes secrets in place) must not reach the ticket.
    ``claim`` is the run-once guard: it returns True for exactly one caller,
    and only while the ticket has no result; ``complete`` stores that result.
    """

    async def create(self, ticket_id: str, call: ToolCall) -> None: ...

    async def get(self, ticket_id: str) -> Ticket | None: ...

    async def decide(self, ticket_id: str, decision: Decision) -> None: ...

    async def await_decision(self, ticket_id: str, *, timeout_seconds: float) -> Decision: ...

    async def claim(self, ticket_id: str) -> bool: ...

    async def complete(self, ticket_id: str, result: ToolResult) -> None: ...


@runtime_checkable
class AuditLog(Protocol):
    """One sink for the whole flow; the pipeline wraps steps with it so
    audit is cross-cutting, not interleaved with logic."""

    async def audit_event(self, event: str, call: ToolCall, **fields: Any) -> None: ...


@runtime_checkable
class ToolCatalog(Protocol):
    """The tools an agent may discover, as MCP tool specs
    (``{"name", "description", "inputSchema"}``). Backs ``tools/list``."""

    async def tools_for(self, agent_id: str) -> list[dict[str, Any]]: ...
