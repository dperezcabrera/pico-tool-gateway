"""Declarative policy: the GrantResolver as DATA, not code.

Policy is an ordered list of rules plus a default. Each rule matches on the
agent, the tool (glob), and optional conditions over the call arguments, and
yields an approval mode or a denial. First match wins; no match falls to the
default. Change policy by editing the document and reloading — no gateway
code, no companion process. Pure stdlib (fnmatch); a Rego/Cedar engine plugs
into the same GrantResolver port when you outgrow this.

    default: deny
    rules:
      - {tool: "github.get_*", mode: auto}
      - {tool: "*.delete_*", mode: interactive}
      - {tool: "payments.charge", when: [{arg: amount_cents, op: le, value: 10000}], mode: auto}
      - {tool: "payments.charge", mode: interactive}   # larger charges
      - {tool: "*", agent: "trusted-*", mode: async}
"""

import json
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from .domain import ApprovalMode, Grant, ToolCall

_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: _num(a) > _num(b),
    "ge": lambda a, b: _num(a) >= _num(b),
    "lt": lambda a, b: _num(a) < _num(b),
    "le": lambda a, b: _num(a) <= _num(b),
    "in": lambda a, b: a in b,
}


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
    deny: bool
    mode: ApprovalMode | None
    input_schema: dict | None

    def matches(self, call: ToolCall) -> bool:
        if not fnmatchcase(call.full_name, self.tool):
            return False
        if not any(fnmatchcase(call.agent_id, g) for g in self.agents):
            return False
        return all(c.holds(call.arguments) for c in self.conds)


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
    return _Rule(
        tool=str(raw.get("tool", "*")),
        agents=agents,
        conds=conds,
        deny=deny,
        mode=mode,
        input_schema=raw.get("input_schema"),
    )


class DeclarativePolicy:
    """A GrantResolver driven by declarative rules; swap the ruleset at
    runtime with :meth:`reload` (an operator can push new policy without a
    restart)."""

    def __init__(self, default: str = "deny", rules: list[dict] | None = None, *, path: str = ""):
        self._default: Grant | None = None
        self._rules: list[_Rule] = []
        self._path = path
        if path:
            self.reload_from_file()
        else:
            self.reload(default, rules or [])

    def reload_from_file(self) -> None:
        """Re-read the JSON policy file (edit it, then reload — no restart)."""
        if not self._path:
            raise PolicyError("no policy file configured")
        doc = json.loads(Path(self._path).read_text(encoding="utf-8"))
        self.reload(doc.get("default", "deny"), doc.get("rules") or [])

    def reload(self, default: str, rules: list[dict]) -> None:
        compiled = [_compile_rule(r) for r in rules]  # fail-fast on a bad ruleset
        if default == "deny":
            default_grant = None
        else:
            try:
                default_grant = Grant(ApprovalMode(default))
            except ValueError as exc:
                raise PolicyError(f"invalid default {default!r}") from exc
        self._rules = compiled
        self._default = default_grant

    async def grant_for(self, call: ToolCall) -> Grant | None:
        for rule in self._rules:
            if rule.matches(call):
                return None if rule.deny else Grant(rule.mode, rule.input_schema)
        return self._default
