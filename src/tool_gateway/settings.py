"""pico-ioc settings, populated from the ``tool_gateway`` config prefix."""

from dataclasses import dataclass

from pico_ioc import configured


@configured(target="self", prefix="tool_gateway", mapping="tree")
@dataclass
class ToolGatewaySettings:
    approval_timeout_seconds: float = 300.0
