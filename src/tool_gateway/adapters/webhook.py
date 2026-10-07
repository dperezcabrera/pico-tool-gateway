"""Approver notification over a signed webhook, delivered in the background
with bounded retries so the agent never waits on the approval channel."""

import asyncio
import hashlib
import hmac
import json
import logging

import httpx

from ..domain import ApprovalMode, ToolCall
from ..ports import AuditLog

logger = logging.getLogger(__name__)


class WebhookNotifier:
    """POSTs one JSON event per gated call to ``url``. With ``secret`` the body
    is signed (``X-Pico-Signature: sha256=<hex hmac>``) so the receiver can
    tell the gateway from anyone else. Network errors and 5xx are retried with
    exponential backoff; a 4xx is final. The outcome lands in the audit log."""

    def __init__(
        self,
        url: str,
        audit: AuditLog,
        *,
        secret: str = "",
        attempts: int = 3,
        backoff_seconds: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._url = url
        self._audit = audit
        self._secret = secret.encode()
        self._attempts = attempts
        self._backoff = backoff_seconds
        self._transport = transport
        self._inflight: set[asyncio.Task] = set()

    async def notify_approvers(self, ticket_id: str, call: ToolCall, mode: ApprovalMode) -> None:
        if not self._url:
            return  # no channel configured: operators work from the ticket API
        # serialize now: the pipeline goes on to materialize secrets into this call
        body = json.dumps(
            {
                "event": "approval_requested",
                "ticket_id": ticket_id,
                "approval_mode": mode.value,
                "agent_id": call.agent_id,
                "tool": call.full_name,
                "arguments": call.arguments,
                "annotations": call.annotations,
            },
            default=str,
        ).encode()
        task = asyncio.create_task(self._deliver(ticket_id, call, body))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def drain(self) -> None:
        """Wait for the deliveries in flight (shutdown, tests)."""
        while self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)

    async def _deliver(self, ticket_id: str, call: ToolCall, body: bytes) -> None:
        headers = {"Content-Type": "application/json"}
        if self._secret:
            headers["X-Pico-Signature"] = "sha256=" + hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        error = ""
        async with httpx.AsyncClient(transport=self._transport, timeout=10) as client:
            for attempt in range(1, self._attempts + 1):
                try:
                    response = await client.post(self._url, content=body, headers=headers)
                    if response.status_code < 400:
                        await self._audit.audit_event("notified", call, ticket_id=ticket_id, attempts=attempt)
                        return
                    error = f"HTTP {response.status_code}"
                    if response.status_code < 500:
                        break  # the receiver rejected it: retrying will not help
                except httpx.HTTPError as exc:
                    error = f"{type(exc).__name__}: {exc}"
                if attempt < self._attempts:
                    await asyncio.sleep(self._backoff * 2 ** (attempt - 1))
        logger.error("approval notification for %s failed: %s", ticket_id, error)
        await self._audit.audit_event("notify.error", call, ticket_id=ticket_id, error=error)
