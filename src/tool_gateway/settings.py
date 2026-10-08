"""pico-ioc settings, populated from the ``tool_gateway`` config prefix."""

from dataclasses import dataclass, field

from pico_ioc import configured


@configured(target="self", prefix="tool_gateway", mapping="tree")
@dataclass
class ToolGatewaySettings:
    approval_timeout_seconds: float = 300.0
    # an interactive waiter rereads its ticket at least this often, in case a
    # decision signal was lost (e.g. the verdict landed on another replica)
    decision_recheck_seconds: float = 5.0
    # path to a JSON policy file {"default": "deny", "rules": [...]}; the
    # plug-and-play artifact — edit it and POST /api/v1/policy/reload. Empty
    # means deny-all.
    policy_path: str = ""
    # how often each replica asks the PolicySource for a newer version
    policy_refresh_seconds: float = 5.0
    # upstream_id -> streamable HTTP URL of an MCP server; tools are listed as
    # "<upstream_id>.<tool>" with the server's annotations
    upstreams: dict[str, str] = field(default_factory=dict)
    # how long a tools/list of the upstreams is reused before asking them again
    catalog_ttl_seconds: float = 30.0
    # webhook told about every gated call (empty: no notification); with a
    # secret the body is signed in X-Pico-Signature (HMAC-SHA256)
    notify_url: str = ""
    # calls per agent per minute admitted by the default limiter (0: unlimited)
    rate_limit_per_minute: int = 0
    notify_secret: str = ""
