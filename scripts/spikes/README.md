# Phase 0 spikes

One command runs every live check, saves redacted receipts to `receipts/`, and regenerates
`docs/READINESS.md`.

```bash
uv run scripts/spikes/run_all.py              # all spikes, cheapest first
uv run scripts/spikes/run_all.py guardrail    # one spike
uv run scripts/spikes/bakeoff.py              # 10 fixtures x 3-4 models, about 40 calls
uv run --with pytest --with httpx pytest scripts/spikes/tests -q    # offline, no key
```

A spike reports `PASS`, `FAIL` (exercised, wrong behaviour) or `BLOCKED` (setup missing, with the
reason). A run with no key costs nothing and records nothing. Receipts from anything other than
hosted Otari are refused inside this repository.

## Setup in the Otari dashboard

Do these in order: a key's service-key status cannot be changed after creation.

1. Sign up at <https://otari.ai/dashboard/#/signup> and top up. The minimum is $5.
2. **Spend & budgets**, add three budgets:
   - `themis-contributor-daily`: $0.05, resets daily
   - `themis-spike-tiny`: request limit 2, resets daily
   - `themis-budget-demo`: request limit 3, resets daily
3. **API keys**, create:
   - `themis-dev`: under **Advanced** tick **Service key**. End-user budgets: the first two above.
     Default: `themis-contributor-daily`. Key budget: $5.
   - `themis-demo`: the same, with a $3 key budget.
   - `themis-budget-demo`: a normal key using the `themis-budget-demo` budget.
4. Allow inline MCP: `PATCH /api/v1/workspaces/{id}` with `{"request_mcp_servers_allowed": true}`
   from a dashboard session, or the workspace setting if the dashboard shows one.
5. **Tools**: confirm web search, web fetch and code execution are allowed.
6. **Organization, Guardrails**: only if `GET /tool-settings/guardrails/profiles` lists nothing,
   configure a prompt-injection guardrail from the catalog (it may need a vendor key).
7. Routing, for `themis-fallback-demo`:
   - **Providers**: add a provider key with a deliberately invalid value, named `fallback-test`.
   - **Routing, Policies**: create fallback policy `themis-fallback-demo`. Main model: an
     open-weight model on that provider. Backup: an open-weight model on another provider.
   - Pin `fallback-test` on the test workspace only.

Then fill `.env` from `.env.example` and run the commands above.
