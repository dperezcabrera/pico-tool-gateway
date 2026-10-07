"""pico-ioc wiring: makes the gateway a drop-in module that runs in ONE
process with NO companion services.

Every port has a safe in-process default registered with
``on_missing_selector`` — provide your own ``@component`` of the same
protocol to override it. ``Upstream`` and ``ToolCatalog`` default to the MCP
servers listed in ``tool_gateway.upstreams``; with none configured a call
fails naming the missing upstream rather than silently doing nothing.

No broker, no worker, no external DB: async approval is a durable ticket
plus an in-process ``resume()`` call, not a separate consumer.
"""

from pico_ioc import component, factory, provides

from .adapters.mcp_upstreams import McpUpstreams
from .adapters.memory import (
    DictSecretResolver,
    ListAuditLog,
    MemoryDecisionSignal,
    MemoryTicketStore,
    MiniSchemaValidator,
    WindowRateLimiter,
)
from .adapters.webhook import WebhookNotifier
from .gateway import ToolGateway
from .policy import DeclarativePolicy
from .ports import (
    ApproverNotifier,
    AuditLog,
    DecisionSignal,
    GatewayStep,
    GrantResolver,
    RateLimiter,
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


@component(on_missing_selector=Upstream)
class _McpUpstream(McpUpstreams):
    """Calls the MCP servers in ``tool_gateway.upstreams``. With none
    configured, booting is fine and the first call names the missing upstream."""

    def __init__(self, settings: ToolGatewaySettings):
        super().__init__(settings.upstreams, ttl_seconds=settings.catalog_ttl_seconds)


@component(on_missing_selector=ToolCatalog)
class _McpCatalog(McpUpstreams):
    """Lists the tools of the MCP servers in ``tool_gateway.upstreams`` (none: empty)."""

    def __init__(self, settings: ToolGatewaySettings):
        super().__init__(settings.upstreams, ttl_seconds=settings.catalog_ttl_seconds)


@component(on_missing_selector=ApproverNotifier)
class _DefaultNotifier(WebhookNotifier):
    """Signed webhook to ``tool_gateway.notify_url``; silent when unset."""

    # ponytail: deliveries in flight at shutdown are lost; the ticket stays listed for operators

    def __init__(self, settings: ToolGatewaySettings, audit: AuditLog):
        super().__init__(settings.notify_url, audit, secret=settings.notify_secret)


@component(on_missing_selector=DecisionSignal)
class _DefaultSignal(MemoryDecisionSignal):
    """In-process wake-up; replicas fall back on the periodic recheck."""


@component(on_missing_selector=RateLimiter)
class _DefaultRateLimiter(WindowRateLimiter):
    """Per-process window from ``tool_gateway.rate_limit_per_minute`` (0: off)."""

    def __init__(self, settings: ToolGatewaySettings):
        super().__init__(settings.rate_limit_per_minute)


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
        notifier: ApproverNotifier,
        rate_limiter: RateLimiter,
        steps: list[GatewayStep],
        signal: DecisionSignal,
    ) -> ToolGateway:
        return ToolGateway(
            grants=grants,
            validator=validator,
            secrets=secrets,
            upstream=upstream,
            tickets=tickets,
            audit=audit,
            approval_timeout_seconds=settings.approval_timeout_seconds,
            notifier=notifier,
            rate_limiter=rate_limiter,
            steps=steps,
            signal=signal,
            decision_recheck_seconds=settings.decision_recheck_seconds,
        )
