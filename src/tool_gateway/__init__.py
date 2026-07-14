"""pico-tool-gateway: a clean-room redesign of the tool proxy + approval flow.

A tool call runs through a composable pipeline of small steps
(authorize -> approval gate -> validate -> materialize secrets -> redact ->
dispatch), audit is a cross-cutting wrapper, and the three approval modes are
genuinely distinct flows -- with async approval decoupled from the request via
a durable ticket instead of a blocking poll.
"""

from .domain import (
    ApprovalMode,
    Decision,
    DecisionStatus,
    GatewayError,
    Grant,
    ToolCall,
    ToolResult,
)
from .gateway import Pending, ToolGateway, UnknownTicket

__all__ = [
    "ApprovalMode",
    "Decision",
    "DecisionStatus",
    "Grant",
    "GatewayError",
    "ToolCall",
    "ToolResult",
    "ToolGateway",
    "Pending",
    "UnknownTicket",
]
