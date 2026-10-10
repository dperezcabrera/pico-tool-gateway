"""Declarative policy: the GrantResolver as DATA, not code.

Policy is an ordered list of rules plus a default. Each rule matches on the
agent, the tool (glob), and optional conditions over the call arguments, and
yields an approval mode or a denial. First match wins; no match falls to the
default. Change policy by publishing a new document — no gateway code, no restart;
every replica picks it up from the shared PolicySource. Pure stdlib (fnmatch); a Rego/Cedar engine plugs
into the same GrantResolver port when you outgrow this.

    default: deny
    trust_hints_from: [bank, "internal-*"]
    rules:
      - {tool: "github.get_*", mode: auto}
      - {tool: "*.delete_*", mode: interactive}
      - {tool: "payments.charge", when: [{arg: amount_cents, op: le, value: 10000}], mode: auto}
      - {tool: "payments.charge", mode: interactive}   # larger charges
      - {tool: "*", agent: "trusted-*", mode: async}
      - {tool: "*", hints: {readOnlyHint: true}, mode: auto}
      - {tool: "*", hints: {destructiveHint: true}, mode: interactive}

``hints`` match the MCP tool annotations the upstream declares, read as the
spec does: a missing hint takes its default (destructive and open-world unless
said otherwise) and a read-only tool is never destructive. They are claims of
the upstream, so they count only for upstreams listed in ``trust_hints_from``
(globs); for any other upstream every hint takes its spec default, which is
the conservative reading (possibly destructive, open world).
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from .domain import ApprovalMode, Grant, ToolCall

logger = logging.getLogger(__name__)

_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: _num(a) > _num(b),
    "ge": lambda a, b: _num(a) >= _num(b),
    "lt": lambda a, b: _num(a) < _num(b),
    "le": lambda a, b: _num(a) <= _num(b),
    "in": lambda a, b: a in b,
}


# MCP spec defaults for an absent annotation
_HINT_DEFAULTS = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}


def _effective_hints(annotations: dict) -> dict[str, bool]:
    """The four hints as the spec reads them, defaults filled in."""
    hints = {k: bool(annotations[k]) if k in annotations else v for k, v in _HINT_DEFAULTS.items()}
    if hints["readOnlyHint"]:
        hints["destructiveHint"] = False
    return hints


class PolicyError(Exception):
    pass


def _num(v: Any) -> float:
    return float(v)


@dataclass
class _Cond:
    arg: str
    op: str
    value: Any

    def holds(self, arguments: dict) -> bool:
        if self.arg not in arguments:
            return False
        try:
            return bool(_OPS[self.op](arguments[self.arg], self.value))
        except (TypeError, ValueError):
            return False  # type mismatch fails closed


@dataclass
class _Rule:
    tool: str
    agents: list[str]
    conds: list[_Cond]
    hints: dict[str, bool]
    deny: bool
    mode: ApprovalMode | None
    input_schema: dict | None

    def targets(self, call: ToolCall, trusted: bool) -> bool:
        """Tool, agent and hints match; the argument conditions are not checked."""
        if not fnmatchcase(call.full_name, self.tool):
            return False
        if not any(fnmatchcase(call.agent_id, g) for g in self.agents):
            return False
        if self.hints:
            effective = _effective_hints(call.annotations if trusted else {})
            if any(effective[k] != v for k, v in self.hints.items()):
                return False
        return True

    def matches(self, call: ToolCall, trusted: bool) -> bool:
        return self.targets(call, trusted) and all(c.holds(call.arguments) for c in self.conds)


def _compile_rule(raw: dict) -> _Rule:
    deny = bool(raw.get("deny", False))
    mode = None
    if not deny:
        try:
            mode = ApprovalMode(raw.get("mode", "auto"))
        except ValueError as exc:
            raise PolicyError(f"invalid mode {raw.get('mode')!r}") from exc
    agent = raw.get("agent", "*")
    agents = [str(a) for a in agent] if isinstance(agent, list) else [str(agent)]
    conds = []
    for c in raw.get("when") or []:
        if c.get("op") not in _OPS:
            raise PolicyError(f"invalid op {c.get('op')!r}")
        conds.append(_Cond(arg=str(c["arg"]), op=c["op"], value=c.get("value")))
    hints = raw.get("hints") or {}
    for key, value in hints.items():
        if key not in _HINT_DEFAULTS or not isinstance(value, bool):
            raise PolicyError(f"invalid hint {key!r}: {value!r}")
    return _Rule(
        tool=str(raw.get("tool", "*")),
        agents=agents,
        conds=conds,
        hints=hints,
        deny=deny,
        mode=mode,
        input_schema=raw.get("input_schema"),
    )


@dataclass
class _Compiled:
    rules: list[_Rule]
    default: Grant | None
    trust: list[str]


def compile_policy(doc: dict) -> _Compiled:
    """Validate and compile a policy document; raises PolicyError, changes nothing."""
    rules = [_compile_rule(r) for r in doc.get("rules") or []]
    default = doc.get("default", "deny")
    if default == "deny":
        default_grant = None
    else:
        try:
            default_grant = Grant(ApprovalMode(default))
        except ValueError as exc:
            raise PolicyError(f"invalid default {default!r}") from exc
    return _Compiled(rules, default_grant, [str(g) for g in doc.get("trust_hints_from") or []])


class MemoryPolicySource:
    """One process only: publishing here reaches no other replica."""

    def __init__(self, doc: dict | None = None):
        self._doc = doc
        self._version = 1 if doc is not None else 0

    async def load_policy(self, newer_than: str | None) -> tuple[str, dict] | None:
        if self._doc is None or str(self._version) == newer_than:
            return None
        return str(self._version), self._doc

    async def publish_policy(self, doc: dict, *, by: str = "") -> str:
        self._doc, self._version = doc, self._version + 1
        return str(self._version)


class FilePolicySource:
    """A JSON file, versioned by content hash. Replicas that share the file
    (a mounted volume, a ConfigMap) converge on whatever it holds."""

    def __init__(self, path: str):
        self._path = Path(path)

    async def load_policy(self, newer_than: str | None) -> tuple[str, dict] | None:
        try:
            raw = await asyncio.to_thread(self._path.read_bytes)
        except FileNotFoundError:
            return None
        version = hashlib.sha256(raw).hexdigest()[:16]
        return None if version == newer_than else (version, json.loads(raw))

    async def publish_policy(self, doc: dict, *, by: str = "") -> str:
        raw = json.dumps(doc, indent=2).encode()
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        await asyncio.to_thread(tmp.write_bytes, raw)
        await asyncio.to_thread(os.replace, tmp, self._path)  # atomic: readers never see half a file
        return hashlib.sha256(raw).hexdigest()[:16]


class DeclarativePolicy:
    """A GrantResolver driven by declarative rules kept in a PolicySource.

    Every replica asks the source for a newer version at most every
    ``refresh_seconds`` and recompiles only when it changed, so a publish
    reaches all replicas within that time without any coordination. A
    document that fails to compile is logged and ignored: the replica keeps
    its last good policy. With no policy loaded at all, everything is denied.
    """

    def __init__(
        self,
        default: str = "deny",
        rules: list[dict] | None = None,
        *,
        trust_hints_from: list[str] | None = None,
        path: str = "",
        source: Any = None,
        refresh_seconds: float = 5.0,
    ):
        self._compiled: _Compiled | None = None
        self._doc: dict | None = None
        self._version: str | None = None
        self._checked = float("-inf")
        self._refresh = refresh_seconds
        self._lock = asyncio.Lock()
        if source is None and not path:
            doc = {"default": default, "rules": rules or [], "trust_hints_from": trust_hints_from or []}
            self._compiled = compile_policy(doc)  # fail fast on a bad inline ruleset
            source = MemoryPolicySource(doc)
            self._doc, self._version, self._checked = doc, "1", time.monotonic()
        self._source = source or FilePolicySource(path)

    async def grant_for(self, call: ToolCall) -> Grant | None:
        await self._refresh_if_due()
        policy = self._compiled
        if policy is None:
            return None  # nothing loaded: fail closed
        trusted = any(fnmatchcase(call.upstream_id, g) for g in policy.trust)
        for rule in policy.rules:
            if rule.matches(call, trusted):
                return None if rule.deny else Grant(rule.mode, rule.input_schema)
        return policy.default

    async def may_call(self, call: ToolCall) -> bool:
        """Could this agent call this tool with some arguments? Decides what
        ``tools/list`` shows, before any arguments exist: an unconditional
        rule is final, a conditional allow means "yes, for some arguments",
        and a conditional deny might not apply, so the search goes on."""
        await self._refresh_if_due()
        policy = self._compiled
        if policy is None:
            return False
        trusted = any(fnmatchcase(call.upstream_id, g) for g in policy.trust)
        for rule in policy.rules:
            if not rule.targets(call, trusted):
                continue
            if not rule.conds or not rule.deny:
                return not rule.deny
        return policy.default is not None

    async def publish(self, doc: dict, *, by: str = "") -> str:
        """Validate, store as the new version for every replica, apply here now."""
        compiled = compile_policy(doc)
        version = await self._source.publish_policy(doc, by=by)
        self._compiled, self._doc, self._version, self._checked = compiled, doc, version, time.monotonic()
        return version

    async def refresh(self) -> None:
        """Check the source now instead of waiting for the next interval."""
        self._checked = float("-inf")
        await self._refresh_if_due()

    def current(self) -> tuple[str | None, dict | None]:
        return self._version, self._doc

    async def _refresh_if_due(self) -> None:
        if time.monotonic() - self._checked < self._refresh:
            return
        async with self._lock:
            if time.monotonic() - self._checked < self._refresh:
                return  # another caller refreshed while this one waited
            self._checked = time.monotonic()
            try:
                loaded = await self._source.load_policy(self._version)
                if loaded is None:
                    return
                version, doc = loaded
                compiled = compile_policy(doc)
            except Exception:  # noqa: BLE001
                logger.exception("policy refresh failed; keeping version %s", self._version)
                return
            self._compiled, self._doc, self._version = compiled, doc, version
