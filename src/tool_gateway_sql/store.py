import asyncio
import dataclasses
import time
from datetime import UTC, datetime
from typing import Any

from pico_ioc import component
from pico_sqlalchemy import AppBase, Mapped, SessionManager, mapped_column
from sqlalchemy import JSON, DateTime, String, select, update

from tool_gateway.domain import Decision, DecisionStatus, Ticket, ToolCall, ToolResult


class TicketRow(AppBase):
    __tablename__ = "tool_gateway_tickets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    call: Mapped[dict] = mapped_column(JSON)
    decision: Mapped[dict] = mapped_column(JSON)
    claimed: Mapped[bool] = mapped_column(default=False)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditRow(AppBase):
    __tablename__ = "tool_gateway_audit"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    event: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(255))
    agent_id: Mapped[str] = mapped_column(String(255), index=True)
    tool: Mapped[str] = mapped_column(String(255))
    fields: Mapped[dict] = mapped_column(JSON)


def _decision(raw: dict) -> Decision:
    return Decision(**{**raw, "status": DecisionStatus(raw["status"])})


@component
class SqlTicketStore:
    """Tickets as rows. ``claim`` is a conditional UPDATE, so exactly one
    caller wins even across replicas sharing the database."""

    poll_seconds = 0.5  # interactive waits poll the row: a decision may come from another replica

    def __init__(self, sessions: SessionManager):
        self._sessions = sessions

    async def create(self, ticket_id: str, call: ToolCall) -> None:
        pending = Decision(status=DecisionStatus.PENDING)
        async with self._sessions.transaction() as session:
            session.add(
                TicketRow(
                    id=ticket_id,
                    agent_id=call.agent_id,
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

    async def decide(self, ticket_id: str, decision: Decision) -> None:
        async with self._sessions.transaction() as session:
            await session.execute(
                update(TicketRow)
                .where(TicketRow.id == ticket_id)
                .values(
                    status=decision.status.value,
                    decision={**dataclasses.asdict(decision), "status": decision.status.value},
                )
            )

    async def await_decision(self, ticket_id: str, *, timeout_seconds: float) -> Decision:
        deadline = time.monotonic() + timeout_seconds
        while True:
            async with self._sessions.transaction(read_only=True) as session:
                raw = (await session.execute(select(TicketRow.decision).where(TicketRow.id == ticket_id))).scalar()
            if raw is not None and raw["status"] != DecisionStatus.PENDING.value:
                return _decision(raw)
            if time.monotonic() >= deadline:
                return Decision(status=DecisionStatus.TIMEOUT)
            await asyncio.sleep(min(self.poll_seconds, max(deadline - time.monotonic(), 0)))

    async def claim(self, ticket_id: str) -> bool:
        async with self._sessions.transaction() as session:
            won = await session.execute(
                update(TicketRow)
                .where(TicketRow.id == ticket_id, TicketRow.claimed.is_(False), TicketRow.result.is_(None))
                .values(claimed=True)
            )
            return won.rowcount == 1

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
class GatewaySchema:
    """Creates the two gateway tables if missing (a pico-sqlalchemy DatabaseConfigurer)."""

    def configure_database(self, engine) -> None:
        async def _create() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(AppBase.metadata.create_all, tables=[TicketRow.__table__, AuditRow.__table__])
            await engine.dispose()  # asyncpg pools are bound to this loop

        asyncio.run(_create())
