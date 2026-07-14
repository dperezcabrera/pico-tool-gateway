"""Pure domain: a tool call, its result, an approval decision, and the
control signals the pipeline raises. No I/O, no framework."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ApprovalMode(StrEnum):
    AUTO = "auto"  # forward immediately, no human in the loop
    INTERACTIVE = "interactive"  # hold the caller until a human decides
    ASYNC = "async"  # persist, hand back a ticket, execute on approval


class DecisionStatus(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    PENDING = "pending"


@dataclass
class ToolCall:
    request_id: str
    agent_id: str
    upstream_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{self.upstream_id}.{self.tool_name}"


@dataclass
class ToolResult:
    content: Any
    is_error: bool = False
    elapsed_ms: int = 0
    notes: list[str] = field(default_factory=list)  # operator-edit hints for the agent


@dataclass
class Grant:
    """What the authorizer returns: the call is allowed under this mode,
    validated against this schema (None = no schema declared)."""

    approval_mode: ApprovalMode
    input_schema: dict[str, Any] | None = None


@dataclass
class Decision:
    status: DecisionStatus
    approver: str = ""
    reason: str = ""
    edited_arguments: dict[str, Any] | None = None  # operator patched before approving

    @property
    def approved(self) -> bool:
        return self.status is DecisionStatus.APPROVED


# ── control signals / errors ─────────────────────────────────────


class GatewayError(Exception):
    """Base for every terminal outcome the pipeline maps to a caller error."""


class ToolNotAllowed(GatewayError):
    pass


class UpstreamUnavailable(GatewayError):
    pass


class SchemaInvalid(GatewayError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("arguments do not satisfy input_schema: " + "; ".join(errors))


class ApprovalDenied(GatewayError):
    def __init__(self, decision: Decision):
        self.decision = decision
        super().__init__(
            f"approval {decision.status.value}" + (f" by {decision.approver}" if decision.approver else "")
        )


class SecretRefMissing(GatewayError):
    def __init__(self, refs: list[str]):
        self.refs = refs
        super().__init__("unknown secret refs: " + ", ".join(refs))


class SecretLeak(GatewayError):
    """Fail-closed: the upstream echoed an active secret; the result is
    rejected rather than forwarded."""


class PendingApproval(GatewayError):
    """Not a failure: async mode aborts the request path here so the caller
    returns a ticket handle. Execution resumes via ToolGateway.resume()."""

    def __init__(self, ticket_id: str):
        self.ticket_id = ticket_id
        super().__init__(f"pending approval: {ticket_id}")
