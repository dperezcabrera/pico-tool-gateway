"""pico-ioc wiring: makes the gateway a drop-in module that runs in ONE
process with NO companion services.

Every port has a safe in-process default registered with
``on_missing_selector`` — provide your own ``@component`` of the same
protocol to override it. The only port WITHOUT a default is ``Upstream``:
the real tool executor is yours to wire, and the container fails fast if
it is missing rather than silently doing nothing.

No broker, no worker, no external DB: async approval is a durable ticket
plus an in-process ``resume()`` call, not a separate consumer.
"""

from pico_ioc import component, factory, provides

from .adapters.memory import (
    DictSecretResolver,
    DictToolCatalog,
    ListAuditLog,
    MemoryTicketStore,
    MiniSchemaValidator,
)
from .domain import ToolResult, UpstreamUnavailable
from .gateway import ToolGateway
from .policy import DeclarativePolicy
from .ports import (
    AuditLog,
    GrantResolver,
    SchemaValidator,
    SecretResolver,
    TicketStore,
    ToolCatalog,
    Upstream,
)
from .settings import ToolGatewaySettings


@component(on_missing_selector=GrantResolver)
class _DefaultPolicy(DeclarativePolicy):
    """Default authorizer: declarative rules from the JSON policy file at
    ``tool_gateway.policy_path`` (deny-all when unset). Override by
    registering your own GrantResolver (e.g. an OPA/Cedar adapter)."""

    def __init__(self, settings: ToolGatewaySettings):
        super().__init__(path=settings.policy_path)


@component(on_missing_selector=SchemaValidator)
class _DefaultValidator(MiniSchemaValidator):
    """Dependency-free JSON-Schema subset; swap for a full validator."""


@component(on_missing_selector=SecretResolver)
class _DefaultSecrets(DictSecretResolver):
    """No secrets defined: refs raise, redaction is a no-op. Wire a vault."""


@component(on_missing_selector=TicketStore)
class _DefaultTickets(MemoryTicketStore):
    """In-process, single-instance. For durability across restarts or
    multiple replicas, register a persistent TicketStore (e.g. sqlite via
    pico-sqlalchemy — still one process, no server)."""


@component(on_missing_selector=AuditLog)
class _DefaultAudit(ListAuditLog):
    """In-memory audit; register a persistent AuditLog for retention."""


@component(on_missing_selector=ToolCatalog)
class _DefaultCatalog(DictToolCatalog):
    """Empty by default: agents discover no tools until a catalog is wired."""


@component(on_missing_selector=Upstream)
class _UnwiredUpstream:
    """Fail-fast default: an app MUST wire a real tool executor. Booting
    without one is fine; the first tool call reports the missing wiring."""

    async def invoke(self, upstream_id: str, tool_name: str, arguments: dict) -> ToolResult:
        raise UpstreamUnavailable("no Upstream wired: register a @component implementing tool_gateway.ports.Upstream")


@factory
class ToolGatewayFactory:
    """Assembles the pure ToolGateway from injected ports. The core stays
    framework-free; this factory is the only pico-aware assembly."""

    @provides(ToolGateway, scope="singleton")
    def build(
        self,
        settings: ToolGatewaySettings,
        grants: GrantResolver,
        validator: SchemaValidator,
        secrets: SecretResolver,
        upstream: Upstream,
        tickets: TicketStore,
        audit: AuditLog,
    ) -> ToolGateway:
        return ToolGateway(
            grants=grants,
            validator=validator,
            secrets=secrets,
            upstream=upstream,
            tickets=tickets,
            audit=audit,
            approval_timeout_seconds=settings.approval_timeout_seconds,
        )
