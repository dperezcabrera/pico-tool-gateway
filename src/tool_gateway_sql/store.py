import asyncio
import dataclasses
from datetime import UTC, datetime
from typing import Any

from pico_ioc import component
from pico_sqlalchemy import AppBase, Mapped, SessionManager, mapped_column
from sqlalchemy import JSON, DateTime, Index, String, and_, or_, select, update

from tool_gateway.domain import Decision, DecisionStatus, Ticket, ToolCall, ToolResult


class TicketRow(AppBase):
    __tablename__ = "tool_gateway_tickets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(255), index=True)
    tool: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(16))
    call: Mapped[dict] = mapped_column(JSON)
    decision: Mapped[dict] = mapped_column(JSON)
    claimed: Mapped[bool] = mapped_column(default=False)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_tool_gateway_tickets_queue", "status", "created_at", "id"),)


class AuditRow(AppBase):
    __tablename__ = "tool_gateway_audit"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    event: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(255))
    agent_id: Mapped[str] = mapped_column(String(255), index=True)
    tool: Mapped[str] = mapped_column(String(255))
    fields: Mapped[dict] = mapped_column(JSON)


class PolicyRow(AppBase):
    """Append-only: every publish is a new version, kept for audit and rollback."""

    __tablename__ = "tool_gateway_policy"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    doc: Mapped[dict] = mapped_column(JSON)
    published_by: Mapped[str] = mapped_column(String(255))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def _decision(raw: dict) -> Decision:
    return Decision(**{**raw, "status": DecisionStatus(raw["status"])})


@component
class SqlTicketStore:
    """Tickets as rows. ``claim`` and ``decide`` are conditional UPDATEs, so
    exactly one caller wins even across replicas sharing the database."""

    def __init__(self, sessions: SessionManager):
        self._sessions = sessions

    async def create(self, ticket_id: str, call: ToolCall) -> None:
        pending = Decision(status=DecisionStatus.PENDING)
        async with self._sessions.transaction() as session:
            session.add(
                TicketRow(
                    id=ticket_id,
                    agent_id=call.agent_id,
                    tool=call.full_name,
                    status=pending.status.value,
                    call=dataclasses.asdict(call),
                    decision={**dataclasses.asdict(pending), "status": pending.status.value},
                    created_at=datetime.now(UTC),
                )
            )

    async def get(self, ticket_id: str) -> Ticket | None:
        async with self._sessions.transaction(read_only=True) as session:
            row = await session.get(TicketRow, ticket_id)
            if row is None:
                return None
            return Ticket(
                call=ToolCall(**row.call),
                decision=_decision(row.decision),
                result=ToolResult(**row.result) if row.result is not None else None,
            )

    async def decide(self, ticket_id: str, decision: Decision) -> bool:
        async with self._sessions.transaction() as session:
            decided = await session.execute(
                update(TicketRow)
                .where(TicketRow.id == ticket_id, TicketRow.status == DecisionStatus.PENDING.value)
                .values(
                    status=decision.status.value,
                    decision={**dataclasses.asdict(decision), "status": decision.status.value},
                )
            )
            return decided.rowcount == 1

    async def claim(self, ticket_id: str) -> bool:
        async with self._sessions.transaction() as session:
            won = await session.execute(
                update(TicketRow)
                .where(TicketRow.id == ticket_id, TicketRow.claimed.is_(False), TicketRow.result.is_(None))
                .values(claimed=True)
            )
            return won.rowcount == 1

    async def pending(
        self, *, limit: int = 100, after: str | None = None, tool: str | None = None, agent_id: str | None = None
    ) -> dict[str, Ticket]:
        # keyset pagination on (created_at, id): constant cost per page however deep the queue
        query = select(TicketRow).where(TicketRow.status == DecisionStatus.PENDING.value)
        if tool:
            pattern = tool.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("*", "%")
            query = query.where(TicketRow.tool.like(pattern, escape="\\"))
        if agent_id:
            query = query.where(TicketRow.agent_id == agent_id)
        async with self._sessions.transaction(read_only=True) as session:
            if after is not None:
                cursor = (await session.execute(select(TicketRow.created_at).where(TicketRow.id == after))).scalar()
                if cursor is None:
                    return {}
                query = query.where(
                    or_(TicketRow.created_at > cursor, and_(TicketRow.created_at == cursor, TicketRow.id > after))
                )
            rows = (await session.execute(query.order_by(TicketRow.created_at, TicketRow.id).limit(limit))).scalars()
            return {row.id: Ticket(call=ToolCall(**row.call), decision=_decision(row.decision)) for row in rows}

    async def complete(self, ticket_id: str, result: ToolResult) -> None:
        async with self._sessions.transaction() as session:
            await session.execute(
                update(TicketRow).where(TicketRow.id == ticket_id).values(result=dataclasses.asdict(result))
            )


@component
class SqlAuditLog:
    """Append-only audit rows, one per pipeline event."""

    def __init__(self, sessions: SessionManager):
        self._sessions = sessions

    async def audit_event(self, event: str, call: ToolCall, **fields: Any) -> None:
        async with self._sessions.transaction() as session:
            session.add(
                AuditRow(
                    at=datetime.now(UTC),
                    event=event,
                    request_id=call.request_id,
                    agent_id=call.agent_id,
                    tool=call.full_name,
                    fields=fields,
                )
            )


@component
class SqlPolicySource:
    """The policy as versioned rows shared by every replica. A refresh asks
    only for the latest id; the document is read when it changed."""

    def __init__(self, sessions: SessionManager):
        self._sessions = sessions

    async def load_policy(self, newer_than: str | None) -> tuple[str, dict] | None:
        async with self._sessions.transaction(read_only=True) as session:
            latest = (await session.execute(select(PolicyRow.id).order_by(PolicyRow.id.desc()).limit(1))).scalar()
            if latest is None or str(latest) == newer_than:
                return None
            return str(latest), (await session.get(PolicyRow, latest)).doc

    async def publish_policy(self, doc: dict, *, by: str = "") -> str:
        async with self._sessions.transaction() as session:
            row = PolicyRow(doc=doc, published_by=by, published_at=datetime.now(UTC))
            session.add(row)
            await session.flush()
            return str(row.id)


@component
class GatewaySchema:
    """Creates the gateway tables if missing (a pico-sqlalchemy DatabaseConfigurer)."""

    def configure_database(self, engine) -> None:
        async def _create() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(
                    AppBase.metadata.create_all, tables=[TicketRow.__table__, AuditRow.__table__, PolicyRow.__table__]
                )
            await engine.dispose()  # asyncpg pools are bound to this loop

        asyncio.run(_create())
