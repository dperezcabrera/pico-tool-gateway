"""pico-ioc settings, populated from the ``tool_gateway`` config prefix."""

from dataclasses import dataclass

from pico_ioc import configured


@configured(target="self", prefix="tool_gateway", mapping="tree")
@dataclass
class ToolGatewaySettings:
    approval_timeout_seconds: float = 300.0
    # path to a JSON policy file {"default": "deny", "rules": [...]}; the
    # plug-and-play artifact — edit it and POST /api/v1/policy/reload. Empty
    # means deny-all.
    policy_path: str = ""
