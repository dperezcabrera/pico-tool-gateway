"""The steps. Each is small, single-purpose and testable on its own — the
opposite of the 290-line procedural method this redesign replaces."""

import time

from .domain import (
    SchemaInvalid,
    SecretLeak,
    ToolNotAllowed,
    UpstreamUnavailable,
)
from .pipeline import CallContext, Next
from .ports import GrantResolver, SchemaValidator, SecretResolver, Upstream


class Authorize:
    """Resolve the grant (authz + approval mode + schema) or reject."""

    def __init__(self, grants: GrantResolver):
        self._grants = grants

    async def __call__(self, ctx: CallContext, call_next: Next):
        grant = await self._grants.resolve(ctx.call)
        if grant is None:
            raise ToolNotAllowed(f"not allowed: {ctx.call.full_name}")
        ctx.grant = grant
        await ctx.audit.record("authorized", ctx.call, approval_mode=grant.approval_mode.value)
        return await call_next(ctx)


class ValidateSchema:
    """Validate arguments against the grant's input_schema. Runs AFTER the
    approval gate so operator-edited arguments are validated too."""

    def __init__(self, validator: SchemaValidator):
        self._validator = validator

    async def __call__(self, ctx: CallContext, call_next: Next):
        schema = ctx.grant.input_schema if ctx.grant else None
        errors = self._validator.validate(ctx.call.arguments, schema)
        if errors:
            raise SchemaInvalid(errors)
        return await call_next(ctx)


class MaterializeSecrets:
    """Resolve secret refs just before dispatch; record which refs were used
    so the redactor can catch a buggy upstream echoing them back."""

    def __init__(self, secrets: SecretResolver):
        self._secrets = secrets

    async def __call__(self, ctx: CallContext, call_next: Next):
        args, refs = await self._secrets.materialize(
            ctx.call.arguments, upstream_id=ctx.call.upstream_id, agent_id=ctx.call.agent_id
        )
        ctx.call.arguments = args
        if refs:
            ctx.bag["materialized_refs"] = refs
            await ctx.audit.record("refs_materialized", ctx.call, refs=refs)
        return await call_next(ctx)


class Redact:
    """Wrap the dispatch: redact the result fail-closed. A leak is rejected,
    never forwarded."""

    def __init__(self, secrets: SecretResolver):
        self._secrets = secrets

    async def __call__(self, ctx: CallContext, call_next: Next):
        result = await call_next(ctx)
        try:
            return self._secrets.redact(result, upstream_id=ctx.call.upstream_id)
        except SecretLeak:
            await ctx.audit.record("leak_detected", ctx.call)
            raise


class Dispatch:
    """Terminal step: run the tool upstream. Does not call ``call_next``."""

    def __init__(self, upstream: Upstream):
        self._upstream = upstream

    async def __call__(self, ctx: CallContext, call_next: Next):
        started = time.monotonic()
        try:
            result = await self._upstream.call(ctx.call.upstream_id, ctx.call.tool_name, ctx.call.arguments)
        except Exception as exc:  # noqa: BLE001
            await ctx.audit.record("call_failed", ctx.call, error=f"{type(exc).__name__}: {exc}")
            raise UpstreamUnavailable(f"tool call failed: {exc}") from exc
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        note = ctx.bag.get("edit_note")
        if note:
            result.notes.insert(0, note)
        await ctx.audit.record("call", ctx.call, elapsed_ms=result.elapsed_ms, is_error=result.is_error)
        return result
