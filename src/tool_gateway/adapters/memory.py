"""In-memory adapters: runnable and testable with zero infrastructure.

They implement every port so the gateway works end to end out of the box;
fleet swaps them for a vault, an MCP transport and a DB one at a time.
"""

import asyncio
import copy
import time

from ..domain import (
    Decision,
    DecisionStatus,
    Grant,
    SecretLeak,
    SecretRefMissing,
    Ticket,
    ToolCall,
    ToolResult,
)


class DictGrantResolver:
    """Grants keyed by ``(agent_id, full_name)`` with a fallback per tool."""

    def __init__(self, grants: dict[tuple[str, str], Grant] | None = None):
        self._grants = grants or {}

    def allow(self, agent_id: str, full_name: str, grant: Grant) -> None:
        self._grants[(agent_id, full_name)] = grant

    async def grant_for(self, call: ToolCall) -> Grant | None:
        return self._grants.get((call.agent_id, call.full_name))


class MiniSchemaValidator:
    """Dependency-free JSON-Schema subset: ``type=object``, ``required`` and
    per-property scalar ``type``. A full jsonschema impl plugs into the same
    port when richer validation is needed."""

    _PY = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}

    def check_args(self, arguments: dict, schema: dict | None) -> list[str]:
        if not schema:
            return []
        errors: list[str] = []
        for key in schema.get("required", []):
            if key not in arguments:
                errors.append(f"missing required field '{key}'")
        for key, spec in (schema.get("properties") or {}).items():
            if key in arguments and (expected := self._PY.get(spec.get("type"))):
                if not isinstance(arguments[key], expected):
                    errors.append(f"field '{key}' must be {spec['type']}")
        return errors


class DictSecretResolver:
    """Materializes ``secret://ref`` values from a dict and redacts any active
    secret echoed back (fail-closed)."""

    def __init__(self, secrets: dict[str, str] | None = None):
        self._secrets = secrets or {}

    def define(self, ref: str, value: str) -> None:
        self._secrets[ref] = value

    async def materialize(self, arguments: dict, *, upstream_id: str, agent_id: str) -> tuple[dict, list[str]]:
        used: list[str] = []
        resolved = {}
        for key, value in arguments.items():
            if isinstance(value, str) and value.startswith("secret://"):
                ref = value.removeprefix("secret://")
                if ref not in self._secrets:
                    raise SecretRefMissing([ref])
                resolved[key] = self._secrets[ref]
                used.append(ref)
            else:
                resolved[key] = value
        return resolved, used

    def redact(self, result: ToolResult, *, upstream_id: str) -> ToolResult:
        text = str(result.content)
        for value in self._secrets.values():
            if value and value in text:
                raise SecretLeak("upstream echoed an active secret")
        return result


class EchoUpstream:
    """A fake upstream. Captures what it received in ``received`` and, by
    default, echoes the arguments back; ``echo=False`` returns a benign
    response (so a materialized secret is not sent back to the agent).
    ``leak`` forces a configured value into the response to exercise
    fail-closed redaction."""

    def __init__(self, leak: str | None = None, echo: bool = True):
        self._leak = leak
        self._echo = echo
        self.received: list[dict] = []

    async def call_tool(self, call: ToolCall) -> ToolResult:
        self.received.append(call.arguments)
        content = (
            {"tool": call.tool_name, "echo": call.arguments} if self._echo else {"tool": call.tool_name, "ok": True}
        )
        if self._leak:
            content["oops"] = self._leak
        return ToolResult(content=content)


class MemoryTicketStore:
    """Single process, lost on restart."""

    def __init__(self):
        self._tickets: dict[str, Ticket] = {}
        self._claimed: set[str] = set()

    async def create(self, ticket_id: str, call: ToolCall) -> None:
        self._tickets[ticket_id] = Ticket(copy.deepcopy(call), Decision(status=DecisionStatus.PENDING))

    async def get(self, ticket_id: str) -> Ticket | None:
        ticket = self._tickets.get(ticket_id)
        return copy.deepcopy(ticket) if ticket else None

    async def decide(self, ticket_id: str, decision: Decision) -> bool:
        ticket = self._tickets.get(ticket_id)
        if ticket is None or ticket.decision.status is not DecisionStatus.PENDING:
            return False
        ticket.decision = copy.deepcopy(decision)
        return True

    async def claim(self, ticket_id: str) -> bool:
        ticket = self._tickets.get(ticket_id)
        if ticket is None or ticket.result is not None or ticket_id in self._claimed:
            return False
        self._claimed.add(ticket_id)
        return True

    async def pending(self) -> dict[str, Ticket]:
        return {
            tid: copy.deepcopy(t) for tid, t in self._tickets.items() if t.decision.status is DecisionStatus.PENDING
        }

    async def complete(self, ticket_id: str, result: ToolResult) -> None:
        self._tickets[ticket_id].result = copy.deepcopy(result)


class MemoryDecisionSignal:
    """In-process wake-up. Race-free here: a signal sent before anyone waits
    is kept, so the wait returns at once. Other replicas never hear it."""

    def __init__(self):
        self._events: dict[str, asyncio.Event] = {}

    async def wait_for_decision(self, ticket_id: str, *, timeout_seconds: float) -> None:
        event = self._events.setdefault(ticket_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return
        self._events.pop(ticket_id, None)

    async def signal_decision(self, ticket_id: str) -> None:
        self._events.setdefault(ticket_id, asyncio.Event()).set()


class DictToolCatalog:
    """Tool specs per agent for ``tools/list``. Empty by default: an agent
    discovers nothing until a real catalog is wired."""

    def __init__(self, tools: dict[str, list[dict]] | None = None):
        self._tools = tools or {}

    def publish(self, agent_id: str, spec: dict) -> None:
        self._tools.setdefault(agent_id, []).append(spec)

    async def tools_for(self, agent_id: str) -> list[dict]:
        return list(self._tools.get(agent_id, []))


class ListAuditLog:
    def __init__(self):
        self.events: list[dict] = []

    async def audit_event(self, event: str, call: ToolCall, **fields) -> None:
        self.events.append({"event": event, "tool": call.full_name, "agent": call.agent_id, **fields})

    def actions(self) -> list[str]:
        return [e["event"] for e in self.events]


class WindowRateLimiter:
    """At most ``calls_per_minute`` per agent in each clock minute; 0 admits all.

    ponytail: fixed window in this process. With N replicas each enforces its
    own budget (N x the limit); a shared limiter (Redis INCR + EXPIRE) plugs
    into the RateLimiter port when one global budget matters.
    """

    def __init__(self, calls_per_minute: int = 0, *, clock=time.time):
        self._limit = calls_per_minute
        self._clock = clock
        self._window = -1
        self._counts: dict[str, int] = {}

    async def admit_call(self, agent_id: str, tool: str) -> bool:
        if self._limit <= 0:
            return True
        window = int(self._clock() // 60)
        if window != self._window:
            self._window, self._counts = window, {}
        self._counts[agent_id] = self._counts.get(agent_id, 0) + 1
        return self._counts[agent_id] <= self._limit
