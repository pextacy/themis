# Otari feedback log

Everything about Otari that surprised us, good or bad, with a request ID where one exists.

## v0: from reading the docs, Nov 6, 2026

Source: `mozilla-ai/otari` docs and `openapi.json` at `e9a72e7c8e`. Nothing in this section has
been checked against a funded workspace yet, so none of it has a request ID. Each item moves to
the live section once a spike confirms or contradicts it.

| # | What we assumed | What the docs say | Effect on THEMIS |
|---|---|---|---|
| 1 | `user=gh:<id>` works on any key | A normal key naming another user gets 403. Only a service key may name end users | `themis-dev` and `themis-demo` must be created as service keys, and that choice is fixed at creation |
| 2 | `user` works everywhere | `/messages` uses `metadata.user_id` | The client needs a per-endpoint mapping |
| 3 | Budget refusal has its own status | It is a 403, same status as a guardrail block. `Otari-Error-Code` tells them apart | Errors must map on the code, never the status |
| 4 | Streamed `/messages` shows MCP activity | Only with `anthropic-beta: mcp-client-2025-11-20`. Without it the call still runs, silently | Easy to miss. The spike sends the header and fails if no pair appears |
| 5 | Inline `mcp_servers` just work | A new workspace has `request_mcp_servers_allowed` off and answers 400 | One setup step, not mentioned in the MCP quick path |
| 6 | `Otari-End-User-Budget` sets the budget | Only when the request creates the end user | Budget changes go through the end-user routes |
| 7 | Hosted routing needs BYOK | Entries without a key are served from the balance | Likely removes R2 |
| 8 | A block guardrail outage is a special case | It fails closed with a plain 502 | THEMIS treats a 502 on a guarded call as `TAINTED_UNCHECKED` |
| 9 | Built-in tools reject `max_uses` | Docs describe `max_uses` as honoured | Possibly stale in our notes. We stay type-only until tested |
| 10 | Only tool-loop code execution exists | `POST /code/execute` runs code with no model | Could run Prover scripts exactly as written, without a model between script and sandbox. Worth weighing in Phase 3 |

Good surprises: `Otari-Attempt-Count` and `Otari-Model` make fallback provable without reading
usage logs, error codes are stable and documented, and a hold is the worst case, so concurrent
spam cannot race past a budget.

## Live findings

| Date | Finding | Request ID |
|---|---|---|
| Nov 6 | No key gives 401 with `Missing Otari-Key, Authorization, or x-api-key header`; a bad key gives 401 with `Invalid API key`. Both carry no request ID, as documented | none issued |
