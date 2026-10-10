# pico-tool-gateway

A clean-room redesign of the tool proxy + approval flow: an agent's tool call runs through a **composable pipeline** of small steps, audit is a **cross-cutting wrapper**, and the three approval modes are **genuinely distinct flows** — with async approval **decoupled from the request** instead of blocking on a human.

A pico module that runs in **one process with no companion services** — no broker, no worker, no external DB. The core is pure Python (zero framework in `domain`, `pipeline`, `steps`, `approval`, `gateway`); a thin `wiring` layer registers it with pico-ioc so it drops into any pico app. Fleet (or anyone) plugs real infrastructure in through ports; the domain never sees a vault, a DB or an MCP transport.

## As a pico module

```python
from pico_ioc import init
from tool_gateway import ToolGateway

container = init(modules=["tool_gateway", my_app])
gateway = container.get(ToolGateway)
```

Every port has a default (`on_missing_selector`). `Upstream` and `ToolCatalog` default to the MCP servers configured in `tool_gateway.upstreams` (see below); with none configured, booting is fine and the first call names the missing upstream. Override any default by registering your own `@component` of the same protocol. Async approval is a durable ticket plus an in-process `resume()` call, so nothing else needs to be running.

With `pico_boot.init()` the module auto-discovers via its `pico_boot.modules` entry point — an app never lists it:

```python
from pico_boot import init
container = init(modules=[my_app])   # tool_gateway loads itself
```

## MCP upstreams

Point the gateway at MCP servers by URL (streamable HTTP):

```yaml
tool_gateway:
  upstreams:
    bank: http://bank-mcp:8000/mcp
    github: http://github-mcp:8000/mcp
  catalog_ttl_seconds: 30
```

Agents see each server's tools as `<upstream>.<tool>` (`bank.wire`), with the server's description, input schema and annotations. A call goes through the pipeline and then to the server via the official `mcp` SDK client, with the verified agent in the request `_meta` (`agent_id`) so a multi-tenant server knows who is calling. One session per upstream is opened on first use and shared by every call, concurrent ones included, so a call costs one request instead of a fresh handshake (about 4x faster measured locally, more over TLS). A call that fails at the transport drops the session and the next call reconnects; the failed call is not retried, because it may have run upstream. The tool listing is reused for `catalog_ttl_seconds`. An upstream that cannot list its tools fails the listing loudly instead of disappearing from the catalog.

