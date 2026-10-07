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

Agents see each server's tools as `<upstream>.<tool>` (`bank.wire`), with the server's description, input schema and annotations. A call goes through the pipeline and then to the server via the official `mcp` SDK client, with the verified agent in the request `_meta` (`agent_id`) so a multi-tenant server knows who is calling. Each operation opens its own connection, so an upstream restart loses nothing; the tool listing is reused for `catalog_ttl_seconds`. An upstream that cannot list its tools fails the listing loudly instead of disappearing from the catalog.

A server built with [pico-mcp](https://github.com/dperezcabrera/pico-mcp) declares its risk with `@tool(read_only=True)` / `@tool(destructive=True)`, and the policy routes by it with `hints` (below): read-only calls pass, destructive ones wait for an operator.

## HTTP edge

Installing the package brings pico-fastapi + pico-client-auth. There are two authenticated surfaces on two identity planes:

**Agent plane — MCP.** An agent's MCP client connects to `POST /mcp` (JSON-RPC `tools/list` + `tools/call`) with a Bearer token. The agent identity is the **verified `sub` claim**, never a field in the body — an agent cannot claim to be another.

A gated tool does NOT block the agent. `tools/call` returns a **pending** result at once ("approval requested, ticket X — tell the user, then call `gateway.check`"), so the agent stays free: it informs the user and moves on. When it wants the outcome it polls the built-in `gateway.check` tool with the ticket_id — still pending, denied, or the real result once an operator decides. MCP stays synchronous on the wire; the approval is asynchronous for the agent. An agent can only check its own tickets.

**Operator plane — REST, `operator` role.** Humans (or an operator UI) record decisions and resume tickets:

| Endpoint | Auth |
|---|---|
| `POST /mcp` (`tools/list`, `tools/call`) | valid agent token; identity from `sub` |
| `POST /api/v1/tickets/{id}/decide` | `operator` role |
| `POST /api/v1/tickets/{id}/resume` | `operator` role |

Tokens come from the embedded pico-server-auth or an external issuer (`AUTH_ISSUER`); with `auth_client.enabled=false` the gateway runs open for local/dev, matching the original's dev-trust posture. The controllers only translate the wire to `ToolCall`/`ToolResult` — every rule lives in the pipeline.

## The pipeline

```
authorize → approval-gate → validate-schema → materialize-secrets → redact → dispatch
```

Each step is `async (ctx, call_next) -> ToolResult` — the same before/after idiom as pico-ioc's AOP interceptors, so a step acts on the way in (authorize, gate, validate) and on the way out (redact wraps dispatch). Every step is one small class, testable alone. Audit is `audited(step, event)` applied at build time, not `audit.append(...)` sprinkled through the logic.

## The three approval modes

| Mode | Flow |
|---|---|
| `auto` | Forwarded immediately. |
| `interactive` | Create a durable ticket, block-await a bounded decision (for a human answering in seconds). |
| `async` | Create a ticket, return a `Pending(ticket_id)` **at once** — nothing held. A human approves out of band; execution resumes via `resume(ticket_id)`. Survives a client disconnect. |

An approved ticket executes **once**. The result is stored on the ticket, so every later `gateway.check` or `/resume` returns it instead of calling the tool again; concurrent resumes race for a single claim and the losers see the ticket as still pending. A failed execution is stored as an error result too: nothing is retried behind the operator's back. Ticket ids are random (`tkt-<uuid>`), never derived from the client's request id, and a ticket keeps the call as the agent sent it (`secret://` references, not the materialized values).

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

The keys are the four MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`), read as the spec does: a missing hint takes its default (destructive and open-world unless declared otherwise), and a read-only tool is never destructive. The MCP edge copies each tool's annotations from the `ToolCatalog` into the call. Servers built with [pico-mcp](https://github.com/dperezcabrera/pico-mcp) declare them with `@tool(read_only=True)` / `@tool(destructive=True)`. Annotations are claims of the upstream: rely on them only for upstreams you vetted, and keep name-based rules first for the ones you do not.

An operator hot-reloads it with `POST /api/v1/policy/reload` (push a body or re-read the file) — no restart. The `DeclarativePolicy` is the default `GrantResolver`; the port stays open, so a Rego/Cedar or remote-PDP adapter drops in when you outgrow declarative rules — this is the Policy Enforcement Point, the decision engine is pluggable.

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

`tool_gateway_sql` replaces the `TicketStore` and `AuditLog` defaults with pico-sqlalchemy tables (`tool_gateway_tickets`, `tool_gateway_audit`) and creates them at startup if missing. A ticket survives a restart and is resumed by whichever process the operator reaches. The run-once claim is a conditional `UPDATE`, so it holds across replicas sharing the database, and an interactive wait polls the ticket row (every 0.5 s) so it also sees a decision recorded by another replica.

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
