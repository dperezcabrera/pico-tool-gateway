"""Ports: the seams fleet (or anyone) plugs real infrastructure into.

pico-ioc matches these protocols by method names, so each name is specific
(``call_tool``, ``audit_event``): a generic one like ``invoke`` would match
every AOP interceptor in the container.

Each is a Protocol so an adapter needs no base class. The domain and the
pipeline depend only on these — never on a vault, a DB or an MCP transport.
"""

from typing import Any, Protocol, runtime_checkable

from .domain import ApprovalMode, Decision, Grant, Ticket, ToolCall, ToolResult


@runtime_checkable
class GrantResolver(Protocol):
    """Authorize a call and resolve its approval mode + schema.
    Returns None when the agent may not run the tool.

    Optionally also ``async may_call(call) -> bool``: whether the agent could
    call the tool with some arguments. When present, ``tools/list`` only shows
    those tools; without it every catalog tool is listed."""

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
    """Durable home for gated calls: state only. Waking a waiter when a
    verdict lands is the DecisionSignal's job.

    ``create`` keeps a snapshot of the call: later changes to the object (the
    pipeline materializes secrets in place) must not reach the ticket.
    ``claim`` is the run-once guard: it returns True for exactly one caller,
    and only while the ticket has no result; ``complete`` stores that result.
    """

    async def create(self, ticket_id: str, call: ToolCall) -> None: ...

    async def get(self, ticket_id: str) -> Ticket | None: ...

    async def decide(self, ticket_id: str, decision: Decision) -> bool:
        """Record the verdict only while the ticket is pending: a verdict is
        final. False when the ticket is unknown or already decided."""
        ...

    async def claim(self, ticket_id: str) -> bool: ...

    async def complete(self, ticket_id: str, result: ToolResult) -> None: ...

    async def close_stale_claim(self, ticket_id: str, *, older_than_seconds: float, result: ToolResult) -> bool:
        """Store ``result`` if the ticket was claimed more than
        ``older_than_seconds`` ago and still has none (its executor died).
        Atomic: True for exactly one caller."""
        ...

    async def pending(
        self, *, limit: int = 100, after: str | None = None, tool: str | None = None, agent_id: str | None = None
    ) -> dict[str, Ticket]:
        """One page of the operator's queue: tickets still waiting for a
        decision, oldest first, starting after the ticket id ``after``.
        ``tool`` is a glob over ``upstream.tool`` (``bank.*`` for the bank
        team); ``agent_id`` matches exactly."""
        ...


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


@runtime_checkable
class ApproverNotifier(Protocol):
    """Tell the humans that a call waits for them. Called right after the
    ticket exists; must return quickly (deliver in the background) and the
    ticket stands whether or not the message gets through."""

    async def notify_approvers(self, ticket_id: str, call: ToolCall, mode: ApprovalMode) -> None: ...


@runtime_checkable
class RateLimiter(Protocol):
    """Admission control per agent, checked before anything else. Back it
    with shared storage (Redis...) when replicas must share one budget."""

    async def admit_call(self, agent_id: str, tool: str) -> bool: ...


@runtime_checkable
class GatewayStep(Protocol):
    """An extra pipeline step: any component with this shape is inserted at
    ``order`` within its ``stage`` (a ``tool_gateway.pipeline.Stage``).

    Built-in orders: before approval, rate limit 50 and authorize 100; after
    approval, validate 100, materialize secrets 200, redact 300. The approval
    gate sits between the stages and dispatch is always last. A step acts on
    the way in before ``await call_next(ctx)`` and on the way out after it.
    """

    stage: Any
    order: int

    async def handle_call(self, ctx: Any, call_next: Any) -> ToolResult: ...


@runtime_checkable
class DecisionSignal(Protocol):
    """Wake an interactive waiter when its ticket is decided, without polling
    the ticket store. The store stays the source of truth: the gate rereads
    the ticket after every wake and rechecks it periodically, so a lost signal
    only delays a waiter, never misleads it. Back it with Postgres
    LISTEN/NOTIFY or Redis pub/sub when the decision may land on another replica."""

    async def wait_for_decision(self, ticket_id: str, *, timeout_seconds: float) -> None:
        """Return when signalled (or already signalled) or after the timeout."""
        ...

    async def signal_decision(self, ticket_id: str) -> None: ...


@runtime_checkable
class PolicySource(Protocol):
    """Where the declarative policy lives, shared by every replica. Versions
    are opaque strings; ``load_policy`` returns None when nothing newer than
    ``newer_than`` exists (or no policy at all), so a periodic check is cheap."""

    async def load_policy(self, newer_than: str | None) -> tuple[str, dict] | None: ...

    async def publish_policy(self, doc: dict, *, by: str = "") -> str: ...
