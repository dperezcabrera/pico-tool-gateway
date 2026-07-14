"""In-memory adapters: runnable and testable with zero infrastructure.

They implement every port so the gateway works end to end out of the box;
fleet swaps them for a vault, an MCP transport and a DB one at a time.
"""

import asyncio

from ..domain import (
    Decision,
    DecisionStatus,
    Grant,
    SecretLeak,
    SecretRefMissing,
    ToolCall,
    ToolResult,
)


class DictGrantResolver:
    """Grants keyed by ``(agent_id, full_name)`` with a fallback per tool."""

    def __init__(self, grants: dict[tuple[str, str], Grant] | None = None):
        self._grants = grants or {}

    def allow(self, agent_id: str, full_name: str, grant: Grant) -> None:
        self._grants[(agent_id, full_name)] = grant

    async def resolve(self, call: ToolCall) -> Grant | None:
        return self._grants.get((call.agent_id, call.full_name))


class MiniSchemaValidator:
    """Dependency-free JSON-Schema subset: ``type=object``, ``required`` and
    per-property scalar ``type``. A full jsonschema impl plugs into the same
    port when richer validation is needed."""

    _PY = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}

    def validate(self, arguments: dict, schema: dict | None) -> list[str]:
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

    async def call(self, upstream_id: str, tool_name: str, arguments: dict) -> ToolResult:
        self.received.append(arguments)
        content = {"tool": tool_name, "echo": arguments} if self._echo else {"tool": tool_name, "ok": True}
        if self._leak:
            content["oops"] = self._leak
        return ToolResult(content=content)


class MemoryTicketStore:
    """Durable-enough for tests: a dict plus an event per pending ticket so
    interactive mode can block until :meth:`decide` fires."""

    def __init__(self):
        self._calls: dict[str, ToolCall] = {}
        self._decisions: dict[str, Decision] = {}
        self._events: dict[str, asyncio.Event] = {}

    async def create(self, ticket_id: str, call: ToolCall) -> None:
        self._calls[ticket_id] = call
        self._decisions[ticket_id] = Decision(status=DecisionStatus.PENDING)
        self._events[ticket_id] = asyncio.Event()

    async def get(self, ticket_id: str):
        if ticket_id not in self._calls:
            return None
        return self._calls[ticket_id], self._decisions[ticket_id]

    async def decide(self, ticket_id: str, decision: Decision) -> None:
        self._decisions[ticket_id] = decision
        self._events[ticket_id].set()

    async def await_decision(self, ticket_id: str, *, timeout_seconds: float) -> Decision:
        try:
            await asyncio.wait_for(self._events[ticket_id].wait(), timeout=timeout_seconds)
        except TimeoutError:
            return Decision(status=DecisionStatus.TIMEOUT)
        return self._decisions[ticket_id]


class ListAuditLog:
    def __init__(self):
        self.events: list[dict] = []

    async def record(self, event: str, call: ToolCall, **fields) -> None:
        self.events.append({"event": event, "tool": call.full_name, "agent": call.agent_id, **fields})

    def actions(self) -> list[str]:
        return [e["event"] for e in self.events]
