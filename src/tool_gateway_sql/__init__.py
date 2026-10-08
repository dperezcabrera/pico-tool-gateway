"""Persistent TicketStore, AuditLog and PolicySource for pico-tool-gateway,
on pico-sqlalchemy.

Opt in by listing the module next to the gateway; its components replace the
in-memory defaults and create their tables at startup::

    init(modules=["tool_gateway", "tool_gateway_sql", my_app])

The database is pico-sqlalchemy's (``database.url``): sqlite keeps the gateway
one process with no server, Postgres lets several replicas share tickets.
"""

from .store import SqlAuditLog as SqlAuditLog
from .store import SqlPolicySource as SqlPolicySource
from .store import SqlTicketStore as SqlTicketStore

__all__ = ["SqlAuditLog", "SqlPolicySource", "SqlTicketStore"]
