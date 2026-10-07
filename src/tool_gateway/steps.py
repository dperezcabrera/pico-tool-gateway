"""The steps. Each is small, single-purpose and testable on its own — the
opposite of the 290-line procedural method this redesign replaces."""

import time

from .domain import (
    RateLimited,
    SchemaInvalid,
    SecretLeak,
    ToolNotAllowed,
    UpstreamUnavailable,
)
from .pipeline import CallContext, Next, Stage
from .ports import GrantResolver, RateLimiter, SchemaValidator, SecretResolver, Upstream


class RateLimit:
    """Turn away an agent over its call budget before any other work."""

    stage, order = Stage.BEFORE_APPROVAL, 50

    def __init__(self, limiter: RateLimiter):
        self._limiter = limiter

    async def handle_call(self, ctx: CallContext, call_next: Next):
        if not await self._limiter.admit_call(ctx.call.agent_id, ctx.call.full_name):
            raise RateLimited(f"rate limit exceeded for agent {ctx.call.agent_id}")
        return await call_next(ctx)


class Authorize:
    """Resolve the grant (authz + approval mode + schema) or reject."""

    stage, order = Stage.BEFORE_APPROVAL, 100

    def __init__(self, grants: GrantResolver):
        self._grants = grants

    async def handle_call(self, ctx: CallContext, call_next: Next):
        grant = await self._grants.grant_for(ctx.call)
        if grant is None:
            raise ToolNotAllowed(f"not allowed: {ctx.call.full_name}")
        ctx.grant = grant
        await ctx.audit.audit_event("authorized", ctx.call, approval_mode=grant.approval_mode.value)
        return await call_next(ctx)


class ValidateSchema:
    """Validate arguments against the grant's input_schema. Runs AFTER the
    approval gate so operator-edited arguments are validated too."""

    stage, order = Stage.AFTER_APPROVAL, 100

    def __init__(self, validator: SchemaValidator):
        self._validator = validator

    async def handle_call(self, ctx: CallContext, call_next: Next):
        schema = ctx.grant.input_schema if ctx.grant else None
        errors = self._validator.check_args(ctx.call.arguments, schema)
        if errors:
            raise SchemaInvalid(errors)
        return await call_next(ctx)


class MaterializeSecrets:
    """Resolve secret refs just before dispatch; record which refs were used
    so the redactor can catch a buggy upstream echoing them back."""

    stage, order = Stage.AFTER_APPROVAL, 200

    def __init__(self, secrets: SecretResolver):
        self._secrets = secrets

    async def handle_call(self, ctx: CallContext, call_next: Next):
        args, refs = await self._secrets.materialize(
            ctx.call.arguments, upstream_id=ctx.call.upstream_id, agent_id=ctx.call.agent_id
        )
        ctx.call.arguments = args
        if refs:
            ctx.bag["materialized_refs"] = refs
            await ctx.audit.audit_event("refs_materialized", ctx.call, refs=refs)
        return await call_next(ctx)


class Redact:
    """Wrap the dispatch: redact the result fail-closed. A leak is rejected,
    never forwarded."""

    stage, order = Stage.AFTER_APPROVAL, 300

    def __init__(self, secrets: SecretResolver):
        self._secrets = secrets

    async def handle_call(self, ctx: CallContext, call_next: Next):
        result = await call_next(ctx)
        try:
            return self._secrets.redact(result, upstream_id=ctx.call.upstream_id)
        except SecretLeak:
            await ctx.audit.audit_event("leak_detected", ctx.call)
            raise


class Dispatch:
    """Terminal step: run the tool upstream. Does not call ``call_next``."""

    def __init__(self, upstream: Upstream):
        self._upstream = upstream

    async def handle_call(self, ctx: CallContext, call_next: Next):
        started = time.monotonic()
        try:
            result = await self._upstream.call_tool(ctx.call)
        except Exception as exc:  # noqa: BLE001
            await ctx.audit.audit_event("call_failed", ctx.call, error=f"{type(exc).__name__}: {exc}")
            raise UpstreamUnavailable(f"tool call failed: {exc}") from exc
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        note = ctx.bag.get("edit_note")
        if note:
            result.notes.insert(0, note)
        await ctx.audit.audit_event("call", ctx.call, elapsed_ms=result.elapsed_ms, is_error=result.is_error)
        return result
