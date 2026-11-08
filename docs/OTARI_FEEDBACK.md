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
| 11 | Retrying a lost response is safe | Only with `Idempotency-Key`; otherwise it is billed twice. Streams ignore the header, and `POST /code/execute` bills every run | Chat sends one key across retries. Code execution is never retried |
| 12 | A guarded call needs a model reply | A check is attached to a completion, so screening text costs a generation. We send `max_tokens: 1`. A way to validate text without a model call would suit a pre-filter better | Small cost on every submission |
| 13 | A fetched 404 looks like an outage | `POST /fetch` turns a page 404 into a gateway 502, and only `Otari-Error-Status` tells them apart | We read that header. It must be checked live, because a CVE contradiction depends on it |

Good surprises: `Otari-Attempt-Count` and `Otari-Model` make fallback provable without reading
usage logs, error codes are stable and documented, and a hold is the worst case, so concurrent
spam cannot race past a budget.

## Live findings

| Date | Finding | Request ID |
|---|---|---|
| Nov 6 | No key gives 401 with `Missing Otari-Key, Authorization, or x-api-key header`; a bad key gives 401 with `Invalid API key`. Both carry no request ID, as documented | none issued |
| Nov 6 | Keys now look like `otk_v1_eu_...`, not the documented `tk-...`, and the prefix names the region. An EU key needs `eu.api.otari.ai` | none issued |
| Nov 6 | `GET /key/budget` answers 404 on hosted, though it is in `openapi.json`. Balance cannot be read with an API key | none issued |
| Nov 6 | `GET /workspaces` answers 401 `Invalid master key` for an API key, so `request_mcp_servers_allowed` cannot be set with one | none issued |
| Nov 6 | `GET /tool-settings/guardrails/profiles` on hosted: `available: false`, no profiles. R1 is real: guardrails need a definition set up in the dashboard | none issued |
| Nov 6 | `otari_web_search` is `workspace_disabled` on a new workspace | none issued |
| Nov 6 | A new account has $0.00. Every model call answers 402 `insufficient_funds`, cleanly and with a request ID, including `/messages` | `e2efb2e3-eac6-49be-bae6-cb6ec599f332`, `136aaa43-1f0b-4a9a-b6ac-e732a6e551dc` |
| Nov 6 | `GET /models`, `/tools` responses carry no `Otari-Request-ID` | none issued |
| Nov 6 | First model reply through Otari: a provider key on a $0.00 account works, as documented, and the free model reports `cost_usd` 0 | `7c48f25f-90b6-4a42-b696-d16edcd0ea8b` |
| Nov 6 | MCP worked end to end on a $0.00 account: DeepWiki through streamed `/messages`, with the beta header and activity blocks. Inline servers were not refused, although the docs say a new workspace refuses them | `610dff25` (see `evidence/mcp-live.json`) |
| Nov 6 | A provider key does not cover hosted tools: `POST /code/execute` and a chat declaring `otari_code_execution` answer 402 at $0.00 | `18613488-b96d-4e5e-8708-ee1780037472`, `f691f997-9d26-41df-83d5-fc92ddfc036f` |
| Nov 6 | `POST /fetch` answers 403 `web access is not enabled for this workspace` until the workspace turns it on | `ef66e353-03e8-4f2b-8a17-4ee5ad3c6b1d` |
| Nov 6 | A normal key sending `user` gets 403 `'user' field does not match the authenticated API key's user`, as documented | none recorded |
| Nov 6 | Upstream free-tier limits arrive as 429 `upstream_rate_limited`, distinct from the gateway's own `rate_limited` | `3ff8b858-66d4-41b3-89b0-423556d0ce9c` |
| Nov 6 | Web tools turned on by the workspace switch, then both `POST /fetch` and a chat declaring them answer 402 at $0.00, even with a provider key serving the model | `4e053767-5509-49bd-b122-75f715d5b35e`, `75aa4fa5-8501-4565-806b-2aca1569c203` |
| Nov 6 | Web works on a $0.00 account once Exa keys are the organization's search and fetch keys and the workspace uses them. `otari_web_search` and `otari_web_fetch` both completed in one chat | `6b214f61` (see `evidence/web-live.json`) |
| Nov 6 | Confirmed live: a page 404 through `POST /fetch` is a 502 with `Otari-Error-Status: 404`, tag `CRAWL_NOT_FOUND`, provider `exa`. Our CVE absence check depends on this | `a5c168ba`, `e400fc03` |
| Nov 6 | Odd: a successful `/fetch` reports `fetch_tool: builtin_fetch` while its failures report `exa` | `ab942367` |
| Nov 6 | The web chat reported `cost_usd` 0.015 on a $0.00 balance with a free model. We have not established what that charge is or who pays it | `6b214f61` |
| Nov 6 | `POST /search` still answers 400 `No search tools are configured`, though the in-loop `otari_web_search` tool works | `aec3a65d` |
| Nov 6 | The hosted provider list omits Tavily and Brave, though the docs say they are accepted as search keys | none issued |
| Nov 6 | Eight calls ran past a key budget and an end-user budget that were meant to refuse sooner, on a free model at `cost_usd` 0. We have not established whether the limit was set too high or whether provider-key calls skip it | `908fcf05-87a5-4a8c-b80d-b862be452f96`, `7b0e1b16-a972-4e65-b217-7017d19afeb6` |
| Nov 7 | `GET /models` lists 432 models and none with a `:free` id, though `openrouter:...:free` ids answer through a provider key. The catalog cannot be used to find them | none issued |
| Nov 7 | Both free Gemma models answered 429 `upstream_rate_limited` on repeated tries, so a second-family role cannot rest on a free tier | `5b3a4029-21b7-43d1-b0d3-2a99abd58b1f`, `2707468b-89e8-41bd-ac05-90232bc0fef8` |
| Nov 7 | With no guardrail set up, a `block` guardrail answers 502 on every call, so THEMIS in its default mode stops every submission as `TAINTED_UNCHECKED`. Correct by our rule 9, and easy to mistake for a working injection filter: our first benchmark read 100% catch on 40 items and was discarded | `0922b8ea-5780-435a-8a8b-a9ed36f4f636` |
| Nov 7 | The provider's free daily quota ended mid-run with 429 `upstream_rate_limited`, and its message says 10 credits lift the limit to 1000 requests a day. The gateway passes the message through | `6aa4b97b` prefix, in the local receipt |
| Nov 7 | The Clerk on `nvidia/nemotron-3.5-super-vl:free` gave a schema-valid reply on 6 of 16 answered fixtures, after one repair. This is ours to solve, not a gateway fault, and the cause is not yet known | `0b092f61-6e8b-4fef-b5ad-be4bcb55cfd7`, `f8366625-9fa2-411c-9e63-a7896da8780f` |
| Nov 7 | Fallback passed live: a rejected provider key on the main model moved the call to the backup, with `Otari-Attempt-Count` 2 and `Otari-Fallback` true. The backup picked as a free model in the dashboard answered as the paid `nvidia/nemotron-3.5-super-vl` at `cost_usd` 0.000007, while the free id was over quota. The picker does not make that difference obvious | `c5e386f3-2d43-4a98-9e20-b7cf126cd90c` |
| Nov 8 | Budgets passed live: the key budget refused with 403 `budget_exceeded`, and a fresh end user on the tiny end-user budget was refused with scope `user`. The Nov 6 overrun did not repeat | `45be8b4d-6f1a-45c9-ad0d-307aa2a5f4e3`, `e4db2a1b-47e6-4386-bac4-75afc8d94bd2` |
| Nov 8 | With no `Otari-End-User-Budget` header, every new end user got the key's "Default end-user budget", which the dashboard had set to the first one added, the tiny demo budget. A real contributor was refused after two calls. The setting is on the key's edit page but easy to miss. THEMIS now names an everyday budget on each contributor call, and the default was changed to it | `5f810696-1f06-4581-a4c4-ae6ce7add732`, `a667f5b1-51c5-466f-ae47-99062ccf6c2a` |
| Nov 8 | Free Gemma models still answered 429 `upstream_rate_limited` right after the daily reset. Liquid's open-weight `lfm2.5-2.6b-free` answered and now holds the second role | `a9d0c07b-a1d1-408b-a250-1e3517c5a60b`, `94998859-ab60-4245-b76d-fe174a2a7cba` |
| Nov 8 | `GET /models` now lists free models with a `-free` suffix (`lfm2.5-2.6b-free`, `google/gemma-4-31b-free`), and `Otari-Model` reports the provider id `liquid/lfm-2.5-2.6b:free` | `07321bd4-1981-4caa-836d-85c641a00223` |