A server built with [pico-mcp](https://github.com/dperezcabrera/pico-mcp) declares its risk with `@tool(read_only=True)` / `@tool(destructive=True)`, and the policy routes by it with `hints` (below): read-only calls pass, destructive ones wait for an operator.

## HTTP edge

Installing the package brings pico-fastapi + pico-client-auth. There are two authenticated surfaces on two identity planes:

**Agent plane — MCP.** `/mcp` is served by the official `mcp` SDK, so any MCP client connects with a Bearer token: Claude Desktop, Cursor, `mcp.Client`, or a bare JSON-RPC POST with no handshake. It runs stateless with JSON responses: every request stands alone, so replicas need no session affinity and the identity is always that of the request being served. The agent identity is the **verified `sub` claim**, never a field in the body — an agent cannot claim to be another. Tool results come back as JSON text, after any operator notes. `tools/list` shows an agent only the tools the policy could let it call: an unconditional rule decides, a conditional allow counts (some arguments will pass), and a conditional deny may not apply, so it does not hide the tool. A custom `GrantResolver` gets the same filtering by implementing `may_call(call)`; without it every catalog tool is listed, and every call is still authorized when made.

A gated tool does NOT block the agent. `tools/call` returns a **pending** result at once ("approval requested, ticket X — tell the user, then call `gateway.check`"), so the agent stays free: it informs the user and moves on. When it wants the outcome it polls the built-in `gateway.check` tool with the ticket_id — still pending, denied, or the real result once an operator decides. MCP stays synchronous on the wire; the approval is asynchronous for the agent. An agent can only check its own tickets.

**Operator plane — REST, `operator` role.** Humans (or an operator UI) record decisions and resume tickets. The approver recorded on a decision is the verified `sub` of the operator's token, never a field in the body. A verdict is final: deciding a ticket that is no longer pending answers 409, so a rejected call cannot be approved later, and an interactive call whose wait timed out is recorded as `timeout` and cannot be approved afterwards either. Every decision is audited (`decision`, with approver, status, reason and whether the arguments were edited).

| Endpoint | Auth |
|---|---|
| `POST /mcp` (`tools/list`, `tools/call`) | valid agent token; identity from `sub` |
| `GET /api/v1/tickets?limit=&after=&tool=&agent_id=` | `operator` role: one page of the pending queue, oldest first |
| `POST /api/v1/tickets/{id}/decide` | `operator` role |
| `POST /api/v1/tickets/{id}/resume` | `operator` role |

Tokens come from the embedded pico-server-auth or an external issuer (`AUTH_ISSUER`); with `auth_client.enabled=false` the gateway runs open for local/dev, matching the original's dev-trust posture. The controllers only translate the wire to `ToolCall`/`ToolResult` — every rule lives in the pipeline.

## The pipeline

```
before approval:  rate-limit (50) → authorize (100)
                  approval-gate
after approval:   validate-schema (100) → materialize-secrets (200) → redact (300)
                  dispatch
```

Each step has `handle_call(ctx, call_next)` — the same before/after idiom as pico-ioc's AOP interceptors, so a step acts on the way in (authorize, gate, validate) and on the way out (redact wraps dispatch). Every step is one small class, testable alone, and every step's failure is audited the same way (`audited(step, event)` at assembly time), not with `audit.append(...)` sprinkled through the logic.

The pipeline is assembled, not hard-wired. Any component with a `stage`, an `order` and `handle_call` (the `GatewayStep` port) is picked up from the container and slotted in by order within its stage:

```python
from pico_ioc import component
from tool_gateway.domain import GatewayError
from tool_gateway.pipeline import Stage


@component
class MonthlyQuota:
    stage, order = Stage.BEFORE_APPROVAL, 120   # after authorize, before the gate

    def __init__(self, usage: UsageStore):
        self._usage = usage

    async def handle_call(self, ctx, call_next):
        if await self._usage.spent(ctx.call.agent_id):
            raise GatewayError("monthly quota spent")
        return await call_next(ctx)
```

A `BEFORE_APPROVAL` step runs once, when the call arrives (quotas, routing, enrichment). An `AFTER_APPROVAL` step runs on every execution, including `resume()` of an approved ticket (metrics, extra redaction). The gate and dispatch stay fixed: they are the boundary between the stages and the end of the chain. Everything else the steps use comes through ports, so each piece is replaced by registering another component: the policy engine, the ticket store, the audit sink, the rate limiter, the notifier, the upstream transport.

**Rate limit.** `tool_gateway.rate_limit_per_minute` caps calls per agent per minute with the default `WindowRateLimiter` (0, the default, admits everything). It counts in-process, so N replicas give an agent N times the budget; register a `RateLimiter` backed by shared storage (Redis `INCR` with a TTL) when one global budget matters.

## The three approval modes

| Mode | Flow |
|---|---|
| `auto` | Forwarded immediately. |
| `interactive` | Create a durable ticket, block-await a bounded decision (for a human answering in seconds). |
| `async` | Create a ticket, return a `Pending(ticket_id)` **at once** — nothing held. A human approves out of band; execution resumes via `resume(ticket_id)`. Survives a client disconnect. |

An approved ticket executes **once**. The result is stored on the ticket, so every later `gateway.check` or `/resume` returns it instead of calling the tool again; concurrent resumes race for a single claim and the losers see the ticket as still pending. A failed execution is stored as an error result too: nothing is retried behind the operator's back. If the process executing an approved call dies before storing its result, the claim it left is closed after `tool_gateway.execution_lease_seconds` (10 min by default; keep it above your slowest tool) as an error saying the outcome is unknown. The call is never run a second time, since it may already have taken effect; the operator checks the upstream and resubmits if needed. Ticket ids are random (`tkt-<uuid>`), never derived from the client's request id, and a ticket keeps the call as the agent sent it (`secret://` references, not the materialized values).

`call()` runs the full pipeline; `resume()` runs the post-approval pipeline (no gate — the decision exists). Both share the same steps, so the async path can never skip schema validation, secret materialization or redaction.

## Policy is data, not code

Which agent may run which tool, and under which approval mode, is a **declarative policy** — a JSON document, not a Python class. Just as MCP upstreams are configured (not compiled in), so is authorization. Point `tool_gateway.policy_path` at a file:

```json
{
  "default": "deny",
  "rules": [
    {"tool": "github.get_*", "mode": "auto"},
    {"tool": "*.delete_*", "mode": "interactive"},
    {"tool": "payments.charge", "when": [{"arg": "amount_cents", "op": "le", "value": 10000}], "mode": "auto"},
    {"tool": "payments.charge", "mode": "async"},
    {"tool": "*", "agent": "trusted-*", "mode": "auto"}
  ]
}
```

Rules match on `tool` (glob), `agent` (glob or list), `when` conditions over call arguments (`eq/ne/gt/ge/lt/le/in`) and `hints` over the tool's MCP annotations; first match wins, no match falls to `default` (`deny` or a mode).

`hints` route by what a tool declares about itself instead of by name, so one policy covers every upstream:

```json
{"tool": "*", "hints": {"readOnlyHint": true}, "mode": "auto"},
{"tool": "*", "hints": {"destructiveHint": true}, "mode": "interactive"}
```

The keys are the four MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`), read as the spec does: a missing hint takes its default (destructive and open-world unless declared otherwise), and a read-only tool is never destructive. The MCP edge copies each tool's annotations from the `ToolCatalog` into the call. Servers built with [pico-mcp](https://github.com/dperezcabrera/pico-mcp) declare them with `@tool(read_only=True)` / `@tool(destructive=True)`.

Annotations are claims of the upstream, so they only count for the upstreams the policy trusts:

```json
{"default": "deny", "trust_hints_from": ["bank", "internal-*"], "rules": [...]}
```

For any other upstream every hint takes its spec default, so a server that claims `readOnlyHint: true` on a tool that deletes is still treated as possibly destructive. No upstream is trusted unless listed.

**One policy for every replica.** The document lives in a `PolicySource` that all replicas share, and is versioned. An operator publishes a new one with `POST /api/v1/policy` (the body is the document) and reads the live one with `GET /api/v1/policy` (`{"version", "policy"}`); both need the `operator` role. A publish is validated before it is stored, so an invalid document answers 422 and changes nothing. The replica that took the publish applies it at once; every other replica asks the source for a newer version every `tool_gateway.policy_refresh_seconds` (5 s) and recompiles only when it changed, so all of them converge within that interval with no coordination. A document that does not compile (a hand-edited file with a typo) is logged and ignored: the replica keeps its last good policy. With no policy loaded at all, everything is denied.

| Source | Shared by | Notes |
|---|---|---|
| `FilePolicySource` (`policy_path`) | replicas mounting the same file (volume, ConfigMap) | versioned by content hash; publish writes it atomically |
| `SqlPolicySource` (`tool_gateway_sql`) | replicas on the same database | append-only `tool_gateway_policy` table: every version is kept with who published it, for audit and rollback |
| `MemoryPolicySource` | one process | default without `policy_path` |

The `DeclarativePolicy` is the default `GrantResolver`; the port stays open, so a Rego/Cedar or remote-PDP adapter drops in when you outgrow declarative rules — this is the Policy Enforcement Point, the decision engine is pluggable.

## The approval queue

`GET /api/v1/tickets` returns one page of the tickets waiting for a decision, oldest first, as `{"items": [...], "next": "<cursor>"}`; pass `next` back as `after` for the following page (`next` is null on the last one). `limit` defaults to 100 and is capped at 500. `tool` is a glob over `upstream.tool` (`bank.*` gives the bank team its own queue) and `agent_id` filters by agent. The SQL store pages by keyset on `(created_at, id)` over an index on `(status, created_at, id)`, so a page costs the same at the head of the queue and a million tickets deep.

## Notifying approvers

Every gated call can be pushed to a webhook (Slack, a chat bridge, an operator UI):

```yaml
tool_gateway:
  notify_url: https://approvals.example/hook
  notify_secret: change-me   # signs the body: X-Pico-Signature: sha256=<hex hmac>
```

The gateway POSTs one JSON event per ticket:

```json
{"event": "approval_requested", "ticket_id": "tkt-...", "approval_mode": "async",
 "agent_id": "agent-1", "tool": "bank.wire", "arguments": {"cents": 5, "key": "secret://key"},
 "annotations": {"destructiveHint": true}}
```

Arguments go as the agent sent them: `secret://` references, never the materialized values. Delivery runs in the background, so the agent gets its pending result at once. Network errors and 5xx are retried three times with exponential backoff; a 4xx is final. Both outcomes are audited (`notified`, `notify.error`). The message is a nudge, not the record: a ticket exists whether or not it got through, and `GET /api/v1/tickets` always shows the queue. Deliveries still in flight when the process stops are lost; the tickets are not. Another channel plugs in through the `ApproverNotifier` port.

## Persistence

The defaults keep tickets and audit in memory: one process, lost on restart. For durable tickets, install the `sql` extra and list one more module:

```bash
pip install "pico-tool-gateway[sql]" aiosqlite
```

```python
container = init(modules=["tool_gateway", "tool_gateway_sql", my_app])
```

```yaml
database:
  url: sqlite+aiosqlite:///gateway.db   # or postgresql+asyncpg://... for several replicas
```

`tool_gateway_sql` replaces the `TicketStore` and `AuditLog` defaults with pico-sqlalchemy tables (`tool_gateway_tickets`, `tool_gateway_audit`) and creates them at startup if missing. A ticket survives a restart and is resumed by whichever process the operator reaches. The run-once claim and the verdict are conditional `UPDATE`s, so they hold across replicas sharing the database.

## Waiting for a decision

An interactive call waits on a `DecisionSignal`, not on the database: `ToolGateway.decide` records the verdict and then signals, and the waiter rereads its ticket when woken. The store stays the source of truth, so the waiter also rereads it every `tool_gateway.decision_recheck_seconds` (5 s by default) whatever the signal does: a lost signal delays a waiter, it never misleads one. The default signal is in-process (instant on the replica that took the decision); register a `DecisionSignal` on Postgres `LISTEN/NOTIFY` or Redis pub/sub to wake waiters on every replica at once. With many callers, prefer `async` approval: nothing waits at all.

## Usage

```python
from tool_gateway import ToolGateway, ToolCall, Grant, ApprovalMode
from tool_gateway.adapters.memory import (
    DictGrantResolver, MiniSchemaValidator, DictSecretResolver,
    EchoUpstream, MemoryTicketStore, ListAuditLog,
)

grants = DictGrantResolver()
grants.allow("agent-1", "github.create_pr", Grant(ApprovalMode.ASYNC))

gw = ToolGateway(
    grants=grants, validator=MiniSchemaValidator(), secrets=DictSecretResolver(),
    upstream=EchoUpstream(), tickets=MemoryTicketStore(), audit=ListAuditLog(),
)

pending = await gw.call(ToolCall("r1", "agent-1", "github", "create_pr", {"title": "x"}))
# ... a human approves the ticket out of band ...
result = await gw.resume(pending.ticket_id)
```

## Why this over the original

The component it replaces was a 290-line procedural method inside a 2900-line `build_app`, with ~10 audit calls interleaved through the logic and a single blocking path that **held the agent's HTTP request open for up to five minutes** polling a DB — even though the pending request was already persisted durably. The durable spine existed; the proxy just didn't use it to decouple.

Here the orchestration is a list of composable steps, audit is declarative, and async approval returns a handle instead of pinning a connection and a coroutine per pending call. Adding a step (rate limit, cost cap, a fuller JSON-Schema validator) is one entry in the pipeline, not surgery on a god-method.

## Ports to implement for production

`GrantResolver`, `SchemaValidator`, `SecretResolver`, `Upstream`, `TicketStore`, `AuditLog`, `ToolCatalog` (see `ports.py`). MCP servers are covered by `adapters/mcp_upstreams.py` and the database by `tool_gateway_sql`; the `adapters/memory.py` set is a complete, runnable reference for the rest. pico-ioc matches ports by method name, so the names are specific (`call_tool`, `audit_event`): an adapter method called `invoke` would be confused with an AOP interceptor.

## Development

```bash
pip install -e ".[dev]"
pytest && ruff check .
```

## License

MIT
