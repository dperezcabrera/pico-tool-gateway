# pico-tool-gateway

A clean-room redesign of the tool proxy + approval flow: an agent's tool call runs through a **composable pipeline** of small steps, audit is a **cross-cutting wrapper**, and the three approval modes are **genuinely distinct flows** — with async approval **decoupled from the request** instead of blocking on a human.

A pico module that runs in **one process with no companion services** — no broker, no worker, no external DB. The core is pure Python (zero framework in `domain`, `pipeline`, `steps`, `approval`, `gateway`); a thin `wiring` layer registers it with pico-ioc so it drops into any pico app. Fleet (or anyone) plugs real infrastructure in through ports; the domain never sees a vault, a DB or an MCP transport.

## As a pico module

```python
from pico_ioc import init
from tool_gateway import ToolGateway

container = init(modules=["tool_gateway", my_app])  # my_app registers a real Upstream
gateway = container.get(ToolGateway)
```

Every port has a safe in-process default (`on_missing_selector`) except `Upstream` — the real tool executor is yours to wire; booting without one is fine, the first call reports it. Override any default by registering your own `@component` of the same protocol. Async approval is a durable ticket plus an in-process `resume()` call, so nothing else needs to be running.

With `pico_boot.init()` the module auto-discovers via its `pico_boot.modules` entry point — an app never lists it:

```python
from pico_boot import init
container = init(modules=[my_app])   # tool_gateway loads itself
```

## HTTP edge

Installing the package brings pico-fastapi and a thin controller, so the gateway is reachable over HTTP with no extra code — include `pico_fastapi` when you boot:

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/tools/call` | Run a tool. Returns the result, or `{"status": "pending", "ticket_id": ...}` for async approval. |
| `POST /api/v1/tickets/{id}/decide` | Record a human verdict (unblocks an interactive call). |
| `POST /api/v1/tickets/{id}/resume` | Execute an approved async ticket. |

Interactive callers hold the `call` request open until someone `decide`s; async callers get a ticket back at once and `resume` after approval. The controller only translates HTTP to `ToolCall`/`ToolResult` — every rule lives in the pipeline.

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

`call()` runs the full pipeline; `resume()` runs the post-approval pipeline (no gate — the decision exists). Both share the same steps, so the async path can never skip schema validation, secret materialization or redaction.

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

`GrantResolver`, `SchemaValidator`, `SecretResolver`, `Upstream`, `TicketStore`, `AuditLog` (see `ports.py`). The `adapters/memory.py` set is a complete, runnable reference — swap them one at a time for a vault, an MCP session and a database.

## Development

```bash
pip install -e ".[dev]"
pytest && ruff check .
```

## License

MIT
